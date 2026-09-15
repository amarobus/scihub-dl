"""Sci-Hub client: mirror selection, captcha handling, metadata scraping, download.

Design notes on correctness (see also docs in README):

* Mirrors differ structurally. ``sci-hub.ru`` publishes Highwire
  ``citation_*`` meta tags; ``sci-hub.ren`` publishes none at all and exposes
  the PDF only via ``<embed type="application/pdf" src=...>``. Extraction is
  therefore multi-strategy, never meta-tag-only.
* "Could not find a link" is NOT "does not exist". A parse failure raises
  ParseError; NotFoundError is reserved for positive evidence of absence.
  Conflating the two produces confident false negatives.
* Mirror health is per-request, not per-host. A mirror whose homepage is 200
  can still fail on a specific DOI (redirect loop, bot-check), so failover
  happens at the DOI level, not only during initial selection.
"""

import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import altcha
from .errors import (
    BotCheckError,
    ChallengeError,
    DownloadError,
    MirrorUnreachableError,
    NotFoundError,
    ParseError,
    RedirectLoopError,
)
from .models import Paper
from .utils import absolutize_url, normalize_doi

logger = logging.getLogger(__name__)

DEFAULT_MIRRORS: Sequence[str] = (
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.ren",
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

_CHALLENGE_ID_RE = re.compile(r"/captcha/challenge/(\d+)")

# Positive evidence that Sci-Hub *does not have* the document. Only these
# justify NotFoundError.
_NOT_FOUND_MARKERS = (
    "article not found",
    "статья не найдена",
    "unfortunately, sci-hub doesn",
    "unfortunately, sci-hub does not",
    "sci-hub could not find",
    "не удалось найти",
)

# Interstitial bot-check pages (distinct from Altcha, which we can solve).
_BOT_CHECK_MARKERS = (
    "please complete the check to continue",
    "waiting for verification",
    "checking your browser",
    "ddos-guard",
    "just a moment",
    "enable javascript and cookies to continue",
)

# Titles/markers indicating a real article page even without meta tags.
_PDF_EXTENSIONS = (".pdf",)


def _looks_like_pdf_url(url: str) -> bool:
    """Heuristic: does this URL plausibly point at a PDF?"""
    if not url:
        return False
    clean = url.split("#", 1)[0].split("?", 1)[0].lower()
    if clean.endswith(_PDF_EXTENSIONS):
        return True
    # Sci-Hub storage paths sometimes omit the extension but include markers.
    return "/pdf/" in clean or "/storage/" in clean or "/downloads/" in clean


class ScihubClient:
    """Resolve DOIs to PDFs via Sci-Hub.

    Usage::

        with ScihubClient() as client:
            paper = client.resolve("10.1038/nature12373")
            client.download(paper, "out.pdf")

        # or in one step
        result_path = ScihubClient().fetch("10.1038/nature12373", "out/")
    """

    def __init__(
        self,
        mirrors: Optional[Sequence[str]] = None,
        timeout: int = 30,
        retries: int = 2,
        backoff: float = 1.5,
        user_agent: str = DEFAULT_USER_AGENT,
        session: Optional[requests.Session] = None,
        verify_ssl: bool = True,
        max_redirects: int = 5,
    ):
        self.mirrors = [m.rstrip("/") for m in (mirrors or DEFAULT_MIRRORS)]
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.verify_ssl = verify_ssl
        self.max_redirects = max_redirects
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        # Cap redirects so an anti-bot bounce fails fast instead of burning
        # 30 hops before raising (bug: redirect loop looked like a hang).
        self.session.max_redirects = max_redirects
        self._active_mirror: Optional[str] = None
        self._dead_mirrors: set = set()

    # ------------------------------------------------------------------ #
    # context manager
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "ScihubClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

    # ------------------------------------------------------------------ #
    # mirrors
    # ------------------------------------------------------------------ #
    @property
    def active_mirror(self) -> Optional[str]:
        """The mirror that last answered successfully, if any."""
        return self._active_mirror

    def _get(self, url: str, **kwargs) -> requests.Response:
        """GET with retries and exponential backoff.

        Redirect loops are translated to RedirectLoopError immediately (not
        retried on the same host) because retrying an anti-bot bounce on the
        same mirror is futile - the caller should try a different mirror.
        """
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify_ssl)
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.get(url, **kwargs)
                if resp.status_code >= 500 or resp.status_code == 444:
                    raise requests.exceptions.HTTPError(
                        f"HTTP {resp.status_code} from {url}"
                    )
                return resp
            except requests.exceptions.TooManyRedirects as exc:
                # Do not retry: the host is up but cycling us.
                raise RedirectLoopError(
                    f"GET {url} hit a redirect loop (>{self.max_redirects} hops)"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - retry on transport errors
                last_exc = exc
                if attempt < self.retries:
                    sleep_for = self.backoff ** attempt
                    logger.debug(
                        "GET %s failed (%s); retrying in %.1fs", url, exc, sleep_for
                    )
                    time.sleep(sleep_for)
        raise MirrorUnreachableError(f"GET {url} failed: {last_exc}") from last_exc

    def _candidate_mirrors(self) -> List[str]:
        """Mirrors to try, preferring the last known-good one, skipping dead."""
        ordered: List[str] = []
        if self._active_mirror and self._active_mirror not in self._dead_mirrors:
            ordered.append(self._active_mirror)
        for m in self.mirrors:
            if m not in ordered and m not in self._dead_mirrors:
                ordered.append(m)
        return ordered

    def _pick_mirror(self) -> str:
        """Return the first mirror whose homepage answers (warm-up only).

        NOTE: a healthy homepage does NOT guarantee a given DOI will resolve
        on that mirror, so resolve() re-selects per DOI. This is only used to
        warm cookies and give batch runs a sensible starting point.
        """
        candidates = self._candidate_mirrors()
        errors: Dict[str, str] = {}
        for mirror in candidates:
            try:
                resp = self.session.get(
                    mirror, timeout=self.timeout, verify=self.verify_ssl
                )
                if resp.status_code == 200 and len(resp.text) > 500:
                    self._active_mirror = mirror
                    logger.debug("Warmed up on mirror %s", mirror)
                    return mirror
                errors[mirror] = f"HTTP {resp.status_code}, len={len(resp.text)}"
            except requests.exceptions.TooManyRedirects:
                errors[mirror] = "redirect loop"
            except Exception as exc:  # noqa: BLE001
                errors[mirror] = str(exc)

        detail = "; ".join(f"{k}: {v}" for k, v in errors.items()) or "none configured"
        raise MirrorUnreachableError(f"No reachable Sci-Hub mirror. Tried: {detail}")

    # ------------------------------------------------------------------ #
    # captcha
    # ------------------------------------------------------------------ #
    def solve_captcha(self, mirror: str, challenge_id: str) -> bool:
        """Fetch, solve and submit an Altcha challenge. Returns True on success."""
        chal_url = f"{mirror}/captcha/challenge/{challenge_id}"
        resp = self._get(chal_url)
        try:
            challenge = resp.json()
        except ValueError as exc:
            raise ChallengeError(
                f"Challenge endpoint returned non-JSON: {chal_url}"
            ) from exc

        payload = altcha.solve(challenge)

        sol_resp = self.session.post(
            f"{mirror}/captcha/solution/{challenge_id}",
            headers={"Content-Type": "application/json"},
            json={"captcha": payload},
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        if not sol_resp.ok:
            raise ChallengeError(
                f"Captcha solution rejected: HTTP {sol_resp.status_code}"
            )
        try:
            success = bool(sol_resp.json().get("success"))
        except ValueError:
            success = False
        if not success:
            raise ChallengeError("Captcha solution was not accepted by the server")
        logger.debug("Solved Altcha challenge %s", challenge_id)
        return True

    # ------------------------------------------------------------------ #
    # page classification
    # ------------------------------------------------------------------ #
    @staticmethod
    def _visible_text(html: str) -> str:
        """Strip script/style and tags to get roughly the human-visible text."""
        text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return " ".join(text.split()).lower()

    @classmethod
    def _is_not_found_page(cls, html: str) -> bool:
        """True only on positive evidence that the document is absent."""
        blob = cls._visible_text(html)
        return any(marker in blob for marker in _NOT_FOUND_MARKERS)

    @classmethod
    def _is_bot_check_page(cls, html: str) -> bool:
        """True if this is an interstitial verification page (not Altcha)."""
        if _CHALLENGE_ID_RE.search(html):
            return False  # Altcha - we handle that separately
        blob = cls._visible_text(html)
        if any(marker in blob for marker in _BOT_CHECK_MARKERS):
            return True
        # A tiny page titled "verification" with no article content.
        title = cls._extract_title(html) or ""
        return "verification" in title.lower() and len(html) < 5000

    @staticmethod
    def _extract_title(html: str) -> Optional[str]:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I)
        if not m:
            return None
        return " ".join(m.group(1).split()) or None

    # ------------------------------------------------------------------ #
    # PDF link extraction (multi-strategy)
    # ------------------------------------------------------------------ #
    @classmethod
    def _extract_pdf_url(cls, soup: BeautifulSoup, mirror: str) -> Optional[str]:
        """Find the PDF URL using every known mirror layout, in priority order.

        Strategies, highest-confidence first:
          1. ``<meta name="citation_pdf_url">``      (sci-hub.ru / Highwire)
          2. ``#pdf`` element's src                  (most mirrors)
          3. ``<embed type="application/pdf">``      (sci-hub.ren)
          4. ``<iframe>`` with a PDF-ish src
          5. save/download button ``onclick`` target
          6. any ``<a href>`` that looks like a PDF
        """
        # 1. Highwire meta tag
        tag = soup.find("meta", attrs={"name": "citation_pdf_url"})
        if tag and tag.get("content"):
            return absolutize_url(tag["content"].strip(), mirror)

        # 2. id="pdf" (note: sci-hub.ren writes `id = "pdf"` with spaces,
        #    which BeautifulSoup normalises for us)
        node = soup.find(id="pdf")
        if node and node.get("src"):
            return absolutize_url(node["src"].strip(), mirror)

        # 3. <embed type="application/pdf">
        for embed in soup.find_all("embed"):
            src = (embed.get("src") or "").strip()
            etype = (embed.get("type") or "").lower()
            if src and ("pdf" in etype or _looks_like_pdf_url(src)):
                return absolutize_url(src, mirror)

        # 4. <iframe src=...pdf>
        for frame in soup.find_all("iframe"):
            src = (frame.get("src") or "").strip()
            if _looks_like_pdf_url(src):
                return absolutize_url(src, mirror)

        # 5. save/download buttons: onclick="location.href='//host/x.pdf?...'"
        for node in soup.find_all(attrs={"onclick": True}):
            m = re.search(r"""["']((?:https?:)?//[^"']+?\.pdf[^"']*)["']""",
                          node["onclick"], flags=re.I)
            if m:
                return absolutize_url(m.group(1), mirror)

        # 6. plain anchors
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if _looks_like_pdf_url(href):
                return absolutize_url(href, mirror)

        return None

    @classmethod
    def _parse_paper(cls, doi: str, html: str, mirror: str) -> Paper:
        """Build a Paper from a mirror page, tolerating absent meta tags."""
        soup = BeautifulSoup(html, "html.parser")

        def meta(name: str) -> Optional[str]:
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
            return None

        authors = [
            t["content"].strip()
            for t in soup.find_all("meta", attrs={"name": "citation_author"})
            if t.get("content")
        ]

        title = meta("citation_title")
        year = meta("citation_publication_date")
        journal = meta("citation_journal_title")

        # Fallback for mirrors with no citation_* tags (e.g. sci-hub.ren),
        # whose <title> looks like: "Sci-Hub | <Article Title> | <doi>"
        if not title:
            raw_title = cls._extract_title(html)
            if raw_title:
                parts = [p.strip() for p in raw_title.split("|")]
                parts = [
                    p
                    for p in parts
                    if p and p.lower() != "sci-hub" and p.lower() != doi.lower()
                ]
                if parts:
                    title = max(parts, key=len)

        pdf_url = cls._extract_pdf_url(soup, mirror)

        return Paper(
            doi=doi,
            title=title,
            authors=authors,
            year=year,
            journal=journal,
            pdf_url=pdf_url,
        )

    # ------------------------------------------------------------------ #
    # resolve
    # ------------------------------------------------------------------ #
    def _resolve_on_mirror(self, doi: str, mirror: str) -> Paper:
        """Attempt a full resolve against one mirror. Raises on any failure."""
        resp = self._get(f"{mirror}/{doi}")
        html = resp.text

        # Altcha proof-of-work gate: solve, then re-request.
        match = _CHALLENGE_ID_RE.search(html)
        if match:
            self.solve_captcha(mirror, match.group(1))
            resp = self._get(f"{mirror}/{doi}")
            html = resp.text
            if _CHALLENGE_ID_RE.search(html):
                raise ChallengeError(
                    "Still served a captcha page after solving the challenge"
                )

        # Interstitial bot-check we cannot clear -> try another mirror.
        if self._is_bot_check_page(html):
            raise BotCheckError(f"{mirror} served a bot-check page for {doi}")

        # Positive not-found evidence from this mirror.
        if self._is_not_found_page(html):
            raise NotFoundError(f"{mirror} reports no PDF for DOI {doi}")

        paper = self._parse_paper(doi, html, mirror)
        if not paper.pdf_url:
            # We do NOT claim the paper doesn't exist - we failed to parse.
            raise ParseError(
                f"{mirror} returned a page for {doi} but no PDF link could be "
                f"extracted (title={paper.title!r}, {len(html)} bytes); "
                f"the mirror layout may have changed"
            )

        self._active_mirror = mirror
        return paper

    def resolve(self, doi: str) -> Paper:
        """Resolve a DOI to a Paper (metadata + direct PDF URL).

        Tries each candidate mirror in turn. Per-mirror failures (redirect
        loops, bot checks, layout changes) fail over to the next mirror rather
        than aborting, because mirror health is per-request, not per-host.

        Raises:
            NotFoundError: every mirror that answered affirmatively said the
                document is absent.
            ParseError: pages loaded but no PDF link could be extracted
                anywhere (distinct from "does not exist").
            MirrorUnreachableError: no mirror answered at all.
        """
        doi = normalize_doi(doi)
        candidates = self._candidate_mirrors()
        if not candidates:
            raise MirrorUnreachableError("All configured mirrors are marked dead")

        failures: List[Tuple[str, Exception]] = []
        saw_not_found = False
        saw_parse_error = False
        challenge_errors: List[Exception] = []

        for mirror in candidates:
            try:
                paper = self._resolve_on_mirror(doi, mirror)
                if failures:
                    logger.debug(
                        "Resolved %s on %s after %d mirror failure(s)",
                        doi,
                        mirror,
                        len(failures),
                    )
                return paper

            except NotFoundError as exc:
                # This mirror positively says "absent". Other mirrors may
                # still have it, so keep going but remember the claim.
                saw_not_found = True
                failures.append((mirror, exc))
                logger.debug("%s: not found (%s)", mirror, exc)

            except ParseError as exc:
                saw_parse_error = True
                failures.append((mirror, exc))
                logger.debug("%s: parse failure (%s)", mirror, exc)

            except (RedirectLoopError, BotCheckError) as exc:
                # Host-level problem: stop using this mirror this session.
                self._dead_mirrors.add(mirror)
                failures.append((mirror, exc))
                logger.debug("%s: unusable (%s)", mirror, exc)

            except ChallengeError as exc:
                # The host answered fine - we just could not clear its
                # proof-of-work gate. Remember it so we don't misreport this
                # as "nothing answered" if every mirror fails the same way.
                challenge_errors.append(exc)
                failures.append((mirror, exc))
                logger.debug("%s: captcha failure (%s)", mirror, exc)

            except MirrorUnreachableError as exc:
                failures.append((mirror, exc))
                logger.debug("%s: %s", mirror, exc)

        detail = "; ".join(f"{m}: {e}" for m, e in failures) or "no mirrors tried"

        # Ordering matters: never report "not found" when the real story is
        # "we could not read the page" or "nothing answered".
        if saw_parse_error:
            raise ParseError(
                f"Could not extract a PDF link for {doi} on any mirror. "
                f"This is NOT proof the paper is unavailable - the page(s) "
                f"loaded but could not be parsed. Details: {detail}"
            )
        if saw_not_found:
            raise NotFoundError(
                f"Sci-Hub has no PDF for DOI {doi} (every responding mirror "
                f"reported it absent). Details: {detail}"
            )
        if challenge_errors:
            # Mirrors responded but we could not clear the proof-of-work gate.
            # Surfacing this as "unreachable" would misattribute the cause, so
            # re-raise the captcha failure itself (preserving its message).
            if len(challenge_errors) == 1:
                raise challenge_errors[0]
            raise ChallengeError(
                f"Could not clear the proof-of-work challenge for {doi} on any "
                f"mirror. Details: {detail}"
            )
        raise MirrorUnreachableError(
            f"No mirror could resolve {doi}. Details: {detail}"
        )

    # ------------------------------------------------------------------ #
    # download
    # ------------------------------------------------------------------ #
    def download(
        self,
        paper: Union[Paper, str],
        dest: Union[str, Path],
        overwrite: bool = False,
        chunk_size: int = 65536,
    ) -> Path:
        """Download the PDF for a Paper (or DOI) to ``dest``.

        ``dest`` may be a directory (filename derived from metadata) or a full
        file path. Returns the written path.
        """
        if isinstance(paper, str):
            paper = self.resolve(paper)
        if not paper.pdf_url:
            raise ParseError(f"No PDF URL resolved for DOI {paper.doi}")

        dest = Path(dest)
        if dest.is_dir() or str(dest).endswith(("/", "\\")) or dest.suffix == "":
            dest.mkdir(parents=True, exist_ok=True)
            out_path = dest / paper.suggested_filename()
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            out_path = dest

        if out_path.exists() and not overwrite:
            logger.debug("Skipping existing file %s", out_path)
            return out_path

        resp = self._get(paper.pdf_url, stream=True)
        if not resp.ok:
            raise DownloadError(
                f"PDF download failed for {paper.doi}: HTTP {resp.status_code}"
            )

        tmp_path = out_path.with_suffix(out_path.suffix + ".part")
        first = True
        try:
            with open(tmp_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    if first:
                        if not chunk.startswith(b"%PDF"):
                            raise DownloadError(
                                f"Response for {paper.doi} is not a PDF "
                                f"(got {chunk[:16]!r} from {paper.pdf_url})"
                            )
                        first = False
                    fh.write(chunk)
            if first:  # no bytes at all
                raise DownloadError(f"Empty response body for {paper.doi}")
            tmp_path.replace(out_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        return out_path

    def fetch(
        self,
        doi: str,
        dest: Union[str, Path] = ".",
        overwrite: bool = False,
    ) -> Path:
        """Resolve + download in one call. Returns the written path."""
        paper = self.resolve(doi)
        return self.download(paper, dest, overwrite=overwrite)
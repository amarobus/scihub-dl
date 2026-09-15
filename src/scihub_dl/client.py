"""Sci-Hub client: mirror selection, captcha handling, metadata scraping, download."""

import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

import requests
from bs4 import BeautifulSoup

from . import altcha
from .errors import (
    ChallengeError,
    DownloadError,
    MirrorUnreachableError,
    NotFoundError,
)
from .models import Paper
from .utils import absolutize_url, normalize_doi, safe_filename

logger = logging.getLogger(__name__)

DEFAULT_MIRRORS: Sequence[str] = (
    "https://sci-hub.ru",
    "https://sci-hub.st",
    "https://sci-hub.se",
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

_CHALLENGE_ID_RE = re.compile(r"/captcha/challenge/(\d+)")
# Sci-Hub renders "article not found" pages without a citation_pdf_url meta tag.
_NOT_FOUND_HINTS = (
    "article not found",
    "статья не найдена",
    "unfortunately, sci-hub doesn",
)


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
    ):
        self.mirrors = list(mirrors) if mirrors else list(DEFAULT_MIRRORS)
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.verify_ssl = verify_ssl
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self._active_mirror: Optional[str] = None

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
        """GET with retries and exponential backoff."""
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
            except Exception as exc:  # noqa: BLE001 - retry on any transport error
                last_exc = exc
                if attempt < self.retries:
                    sleep_for = self.backoff ** attempt
                    logger.debug("GET %s failed (%s); retrying in %.1fs", url, exc, sleep_for)
                    time.sleep(sleep_for)
        raise MirrorUnreachableError(f"GET {url} failed: {last_exc}") from last_exc

    def _pick_mirror(self) -> str:
        """Return the first reachable mirror, caching the choice."""
        if self._active_mirror:
            return self._active_mirror

        errors: Dict[str, str] = {}
        for mirror in self.mirrors:
            try:
                resp = self.session.get(
                    mirror, timeout=self.timeout, verify=self.verify_ssl
                )
                if resp.status_code == 200 and len(resp.text) > 500:
                    self._active_mirror = mirror.rstrip("/")
                    logger.debug("Using mirror %s", self._active_mirror)
                    return self._active_mirror
                errors[mirror] = f"HTTP {resp.status_code}, len={len(resp.text)}"
            except Exception as exc:  # noqa: BLE001
                errors[mirror] = str(exc)

        detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
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
            raise ChallengeError(f"Challenge endpoint returned non-JSON: {chal_url}") from exc

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
    # metadata
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_paper(doi: str, html: str, mirror: str) -> Paper:
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

        pdf_url = meta("citation_pdf_url")
        if pdf_url:
            pdf_url = absolutize_url(pdf_url, mirror)

        return Paper(
            doi=doi,
            title=meta("citation_title"),
            authors=authors,
            year=meta("citation_publication_date"),
            journal=meta("citation_journal_title"),
            pdf_url=pdf_url,
        )

    def resolve(self, doi: str) -> Paper:
        """Resolve a DOI to a Paper (metadata + direct PDF URL).

        Raises NotFoundError if Sci-Hub has no copy, InvalidDOIError for
        malformed input, MirrorUnreachableError if no mirror responds.
        """
        doi = normalize_doi(doi)
        mirror = self._pick_mirror()

        resp = self._get(f"{mirror}/{doi}")
        html = resp.text

        # Solve the proof-of-work gate if present, then re-request the page.
        match = _CHALLENGE_ID_RE.search(html)
        if match:
            self.solve_captcha(mirror, match.group(1))
            resp = self._get(f"{mirror}/{doi}")
            html = resp.text
            if _CHALLENGE_ID_RE.search(html):
                raise ChallengeError(
                    "Still served a captcha page after solving the challenge"
                )

        paper = self._parse_paper(doi, html, mirror)

        if not paper.pdf_url:
            lowered = html.lower()
            if any(hint in lowered for hint in _NOT_FOUND_HINTS) or len(html) < 20000:
                raise NotFoundError(f"Sci-Hub has no PDF for DOI {doi}")
            raise NotFoundError(
                f"Could not find a PDF link for DOI {doi} (page layout may have changed)"
            )
        return paper

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
            raise NotFoundError(f"No PDF URL resolved for DOI {paper.doi}")

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
                                f"(got {chunk[:16]!r})"
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

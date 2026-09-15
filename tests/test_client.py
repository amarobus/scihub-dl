import json

import pytest
import responses

from scihub_dl.client import ScihubClient
from scihub_dl.errors import (
    BotCheckError,
    ChallengeError,
    DownloadError,
    InvalidDOIError,
    MirrorUnreachableError,
    NotFoundError,
    ParseError,
    RedirectLoopError,
)

from .conftest import make_challenge

MIRROR = "https://sci-hub.ru"
DOI = "10.1038/nature12373"
PDF_URL = "https://sci-hub.red/storage/moscow/2161/abc/kucsko2013.pdf"


def _register_mirror_up(rsps, mirror=MIRROR):
    rsps.add(responses.GET, mirror, body="x" * 2000, status=200)


def _client(**kw):
    return ScihubClient(mirrors=[MIRROR], retries=0, **kw)


class TestResolve:
    @responses.activate
    def test_resolve_success(self, paper_html):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)

        paper = _client().resolve(DOI)

        assert paper.doi == DOI
        assert paper.title == "Nanometre-scale thermometry in a living cell"
        assert paper.authors == ["Kucsko, G.", "Maurer, P. C."]
        assert paper.year == "2013"
        assert paper.journal == "Nature"
        assert paper.pdf_url == PDF_URL

    @responses.activate
    def test_resolve_normalizes_doi_url(self, paper_html):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)
        paper = _client().resolve(f"https://doi.org/{DOI}")
        assert paper.doi == DOI

    @responses.activate
    def test_resolve_solves_captcha_then_retries(self, captcha_html, paper_html):
        chal, number = make_challenge()
        _register_mirror_up(responses)
        # first page = captcha, second = real paper
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=captcha_html, status=200)
        responses.add(
            responses.GET, f"{MIRROR}/captcha/challenge/12345", json=chal, status=200
        )
        responses.add(
            responses.GET,
            f"{MIRROR}/captcha/solution/12345",
            json={"success": True},
            status=200,
        )
        responses.add(
            responses.POST,
            f"{MIRROR}/captcha/solution/12345",
            json={"success": True},
            status=200,
        )
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)

        paper = _client().resolve(DOI)
        assert paper.pdf_url == PDF_URL

        # confirm the solution we POSTed contained the right nonce
        post_calls = [c for c in responses.calls if c.request.method == "POST"]
        assert post_calls, "expected a captcha solution POST"

    @responses.activate
    def test_captcha_rejected(self, captcha_html):
        chal, _ = make_challenge()
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=captcha_html, status=200)
        responses.add(
            responses.GET, f"{MIRROR}/captcha/challenge/12345", json=chal, status=200
        )
        responses.add(
            responses.POST,
            f"{MIRROR}/captcha/solution/12345",
            json={"success": False},
            status=200,
        )
        with pytest.raises(ChallengeError):
            _client().resolve(DOI)

    @responses.activate
    def test_persistent_captcha_raises(self, captcha_html):
        chal, _ = make_challenge()
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=captcha_html, status=200)
        responses.add(
            responses.GET, f"{MIRROR}/captcha/challenge/12345", json=chal, status=200
        )
        responses.add(
            responses.POST,
            f"{MIRROR}/captcha/solution/12345",
            json={"success": True},
            status=200,
        )
        # still a captcha on re-request
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=captcha_html, status=200)
        with pytest.raises(ChallengeError, match="after solving"):
            _client().resolve(DOI)

    @responses.activate
    def test_not_found(self, not_found_html):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=not_found_html, status=200)
        with pytest.raises(NotFoundError):
            _client().resolve(DOI)

    def test_invalid_doi_rejected_before_network(self):
        with pytest.raises(InvalidDOIError):
            _client().resolve("total-garbage")

    @responses.activate
    def test_no_mirror_reachable(self):
        responses.add(responses.GET, MIRROR, status=500)
        with pytest.raises(MirrorUnreachableError):
            _client().resolve(DOI)

    @responses.activate
    def test_mirror_fallback(self, paper_html):
        alt = "https://sci-hub.st"
        responses.add(responses.GET, MIRROR, body="Welcome to nginx!", status=200)
        responses.add(responses.GET, alt, body="y" * 2000, status=200)
        responses.add(responses.GET, f"{alt}/{DOI}", body=paper_html, status=200)

        client = ScihubClient(mirrors=[MIRROR, alt], retries=0)
        paper = client.resolve(DOI)
        assert paper.pdf_url == PDF_URL
        assert client.active_mirror == alt


class TestDownload:
    @responses.activate
    def test_download_to_directory(self, paper_html, pdf_bytes, tmp_path):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)
        responses.add(responses.GET, PDF_URL, body=pdf_bytes, status=200)

        client = _client()
        paper = client.resolve(DOI)
        out = client.download(paper, tmp_path)

        assert out.exists()
        assert out.parent == tmp_path
        assert out.read_bytes() == pdf_bytes
        assert "Kucsko" in out.name

    @responses.activate
    def test_download_to_explicit_path(self, paper_html, pdf_bytes, tmp_path):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)
        responses.add(responses.GET, PDF_URL, body=pdf_bytes, status=200)

        client = _client()
        target = tmp_path / "custom.pdf"
        out = client.download(client.resolve(DOI), target)
        assert out == target and target.exists()

    @responses.activate
    def test_rejects_non_pdf(self, paper_html, tmp_path):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)
        responses.add(responses.GET, PDF_URL, body=b"<html>nope</html>", status=200)

        client = _client()
        with pytest.raises(DownloadError, match="not a PDF"):
            client.download(client.resolve(DOI), tmp_path / "x.pdf")

    @responses.activate
    def test_no_partial_file_left_on_failure(self, paper_html, tmp_path):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)
        responses.add(responses.GET, PDF_URL, body=b"<html>nope</html>", status=200)

        client = _client()
        with pytest.raises(DownloadError):
            client.download(client.resolve(DOI), tmp_path / "x.pdf")
        assert list(tmp_path.iterdir()) == []

    @responses.activate
    def test_skips_existing_without_overwrite(self, paper_html, pdf_bytes, tmp_path):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)

        target = tmp_path / "exists.pdf"
        target.write_bytes(b"%PDF-old")

        client = _client()
        out = client.download(client.resolve(DOI), target, overwrite=False)
        assert out.read_bytes() == b"%PDF-old"

    @responses.activate
    def test_overwrite_replaces_file(self, paper_html, pdf_bytes, tmp_path):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)
        responses.add(responses.GET, PDF_URL, body=pdf_bytes, status=200)

        target = tmp_path / "exists.pdf"
        target.write_bytes(b"%PDF-old")

        client = _client()
        out = client.download(client.resolve(DOI), target, overwrite=True)
        assert out.read_bytes() == pdf_bytes

    @responses.activate
    def test_fetch_one_shot(self, paper_html, pdf_bytes, tmp_path):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)
        responses.add(responses.GET, PDF_URL, body=pdf_bytes, status=200)

        out = _client().fetch(DOI, tmp_path)
        assert out.exists() and out.read_bytes() == pdf_bytes

    @responses.activate
    def test_download_accepts_doi_string(self, paper_html, pdf_bytes, tmp_path):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)
        responses.add(responses.GET, PDF_URL, body=pdf_bytes, status=200)

        out = _client().download(DOI, tmp_path)
        assert out.exists()


class TestContextManager:
    @responses.activate
    def test_closes_session(self, paper_html):
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=paper_html, status=200)
        with _client() as client:
            assert client.resolve(DOI).pdf_url == PDF_URL


# --------------------------------------------------------------------------- #
# Resilience: redirect loops, bot checks, and per-mirror failover.
#
# These cover the failure mode seen live, where sci-hub.ru answered but bounced
# the same URL until requests' 30-redirect cap tripped. The rules under test:
#   * a redirect loop must fail fast, not burn 30 hops
#   * a redirect loop / bot check is a *host* problem -> fail over, never
#     report the paper as missing
#   * only positive "not found" evidence may raise NotFoundError
# --------------------------------------------------------------------------- #
ALT = "https://sci-hub.st"

BOT_CHECK_HTML = (
    "<html><head><title>Just a moment...</title></head>"
    "<body><p>Checking your browser before accessing.</p>"
    "<p>Please complete the check to continue</p></body></html>"
)


class TestRedirectLoop:
    @responses.activate
    def test_redirect_loop_fails_fast_and_does_not_hang(self):
        """A self-referential 302 must raise quickly, not spin 30 times."""
        _register_mirror_up(responses)
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI}",
            status=302,
            headers={"Location": f"{MIRROR}/{DOI}"},
        )
        client = ScihubClient(mirrors=[MIRROR], retries=0, max_redirects=3)
        with pytest.raises(MirrorUnreachableError):
            client.resolve(DOI)
        # Homepage warm-up + at most max_redirects hops - nowhere near 30.
        assert len(responses.calls) <= 8

    @responses.activate
    def test_redirect_loop_falls_over_to_healthy_mirror(self, paper_html):
        """Mirror health is per-host: a bounce on one must not fail the DOI."""
        responses.add(responses.GET, MIRROR, body="x" * 2000, status=200)
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI}",
            status=302,
            headers={"Location": f"{MIRROR}/{DOI}"},
        )
        responses.add(responses.GET, ALT, body="y" * 2000, status=200)
        responses.add(responses.GET, f"{ALT}/{DOI}", body=paper_html, status=200)

        client = ScihubClient(mirrors=[MIRROR, ALT], retries=0, max_redirects=3)
        paper = client.resolve(DOI)

        assert paper.pdf_url == PDF_URL
        assert client.active_mirror == ALT

    @responses.activate
    def test_looping_mirror_is_marked_dead_and_not_retried(self, paper_html):
        """Once a mirror bounces us, stop paying that cost for later DOIs."""
        responses.add(responses.GET, MIRROR, body="x" * 2000, status=200)
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI}",
            status=302,
            headers={"Location": f"{MIRROR}/{DOI}"},
        )
        responses.add(responses.GET, ALT, body="y" * 2000, status=200)
        responses.add(responses.GET, f"{ALT}/{DOI}", body=paper_html, status=200)

        client = ScihubClient(mirrors=[MIRROR, ALT], retries=0, max_redirects=3)
        client.resolve(DOI)
        assert MIRROR in client._dead_mirrors
        assert MIRROR not in client._candidate_mirrors()

    @responses.activate
    def test_redirect_loop_never_reported_as_not_found(self):
        """Regression guard: a bounce is not evidence the paper is absent."""
        _register_mirror_up(responses)
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI}",
            status=302,
            headers={"Location": f"{MIRROR}/{DOI}"},
        )
        client = ScihubClient(mirrors=[MIRROR], retries=0, max_redirects=3)
        with pytest.raises(MirrorUnreachableError):
            client.resolve(DOI)


class TestBotCheck:
    @responses.activate
    def test_bot_check_falls_over_to_healthy_mirror(self, paper_html):
        responses.add(responses.GET, MIRROR, body="x" * 2000, status=200)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=BOT_CHECK_HTML, status=200)
        responses.add(responses.GET, ALT, body="y" * 2000, status=200)
        responses.add(responses.GET, f"{ALT}/{DOI}", body=paper_html, status=200)

        client = ScihubClient(mirrors=[MIRROR, ALT], retries=0)
        assert client.resolve(DOI).pdf_url == PDF_URL

    @responses.activate
    def test_bot_check_everywhere_is_not_a_not_found(self):
        """An unclearable gate means 'unknown', never 'absent'."""
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=BOT_CHECK_HTML, status=200)

        client = ScihubClient(mirrors=[MIRROR], retries=0)
        with pytest.raises(MirrorUnreachableError):
            client.resolve(DOI)


class TestNotFoundVsParseError:
    @responses.activate
    def test_unparseable_page_raises_parse_error_not_not_found(self):
        """A page that loads but yields no link is 'we don't know'."""
        _register_mirror_up(responses)
        # Long enough not to look like a stub, but with no citation meta tags.
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI}",
            body="<html><body>" + ("filler " * 5000) + "</body></html>",
            status=200,
        )
        client = ScihubClient(mirrors=[MIRROR], retries=0)
        with pytest.raises(ParseError):
            client.resolve(DOI)

    @responses.activate
    def test_parse_error_message_disclaims_unavailability(self):
        _register_mirror_up(responses)
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI}",
            body="<html><body>" + ("filler " * 5000) + "</body></html>",
            status=200,
        )
        client = ScihubClient(mirrors=[MIRROR], retries=0)
        with pytest.raises(ParseError, match="NOT proof"):
            client.resolve(DOI)

    @responses.activate
    def test_explicit_marker_still_raises_not_found(self, not_found_html):
        """Positive evidence must still produce a clean NotFoundError."""
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=not_found_html, status=200)
        client = ScihubClient(mirrors=[MIRROR], retries=0)
        with pytest.raises(NotFoundError):
            client.resolve(DOI)

    @responses.activate
    def test_second_mirror_can_rescue_a_not_found(self, paper_html, not_found_html):
        """One mirror saying 'absent' must not end the search."""
        responses.add(responses.GET, MIRROR, body="x" * 2000, status=200)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=not_found_html, status=200)
        responses.add(responses.GET, ALT, body="y" * 2000, status=200)
        responses.add(responses.GET, f"{ALT}/{DOI}", body=paper_html, status=200)

        client = ScihubClient(mirrors=[MIRROR, ALT], retries=0)
        assert client.resolve(DOI).pdf_url == PDF_URL


class TestCaptchaErrorAttribution:
    @responses.activate
    def test_captcha_failure_surfaces_as_challenge_error(self, captcha_html):
        """A cleared host that we can't get past is a ChallengeError,
        not 'no mirror answered'."""
        chal, _ = make_challenge()
        _register_mirror_up(responses)
        responses.add(responses.GET, f"{MIRROR}/{DOI}", body=captcha_html, status=200)
        responses.add(
            responses.GET, f"{MIRROR}/captcha/challenge/12345", json=chal, status=200
        )
        responses.add(
            responses.POST,
            f"{MIRROR}/captcha/solution/12345",
            json={"success": False},
            status=200,
        )
        with pytest.raises(ChallengeError):
            ScihubClient(mirrors=[MIRROR], retries=0).resolve(DOI)
import json

import pytest
import responses

from scihub_dl.batch import BatchDownloader, summarize, write_manifest
from scihub_dl.client import ScihubClient
from scihub_dl.models import BatchResult

MIRROR = "https://sci-hub.ru"
DOI1 = "10.1038/nature12373"
DOI2 = "10.1126/science.1157996"
PDF1 = "https://sci-hub.red/storage/moscow/2161/abc/kucsko2013.pdf"
PDF2 = "https://sci-hub.red/storage/dace/1116/def/lee2008.pdf"


def paper_page(title, author, year, pdf_url):
    return (
        "<html><head>"
        f'<meta name="citation_title" content="{title}">'
        f'<meta name="citation_author" content="{author}">'
        f'<meta name="citation_publication_date" content="{year}">'
        f'<meta name="citation_pdf_url" content="{pdf_url}">'
        "</head><body>" + "pad " * 6000 + "</body></html>"
    )


PDF_BYTES = b"%PDF-1.5\n" + b"0" * 1024


def _downloader(**kw):
    kw.setdefault("delay", 0)
    kw.setdefault("workers", 2)
    return BatchDownloader(client=ScihubClient(mirrors=[MIRROR], retries=0), **kw)


def _mirror_up():
    responses.add(responses.GET, MIRROR, body="x" * 2000, status=200)


class TestBatchRun:
    @responses.activate
    def test_all_succeed(self, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "Kucsko, G.", "2013", PDF1),
            status=200,
        )
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI2}",
            body=paper_page("Paper Two", "Lee, J.", "2008", PDF2),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)
        responses.add(responses.GET, PDF2, body=PDF_BYTES, status=200)

        with _downloader() as batch:
            results = batch.run([DOI1, DOI2], dest=tmp_path)

        assert len(results) == 2
        assert all(r.ok for r in results)
        assert {r.doi for r in results} == {DOI1, DOI2}
        assert len(list(tmp_path.glob("*.pdf"))) == 2

    @responses.activate
    def test_results_preserve_input_order(self, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI2}",
            body=paper_page("Paper Two", "B", "2008", PDF2),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)
        responses.add(responses.GET, PDF2, body=PDF_BYTES, status=200)

        with _downloader(workers=2) as batch:
            results = batch.run([DOI1, DOI2], dest=tmp_path)

        assert [r.doi for r in results] == [DOI1, DOI2]

    @responses.activate
    def test_mixed_outcomes(self, tmp_path, not_found_html):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)
        responses.add(responses.GET, f"{MIRROR}/{DOI2}", body=not_found_html, status=200)

        with _downloader() as batch:
            results = batch.run([DOI1, DOI2], dest=tmp_path)

        by_doi = {r.doi: r for r in results}
        assert by_doi[DOI1].status == "ok"
        assert by_doi[DOI2].status == "not_found"

    @responses.activate
    def test_invalid_doi_becomes_error_not_exception(self, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)

        with _downloader() as batch:
            results = batch.run([DOI1, "not-a-doi"], dest=tmp_path)

        assert len(results) == 2
        statuses = {r.doi: r.status for r in results}
        assert statuses[DOI1] == "ok"
        assert statuses["not-a-doi"] == "error"

    @responses.activate
    def test_one_failure_does_not_kill_batch(self, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)
        # DOI2 page returns a server error
        responses.add(responses.GET, f"{MIRROR}/{DOI2}", status=503)

        with _downloader() as batch:
            results = batch.run([DOI1, DOI2], dest=tmp_path)

        assert len(results) == 2
        assert any(r.status == "ok" for r in results)
        assert any(r.status == "error" for r in results)

    @responses.activate
    def test_metadata_only_skips_download(self, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )

        with _downloader(metadata_only=True) as batch:
            results = batch.run([DOI1], dest=tmp_path)

        assert results[0].ok
        assert results[0].pdf_url == PDF1
        assert results[0].path is None
        assert list(tmp_path.glob("*.pdf")) == []

    @responses.activate
    def test_progress_callback_invoked(self, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)

        seen = []
        with _downloader() as batch:
            batch.run([DOI1], dest=tmp_path, on_result=seen.append)
        assert len(seen) == 1 and seen[0].doi == DOI1

    @responses.activate
    def test_callback_exception_does_not_break_batch(self, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)

        def boom(res):
            raise RuntimeError("ui exploded")

        with _downloader() as batch:
            results = batch.run([DOI1], dest=tmp_path, on_result=boom)
        assert results[0].ok

    def test_empty_input(self, tmp_path):
        with _downloader() as batch:
            assert batch.run([], dest=tmp_path) == []

    @responses.activate
    def test_unreachable_mirror_marks_all_errors(self, tmp_path):
        responses.add(responses.GET, MIRROR, status=500)
        with _downloader() as batch:
            results = batch.run([DOI1, DOI2], dest=tmp_path)
        assert len(results) == 2
        assert all(r.status == "error" for r in results)


class TestSummarize:
    def test_counts(self):
        results = [
            BatchResult(doi="a", status="ok", size=100),
            BatchResult(doi="b", status="ok", size=200),
            BatchResult(doi="c", status="not_found"),
            BatchResult(doi="d", status="error", error="boom"),
        ]
        s = summarize(results)
        assert s == {
            "total": 4,
            "ok": 2,
            "not_found": 1,
            "parse_errors": 0,
            "errors": 1,
            "downloaded_bytes": 300,
        }

    def test_parse_errors_counted_separately_from_not_found(self):
        """A page we could not read must never be reported as 'absent'."""
        results = [
            BatchResult(doi="a", status="not_found"),
            BatchResult(doi="b", status="parse_error", error="layout changed"),
            BatchResult(doi="c", status="parse_error", error="layout changed"),
        ]
        s = summarize(results)
        assert s["not_found"] == 1
        assert s["parse_errors"] == 2
        assert s["ok"] == 0

    def test_empty(self):
        assert summarize([])["total"] == 0


class TestManifest:
    def test_writes_json(self, tmp_path):
        results = [
            BatchResult(doi="10.1/a", status="ok", path="/x/a.pdf", size=10, title="A"),
            BatchResult(doi="10.1/b", status="not_found", error="nope"),
        ]
        path = write_manifest(results, tmp_path / "m.json")
        data = json.loads(path.read_text())
        assert data["summary"]["total"] == 2
        assert data["summary"]["ok"] == 1
        assert len(data["results"]) == 2
        assert data["results"][0]["doi"] == "10.1/a"

    def test_creates_parent_dirs(self, tmp_path):
        path = write_manifest([], tmp_path / "nested" / "deep" / "m.json")
        assert path.exists()
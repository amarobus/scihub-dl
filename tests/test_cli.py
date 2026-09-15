import json

import pytest
import responses
from click.testing import CliRunner

from scihub_dl.cli import main

MIRROR = "https://sci-hub.ru"
DOI1 = "10.1038/nature12373"
DOI2 = "10.1126/science.1157996"
PDF1 = "https://sci-hub.red/storage/moscow/2161/abc/kucsko2013.pdf"
PDF2 = "https://sci-hub.red/storage/dace/1116/def/lee2008.pdf"
PDF_BYTES = b"%PDF-1.5\n" + b"0" * 1024


def paper_page(title, author, year, pdf_url):
    return (
        "<html><head>"
        f'<meta name="citation_title" content="{title}">'
        f'<meta name="citation_author" content="{author}">'
        f'<meta name="citation_publication_date" content="{year}">'
        f'<meta name="citation_journal_title" content="Nature">'
        f'<meta name="citation_pdf_url" content="{pdf_url}">'
        "</head><body>" + "pad " * 6000 + "</body></html>"
    )


def _mirror_up():
    responses.add(responses.GET, MIRROR, body="x" * 2000, status=200)


@pytest.fixture
def runner():
    return CliRunner()


class TestHelpAndVersion:
    def test_help(self, runner):
        res = runner.invoke(main, ["--help"])
        assert res.exit_code == 0
        assert "Sci-Hub" in res.output
        for cmd in ("fetch", "batch", "info"):
            assert cmd in res.output

    def test_version(self, runner):
        res = runner.invoke(main, ["--version"])
        assert res.exit_code == 0
        assert "0.1.0" in res.output

    @pytest.mark.parametrize("cmd", ["fetch", "batch", "info"])
    def test_subcommand_help(self, runner, cmd):
        res = runner.invoke(main, [cmd, "--help"])
        assert res.exit_code == 0


class TestInfo:
    @responses.activate
    def test_info_table(self, runner):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Nanometre-scale thermometry", "Kucsko, G.", "2013", PDF1),
            status=200,
        )
        res = runner.invoke(main, ["info", DOI1, "--mirror", MIRROR])
        assert res.exit_code == 0
        assert "Nanometre-scale thermometry" in res.output

    @responses.activate
    def test_info_json(self, runner):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Title Here", "Kucsko, G.", "2013", PDF1),
            status=200,
        )
        res = runner.invoke(main, ["info", DOI1, "--json", "--mirror", MIRROR])
        assert res.exit_code == 0
        data = json.loads(res.output)
        assert data["doi"] == DOI1
        assert data["pdf_url"] == PDF1

    @responses.activate
    def test_info_not_found_exits_1(self, runner, not_found_html):
        _mirror_up()
        responses.add(responses.GET, f"{MIRROR}/{DOI1}", body=not_found_html, status=200)
        res = runner.invoke(main, ["info", DOI1, "--mirror", MIRROR])
        assert res.exit_code == 1

    def test_info_invalid_doi_exits_1(self, runner):
        res = runner.invoke(main, ["info", "garbage", "--mirror", MIRROR])
        assert res.exit_code == 1


class TestFetch:
    @responses.activate
    def test_fetch_downloads(self, runner, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "Kucsko, G.", "2013", PDF1),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)

        res = runner.invoke(
            main, ["fetch", DOI1, "-o", str(tmp_path), "--mirror", MIRROR]
        )
        assert res.exit_code == 0, res.output
        assert "Saved" in res.output
        pdfs = list(tmp_path.glob("*.pdf"))
        assert len(pdfs) == 1 and pdfs[0].read_bytes() == PDF_BYTES

    @responses.activate
    def test_fetch_not_found_exits_1(self, runner, tmp_path, not_found_html):
        _mirror_up()
        responses.add(responses.GET, f"{MIRROR}/{DOI1}", body=not_found_html, status=200)
        res = runner.invoke(
            main, ["fetch", DOI1, "-o", str(tmp_path), "--mirror", MIRROR]
        )
        assert res.exit_code == 1
        assert "Error" in res.output


class TestBatch:
    @responses.activate
    def test_batch_args(self, runner, tmp_path):
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

        res = runner.invoke(
            main,
            ["batch", DOI1, DOI2, "-o", str(tmp_path), "--mirror", MIRROR, "--delay", "0"],
        )
        assert res.exit_code == 0, res.output
        assert len(list(tmp_path.glob("*.pdf"))) == 2

    @responses.activate
    def test_batch_from_file(self, runner, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)

        doi_file = tmp_path / "dois.txt"
        doi_file.write_text(f"# comment\n{DOI1}\n\n")
        out_dir = tmp_path / "out"

        res = runner.invoke(
            main,
            [
                "batch",
                "-f",
                str(doi_file),
                "-o",
                str(out_dir),
                "--mirror",
                MIRROR,
                "--delay",
                "0",
            ],
        )
        assert res.exit_code == 0, res.output
        assert len(list(out_dir.glob("*.pdf"))) == 1

    @responses.activate
    def test_batch_json_output(self, runner, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)

        res = runner.invoke(
            main,
            [
                "batch",
                DOI1,
                "-o",
                str(tmp_path),
                "--json",
                "--mirror",
                MIRROR,
                "--delay",
                "0",
            ],
        )
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["summary"]["ok"] == 1
        assert data["results"][0]["doi"] == DOI1

    @responses.activate
    def test_batch_manifest(self, runner, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)

        manifest = tmp_path / "manifest.json"
        res = runner.invoke(
            main,
            [
                "batch",
                DOI1,
                "-o",
                str(tmp_path / "out"),
                "--manifest",
                str(manifest),
                "--mirror",
                MIRROR,
                "--delay",
                "0",
            ],
        )
        assert res.exit_code == 0, res.output
        assert manifest.exists()
        data = json.loads(manifest.read_text())
        assert data["summary"]["ok"] == 1

    @responses.activate
    def test_batch_metadata_only(self, runner, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )
        res = runner.invoke(
            main,
            [
                "batch",
                DOI1,
                "-o",
                str(tmp_path),
                "--metadata-only",
                "--json",
                "--mirror",
                MIRROR,
                "--delay",
                "0",
            ],
        )
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["results"][0]["pdf_url"] == PDF1
        assert list(tmp_path.glob("*.pdf")) == []

    def test_batch_no_dois_exits_2(self, runner, tmp_path):
        res = runner.invoke(
            main, ["batch", "-o", str(tmp_path), "--mirror", MIRROR], input=""
        )
        assert res.exit_code == 2

    @responses.activate
    def test_batch_all_fail_exits_1(self, runner, tmp_path, not_found_html):
        _mirror_up()
        responses.add(responses.GET, f"{MIRROR}/{DOI1}", body=not_found_html, status=200)
        res = runner.invoke(
            main,
            ["batch", DOI1, "-o", str(tmp_path), "--mirror", MIRROR, "--delay", "0"],
        )
        assert res.exit_code == 1

    @responses.activate
    def test_batch_dedupes(self, runner, tmp_path):
        _mirror_up()
        responses.add(
            responses.GET,
            f"{MIRROR}/{DOI1}",
            body=paper_page("Paper One", "A", "2013", PDF1),
            status=200,
        )
        responses.add(responses.GET, PDF1, body=PDF_BYTES, status=200)

        res = runner.invoke(
            main,
            [
                "batch",
                DOI1,
                f"https://doi.org/{DOI1}",
                "-o",
                str(tmp_path),
                "--json",
                "--mirror",
                MIRROR,
                "--delay",
                "0",
            ],
        )
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["summary"]["total"] == 1

import pytest

from scihub_dl.errors import InvalidDOIError
from scihub_dl.models import Paper
from scihub_dl.utils import (
    absolutize_url,
    is_valid_doi,
    normalize_doi,
    read_dois,
    safe_filename,
)


class TestNormalizeDOI:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("10.1038/nature12373", "10.1038/nature12373"),
            ("  10.1038/nature12373  ", "10.1038/nature12373"),
            ("https://doi.org/10.1038/nature12373", "10.1038/nature12373"),
            ("http://dx.doi.org/10.1038/nature12373", "10.1038/nature12373"),
            ("doi:10.1038/nature12373", "10.1038/nature12373"),
            ("DOI:10.1038/nature12373", "10.1038/nature12373"),
            ("10.1126/science.1157996", "10.1126/science.1157996"),
            ("10.1016/j.cell.2011.02.013", "10.1016/j.cell.2011.02.013"),
            ("10.1038/nature12373,", "10.1038/nature12373"),
        ],
    )
    def test_valid(self, raw, expected):
        assert normalize_doi(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["", "   ", "not-a-doi", "10.x/abc", "https://example.com", "12345"]
    )
    def test_invalid(self, raw):
        with pytest.raises(InvalidDOIError):
            normalize_doi(raw)

    def test_none(self):
        with pytest.raises(InvalidDOIError):
            normalize_doi(None)

    def test_embedded_in_text(self):
        assert normalize_doi("see 10.1038/nature12373 for details") == "10.1038/nature12373"

    def test_is_valid_doi(self):
        assert is_valid_doi("10.1038/nature12373")
        assert not is_valid_doi("garbage")


class TestSafeFilename:
    def test_strips_slashes(self):
        assert "/" not in safe_filename("10.1038/nature12373")

    def test_collapses_spaces(self):
        assert safe_filename("a   b  c") == "a_b_c"

    def test_removes_special_chars(self):
        out = safe_filename("Title: with *bad* chars?")
        assert ":" not in out and "*" not in out and "?" not in out

    def test_truncates(self):
        assert len(safe_filename("x" * 500, max_len=50)) == 50

    def test_never_empty(self):
        assert safe_filename("???") == "paper"


class TestReadDois:
    def test_plain_lines(self):
        lines = ["10.1038/nature12373", "10.1126/science.1157996"]
        assert read_dois(lines) == lines

    def test_skips_blanks_and_comments(self):
        lines = ["", "  ", "# a comment", "10.1038/nature12373"]
        assert read_dois(lines) == ["10.1038/nature12373"]

    def test_dedupes_preserving_order(self):
        lines = ["10.1038/nature12373", "10.1126/science.1157996", "10.1038/nature12373"]
        assert read_dois(lines) == ["10.1038/nature12373", "10.1126/science.1157996"]

    def test_csv_row(self):
        lines = ["10.1038/nature12373,Some Title,2013"]
        assert read_dois(lines) == ["10.1038/nature12373"]

    def test_urls(self):
        lines = ["https://doi.org/10.1038/nature12373"]
        assert read_dois(lines) == ["10.1038/nature12373"]

    def test_ignores_junk(self):
        lines = ["hello world", "10.1038/nature12373"]
        assert read_dois(lines) == ["10.1038/nature12373"]


class TestAbsolutizeUrl:
    def test_protocol_relative(self):
        assert absolutize_url("//sci-hub.red/a.pdf", "https://sci-hub.ru") == (
            "https://sci-hub.red/a.pdf"
        )

    def test_root_relative(self):
        assert absolutize_url("/storage/a.pdf", "https://sci-hub.ru") == (
            "https://sci-hub.ru/storage/a.pdf"
        )

    def test_already_absolute(self):
        url = "https://sci-hub.red/a.pdf"
        assert absolutize_url(url, "https://sci-hub.ru") == url


class TestPaperModel:
    def test_suggested_filename(self):
        p = Paper(
            doi="10.1038/nature12373",
            title="Nanometre-scale thermometry in a living cell",
            authors=["Kucsko, G.", "Maurer, P. C."],
            year="2013",
        )
        name = p.suggested_filename()
        assert name.endswith(".pdf")
        assert "Kucsko" in name
        assert "2013" in name

    def test_filename_fallback_to_doi(self):
        p = Paper(doi="10.1038/nature12373")
        assert p.suggested_filename() == "10.1038_nature12373.pdf"

    def test_to_dict(self):
        p = Paper(doi="10.1/x", title="T")
        d = p.to_dict()
        assert d["doi"] == "10.1/x" and d["title"] == "T"
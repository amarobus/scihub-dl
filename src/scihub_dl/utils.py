"""Small helpers: DOI normalisation/validation, filename sanitising, DOI file parsing."""

import re
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urlparse

from .errors import InvalidDOIError

# DOIs always start with "10." followed by a registrant code, a slash, and a suffix.
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9A-Z]+")

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
    "DOI:",
)


def normalize_doi(raw: str) -> str:
    """Strip URL/`doi:` wrappers and validate, returning a bare DOI.

    Raises InvalidDOIError if no DOI can be extracted.
    """
    if raw is None:
        raise InvalidDOIError("DOI is None")

    s = raw.strip().strip(",;")
    if not s:
        raise InvalidDOIError("DOI is empty")

    for prefix in _DOI_PREFIXES:
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix) :]
            break

    m = DOI_RE.search(s)
    if not m:
        raise InvalidDOIError(f"Not a valid DOI: {raw!r}")
    return m.group(0)


def is_valid_doi(raw: str) -> bool:
    try:
        normalize_doi(raw)
        return True
    except InvalidDOIError:
        return False


def safe_filename(name: str, max_len: int = 120) -> str:
    """Make a string safe to use as a filename across platforms."""
    name = name.replace("/", "_").replace("\\", "_")
    # collapse anything not alphanumeric/dash/underscore/dot/space
    name = re.sub(r"[^\w\-. ]+", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name.strip())
    name = name.strip("._") or "paper"
    return name[:max_len]


def read_dois(source: Iterable[str]) -> List[str]:
    """Extract DOIs from arbitrary lines (supports plain lists and 1-column CSV).

    Blank lines, comments (`#`) and non-DOI tokens are ignored. Order is
    preserved and duplicates removed.
    """
    out: List[str] = []
    seen = set()
    for line in source:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # allow "doi,extra,columns" CSV rows - test each field
        candidates = [line] + [p.strip() for p in line.split(",")]
        for cand in candidates:
            try:
                doi = normalize_doi(cand)
            except InvalidDOIError:
                continue
            if doi not in seen:
                seen.add(doi)
                out.append(doi)
            break
    return out


def read_dois_file(path: str) -> List[str]:
    """Read DOIs from a text/CSV file (use '-' for stdin)."""
    if str(path) == "-":
        import sys

        return read_dois(sys.stdin)
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return read_dois(text.splitlines())


def absolutize_url(url: str, base: str) -> str:
    """Turn protocol-relative or root-relative Sci-Hub URLs into absolute ones."""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        p = urlparse(base)
        return f"{p.scheme}://{p.netloc}{url}"
    return base.rstrip("/") + "/" + url
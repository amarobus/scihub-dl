"""Data models for scihub_dl."""

from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any


@dataclass
class Paper:
    """Metadata scraped from a Sci-Hub paper page."""

    doi: str
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    year: Optional[str] = None
    journal: Optional[str] = None
    pdf_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def suggested_filename(self, ext: str = "pdf") -> str:
        """Build a filesystem-safe filename from the metadata."""
        from .utils import safe_filename

        parts = []
        if self.authors:
            # first author surname, as it appears before the comma
            first = self.authors[0].split(",")[0].strip()
            if first:
                parts.append(first)
        if self.year:
            parts.append(str(self.year))
        if self.title:
            parts.append(self.title[:60])
        if not parts:
            parts.append(self.doi)
        return safe_filename("_".join(parts)) + f".{ext}"


@dataclass
class BatchResult:
    """Outcome of a single DOI within a batch run."""

    doi: str
    # "ok"          - downloaded / resolved
    # "not_found"   - Sci-Hub affirmatively reports the document is absent
    # "parse_error" - page loaded but no PDF link could be extracted
    #                 (NOT evidence of absence; likely a layout change)
    # "error"       - transport/mirror/captcha failure
    # "skipped"     - intentionally not attempted
    status: str
    path: Optional[str] = None
    size: Optional[int] = None
    title: Optional[str] = None
    pdf_url: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
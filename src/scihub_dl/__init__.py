"""scihub-dl - resolve and download papers from Sci-Hub by DOI.

Quick start::

    from scihub_dl import ScihubClient, BatchDownloader

    with ScihubClient() as client:
        paper = client.resolve("10.1038/nature12373")
        print(paper.title, paper.pdf_url)
        client.download(paper, "papers/")

    with BatchDownloader(workers=4) as batch:
        results = batch.run(["10.1038/nature12373", "10.1126/science.1157996"],
                            dest="papers/")

Note: Sci-Hub has no official API; this package scrapes the public site and
solves its open-source Altcha proof-of-work gate. Availability and legality
vary - see the README.
"""

from .batch import BatchDownloader, summarize, write_manifest
from .client import DEFAULT_MIRRORS, ScihubClient
from .errors import (
    BotCheckError,
    ChallengeError,
    DownloadError,
    InvalidDOIError,
    MirrorUnreachableError,
    NotFoundError,
    ParseError,
    RedirectLoopError,
    ScihubError,
)
from .models import BatchResult, Paper
from .utils import is_valid_doi, normalize_doi, read_dois, read_dois_file

__version__ = "0.1.0"

__all__ = [
    "ScihubClient",
    "BatchDownloader",
    "Paper",
    "BatchResult",
    "DEFAULT_MIRRORS",
    "summarize",
    "write_manifest",
    "normalize_doi",
    "is_valid_doi",
    "read_dois",
    "read_dois_file",
    "ScihubError",
    "MirrorUnreachableError",
    "RedirectLoopError",
    "BotCheckError",
    "ChallengeError",
    "NotFoundError",
    "ParseError",
    "InvalidDOIError",
    "DownloadError",
    "__version__",
]
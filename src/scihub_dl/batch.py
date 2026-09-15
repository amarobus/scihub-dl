"""Batch DOI resolution and download.

Uses a thread pool (network-bound work) with a shared, already-verified
session so the Altcha challenge is normally solved once for the whole batch.
Politeness delay and bounded concurrency keep load on the mirror low.
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Union

from .client import ScihubClient
from .errors import InvalidDOIError, NotFoundError, ScihubError
from .models import BatchResult
from .utils import normalize_doi

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[BatchResult], None]


class BatchDownloader:
    """Download many DOIs concurrently.

    Example::

        with BatchDownloader(workers=4) as batch:
            results = batch.run(["10.1038/nature12373", "10.1126/science.1157996"],
                                dest="papers/")
    """

    def __init__(
        self,
        client: Optional[ScihubClient] = None,
        workers: int = 3,
        delay: float = 0.5,
        overwrite: bool = False,
        metadata_only: bool = False,
        **client_kwargs,
    ):
        self.client = client or ScihubClient(**client_kwargs)
        self.workers = max(1, int(workers))
        self.delay = max(0.0, float(delay))
        self.overwrite = overwrite
        self.metadata_only = metadata_only
        self._lock = threading.Lock()
        self._last_request = 0.0

    def __enter__(self) -> "BatchDownloader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------ #
    def _throttle(self) -> None:
        """Serialise a minimum gap between outbound requests across threads."""
        if self.delay <= 0:
            return
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            wait = self.delay - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _handle_one(self, raw_doi: str, dest: Union[str, Path]) -> BatchResult:
        try:
            doi = normalize_doi(raw_doi)
        except InvalidDOIError as exc:
            return BatchResult(doi=str(raw_doi), status="error", error=str(exc))

        try:
            self._throttle()
            paper = self.client.resolve(doi)

            if self.metadata_only:
                return BatchResult(
                    doi=doi,
                    status="ok",
                    title=paper.title,
                    pdf_url=paper.pdf_url,
                )

            path = self.client.download(paper, dest, overwrite=self.overwrite)
            size = path.stat().st_size if path.exists() else None
            return BatchResult(
                doi=doi,
                status="ok",
                path=str(path),
                size=size,
                title=paper.title,
                pdf_url=paper.pdf_url,
            )

        except NotFoundError as exc:
            return BatchResult(doi=doi, status="not_found", error=str(exc))
        except ScihubError as exc:
            return BatchResult(doi=doi, status="error", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - never let one DOI kill the batch
            logger.exception("Unexpected error for DOI %s", doi)
            return BatchResult(doi=doi, status="error", error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    def run(
        self,
        dois: Iterable[str],
        dest: Union[str, Path] = ".",
        on_result: Optional[ProgressCallback] = None,
    ) -> List[BatchResult]:
        """Process every DOI, returning results in input order.

        ``on_result`` is called (from worker threads) as each DOI completes.
        """
        doi_list = [d for d in dois if str(d).strip()]
        if not doi_list:
            return []

        if not self.metadata_only:
            dest_path = Path(dest)
            if dest_path.suffix == "":
                dest_path.mkdir(parents=True, exist_ok=True)

        # Warm up the session once so the captcha is solved a single time
        # instead of racing across every worker thread.
        try:
            self.client._pick_mirror()  # noqa: SLF001 - intentional internal warm-up
        except ScihubError as exc:
            return [
                BatchResult(doi=str(d), status="error", error=str(exc)) for d in doi_list
            ]

        results: List[Optional[BatchResult]] = [None] * len(doi_list)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._handle_one, doi, dest): idx
                for idx, doi in enumerate(doi_list)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                res = fut.result()
                results[idx] = res
                if on_result is not None:
                    try:
                        on_result(res)
                    except Exception:  # noqa: BLE001 - progress UI must not break the batch
                        logger.debug("progress callback raised", exc_info=True)

        return [r for r in results if r is not None]


def summarize(results: Sequence[BatchResult]) -> dict:
    """Aggregate counts for a finished batch."""
    total = len(results)
    ok = sum(1 for r in results if r.status == "ok")
    not_found = sum(1 for r in results if r.status == "not_found")
    errors = sum(1 for r in results if r.status == "error")
    downloaded_bytes = sum(r.size or 0 for r in results if r.status == "ok")
    return {
        "total": total,
        "ok": ok,
        "not_found": not_found,
        "errors": errors,
        "downloaded_bytes": downloaded_bytes,
    }


def write_manifest(
    results: Sequence[BatchResult], path: Union[str, Path]
) -> Path:
    """Write a JSON manifest of the batch (results + summary)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": summarize(results),
        "results": [r.to_dict() for r in results],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path

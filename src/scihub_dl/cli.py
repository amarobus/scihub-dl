"""Command-line interface for scihub-dl."""

import json
import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import __version__
from .batch import BatchDownloader, summarize, write_manifest
from .client import DEFAULT_MIRRORS, ScihubClient
from .errors import InvalidDOIError, NotFoundError, ParseError, ScihubError
from .models import BatchResult
from .utils import normalize_doi, read_dois_file

console = Console()
err_console = Console(stderr=True)

STATUS_STYLE = {
    "ok": "green",
    "not_found": "yellow",
    "parse_error": "magenta",
    "error": "red",
    "skipped": "dim",
}


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _human_size(num: Optional[int]) -> str:
    if not num:
        return "-"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _collect_dois(dois: Tuple[str, ...], from_file: Optional[str]) -> List[str]:
    collected: List[str] = []
    seen = set()

    if from_file:
        for doi in read_dois_file(from_file):
            if doi not in seen:
                seen.add(doi)
                collected.append(doi)

    for raw in dois:
        try:
            doi = normalize_doi(raw)
        except InvalidDOIError as exc:
            err_console.print(f"[red]Skipping invalid DOI:[/red] {exc}")
            continue
        if doi not in seen:
            seen.add(doi)
            collected.append(doi)

    return collected


# --------------------------------------------------------------------------- #
# shared options
# --------------------------------------------------------------------------- #
def client_options(fn):
    fn = click.option(
        "--mirror",
        "mirrors",
        multiple=True,
        help=f"Sci-Hub mirror to try (repeatable). Default: {', '.join(DEFAULT_MIRRORS)}",
    )(fn)
    fn = click.option("--timeout", default=30, show_default=True, help="Per-request timeout (s).")(fn)
    fn = click.option("--retries", default=2, show_default=True, help="Retries per request.")(fn)
    fn = click.option(
        "--insecure",
        is_flag=True,
        help="Skip TLS verification (some mirrors use self-signed certs).",
    )(fn)
    fn = click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")(fn)
    return fn


def _make_client(mirrors, timeout, retries, insecure) -> ScihubClient:
    return ScihubClient(
        mirrors=list(mirrors) or None,
        timeout=timeout,
        retries=retries,
        verify_ssl=not insecure,
    )


# --------------------------------------------------------------------------- #
# cli group
# --------------------------------------------------------------------------- #
@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="scihub-dl")
def main() -> None:
    """Resolve and download papers from Sci-Hub by DOI.

    \b
    Examples:
      scihub-dl fetch 10.1038/nature12373 -o papers/
      scihub-dl info 10.1038/nature12373
      scihub-dl batch --from-file dois.txt -o papers/ --workers 4
      scihub-dl batch 10.1038/nature12373 10.1126/science.1157996 -o papers/

    Sci-Hub has no official API - this scrapes the public site. Check the
    legality of use in your jurisdiction.
    """


# --------------------------------------------------------------------------- #
# info
# --------------------------------------------------------------------------- #
@main.command()
@click.argument("doi")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@client_options
def info(doi, as_json, mirrors, timeout, retries, insecure, verbose):
    """Show metadata and the resolved PDF URL for a DOI (no download)."""
    _setup_logging(verbose)
    try:
        with _make_client(mirrors, timeout, retries, insecure) as client:
            paper = client.resolve(doi)
    except ScihubError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(paper.to_dict(), indent=2, ensure_ascii=False))
        return

    table = Table(show_header=False, box=None)
    table.add_column(style="bold cyan")
    table.add_column(overflow="fold")
    table.add_row("DOI", paper.doi)
    table.add_row("Title", paper.title or "-")
    table.add_row("Authors", ", ".join(paper.authors) or "-")
    table.add_row("Year", paper.year or "-")
    table.add_row("Journal", paper.journal or "-")
    table.add_row("PDF URL", paper.pdf_url or "-")
    table.add_row("Filename", paper.suggested_filename())
    console.print(table)


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
@main.command()
@click.argument("doi")
@click.option(
    "-o",
    "--output",
    default=".",
    show_default=True,
    help="Output directory or file path.",
)
@click.option("--overwrite", is_flag=True, help="Overwrite an existing file.")
@client_options
def fetch(doi, output, overwrite, mirrors, timeout, retries, insecure, verbose):
    """Download a single paper by DOI."""
    _setup_logging(verbose)
    try:
        with _make_client(mirrors, timeout, retries, insecure) as client:
            with console.status(f"Resolving {doi}..."):
                paper = client.resolve(doi)
            console.print(f"[bold]{paper.title or paper.doi}[/bold]")
            with console.status("Downloading PDF..."):
                path = client.download(paper, output, overwrite=overwrite)
    except ScihubError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    size = path.stat().st_size if path.exists() else 0
    console.print(f"[green]Saved[/green] {path} ({_human_size(size)})")


# --------------------------------------------------------------------------- #
# batch
# --------------------------------------------------------------------------- #
@main.command()
@click.argument("dois", nargs=-1)
@click.option(
    "-f",
    "--from-file",
    type=click.Path(exists=True, allow_dash=True),
    help="Read DOIs from a text/CSV file ('-' for stdin). One DOI per line.",
)
@click.option(
    "-o",
    "--output",
    default="papers",
    show_default=True,
    help="Output directory.",
)
@click.option("--workers", default=3, show_default=True, help="Concurrent downloads.")
@click.option(
    "--delay",
    default=0.5,
    show_default=True,
    help="Minimum delay between requests (s), to stay polite.",
)
@click.option("--overwrite", is_flag=True, help="Re-download existing files.")
@click.option(
    "--metadata-only",
    is_flag=True,
    help="Resolve metadata and PDF URLs without downloading.",
)
@click.option(
    "--manifest",
    type=click.Path(),
    help="Write a JSON manifest of results to this path.",
)
@click.option("--json", "as_json", is_flag=True, help="Print results as JSON to stdout.")
@click.option(
    "--fail-fast/--no-fail-fast",
    default=False,
    show_default=True,
    help="Exit non-zero on the first failure.",
)
@client_options
def batch(
    dois,
    from_file,
    output,
    workers,
    delay,
    overwrite,
    metadata_only,
    manifest,
    as_json,
    fail_fast,
    mirrors,
    timeout,
    retries,
    insecure,
    verbose,
):
    """Resolve/download many DOIs at once.

    \b
    DOIs may be passed as arguments, via --from-file, or piped on stdin:
      cat dois.txt | scihub-dl batch -f - -o papers/
    """
    _setup_logging(verbose)

    if not dois and not from_file and not sys.stdin.isatty():
        from_file = "-"

    doi_list = _collect_dois(dois, from_file)
    if not doi_list:
        err_console.print(
            "[red]No valid DOIs supplied.[/red] Pass them as arguments or use --from-file."
        )
        sys.exit(2)

    results: List[BatchResult] = []

    with BatchDownloader(
        client=_make_client(mirrors, timeout, retries, insecure),
        workers=workers,
        delay=delay,
        overwrite=overwrite,
        metadata_only=metadata_only,
    ) as downloader:
        if as_json:
            results = downloader.run(doi_list, dest=output)
        else:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Fetching papers", total=len(doi_list))

                def on_result(res: BatchResult) -> None:
                    style = STATUS_STYLE.get(res.status, "white")
                    label = res.title or res.doi
                    progress.console.print(
                        f"[{style}]{res.status:<9}[/{style}] {res.doi}  "
                        f"{label[:70] if res.status == 'ok' else (res.error or '')[:70]}"
                    )
                    progress.advance(task)

                results = downloader.run(doi_list, dest=output, on_result=on_result)

    stats = summarize(results)

    if manifest:
        path = write_manifest(results, manifest)
        if not as_json:
            console.print(f"[cyan]Manifest:[/cyan] {path}")

    if as_json:
        click.echo(
            json.dumps(
                {"summary": stats, "results": [r.to_dict() for r in results]},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        table = Table(title="Batch summary", show_header=True, header_style="bold")
        table.add_column("Total", justify="right")
        table.add_column("OK", justify="right", style="green")
        table.add_column("Not found", justify="right", style="yellow")
        # Shown separately from "Not found": an unparseable page means we could
        # not read it, not that the paper is unavailable. Merging the two would
        # under-report totals and hide false negatives.
        table.add_column("Unresolved", justify="right", style="magenta")
        table.add_column("Errors", justify="right", style="red")
        table.add_column("Downloaded", justify="right")
        table.add_row(
            str(stats["total"]),
            str(stats["ok"]),
            str(stats["not_found"]),
            str(stats["parse_errors"]),
            str(stats["errors"]),
            _human_size(stats["downloaded_bytes"]),
        )
        console.print(table)

        if stats["parse_errors"]:
            console.print(
                f"[magenta]Note:[/magenta] {stats['parse_errors']} DOI(s) could not be "
                "resolved to a PDF link. This is not proof they are unavailable - "
                "retry later or try another --mirror."
            )

    if fail_fast and (
        stats["errors"] or stats["not_found"] or stats["parse_errors"]
    ):
        sys.exit(1)
    if stats["ok"] == 0 and stats["total"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
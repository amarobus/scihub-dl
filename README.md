# scihub-dl

A small Python library **and** CLI to resolve and download papers from Sci-Hub by DOI, with first-class **batch** support.

Sci-Hub has no official API. This package scrapes the public site and transparently solves its
[Altcha](https://altcha.org) proof-of-work gate (an open-source computational challenge — no image
recognition or human interaction involved).

## Install

```bash
pip install -e ".[dev]"     # from this directory
```

Requires Python ≥ 3.9. Dependencies: `requests`, `beautifulsoup4`, `click`, `rich`.

## CLI

### Single paper

```bash
# download to a directory (filename derived from author/year/title)
scihub-dl fetch 10.1038/nature12373 -o papers/

# download to an explicit path
scihub-dl fetch 10.1038/nature12373 -o papers/thermometry.pdf
```

### Metadata only, no download

```bash
scihub-dl info 10.1038/nature12373
scihub-dl info 10.1038/nature12373 --json
```

```
 DOI       10.1038/nature12373
 Title     Nanometre-scale thermometry in a living cell
 Authors   Kucsko, G., Maurer, P. C., Yao, N. Y., Kubo, M., et., al.
 Year      2013
 Journal   Nature
 PDF URL   https://sci-hub.ru/storage/2024/2161/f1fa.../kucsko2013.pdf
 Filename  Kucsko_2013_Nanometre-scale_thermometry_in_a_living_cell.pdf
```

### Batch

```bash
# DOIs as arguments
scihub-dl batch 10.1038/nature12373 10.1126/science.1157996 -o papers/

# from a file (one DOI per line; '#' comments and blank lines ignored; 1-column CSV works)
scihub-dl batch -f dois.txt -o papers/ --workers 4

# from stdin
cat dois.txt | scihub-dl batch -f - -o papers/

# resolve URLs only, don't download
scihub-dl batch -f dois.txt --metadata-only --json

# machine-readable results
scihub-dl batch -f dois.txt -o papers/ --manifest run.json
scihub-dl batch -f dois.txt -o papers/ --json
```

Batch output:

```
ok        10.1038/nature12373         Nanometre-scale thermometry in a living cell
not_found 10.9999/definitely.not.real Sci-Hub has no PDF for DOI 10.9999/...
ok        10.1126/science.1157996     Measurement of the Elastic Properties and Intrinsic...
ok        10.1016/j.cell.2011.02.013  Hallmarks of Cancer: The Next Generation
  Fetching papers ━━━━━━━━━━━━━━━━━━━━━━━━━━ 4/4 0:00:05

                 Batch summary
┏━━━━━━━┳━━━━┳━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┓
┃ Total ┃ OK ┃ Not found ┃ Errors ┃ Downloaded ┃
┡━━━━━━━╇━━━━╇━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━┩
│     4 │  3 │         1 │      0 │     3.4 MB │
└───────┴────┴───────────┴────────┴────────────┘
```

DOIs are accepted in any common form and de-duplicated automatically:
`10.1038/nature12373`, `https://doi.org/10.1038/nature12373`, `doi:10.1038/nature12373`.

### Useful flags

| Flag | Applies to | Description |
|---|---|---|
| `-o, --output` | fetch, batch | Output directory or file path |
| `--workers N` | batch | Concurrent downloads (default 3) |
| `--delay S` | batch | Minimum gap between requests (default 0.5s) |
| `--metadata-only` | batch | Resolve metadata/URLs without downloading |
| `--manifest PATH` | batch | Write a JSON manifest of the run |
| `--json` | info, batch | Machine-readable output on stdout |
| `--overwrite` | fetch, batch | Re-download existing files |
| `--mirror URL` | all | Mirror to try (repeatable, in order) |
| `--timeout`, `--retries` | all | Network tuning |
| `--insecure` | all | Skip TLS verification (some mirrors use self-signed certs) |
| `--fail-fast` | batch | Exit non-zero if anything failed |
| `-v, --verbose` | all | Debug logging to stderr |

Exit codes: `0` success · `1` all lookups failed / single fetch failed · `2` no valid DOIs supplied.

## Library

```python
from scihub_dl import ScihubClient, BatchDownloader, summarize

# single paper
with ScihubClient() as client:
    paper = client.resolve("10.1038/nature12373")
    print(paper.title, paper.year, paper.pdf_url)
    path = client.download(paper, "papers/")       # -> pathlib.Path

    # or in one step
    path = client.fetch("10.1126/science.1157996", "papers/")

# batch
with BatchDownloader(workers=4, delay=0.5) as batch:
    results = batch.run(
        ["10.1038/nature12373", "10.1126/science.1157996"],
        dest="papers/",
        on_result=lambda r: print(r.status, r.doi),   # optional progress callback
    )

for r in results:
    if r.ok:
        print(r.doi, "->", r.path, r.size)
    else:
        print(r.doi, "failed:", r.error)

print(summarize(results))
# {'total': 2, 'ok': 2, 'not_found': 0, 'errors': 0, 'downloaded_bytes': 1344704}
```

### API surface

- `ScihubClient(mirrors=None, timeout=30, retries=2, verify_ssl=True, ...)`
  - `.resolve(doi) -> Paper` — metadata + direct PDF URL (solves the captcha as needed)
  - `.download(paper_or_doi, dest, overwrite=False) -> Path`
  - `.fetch(doi, dest) -> Path` — resolve + download
  - `.active_mirror` — which mirror answered
- `BatchDownloader(client=None, workers=3, delay=0.5, overwrite=False, metadata_only=False)`
  - `.run(dois, dest=".", on_result=None) -> list[BatchResult]` (results keep input order)
- `Paper(doi, title, authors, year, journal, pdf_url)` — `.to_dict()`, `.suggested_filename()`
- `BatchResult(doi, status, path, size, title, pdf_url, error)` — `.ok`, `.to_dict()`
  - `status` ∈ `ok` | `not_found` | `error` | `skipped`
- Helpers: `normalize_doi`, `is_valid_doi`, `read_dois`, `read_dois_file`, `summarize`, `write_manifest`
- Exceptions: `ScihubError` (base), `MirrorUnreachableError`, `ChallengeError`, `NotFoundError`,
  `InvalidDOIError`, `DownloadError`

## Design notes

- **Mirror fallback** — tries each mirror in order (`sci-hub.ru`, `.st`, `.se` by default) and caches
  the first that responds with real content. Placeholder/parked pages are rejected.
- **Captcha solved once per session** — batch runs warm up the session before dispatching workers,
  so the proof-of-work is normally solved a single time for the whole batch.
- **Atomic downloads** — PDFs stream to a `.part` file, are validated to start with `%PDF`, and only
  then get moved into place. Failures never leave partial files behind.
- **Batch is fault-isolated** — one bad DOI never aborts the run; every DOI yields a `BatchResult`.
- **Polite by default** — bounded concurrency plus a cross-thread minimum request interval.
- **Retries** with exponential backoff on transport errors and 5xx/444 responses.

## Tests

```bash
python -m pytest -q          # 97 tests, all network mocked via `responses`
```

Covers DOI normalisation, filename sanitising, DOI-file parsing, the Altcha solver (all three hash
algorithms, malformed/unsolvable challenges), captcha retry flow, mirror fallback, not-found
detection, atomic/overwrite/non-PDF download paths, batch ordering and fault isolation, manifest
writing, and every CLI command including exit codes.

## Verified live

Confirmed working against the real site (`sci-hub.ru`) at time of writing:

| DOI | Result |
|---|---|
| `10.1038/nature12373` | 921.7 KB PDF — *Nanometre-scale thermometry in a living cell* |
| `10.1126/science.1157996` | 391.5 KB PDF — *Measurement of the Elastic Properties... of Monolayer Graphene* |
| `10.1016/j.cell.2011.02.013` | 2.1 MB PDF — *Hallmarks of Cancer: The Next Generation* |
| `10.9999/definitely.not.real.99999` | correctly reported `not_found` |

## Caveats

- **Not an official API.** Sci-Hub can change its markup, challenge endpoints, or add rate limiting
  at any time; this package may need updating when it does.
- **Mirror availability varies** by network and over time. Some networks block Sci-Hub domains
  entirely (`sci-hub.se` did not resolve during testing; `.st` presented a self-signed cert). Use
  `--mirror` / `--insecure` as needed.
- **Coverage gap.** Sci-Hub has added few or no new papers since ~2021, so recent work is often
  missing — `not_found` is frequently correct, not a bug.
- **Legality.** Sci-Hub distributes copyrighted material outside publisher licensing in most
  jurisdictions. Downloading may be unlawful and/or violate your institution's policies. You are
  responsible for your own use; prefer legitimate access (institutional subscriptions, open-access
  copies, interlibrary loan, or emailing authors) where available.

## License

MIT
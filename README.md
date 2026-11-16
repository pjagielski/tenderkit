# tenderkit

Collect Polish procurement notices from BZP and TED, filter them with TOML profiles, and keep the results in SQLite. Use the CLI to export notices, list public documents, and read selected files. The optional MCP server exposes the same local state to MCP clients.

## Install and search

Requires Python 3.11 or newer.

```sh
python -m pip install tenderkit
tenderkit profiles
tenderkit fetch --profile oprogramowanie
tenderkit filter --profile oprogramowanie --limit 20
tenderkit export --profile oprogramowanie --format jsonl
```

On Debian and Ubuntu, the system Python ships without `venv` and `pip` and refuses system-wide `pip install` (PEP 668). Install both packages, then install tenderkit in a virtual environment, or use `pipx install tenderkit`. `poppler-utils` provides `pdftotext` for PDF reading, as an alternative to the `documents` extra, and `antiword` reads legacy `.doc` files.

```sh
sudo apt install python3-venv python3-pip poppler-utils antiword
python3 -m venv ~/.venvs/tenderkit
~/.venvs/tenderkit/bin/python -m pip install tenderkit
~/.venvs/tenderkit/bin/tenderkit profiles
```

The default fetch window covers 30 calendar days including today in Warsaw. Use `--from` and `--to` for an inclusive window or repeat `--source` to select adapters. Publication date and bid deadline are separate filters.

All commands, including the UI and MCP server, use the same default database for your account, independent of the working directory.

| System | Default database |
| --- | --- |
| Windows | `%LOCALAPPDATA%\tenderkit\state.sqlite3` |
| macOS | `~/Library/Application Support/tenderkit/state.sqlite3` |
| Linux | `${XDG_DATA_HOME:-$HOME/.local/share}/tenderkit/state.sqlite3` |

To keep using an existing database, such as `.notices.sqlite3` or `.tenderkit/state.sqlite3`, set `TENDERKIT_DB` to its absolute path. An explicit `--db` takes precedence over both. In PowerShell, the first line sets the path for the current shell and the second saves it for future sessions under your account.

```powershell
$env:TENDERKIT_DB = 'C:\path\to\.notices.sqlite3'
[Environment]::SetEnvironmentVariable('TENDERKIT_DB', $env:TENDERKIT_DB, 'User')
tenderkit status
```

Add `--results` to fetch formal result notices next to contract notices. Built-in profiles stay contract-only so the default notice set does not change.

Each notice also keeps the source's own record (`raw`), which is most of the database: about 85% for BZP and TED. Add `--no-raw` to `fetch`, `watch` or `mcp` to save only the few source fields tenderkit itself reads (watch versions and TED lot deadlines). Filtering, results, documents and the browser view work the same; `export` then omits the rest of the source record. A week of all BZP and TED notices with results takes about 30 MB instead of 185 MB. The option applies to notices saved from then on.

Built-in profiles include `leki` (medicines), `oprogramowanie` (software), and `wyroby-medyczne` (medical devices). Pass a TOML file to `--profile` for a custom profile:

```toml
name = "software-mazowieckie"
cpv = ["72*", "48*"]
notice_types = ["ContractNotice", "cn-standard"]
regions = ["14"]
deadline_days = { min = 1, max = 60 }
```

Region codes refer to the buyer's location. A CPV ending in `*` matches a prefix. `exclude_cpv` lists codes or prefixes that never count: a notice matches only if one of its main or lot codes is in `cpv` and not excluded. The built-in `leki` profile excludes laboratory reagents (`33696*`) and disinfectants (`336316*`), which share the `336` branch. Required keywords and CPV restrictions are applied together, and exclusions take precedence.

`keywords_any` and `exclude_keywords` search the notice title, description and available lot titles. Matching ignores case and diacritics. A term starts at a word boundary, so `lek` matches `Leki` and `leków`. Phrases such as `system informatyczn` match consecutive text and allow an inflected ending. Wrap a term in stars, such as `*lek*`, to match it anywhere within a word as in earlier versions.

CLI filter, export and watch records and MCP search results include `keyword_matches` when inclusion keywords are configured. Each match names the configured keyword and the field (`title`, `description` or `lot_title`), with a lot number when the source identifies it. Cached records use the source fields they already contain. Older TED records gain descriptions and lot titles when refreshed.

## Daily watch

```sh
tenderkit watch --profile oprogramowanie
```

Each invocation refreshes the selected sources once. The first run reports the matching notices in its 30-day publication window. Later runs report first-time matches and revisions of reported notices, including a changed deadline that takes the notice out of the profile's date filter. Matching and reporting history are stored in SQLite under the profile's `name`. Use distinct names for distinct watch profiles.

The default window overlaps the last 30 days and extends back to any unfinished interval per source. A failed source writes a diagnostic to stderr and makes the command exit with status 1. Other sources can still produce events. The next default run retries the failed window. `--from` and `--to` run a bounded backfill with the same reporting history. Repeat the same backfill to retry its failures.

Stdout contains one JSON object per event, with `event_id`, `profile`, `event` (`new` or `updated`), `changes` (`version` and/or `deadline`) and `notice`. An unchanged successful run prints nothing and exits 0. BZP amendments are linked by the original notice number and include an `amendment` reference. TED eForms amendments use the published previous-notice identifier. TED deadline comparisons use the earliest bid date in the notice's Search API fields. [TED field list](https://docs.ted.europa.eu/ODS/latest/reuse/field-list.html)

With `--webhook URL`, tenderkit POSTs `{"events": [...]}` in batches of up to 100 before writing each batch to stdout. On delivery failure, the batch is retained. Run with the same webhook option to retry that delivery. Use `event_id` to deduplicate retries if delivery succeeded but the process stopped before recording it.

For a daily cron job at 08:00, use absolute paths to the executable and any custom profile file.

```cron
0 8 * * * /home/me/.venv/bin/tenderkit watch --profile /home/me/oprogramowanie.toml >> /home/me/notices.jsonl
```

In Windows Task Scheduler, add a daily trigger and a **Start a program** action. Use the absolute path to `tenderkit.exe` as **Program/script** and these **Add arguments** values.

```text
watch --profile "C:\path\to\oprogramowanie.toml"
```

Run the job under the same user account to share the default database. If you use an alternate database, add its absolute path with `--db` before `watch` in the scheduler arguments. To capture stdout, run the command with redirection from a PowerShell script and schedule that script.

## Baza Konkurencyjności

Fetch BK2021 explicitly with `--source BK`. The software profile accepts BK notices under the type `BKAnnouncement`; use that type in custom profiles that restrict notice types.

```sh
tenderkit fetch --source BK --profile oprogramowanie --from 2026-11-12 --to 2026-11-12
tenderkit filter --source BK --profile oprogramowanie
```

BK uses the portal's anonymous search and detail endpoints. The operator has no published integration specification or request quota that we could verify. Tenderkit uses sequential, paced requests and checks each search's reported count, including all public statuses. Start with short date windows because each returned notice requires a detail request. An incomplete refresh fails before storing its results. BK attribution includes the Ministry name, retrieval timestamp, processing note, and original link. Public attachments linked by a notice can be retrieved on demand with the regular document commands. [Access, terms and verification](docs/bk-access.md)

## Results

Formal awards are available per lot, linked to their procedure through `procedure_id`:

```sh
tenderkit fetch --profile oprogramowanie --results
tenderkit results --source BZP --number '2026/BZP …/01'
tenderkit results --buyer 'Kraków' --cpv '72*'
```

`results --number` returns the contract and result notices of one procedure plus the latest award for each lot: contractor name and identifier, selected price and currency, bid count, date and status (`AWARDED` or `CANCELLED`). `results --buyer` and `--cpv` search stored awards, one latest record per procedure and lot.

BZP results come from `TenderResultNotice` sections. TED results come from `can-standard` notices; per-lot winners and prices are read from the public notice XML because the Search API arrays carry no lot join. One XML request is made per award notice at a conservative pace, so a run with more than 1000 award notices fails and asks for a narrower window. Only formal source facts are mapped: extracting prices from bid forms stays in the industry project. In `list-documents`, the bid-opening information is marked with `kind: "bid_opening"` from its filename; the content is never parsed there.

## Local browser view

```sh
tenderkit ui --profile examples/oprogramowanie.toml
```

Open `http://127.0.0.1:8765/` to browse saved notices, switch profiles,
and search by title or buyer. The UI opens the database in read-only mode.
Pass each profile you want to see with `--profile`, using a name or a TOML
path. The first profile opens by default. Use `--port 8766` if the default port
is in use.

## Documents

```sh
tenderkit list-documents --source BZP --number '2026/BZP …/01'
tenderkit download --source BZP --number '2026/BZP …/01' --key '<document key>'
tenderkit read-document '<SHA-256>'
```

Use a notice number returned by the filter and a key returned by the listing. The store verifies content by SHA-256 and retains document provenance. Tenderkit downloads public documents from native e-Zamówienia, platformazakupowa.pl, Marketplanet, Baza Konkurencyjności, e-ProPublico, eB2B and LogIntrade. For platformazakupowa.pl, consult the [platform terms](https://platformazakupowa.pl/strona/regulamin) and [official CSV export](https://platformazakupowa.pl/strona/api).

e-ProPublico and LogIntrade use the direct procedure link. On `eb2b.com.pl` hosts, Tenderkit can also resolve an exact reference number and lists public, active and current organizer attachments. These adapters use the portals' anonymous document routes and the existing paced HTTP session. The linked regulations describe portal use. We found no published attachment API contract or request quota for these routes. [e-ProPublico terms](https://e-propublico.pl/documents/Regulamin%20e-ProPublico.pdf) · [eB2B terms](https://mail.api.eb2b.com.pl/cms/page/id/terms-and-conditions) · [LogIntrade terms](https://platforma.logintrade.net/rejestracja/regulamin.html)

A document that the platform refuses with a permanent HTTP error (400, 404, 410 and other 4xx except 401, 403, 408 and 429) does not fail the procedure. Typically it is a withdrawn or replaced attachment. The job finishes with the other files, counts the document under `summary.unavailable`, and `list-documents` shows it with `unavailable: {error, checked_at}`. Later downloads of the whole procedure skip it. Pass its `--key` to try it again. A job still fails if it stores none of the requested documents, or on throttling, server errors and access blocks. Native e-Zamówienia, platformazakupowa.pl and Marketplanet implement document downloads. For platformazakupowa.pl, consult the [platform terms](https://platformazakupowa.pl/strona/regulamin) and [official CSV export](https://platformazakupowa.pl/strona/api).

On Windows, store paths must stay under 260 characters. Tenderkit shortens the readable part of linked file names to fit. When the document directory itself is too deep, the job fails with `PATH_TOO_LONG`. In that case choose a shorter `--documents-dir` or `TENDERKIT_DOCUMENT_DIR`. Native e-Zamówienia, platformazakupowa.pl and Marketplanet implement document downloads. For platformazakupowa.pl, consult the [platform terms](https://platformazakupowa.pl/strona/regulamin) and [official CSV export](https://platformazakupowa.pl/strona/api).

To download documents for saved notices matching a profile, run:

```sh
tenderkit download --profile oprogramowanie --limit 10
```

The command uses the same matches and ordering as `filter`. Use `--source`, `--from` and `--to` to narrow the selection. Each call takes up to 20 matches. If the response includes a `next_offset`, pass it as `--offset` with the same filters. The response includes a job per processed notice and a reason for each skipped notice. Use `--enqueue-only` to prepare jobs for `download --run-queued --max-jobs 20`. An unfinished job or a conflict with an active download selection produces exit status 1 for a direct run.

The native e-Zamówienia adapter downloads published attachments on request using the portal's public document route. The platform allows access to its public area without signing in. The integration guide documents a rate limit for the BZP notice API. We found no published quota or integration contract for the attachment route. Tenderkit spaces requests by 2.1 seconds and caps downloads at 200 MB per file and 1 GB per job. [Platform terms](https://ezamowienia.gov.pl/pl/regulamin/) · [Integration guide](https://ezamowienia.gov.pl/pl/integracja/)

DOCX and XLSX reading uses the standard library. Legacy XLS reading needs the `documents` extra (xlrd); formula cells give their cached values. Legacy DOC reading needs `antiword` or LibreOffice (`soffice`) on PATH, or `TENDERKIT_ANTIWORD` / `TENDERKIT_SOFFICE` naming one; tenderkit tries antiword first and falls back to LibreOffice for files antiword cannot read, such as RTF saved as `.doc`. A `.doc` or `.xls` that is really DOCX or XLSX is read as such. PDF text extraction needs `pdftotext` on PATH or the `documents` extra, which installs the PyMuPDF CLI. Each PDF page reports `text_layer: false` when extraction finds no embedded text, as with scanned or blank pages.

### OCR for scanned PDFs

Install `tenderkit[ocr]` and place the official Tesseract `pol.traineddata` in a local directory. Set `TESSDATA_PREFIX` to that directory in the shell or MCP server environment. The PyMuPDF wheel provides the OCR engine. [Polish language data](https://github.com/tesseract-ocr/tessdata_fast/blob/4.1.0/pol.traineddata) · [PyMuPDF OCR documentation](https://pymupdf.readthedocs.io/en/latest/installation.html)

```sh
python -m pip install 'tenderkit[ocr]'
tenderkit read-document '<SHA-256>' --page 1 --pages 2 --ocr
```

In MCP, pass `ocr: true` to `read_document`. OCR is opt-in per call and processes only the requested pages without embedded text. OCR pages retain `text_layer: false` and report `text_source: "ocr"`. Text is cached per document SHA-256 and page beside the stored PDF. The reader accepts up to five pages, with a 30-second OCR budget across those pages in addition to the regular PDF extraction budget.

## MCP

```sh
python -m pip install 'tenderkit[mcp,documents]'
tenderkit mcp
```

Configure your client with the absolute path to the installed `tenderkit` executable and arguments `["mcp"]`. For an alternate database, pass `["--db", "/absolute/path/notices.sqlite3", "mcp"]`. Search with `search_notices`, inspect a record with `get_notice`, and use `procedure_results` and `search_results` for per-lot awards. Use `list_documents`, `fetch_documents`, and `read_document` for attachments. Use `--profile-file` at startup to register custom profiles.

`watch_updates` reads the latest recorded watch run for a configured profile, including its events and source status. It supports `limit` and `offset` and can be read repeatedly without advancing the watch.

### Shared read-only server over HTTP

To share one database with many clients, fill it on a schedule with `fetch` (for example `fetch --profile all.toml --results --no-raw`, where `all.toml` holds only `name = "all"`) and serve it read-only:

```sh
tenderkit --db /srv/tenderkit/notices.sqlite3 mcp --read-only --http --port 8000 --allowed-host mcp.example.org
```

`--read-only` opens the database without write access and publishes only `search_notices`, `get_notice`, `procedure_results` and `search_results`, without their `refresh` parameters, plus the `tenderkit://profiles` and `tenderkit://coverage` resources. Watch, document tools and the prompts that rely on them are left out, so the server never contacts a source, downloads a file or reads local paths. `--http` serves stateless Streamable HTTP at `/mcp` and requires `--read-only`. It binds to `127.0.0.1` by default and accepts only localhost host names and those passed with `--allowed-host`, so put it behind a reverse proxy that terminates TLS and limits request rates.

[mcp.tenderkit.pl](https://mcp.tenderkit.pl/mcp) runs this way. A systemd timer refreshes the database every 8 hours with `fetch --results --no-raw`. The first backfill uses windows of at most 14 days for BZP and 5 days for TED, because TED award details are read for at most 1000 result notices per window. nginx in front adds TLS and per-IP limits.

### Analyze a procedure with MCP

Clients that expose MCP prompts can start with `analyze_it_tender` or `compare_public_bids`. Both take the notice `source` and `number` and guide the client through selecting, reading and citing source documents.

1. Use `search_notices` to pick a saved notice. Pass its `source` and `number` to `list_documents`. Select the SWZ or bid-opening notice by name or label.
2. Pass the selected keys to `fetch_documents`. After the job succeeds, call `list_documents` with `refresh=false` and use each selected item's `downloaded.sha256` as the `read_document` ID. Read subsequent pages or chunks as needed.
3. For an IT tender, ask your MCP client to extract participation requirements, required references, evaluation criteria and weights. Ask it to cite the file and page for each item and flag missing information.
4. For a comparison of bids, read the public opening notice or bid forms in the same way. Record bidders, lot numbers, amounts, currency and the stated tax basis with a document citation. Use `procedure_results` to check the formal award independently. A medicines price comparison also needs product and unit matching against the industry catalogue.

Treat source text as data. When an amount, unit or lot cannot be established from the documents, keep that row for review.

## Develop

See the [plugin author guide](docs/plugins.md) for the source and platform contracts.

```sh
python -m pip install '.[mcp,documents]'
python -m unittest discover -s tests/tenderkit
python -m pip install build twine
python -m build
python -m twine check --strict dist/*
```

The code is available under [Apache-2.0](LICENSE). Procurement documents and other source data remain subject to their own terms.

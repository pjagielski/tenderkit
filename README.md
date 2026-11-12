# tenderkit

Collect Polish procurement notices from BZP and TED, filter them with TOML profiles, and keep the results in SQLite. Use the CLI to export notices, list public documents, and read selected files. The optional MCP server exposes the same local state to MCP clients.

## Install and search

Requires Python 3.11 or newer and IANA timezone data. On Windows the package installs `tzdata`; on Unix install the system timezone database if it is missing.

```sh
python -m pip install tenderkit
tenderkit profiles
tenderkit fetch --profile oprogramowanie
tenderkit filter --profile oprogramowanie --limit 20
tenderkit export --profile oprogramowanie --format jsonl
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

Built-in profiles include `leki` (medicines), `oprogramowanie` (software), and `wyroby-medyczne` (medical devices). Pass a TOML file to `--profile` for a custom profile:

```toml
name = "software-mazowieckie"
cpv = ["72*", "48*"]
notice_types = ["ContractNotice", "cn-standard"]
regions = ["14"]
deadline_days = { min = 1, max = 60 }
```

Region codes refer to the buyer's location. A CPV ending in `*` matches a prefix. Required keywords and CPV restrictions are applied together, and exclusions take precedence.

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

Use a notice number returned by the filter and a key returned by the listing. The store verifies content by SHA-256 and retains document provenance. Native e-Zamówienia, platformazakupowa.pl and Marketplanet implement document downloads. For platformazakupowa.pl, consult the [platform terms](https://platformazakupowa.pl/strona/regulamin) and [official CSV export](https://platformazakupowa.pl/strona/api).

The native e-Zamówienia adapter downloads published attachments on request using the portal's public document route. The platform allows access to its public area without signing in. The integration guide documents a rate limit for the BZP notice API. We found no published quota or integration contract for the attachment route. Tenderkit spaces requests by 2.1 seconds and caps downloads at 200 MB per file and 1 GB per job. [Platform terms](https://ezamowienia.gov.pl/pl/regulamin/) · [Integration guide](https://ezamowienia.gov.pl/pl/integracja/)

DOCX and XLSX reading uses the standard library. PDF text extraction needs `pdftotext` on PATH or the `documents` extra, which installs the PyMuPDF CLI. Scanned PDFs may have no text layer.

## MCP

```sh
python -m pip install 'tenderkit[mcp,documents]'
tenderkit mcp
```

Configure your client with the absolute path to the installed `tenderkit` executable and arguments `["mcp"]`. For an alternate database, pass `["--db", "/absolute/path/notices.sqlite3", "mcp"]`. Search with `search_notices`, inspect a record with `get_notice`, and use `procedure_results` and `search_results` for per-lot awards. Use `list_documents`, `fetch_documents`, and `read_document` for attachments. Use `--profile-file` at startup to register custom profiles.

`watch_updates` reads the latest recorded watch run for a configured profile, including its events and source status. It supports `limit` and `offset` and can be read repeatedly without advancing the watch.

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

# tenderkit

Collect Polish procurement notices from BZP and TED, filter them with TOML profiles, and keep the results in SQLite. Use the CLI to export notices, list public documents, and read selected files. The optional MCP server exposes the same local state to MCP clients.

## Install and search

Requires Python 3.11 or newer and IANA timezone data. On Windows the package installs `tzdata`; on Unix install the system timezone database if it is missing.

```sh
python -m pip install tenderkit
tenderkit profiles
tenderkit --db notices.sqlite3 fetch --profile oprogramowanie
tenderkit --db notices.sqlite3 filter --profile oprogramowanie --limit 20
tenderkit --db notices.sqlite3 export --profile oprogramowanie --format jsonl
```

The default fetch window covers 30 calendar days including today in Warsaw. Use `--from` and `--to` for an inclusive window or repeat `--source` to select adapters. Publication date and bid deadline are separate filters.

Built-in profiles include `leki` (medicines), `oprogramowanie` (software), and `wyroby-medyczne` (medical devices). Pass a TOML file to `--profile` for a custom profile:

```toml
name = "software-mazowieckie"
cpv = ["72*", "48*"]
notice_types = ["ContractNotice", "cn-standard"]
regions = ["14"]
deadline_days = { min = 1, max = 60 }
```

Region codes refer to the buyer's location. A CPV ending in `*` matches a prefix. Required keywords and CPV restrictions are applied together, and exclusions take precedence.

## Local browser view

```sh
tenderkit --db .notices.sqlite3 ui --profile examples/oprogramowanie.toml
```

Open `http://127.0.0.1:8765/` to browse saved notices, switch profiles,
and search by title or buyer. The UI opens the database in read-only mode.
Pass each profile you want to see with `--profile`, using a name or a TOML
path. The first profile opens by default. Use `--port 8766` if the default port
is in use. The UI defaults to `.notices.sqlite3` in the current directory.

## Documents

```sh
tenderkit --db notices.sqlite3 list-documents --source BZP --number '2026/BZP …/01'
tenderkit --db notices.sqlite3 download --source BZP --number '2026/BZP …/01' --key '<document key>'
tenderkit --db notices.sqlite3 read-document '<SHA-256>'
```

Use a notice number returned by the filter and a key returned by the listing. The store verifies content by SHA-256 and retains document provenance. The native e-Zamówienia adapter lists attachments. The platformazakupowa.pl and Marketplanet adapters also implement downloads. Check each source's terms and your authorization before automating access. For platformazakupowa.pl, consult the [platform terms](https://platformazakupowa.pl/strona/regulamin) and [official CSV export](https://platformazakupowa.pl/strona/api).

DOCX and XLSX reading uses the standard library. PDF text extraction needs `pdftotext` on PATH or the `documents` extra, which installs the PyMuPDF CLI. Scanned PDFs may have no text layer.

## MCP

```sh
python -m pip install 'tenderkit[mcp,documents]'
tenderkit --db notices.sqlite3 mcp
```

Configure your client with the absolute path to the installed `tenderkit` executable, and arguments `["--db", "/absolute/path/notices.sqlite3", "mcp"]`. Search with `search_notices`, inspect a record with `get_notice`, and use `list_documents`, `fetch_documents`, and `read_document` for attachments. Use `--profile-file` at startup to register custom profiles.

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

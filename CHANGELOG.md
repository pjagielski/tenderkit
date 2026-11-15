# Changelog

## Unreleased

- Save notices without the full source record with `--no-raw` on `fetch`, `watch` and `mcp`, or `State(path, keep_raw=False)`. Only the source fields tenderkit reads are kept, which makes the database about six times smaller.

## 0.1.0

- Fetch Polish procurement notices from BZP and TED, store them in SQLite and filter them with TOML profiles.
- Fetch notices from Baza Konkurencyjności (BK2021) with `--source BK`. The adapter validates search totals and supports on-demand downloads of public attachments.
- Report new and revised profile matches with `tenderkit watch`. Watch history is stored in SQLite. Events are available as JSON Lines, through an optional webhook, and through the MCP `watch_updates` tool.
- Use one per-user default database for CLI, browser UI and MCP on Windows, macOS and Linux. Override it with `--db` or `TENDERKIT_DB`.
- Exclude CPV codes or prefixes with `exclude_cpv` in a profile or in MCP `search_notices`. The built-in `leki` profile leaves out laboratory reagents (`33696*`) and disinfectants (`336316*`).
- Match keyword prefixes and phrases in titles, descriptions and available lot titles. Use `*term*` for substring matching. Results can identify the matched keyword and field.
- Browse saved notices with `tenderkit ui`. Choose profiles at startup, search by title or buyer, filter by source, CPV and dates, and sort the results.
- Export matching notices as JSON Lines. The CLI help lists commands and examples.
- Fetch formal result notices with `fetch --results` and read them with `results` or the MCP `procedure_results` and `search_results` tools. Results include the contractor, selected price, bid count and status per lot and link to the contract notice through `procedure_id`.
- List and download public procurement documents on supported platforms: e-Zamówienia, platformazakupowa.pl, Marketplanet (OnePlace), e-ProPublico, eB2B and LogIntrade. Download documents for every notice that matches a profile with `download --profile`. Document listings mark bid-opening information with `kind: "bid_opening"` based on the filename.
- A document the platform refuses for good, such as a withdrawn attachment that answers HTTP 400, no longer fails the download. It is listed as `unavailable` and skipped by later whole-procedure downloads.
- On Windows, document file names are shortened to keep store paths under 260 characters. A store directory that is too deep fails with `PATH_TOO_LONG` instead of an unexplained `FileNotFoundError`.
- Read PDF, DOCX and XLSX text with document provenance. Read legacy XLS with the `documents` extra (xlrd) and legacy DOC with `antiword` or LibreOffice. Detect PDF pages without embedded text and opt in to local Polish OCR with `--ocr` or MCP `ocr: true`. OCR text is cached per document and page.
- Expose search, notice details and document tools through an optional MCP server over stdio, with the `analyze_it_tender` and `compare_public_bids` prompts.
- Test on Linux, Windows and macOS in CI. The README documents the Debian/Ubuntu install with a virtual environment or `pipx`.
- Register notice sources and document platforms through Python entry points.

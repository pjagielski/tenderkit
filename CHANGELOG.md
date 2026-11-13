# Changelog

## 0.1.0

- Fetch Polish procurement notices from BZP and TED, store them in SQLite and filter them with TOML profiles.
- Fetch notices from Baza Konkurencyjności (BK2021) with `--source BK`. The adapter validates search totals and supports on-demand downloads of public attachments.
- Report new and revised profile matches with `tenderkit watch`. Watch history is stored in SQLite. Events are available as JSON Lines, through an optional webhook, and through the MCP `watch_updates` tool.
- Use one per-user default database for CLI, browser UI and MCP on Windows, macOS and Linux. Override it with `--db` or `TENDERKIT_DB`.
- Match keyword prefixes and phrases in titles, descriptions and available lot titles. Use `*term*` for substring matching. Results can identify the matched keyword and field.
- Browse saved notices with `tenderkit ui`. Choose profiles at startup, search by title or buyer, filter by source, CPV and dates, and sort the results.
- Export matching notices as JSON Lines. The CLI help lists commands and examples.
- Fetch formal result notices with `fetch --results` and read them with `results` or the MCP `procedure_results` and `search_results` tools. Results include the contractor, selected price, bid count and status per lot and link to the contract notice through `procedure_id`.
- List and download public procurement documents on supported platforms, including e-Zamówienia. Document listings mark bid-opening information with `kind: "bid_opening"` based on the filename.
- Read PDF, DOCX and XLSX text with document provenance. Detect PDF pages without embedded text and opt in to local Polish OCR with `--ocr` or MCP `ocr: true`. OCR text is cached per document and page.
- Expose search, notice details and document tools through an optional MCP server over stdio.
- Register notice sources and document platforms through Python entry points.

# Changelog

## 0.1.0

- Fetch Polish procurement notices from BZP and TED, store them in SQLite and filter them with TOML profiles.
- Browse saved notices with `tenderkit ui`. Choose profiles at startup, search by title or buyer, filter by source, CPV and dates, and sort the results.
- Export matching notices as JSON Lines. The CLI help lists commands and examples.
- List public procurement documents and download them on supported platforms. Read PDF, DOCX and XLSX text with document provenance.
- Expose search, notice details and document tools through an optional MCP server over stdio.
- Register notice sources and document platforms through Python entry points.

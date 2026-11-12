# Keyword matching comparison for issue #6

The baseline is commit `38878bc`. Both versions read the same three acceptance SQLite snapshots with the date fixed to 2026-11-12. Each comparison uses `(source, number)` as the notice identity. The database files had identical SHA-256 digests before and after.

| Built-in profile | Publication window | Cached notices | Before | After | Added | Removed |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `leki` | 2026-10-10 to 2026-11-08 | 743 | 737 | 737 | 0 | 0 |
| `oprogramowanie` | 2026-10-10 to 2026-11-08 | 1,749 | 1,186 | 1,186 | 0 | 0 |
| `wyroby-medyczne` | 2026-10-11 to 2026-11-09 | 3,577 | 1,779 | 1,779 | 0 | 0 |

The built-in profiles use CPV and notice type filters. Their identical identifier sets preserve the differences recorded in the F1 replay. For `leki`, the legacy pharma feed produced 743 identities and the Tenderkit profile selected 737. The six remaining identities carry CPV codes that contain `336` elsewhere in the code. The historical replay compared the legacy feed on saved responses. Production database parity was outside this run.

## Comparison with keywords

The same example terms were applied to each profile in both versions. The other profile filters stayed in effect.

| Profile | Included terms | Excluded terms | Before, title | After, all fields | Added | Removed |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `leki` | `lek`, `farmaceut` | `weterynar` | 238 | 224 | 15 | 29 |
| `oprogramowanie` | `oprogramowan`, `system informatyczn`, `licencj` | | 363 | 484 | 121 | 0 |
| `wyroby-medyczne` | `wyrób medyczn`, `wyroby medyczn`, `sprzęt medyczn`, `rękawic` | | 58 | 106 | 48 | 0 |

All additions in these snapshots matched BZP descriptions. All 29 removals lost a match that started inside a word. Examples include `lek` in `elektroforezy` and `farmaceut` in `radiofarmaceutyków`. The term `radiofarmaceut` and the substring pattern `*farmaceut*` both match `radiofarmaceutyków`. [Electrophoresis notice](https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/08df2aa8-d0eb-03d9-5997-b20001512b41) · [Radiopharmaceutical notice](https://ted.europa.eu/pl/notice/-/detail/701409-2026)

The description recovered a chemotherapy procurement whose title used `produktów leczniczych`. Another microbiology notice matched through `lekooporności` and `lekowrażliwość`. These are keyword matches, so the result totals measure changes in filter behavior. [Chemotherapy procurement](https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/08df29eb-16e4-1e06-5997-b2000144e0b3) · [Microbiology procurement](https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/08df29c9-66e8-55e2-5997-b20001ccc212)

The saved TED payloads predate collection of descriptions and lot titles. A separate live check fetched 33 TED IT notices published on 2026-11-09. Notice `776678-2026` matched `oprogramowan` in its description and lot title while its notice title yielded no match. The Search API supplies lot titles as a field independent of the lot identifiers, so the result reports the field without assigning a lot number. [TED notice 776678-2026](https://ted.europa.eu/pl/notice/-/detail/776678-2026) · [TED Search API fields](https://docs.ted.europa.eu/ODS/latest/reuse/field-list.html)

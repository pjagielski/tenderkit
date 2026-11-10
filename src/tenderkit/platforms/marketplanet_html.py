"""Marketplanet public document tables and profile resolution (no third-party imports)."""
import re
from urllib.parse import urljoin

ROW_SPLIT_RE = re.compile(r'(?=<tr class="fileDataRow)')

NAME_RE = re.compile(r'<td class="text long">\s*(?:<a[^>]*>)?\s*([^<\n]+?)\s*(?:</a>)?\s*</td>', re.DOTALL)

DATE_RE = re.compile(r'<td class="date-time">([^<]*)</td>')

SIZE_RE = re.compile(r'<td class="text medium">([^<]*)</td>')

DOWNLOAD_RE = re.compile(r"downloadPostUrl: '([^']+)'")

FOLDER_RE = re.compile(r"enterFolderUrl: '([^']+)'")

def parse_rows(html: str) -> list:
    """Split the notice/folder page into per-document <tr> blocks and pull
    out name/date/size plus either a download token or a nested folder URL."""
    rows = []
    for block in ROW_SPLIT_RE.split(html)[1:]:
        end = block.find("</tr>")
        block = block[:end] if end >= 0 else block

        name_m = NAME_RE.search(block)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        if not name:
            continue

        date_m = DATE_RE.search(block)
        size_m = SIZE_RE.search(block)
        download_m = DOWNLOAD_RE.search(block)
        folder_m = FOLDER_RE.search(block)

        rows.append({
            "name": name,
            "publishedDate": date_m.group(1).strip() if date_m else None,
            "sizeText": size_m.group(1).strip() if size_m else None,
            "downloadPath": download_m.group(1) if download_m else None,
            "folderPath": folder_m.group(1) if folder_m else None,
        })
    return rows

STATUS_TABS = ["during", "current", "expired"]

DATA_ROW_RE = re.compile(r'<tr class="dataRow" id="(\d+)">\s*<td class="short">([^<]*)</td>')

ORG_SLUG_RE = re.compile(r'/pn/([^/"]+)/')

def resolve_notice_url(session, base_url: str, reference_number: str):
    """Resolve a bare org homepage URL + reference number to a specific
    notice details URL, by reading the org's real (case-sensitive) slug off
    its home page and then using its own reference-number search across all
    three status tabs. Returns (notice_url, status_tab) or (None, reason)."""
    home = session.get(urljoin(base_url, "/servlet/HomeServlet"), timeout=30)
    home.raise_for_status()
    slug_m = ORG_SLUG_RE.search(home.text)
    if not slug_m:
        return None, "no org slug found on home page"
    slug = slug_m.group(1)

    for status in STATUS_TABS:
        list_url = urljoin(base_url, f"/pn/{slug}/demand/notice/publicpzp/{status}/list")
        response = session.get(list_url, params={"demandNumber": reference_number, "isFiredBySearchButton": "true"}, timeout=30)
        response.raise_for_status()
        if "Brak wynik" in response.text:
            continue

        rows = DATA_ROW_RE.findall(response.text)
        exact_matches = [row_id for row_id, num in rows if num.strip() == reference_number]
        if len(exact_matches) == 1:
            return urljoin(base_url, f"/pn/{slug}/demand/notice/public/{exact_matches[0]}/details"), status
        if len(rows) == 1:
            return (
                urljoin(base_url, f"/pn/{slug}/demand/notice/public/{rows[0][0]}/details"),
                f"{status} (single row, reference text differs: {rows[0][1]!r})",
            )
        if len(rows) > 1:
            return None, f"{status}: {len(rows)} ambiguous rows matched"

    return None, "not found in any status tab (current/during/expired)"

def collect_documents(session, base_url: str, notice_url: str, max_folder_depth: int = 2) -> list:
    """Fetch the notice details page and recursively follow one level (or
    two, for safety) of folder rows, returning a flat list of downloadable
    documents. Folder-only "documents" with no nested files (rare) are
    dropped silently.

    A folder page re-renders the *entire* parent document table plus its own
    extra row(s) appended at the end, rather than just its own contents - so
    documents are deduplicated by name (globally unique in practice within a
    single procedure) to avoid downloading the same main-table files once per
    folder visited."""
    documents = []
    seen_names = set()
    to_visit = [(notice_url, None, 0)]
    seen_urls = set()

    while to_visit:
        url, folder_name, depth = to_visit.pop(0)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        if len(seen_urls) > 100:
            raise ValueError('Platform folder traversal limit reached')

        response = session.get(url, timeout=30)
        response.raise_for_status()
        rows = parse_rows(response.text)

        for row in rows:
            if row["downloadPath"]:
                if row["name"] in seen_names:
                    continue
                seen_names.add(row["name"])
                documents.append({
                    "name": row["name"],
                    "folder": folder_name,
                    "publishedDate": row["publishedDate"],
                    "sizeText": row["sizeText"],
                    "downloadUrl": urljoin(base_url, row["downloadPath"]),
                })
            elif row["folderPath"] and depth < max_folder_depth:
                to_visit.append((urljoin(base_url, row["folderPath"]), row["name"], depth + 1))

    return documents

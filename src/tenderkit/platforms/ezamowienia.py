"""Public documents attached to native e-Zamówienia procedures."""
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

from tenderkit.limits import MAX_BYTES
from tenderkit.model import DocumentRef, PlatformLink
from tenderkit.platforms.backends import FetchError
from tenderkit.platforms.base import extension
from tenderkit.platforms.session import PlatformSession, native_platform_url

BASE = 'https://ezamowienia.gov.pl'
TENDER_ID = r'ocds-148610-[a-fA-F0-9-]{36}'
OBJECT_ID = r'[a-zA-Z0-9_-]{1,200}'
DOWNLOAD_PATH = '/mp-readmodels/api/Tender/DownloadDocument'


class Ezamowienia:
    name = 'e-Zamówienia (native)'
    can_download = True

    def __init__(self, session=None):
        self.session = session or PlatformSession()

    def matches(self, url):
        try:
            parts = urlsplit(url)
            return parts.scheme in ('http', 'https') and parts.hostname == 'ezamowienia.gov.pl' and not parts.username
        except ValueError:
            return False

    def procedure(self, notice):
        if not self.matches(notice.platform_url or ''):
            return None
        found = re.search(TENDER_ID, notice.platform_url or '')
        identifier = found[0] if found else notice.procedure_id
        if not identifier or not re.fullmatch(TENDER_ID, identifier):
            raise FetchError('NO_REFERENCE')
        return PlatformLink(self.name, f'{BASE}/mp-client/search/list/{identifier}', identifier)

    def documents(self, link):
        if link.platform != self.name or not re.fullmatch(TENDER_ID, link.key or ''):
            raise FetchError('NOT_FOUND')
        response = self.session.get(f'{BASE}/mp-readmodels/api/Search/GetTenderDocuments', params={'tenderId': link.key}, timeout=30)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError('Unexpected native document inventory')
        return [DocumentRef(name=row['fileName'], key=row['objectId'], published=row['publishedDate'], origin='Postępowanie',
                            url=f"{BASE}{DOWNLOAD_PATH}/{link.key}/{row['objectId']}")
                for row in rows if row.get('publishedDate') and re.fullmatch(OBJECT_ID, row.get('objectId') or '')]

    def download(self, ref, to):
        if not native_platform_url(ref.url):
            raise FetchError('UNSAFE_URL')
        url = urlsplit(ref.url)
        path = re.fullmatch(f'{DOWNLOAD_PATH}/{TENDER_ID}/({OBJECT_ID})', url.path)
        if not path or path[1] != ref.key or url.query or url.fragment or not ref.published:
            raise FetchError('UNSAFE_URL')

        response = self.session.get(ref.url, timeout=60, stream=True,
                                    headers={'Accept': 'application/octet-stream, */*'})
        content = bytearray()
        try:
            if response.status_code >= 400:
                raise FetchError(f'HTTP_{response.status_code}')
            if response.url and not native_platform_url(response.url):
                raise FetchError('UNSAFE_URL')
            if response.headers.get('Content-Type', '').split(';')[0].strip().lower() == 'text/html':
                raise FetchError('DOWNLOAD_REJECTED')
            for block in response.iter_content(1 << 16):
                if len(content) + len(block) > MAX_BYTES:
                    raise FetchError('TOO_LARGE')
                content.extend(block)
        finally:
            response.close()

        head = content[:2000].removeprefix(b'\xef\xbb\xbf').lstrip().lower()
        if not content or head.startswith((b'<!doctype html', b'<html', b'<head', b'<body')):
            raise FetchError('DOWNLOAD_REJECTED')
        suffix = extension(dict(url=ref.url, name=ref.name, ext=ref.ext))
        destination = Path(to) / f'{hashlib.sha256(content).hexdigest()}.{suffix}'
        destination.write_bytes(content)
        return destination

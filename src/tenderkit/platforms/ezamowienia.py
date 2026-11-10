"""Native e-Zamówienia: public document inventory only in F2."""
import re

from tenderkit.model import DocumentRef, PlatformLink
from tenderkit.platforms.backends import FetchError
from tenderkit.platforms.session import PlatformSession

BASE = 'https://ezamowienia.gov.pl'


class Ezamowienia:
    name = 'e-Zamówienia (native)'
    can_download = False

    def __init__(self, session=None):
        self.session = session or PlatformSession()

    def matches(self, url):
        from urllib.parse import urlsplit
        try:
            parts = urlsplit(url)
            return parts.scheme in ('http', 'https') and parts.hostname == 'ezamowienia.gov.pl' and not parts.username
        except ValueError:
            return False

    def procedure(self, notice):
        if not self.matches(notice.platform_url or ''):
            return None
        found = re.search(r'ocds-148610-[a-fA-F0-9-]{36}', notice.platform_url or '')
        identifier = found[0] if found else notice.procedure_id
        if not identifier or not re.fullmatch(r'ocds-148610-[a-fA-F0-9-]{36}', identifier):
            raise FetchError('NO_REFERENCE')
        return PlatformLink(self.name, f'{BASE}/mp-client/search/list/{identifier}', identifier)

    def documents(self, link):
        if link.platform != self.name or not re.fullmatch(r'ocds-148610-[a-fA-F0-9-]{36}', link.key or ''):
            raise FetchError('NOT_FOUND')
        response = self.session.get(f'{BASE}/mp-readmodels/api/Search/GetTenderDocuments', params={'tenderId': link.key}, timeout=30)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError('Unexpected native document inventory')
        return [DocumentRef(name=row['fileName'], key=row['objectId'], published=row['publishedDate'], origin='Postępowanie',
                            url=f"{BASE}/mp-readmodels/api/Tender/DownloadDocument/{link.key}/{row['objectId']}")
                for row in rows if row.get('publishedDate')]

    def download(self, ref, to):
        raise FetchError('LIST_ONLY')

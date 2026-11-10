"""Transport adapters extracted from pharma; legacy byte APIs remain for compatibility."""
from pathlib import Path
import re
import time

from tenderkit.limits import MAX_BYTES, MAX_DOCUMENTS

class FetchError(Exception):
    """A job failure with a short code shown in the panel."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class PlatformaZakupowa:
    """platformazakupowa.pl: the transaction page lists every attachment with a direct download link."""
    label = 'platformazakupowa.pl'

    # Waits before retrying a throttled request; the first live run was throttled after 20 files 1.5 s apart.
    BACKOFF = (60, 180)

    def __init__(self, session, delay=2.0, sleep=time.sleep):
        from tenderkit.platforms.pz_html import BASE_URL, RATE_LIMIT_MARKER, resolve_transaction_url
        from tenderkit.platforms.pz_html import documents_from_page
        self.session, self.delay, self.sleep = session, delay, sleep
        self.base, self.marker, self.parse, self.resolve = BASE_URL, RATE_LIMIT_MARKER, documents_from_page, resolve_transaction_url

    def transaction(self, notice):
        """The procedure's /transakcja/<id> page; a buyer profile is resolved by the notice's reference."""
        url = notice['platform_url'] or ''
        found = re.search(r'platformazakupowa\.pl/transakcja/(\d+)', url)
        if found:
            return f'{self.base}/transakcja/{found[1]}', False
        profile = re.search(r'platformazakupowa\.pl/pn/([^/?#\s]+)', url)
        if not profile:
            raise FetchError('NOT_FOUND')
        if not notice['reference']:
            # Only the buyer's profile and no reference to search it by (common in TED notices).
            raise FetchError('NO_REFERENCE')
        try:
            return self.resolve(profile[1], notice['reference'], session=self.session), True
        except LookupError:
            raise FetchError('NOT_FOUND')

    def get(self, url, limit=None):
        """The body of url; a throttled request is retried after each BACKOFF wait before failing RATE_LIMITED."""
        limit = limit or 20 * 1024 * 1024
        for wait in (0, *self.BACKOFF):
            if wait:
                self.sleep(wait)
            response = self.session.get(url, timeout=60, stream=True)
            try:
                if response.status_code == 429:
                    continue
                if response.status_code >= 400:
                    raise FetchError(f'HTTP_{response.status_code}')
                content = bytearray()
                for block in response.iter_content(1 << 16):
                    content += block
                    if len(content) > limit:
                        raise FetchError('TOO_LARGE')
            finally:
                if hasattr(response, 'close'):
                    response.close()
            # The platform throttles with HTTP 200 and an HTML notice instead of the file.
            if self.marker not in bytes(content[:4000]).decode('utf-8', errors='ignore'):
                return bytes(content)
        raise FetchError('RATE_LIMITED')

    def list(self, notice):
        url, resolved = self.transaction(notice)
        return self.documents_at(url, resolved, notice['reference'])

    def documents_at(self, url, resolved, reference):
        page = self.get(url).decode('utf-8', errors='replace')
        if resolved and key(reference) not in key(page):
            raise FetchError('REFERENCE_MISMATCH')
        # The origin is the folder the reader needs: the opening summary and one folder per offer, else the platform's
        # own section column (the files of offers are listed on the page apart from the attachments table).
        return [dict(name=d['name'], ext=None, url=d['downloadUrl'], key=d['downloadUrl'],
                     published=d['publishedDate'], origin=d['folder'] or d.get('kind'),
                     metadata={k: d[k] for k in ('offerId', 'hiddenName', 'attachedOffer', 'kind', 'message') if k in d}) for d in self.parse(page)]

    def download(self, attachment):
        self.sleep(self.delay)
        return self.get(attachment['url'], limit=MAX_BYTES)


class Marketplanet:
    """eZamawiający (Marketplanet): server-rendered document table on the notice page, with folders for the price
    funding, the opening of offers and the results. Each file downloads by POST with a token valid in this session."""
    label = 'eZamawiający (Marketplanet)'
    NOTICE = re.compile(r'/pn/[^/]+/demand/(?:notice/public/\d+/details|\d+/notice/public/details)')
    REJECTED = ('Strona nieodnaleziona', '<html')

    def __init__(self, session, delay=2.0, sleep=time.sleep):
        from tenderkit.platforms.marketplanet_html import collect_documents, resolve_notice_url
        self.session, self.delay, self.sleep = session, delay, sleep
        self.collect, self.resolve = collect_documents, resolve_notice_url

    def transaction(self, notice):
        """(base, notice page, resolved); a buyer's home page is resolved by the notice's reference."""
        url = (notice['platform_url'] or '').strip()
        parts = re.match(r'(https?://[^/\s]*ezamawiajacy\.pl)(/[^?#\s]*)?', url)
        if not parts:
            raise FetchError('NOT_FOUND')
        base = 'https://' + parts[1].split('://', 1)[1]
        if self.NOTICE.search(parts[2] or ''):
            return base, base + parts[2], False
        if not notice['reference']:
            raise FetchError('NO_REFERENCE')
        found, _ = self.resolve(self.session, base, notice['reference'])
        if not found:
            raise FetchError('NOT_FOUND')
        return base, found, True

    def list(self, notice):
        base, url, resolved = self.transaction(notice)
        return self.documents_at(url, resolved, notice['reference'])

    def documents_at(self, url, resolved, reference):
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        base = f'{parts.scheme}://{parts.netloc}'
        if resolved:
            # The search may return a single row with another reference; the page must name ours.
            page = self.session.get(url, timeout=30)
            if page.status_code >= 400:
                raise FetchError(f'HTTP_{page.status_code}')
            if key(reference) not in key(page.text):
                raise FetchError('REFERENCE_MISMATCH')
        return [dict(name=d['name'], ext=None, url=d['downloadUrl'], published=d['publishedDate'],
                     origin=d['folder'] or 'Postępowanie',
                     key=f"{url}#{d['folder'] or ''}/{d['name']}@{d['publishedDate'] or ''}")
                for d in self.collect(self.session, base, url)]

    def download(self, attachment):
        self.sleep(self.delay)
        response = self.session.post(attachment['url'], timeout=60, stream=True)
        try:
            if response.status_code >= 400:
                raise FetchError(f'HTTP_{response.status_code}')
            content = bytearray()
            for block in response.iter_content(1 << 16):
                content += block
                if len(content) > MAX_BYTES:
                    raise FetchError('TOO_LARGE')
        finally:
            if hasattr(response, 'close'):
                response.close()
        head = bytes(content[:2000]).decode('utf-8', errors='ignore')
        if 'text/html' in response.headers.get('Content-Type', '') and any(m in head for m in self.REJECTED):
            # An expired token answers with the site's HTML page instead of the file.
            raise FetchError('DOWNLOAD_REJECTED')
        # Names often lack an extension; the attachment header carries the file name.
        name = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', response.headers.get('Content-Disposition', ''))
        if name and Path(name[1]).suffix:
            attachment['ext'] = Path(name[1]).suffix
        return bytes(content)


def key(value):
    return re.sub(r'[\W_]+', '', (value or '').casefold())

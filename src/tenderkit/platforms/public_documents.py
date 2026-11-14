"""Public attachments on e-ProPublico, LogIntrade and eB2B."""
import hashlib
import json
import ntpath
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urljoin, urlsplit

from tenderkit.limits import MAX_BYTES, MAX_LISTED
from tenderkit.model import DocumentRef, PlatformLink
from tenderkit.platforms.backends import FetchError
from tenderkit.platforms.base import extension
from tenderkit.platforms.html import Tree
from tenderkit.platforms.session import eb2b_file_redirect, tenant_url

UUID = r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}'
INTEGER_ID = r'[1-9][0-9]*'


class PublicDocuments:
    def __init__(self, session):
        self.session = session

    def matches(self, url):
        return tenant_url(url, self.domain)

    def procedure(self, notice):
        url = notice.platform_url or ''
        if not self.matches(url):
            return None
        match = self.procedure_path.fullmatch(unquote(urlsplit(url).path).rstrip('/'))
        if match is None:
            raise FetchError('NO_REFERENCE')
        return PlatformLink(self.name, urljoin(url, match[0]), match[1])

    def _base(self, link):
        if link.platform != self.name or not self.matches(link.url):
            raise FetchError('UNSAFE_URL')
        return 'https://' + urlsplit(link.url).hostname

    def _reference(self, page, href, name, **kwargs):
        url = urljoin(page, href)
        parts = urlsplit(url)
        match = self.file_path.fullmatch(unquote(parts.path))
        if (not self.matches(url) or parts.hostname != urlsplit(page).hostname
                or parts.query or parts.fragment or not match):
            raise FetchError('UNSAFE_URL')
        if not name:
            raise FetchError('DOWNLOAD_REJECTED')
        return DocumentRef(name=name, url=url, key=match[1], **kwargs)

    def download(self, ref, to):
        self_ref = self._reference(ref.url, ref.url, ref.name)
        if self_ref.key != ref.key:
            raise FetchError('UNSAFE_URL')
        response = self.session.get(ref.url, stream=True, timeout=60,
                                    headers={'Accept': 'application/octet-stream, */*'})
        size = 0
        digest = hashlib.sha256()
        temporary = None
        try:
            if response.status_code >= 400:
                raise FetchError(f'HTTP_{response.status_code}')
            if response.url and response.url != ref.url and not eb2b_file_redirect(ref.url, response.url):
                raise FetchError('UNSAFE_URL')
            media = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if media in ('text/html', 'application/json'):
                raise FetchError('DOWNLOAD_REJECTED')
            with tempfile.NamedTemporaryFile(dir=to, suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                for block in response.iter_content(1 << 16):
                    if not size and block.removeprefix(b'\xef\xbb\xbf').lstrip().lower().startswith(
                            (b'<!doctype html', b'<html', b'<head', b'<body')):
                        raise FetchError('DOWNLOAD_REJECTED')
                    size += len(block)
                    if size > MAX_BYTES:
                        raise FetchError('TOO_LARGE')
                    digest.update(block)
                    stream.write(block)
            if not size:
                raise FetchError('DOWNLOAD_REJECTED')
            suffix = extension(dict(url='', name=ref.name, ext=ref.ext))
            target = Path(to) / f'{digest.hexdigest()}.{suffix}'
            temporary.replace(target)
            return target
        finally:
            response.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class EProPublico(PublicDocuments):
    name = 'e-ProPublico'
    domain = 'e-propublico.pl'
    procedure_path = re.compile(fr'/Ogloszenia/Details/({UUID})', re.I)
    file_path = re.compile(fr'/Dokumenty/Download/({UUID})', re.I)

    def documents(self, link):
        base = self._base(link)
        match = self.procedure_path.fullmatch(unquote(urlsplit(link.url).path))
        if not match or match[1] != link.key:
            raise FetchError('NO_REFERENCE')
        response = self.session.get(base + '/Ogloszenia/DokumentyZamowienia',
                                    params={'przetargId': link.key}, timeout=30,
                                    headers={'Accept': 'text/html'})
        root = Tree(response.text).root
        if not root.find('h5', attr='class', value='section-header'):
            raise FetchError('NOT_FOUND')
        result = []
        for row in root.find_all('tr'):
            anchor = row.find('a', attr='href', value=lambda href: '/dokumenty/download/' in href.lower())
            if anchor:
                cells = row.find_all('td', direct=True)
                result.append(self._reference(base, anchor.attrs['href'], anchor.text(' '),
                    ext=cells[2].text() if len(cells) > 2 else None,
                    published=cells[4].text() if len(cells) > 4 else None))
        return result


class LogIntrade(PublicDocuments):
    name = 'LogIntrade'
    domain = 'logintrade.net'
    procedure_path = re.compile(fr'/zapytania_email,({INTEGER_ID}),[a-zA-Z0-9_-]+\.html')
    file_path = re.compile(r'/DocumentService,getAttachmentUnlogged,([a-zA-Z0-9_+=/-]+)\.html')

    def documents(self, link):
        self._base(link)
        match = self.procedure_path.fullmatch(unquote(urlsplit(link.url).path))
        if not match or match[1] != link.key:
            raise FetchError('NO_REFERENCE')
        root = Tree(self.session.get(link.url, timeout=30, headers={'Accept': 'text/html'}).text).root
        attachments = root.find(attr='class', value='attachments')
        if attachments is None:
            raise FetchError('NOT_FOUND')
        result = []
        for row in attachments.find_all(attr='class', value='attachment'):
            anchor = row.find('a', attr='href', value=lambda href: 'getAttachmentUnlogged' in href)
            name = row.find(attr='class', value='attachment-name')
            published = row.find(attr='class', value='attachment-date')
            if anchor and name:
                result.append(self._reference(link.url, anchor.attrs['href'], name.text(' '),
                                              published=published.text() if published else None))
        return result


class Eb2b(PublicDocuments):
    name = 'eB2B'
    domain = 'eb2b.com.pl'
    procedure_path = re.compile(fr'/(?:open-preview-auction|preview-auction|auction)\.html/({INTEGER_ID})(?:/[^/]+)?')
    file_path = re.compile(fr'/auction/auction/download-attachment/id/({INTEGER_ID})')

    def _data(self, base, route, params):
        payload = self.session.get(base + '/auction/auction/' + route, params=params, timeout=30).json()
        if not isinstance(payload, dict) or payload.get('success') is not True:
            raise FetchError('NOT_FOUND')
        rows = payload.get('data')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise FetchError('NOT_FOUND')
        return rows

    def procedure(self, notice):
        url = notice.platform_url or ''
        if not self.matches(url):
            return None
        match = self.procedure_path.fullmatch(urlsplit(url).path.rstrip('/'))
        if match:
            return PlatformLink(self.name, url.split('?', 1)[0].split('#', 1)[0].rstrip('/'), match[1])
        if not notice.reference:
            raise FetchError('NO_REFERENCE')
        base = 'https://' + urlsplit(url).hostname
        query = json.dumps([dict(property='auction_number', value=notice.reference)])
        rows = self._data(base, 'list', dict(filter=query, page=1, start=0, limit=25))
        matched = [str(row.get('id')) for row in rows if row.get('auction_number') == notice.reference]
        if len(rows) >= 25 or len(matched) != 1 or not re.fullmatch(INTEGER_ID, matched[0]):
            raise FetchError('NOT_FOUND')
        key = matched[0]
        return PlatformLink(self.name, f'{base}/open-preview-auction.html/{key}', key)

    def documents(self, link):
        base = self._base(link)
        match = self.procedure_path.fullmatch(urlsplit(link.url).path)
        if not match or match[1] != link.key:
            raise FetchError('NO_REFERENCE')
        result, seen = [], set()
        limit = 100
        for offset in range(0, MAX_LISTED + 1, limit):
            rows = self._data(base, 'list-attachment', dict(id=link.key, leave_date='', deleted=0,
                              bidder=0, page=offset // limit + 1, start=offset, limit=limit))
            if len(rows) > limit:
                raise FetchError('TOO_MANY_DOCUMENTS')
            for row in rows:
                key = str(row.get('id'))
                if not re.fullmatch(INTEGER_ID, key) or key in seen:
                    raise FetchError('DOWNLOAD_REJECTED')
                seen.add(key)
                if len(seen) > MAX_LISTED:
                    raise FetchError('TOO_MANY_DOCUMENTS')
                if (row.get('is_public') is True and row.get('is_active') is True
                        and row.get('is_current') is True and row.get('contractor_id') is None):
                    result.append(self._reference(base, f'/auction/auction/download-attachment/id/{key}',
                                  ntpath.basename(row.get('name') or ''), ext=row.get('extension'),
                                  published=row.get('publication_date'), origin=row.get('type_name')))
            if len(rows) < limit:
                return result
        raise FetchError('TOO_MANY_DOCUMENTS')

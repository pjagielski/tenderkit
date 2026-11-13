"""Attachments linked by a public BK2021 notice."""
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

from tenderkit.limits import MAX_BYTES
from tenderkit.model import DocumentRef, PlatformLink
from tenderkit.platforms.backends import FetchError
from tenderkit.platforms.base import extension
from tenderkit.platforms.session import bk_platform_url
from tenderkit.sources.bk import BASE, detail


class BkDocuments:
    name = 'Baza Konkurencyjności'

    def __init__(self, session):
        self.session = session

    def matches(self, url):
        return bk_platform_url(url) and re.fullmatch(r'/ogloszenia/[1-9]\d*', urlsplit(url).path) is not None

    def procedure(self, notice):
        if not self.matches(notice.platform_url or ''):
            return None
        key = urlsplit(notice.platform_url).path.rsplit('/', 1)[1]
        return PlatformLink(self.name, f'{BASE}/ogloszenia/{key}', key)

    def documents(self, link):
        if link.platform != self.name or not re.fullmatch(r'[1-9]\d*', link.key or ''):
            raise FetchError('NOT_FOUND')
        row = detail(self.session, int(link.key))
        refs = []
        for entry in row['attachments']:
            file = entry.get('file') or {}
            key = str(file.get('id'))
            if not re.fullmatch(r'[1-9]\d*', key) or file.get('uri') != f'/api/files/{key}':
                raise FetchError('UNSAFE_URL')
            refs.append(DocumentRef(name=file['name'], key=key, url=f'{BASE}/api/files/{key}',
                                    published=row['publication_date'], origin='BK announcement'))
        return refs

    def download(self, ref, to):
        if not re.fullmatch(r'[1-9]\d*', ref.key) or ref.url != f'{BASE}/api/files/{ref.key}':
            raise FetchError('UNSAFE_URL')
        response = self.session.get(ref.url, stream=True, timeout=60,
                                    headers={'Accept': 'application/octet-stream, */*'})
        digest = hashlib.sha256()
        temporary = Path(to) / f'bk-{ref.key}.tmp'
        size = 0
        try:
            if response.status_code >= 400:
                raise FetchError(f'HTTP_{response.status_code}')
            if response.url and response.url != ref.url:
                raise FetchError('UNSAFE_URL')
            media = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if media in ('text/html', 'application/json'):
                raise FetchError('DOWNLOAD_REJECTED')
            with temporary.open('wb') as stream:
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
            suffix = extension(dict(url=ref.url, name=ref.name))
            destination = Path(to) / f'{digest.hexdigest()}.{suffix}'
            temporary.replace(destination)
            return destination
        finally:
            response.close()
            temporary.unlink(missing_ok=True)

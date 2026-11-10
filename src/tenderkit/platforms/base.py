"""Core platform contract and compatibility wrapper for existing byte fetchers.

The resolved link carries the reference check across procedure() and documents();
one-time download tokens never become document identities.
"""
from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

from tenderkit.model import DocumentRef, Notice, PlatformLink


def extension(attachment):
    for candidate in (attachment.get('ext'), Path(attachment['url'].split('?')[0]).suffix, Path(attachment['name']).suffix):
        ext = re.sub(r'[^a-z0-9]', '', (candidate or '').lower())
        if ext:
            return ext[:10]
    return 'bin'


@dataclass(frozen=True)
class ResolvedLink(PlatformLink):
    reference: str | None = None
    resolved: bool = False


class WorkerPlatform:
    can_download = True
    def __init__(self, backend):
        self.backend = backend
        self.name = backend.label

    def matches(self, url: str) -> bool:
        from tenderkit.platforms.session import public_platform_url
        if not public_platform_url(url):
            return False
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or '').lower()
        except ValueError:
            return False
        if parsed.scheme not in ('http', 'https'):
            return False
        if self.name == 'platformazakupowa.pl':
            return host == 'platformazakupowa.pl'
        return host == 'ezamawiajacy.pl' or host.endswith('.ezamawiajacy.pl')

    def procedure(self, notice: Notice) -> PlatformLink | None:
        if not self.matches(notice.platform_url or ''):
            return None
        transaction = self.backend.transaction(dict(platform_url=notice.platform_url, reference=notice.reference))
        url, resolved = transaction[-2:]
        return ResolvedLink(self.name, url, key=urlsplit(url).path,
                            reference=notice.reference, resolved=resolved)

    def documents(self, link: PlatformLink) -> list[DocumentRef]:
        if link.platform != self.name or not self.matches(link.url):
            raise ValueError('Link belongs to another platform')
        return [DocumentRef(**item) for item in self.backend.documents_at(
            link.url, getattr(link, 'resolved', False), getattr(link, 'reference', None))]

    def download(self, ref: DocumentRef, to: Path) -> Path:
        from tenderkit.platforms.session import public_platform_url
        from tenderkit.platforms.backends import FetchError
        if not public_platform_url(ref.url):
            raise FetchError('UNSAFE_URL')
        attachment = asdict(ref)
        content = self.backend.download(attachment)
        head = content[:2000].lstrip().lower()
        if Path(ref.name).suffix.lower() not in ('.html', '.htm') and (head.startswith(b'<html') or head.startswith(b'<!doctype html')):
            raise FetchError('DOWNLOAD_REJECTED')
        # Never use a document-supplied name as a local path. The backend may
        # learn the extension from Content-Disposition while downloading.
        name = f'{hashlib.sha256(content).hexdigest()}.{extension(attachment)}'
        destination = Path(to) / name
        destination.write_bytes(content)
        return destination

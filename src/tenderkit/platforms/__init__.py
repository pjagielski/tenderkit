"""Built-in public platforms and entry-point registration."""
from importlib.metadata import entry_points

from tenderkit.interfaces import Platform
from tenderkit.platforms.backends import Marketplanet, PlatformaZakupowa
from tenderkit.platforms.base import WorkerPlatform
from tenderkit.platforms.ezamowienia import Ezamowienia
from tenderkit.platforms.session import PlatformSession


def platforms(session=None):
    from tenderkit.platforms.bk import BkDocuments

    session = session or PlatformSession()
    registered = {backend.label: WorkerPlatform(backend) for backend in
                  (PlatformaZakupowa(session, delay=0), Marketplanet(session, delay=0))}
    native = Ezamowienia(session)
    registered[native.name] = native
    bk = BkDocuments(session)
    registered[bk.name] = bk
    for entry in entry_points(group='tenderkit.platforms'):
        adapter = entry.load()()
        if not isinstance(adapter, Platform) or adapter.name in registered:
            raise ValueError(f'Invalid or duplicate platform entry point: {entry.name}')
        registered[adapter.name] = adapter
    return registered


def detect(notice, adapters):
    return next((adapter for adapter in adapters.values() if adapter.matches(notice.platform_url or '')), None)

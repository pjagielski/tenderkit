"""One application path for CLI and MCP: fetch to SQLite, then filter the same notices."""
from dataclasses import asdict
from datetime import date, datetime, timedelta
from importlib.metadata import entry_points
from zoneinfo import ZoneInfo

from tenderkit.interfaces import Source
from tenderkit.serialization import notice_record
from tenderkit.sources.bzp import Bzp
from tenderkit.sources.ted import Ted

MAX_WINDOW_DAYS = 120
MAX_NOTICES = 20000
MAX_RESULTS = 500


def today():
    return datetime.now(ZoneInfo('Europe/Warsaw')).date()


def window(start=None, end=None):
    end = date.fromisoformat(end) if isinstance(end, str) else end or today()
    start = date.fromisoformat(start) if isinstance(start, str) else start or end - timedelta(days=29)
    if not 0 <= (end - start).days < MAX_WINDOW_DAYS:
        raise ValueError(f'Publication window must contain 1 to {MAX_WINDOW_DAYS} days')
    return start, end


def sources():
    registered = {'BZP': Bzp(), 'TED': Ted()}
    for entry in entry_points(group='tenderkit.sources'):
        source = entry.load()()
        if not isinstance(source, Source) or source.name in registered:
            raise ValueError(f'Invalid or duplicate source entry point: {entry.name}')
        registered[source.name] = source
    return registered


def refresh(state, profile, start, end, adapters=None):
    start, end = window(start, end)
    adapters = sources() if adapters is None else adapters
    summaries = []
    query = profile.source_query()
    for source, adapter in adapters.items():
        try:
            notices = []
            for notice in adapter.fetch(start, end, query):
                if len(notices) >= MAX_NOTICES:
                    raise ValueError('Notice safety limit reached')
                if notice.source != source or notice.published is None or not start <= notice.published <= end:
                    raise ValueError('Source returned a notice outside its declared window')
                notices.append(notice)
            state.record_refresh(source, start, end, asdict(query), notices)
            summaries.append(dict(source=source, status='COMPLETED', notices=len(notices)))
        except Exception as exc:
            state.record_refresh(source, start, end, asdict(query), error=type(exc).__name__)
            summaries.append(dict(source=source, status='FAILED', error=type(exc).__name__))
    return summaries


def filtered(state, profile, *, start=None, end=None, source_names=(), on=None):
    if start is not None or end is not None:
        start, end = window(start, end)
    on = on or today()
    for notice in state.notices(start, end, source_names):
        if profile.matches(notice, today=on):
            yield notice


def search(state, profile, *, start=None, end=None, limit=100, offset=0, source_names=()):
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS or type(offset) is not int or not 0 <= offset <= MAX_NOTICES:
        raise ValueError('Invalid result limit or offset')
    found, more = [], False
    for index, notice in enumerate(filtered(state, profile, start=start, end=end, source_names=source_names)):
        if index < offset:
            continue
        if len(found) == limit:
            more = True
            break
        found.append(notice_record(notice, raw=False))
    return dict(notices=found, offset=offset, next_offset=offset + len(found) if more else None)

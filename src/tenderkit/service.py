"""One application path for CLI and MCP: fetch to SQLite, then filter the same notices."""
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from importlib.metadata import entry_points
import json
from zoneinfo import ZoneInfo

from tenderkit.interfaces import Source
from tenderkit.profile import fold
from tenderkit.serialization import dumps, notice_record
from tenderkit.sources.bzp import AMENDMENT_TYPE, Bzp
from tenderkit.sources.bk import Bk
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


def sources(*, include_bk=False):
    registered = {'BZP': Bzp(), 'TED': Ted()}
    if include_bk:
        registered['BK'] = Bk()
    for entry in entry_points(group='tenderkit.sources'):
        source = entry.load()()
        if not isinstance(source, Source) or source.name in registered:
            raise ValueError(f'Invalid or duplicate source entry point: {entry.name}')
        registered[source.name] = source
    return registered


def refresh(state, profile, start, end, adapters=None, include_results=False, include_amendments=False):
    start, end = window(start, end)
    adapters = sources() if adapters is None else adapters
    summaries = []
    base_query = profile.source_query()
    for source, adapter in adapters.items():
        query = replace(base_query, include_results=include_results,
                        include_amendments=include_amendments or source == 'BZP')
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
    # The search index narrows candidates in SQLite; Profile.matches still decides every notice.
    for notice in state.notices(start, end, source_names, profile=profile, today=on):
        if notice.source == 'BZP' and notice.notice_type == AMENDMENT_TYPE and AMENDMENT_TYPE not in profile.notice_types:
            continue
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
        found.append(matched_notice_record(notice, profile))
    return dict(notices=found, offset=offset, next_offset=offset + len(found) if more else None)


def matched_notice_record(notice, profile, *, raw=False):
    record = notice_record(notice, raw=raw)
    if profile.keywords_any:
        record['keyword_matches'] = profile.keyword_matches(notice)
    return record


def _award_record(award, notice):
    record = json_roundtrip(asdict(award))
    return dict(record, source=notice.source, number=notice.number,
                published=str(notice.published) if notice.published else None,
                title=notice.title, buyer_name=notice.buyer_name)


def json_roundtrip(value):
    return json.loads(dumps(value))


def latest_awards(notices):
    """Latest formal result per source, procedure and lot; a later notice replaces an earlier one."""
    latest = {}
    for notice in sorted(notices, key=lambda n: (n.published or date.min, n.number)):
        if not notice.awards:
            continue
        key = notice.procedure_id or notice.number
        for award in notice.awards:
            latest[(notice.source, key, award.lot)] = (award, notice)
    return [latest[key] for key in sorted(latest, key=lambda k: (k[0], k[1], k[2]))]


def procedure_results(state, source, number):
    """One procedure's contract and result notices, linked by procedure_id, with the latest award per lot."""
    notice = state.get(source, number)
    if notice is None:
        raise ValueError('Notice not found in local state')
    related = [notice]
    if notice.procedure_id:
        related = list(state.notices(sources=(source,), procedure_id=notice.procedure_id))
        if not any(n.number == notice.number for n in related):
            related.append(notice)
    related.sort(key=lambda n: (n.published or date.min, n.number))
    return dict(
        notice=notice_record(notice, raw=False),
        procedure_id=notice.procedure_id,
        contracts=[notice_record(n, raw=False) for n in related if not n.awards and n.notice_type != AMENDMENT_TYPE],
        results=[notice_record(n, raw=False) for n in related if n.awards],
        awards=[_award_record(award, owner) for award, owner in latest_awards(related)])


def search_results(state, *, buyer=None, cpv=(), start=None, end=None, limit=100, offset=0, source_names=()):
    """Earlier awards by buyer text and CPV, latest per procedure and lot, paginated like notice search."""
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS or type(offset) is not int or not 0 <= offset <= MAX_NOTICES:
        raise ValueError('Invalid result limit or offset')
    if start is not None or end is not None:
        start, end = window(start, end)
    patterns = tuple(str(code).strip() for code in cpv)
    if any(not pattern or not (pattern.rstrip('*').isdigit()) for pattern in patterns):
        raise ValueError('Result CPV must be digits, optionally ending in *')
    folded_buyer = fold(buyer) if buyer else None
    candidates = []
    # SQLite narrows by awards, buyer and CPV; the checks below keep the exact matching rules.
    for notice in state.notices(start, end, source_names, awarded=True, cpv=patterns, buyer=buyer):
        if not notice.awards:
            continue
        if folded_buyer and folded_buyer not in fold(notice.buyer_name):
            continue
        if patterns and not any(
                str(code).startswith(pattern[:-1]) if pattern.endswith('*') else str(code) == pattern
                for pattern in patterns for code in notice.cpv):
            continue
        candidates.append(notice)
    awards = [_award_record(award, owner) for award, owner in latest_awards(candidates)]
    page = awards[offset:offset + limit]
    return dict(awards=page, offset=offset, total=len(awards),
                next_offset=offset + len(page) if offset + len(page) < len(awards) else None)

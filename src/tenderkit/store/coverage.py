"""Effective publication windows for each recorded refresh query."""
from collections import defaultdict
from datetime import date, timedelta
import json

_DAY = timedelta(days=1)


def _merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and (start - merged[-1][1]).days <= 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _types(source, query=None):
    """Mirror the built-in adapters' acquisition scope; unknown plugins cannot prove coverage."""
    from tenderkit.sources.bzp import FRESH_NOTICE_TYPES, RESULT_NOTICE_TYPES, AMENDMENT_TYPE
    from tenderkit.sources.ted import NOTICE_KINDS, RESULT_KINDS, SEARCH_KINDS
    if query is None:
        return {'BZP': (*FRESH_NOTICE_TYPES, *RESULT_NOTICE_TYPES, AMENDMENT_TYPE),
                'TED': SEARCH_KINDS, 'BK': ('BKAnnouncement',)}.get(source, ())
    configured = query.get('notice_types') or ()
    if source == 'BZP':
        kinds = set(configured).intersection((*FRESH_NOTICE_TYPES, *RESULT_NOTICE_TYPES)) if configured else set(FRESH_NOTICE_TYPES)
        if query.get('include_results'):
            kinds.update(RESULT_NOTICE_TYPES)
        if AMENDMENT_TYPE in configured or (query.get('include_amendments') and kinds.intersection(FRESH_NOTICE_TYPES)):
            kinds.add(AMENDMENT_TYPE)
        return kinds
    if source == 'TED':
        kinds = set(configured).intersection(SEARCH_KINDS) or set(NOTICE_KINDS)
        if query.get('include_results'):
            kinds.update(RESULT_KINDS)
        return kinds
    if source == 'BK':
        return {'BKAnnouncement'} if not configured or 'BKAnnouncement' in configured else set()
    return set()


def search_coverage(refreshes, profile, start=None, end=None, source_names=(), *, results=False):
    """Conservative acquisition coverage for this search, independent of matching row counts."""
    from tenderkit.notice_kinds import source_types
    rows = [dict(row) for row in refreshes]
    sources = sorted(set(source_names) if source_names else {'BZP', 'TED', *(r['source'] for r in rows)})
    requested = {'from': start.isoformat() if start else None, 'to': end.isoformat() if end else None}
    prefixes = profile.cpv_prefixes() or ('',)
    configured = profile.notice_types or source_types(profile.notice_kinds)
    report = {}
    for source in sources:
        known = set(_types(source))
        wanted = known.intersection(('TenderResultNotice', 'can-standard')) if results else (known.intersection(configured) if configured else known)
        cells = {(prefix, kind): [] for prefix in prefixes for kind in wanted}
        overlapping = []
        latest = None
        for row in rows:
            if row['source'] != source:
                continue
            query = json.loads(row['query'])
            kinds = set(_types(source, query))
            acquired = query.get('cpv_prefixes') or ('',)
            if latest is None or row['id'] > latest['id']:
                latest = row
            left, right = date.fromisoformat(row['start']), date.fromisoformat(row['end'])
            left, right = max(left, start) if start else left, min(right, end) if end else right
            if left > right or row['status'] != 'COMPLETED':
                continue
            for (prefix, kind), intervals in cells.items():
                # BZP amendments are acquired without a CPV filter.
                scopes = ('',) if source == 'BZP' and kind == 'NoticeUpdateNotice' else acquired
                if kind in kinds and any(prefix.startswith(p) or p.startswith(prefix) for p in scopes):
                    overlapping.append((left, right))
                if kind in kinds and any(prefix.startswith(p) for p in scopes):
                    intervals.append((left, right))
        complete = None
        for intervals in cells.values():
            intervals = _merge(intervals)
            complete = intervals if complete is None else _merge(
                (max(a, c), min(b, d)) for a, b in complete for c, d in intervals if max(a, c) <= min(b, d))
        complete = complete or []
        status = 'partial' if overlapping else 'outside_coverage'
        if start and end and complete == [(start, end)]:
            status = 'full'
        if not wanted and known:
            status = 'not_applicable'
        def windows(intervals):
            return [{'from': a.isoformat(), 'to': b.isoformat()} for a, b in _merge(intervals)]
        report[source] = dict(coverage_status=status, covered_windows=windows(complete),
                              partial_windows=windows(overlapping),
                              requested_scope=dict(cpv_prefixes=list(prefixes), notice_types=sorted(wanted)),
                              last_refresh={key: latest[key] for key in ('start', 'end', 'status', 'finished_at')} if latest else None)
    return dict(requested_window=requested, sources=report)


def _ranges(intervals, attempted_start, attempted_end):
    windows = []
    for start, end in sorted(intervals):
        if windows and (start - windows[-1][1]).days <= 1:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    gaps, cursor = [], attempted_start
    for start, end in windows:
        if start > cursor:
            gaps.append((cursor, start - _DAY))
        cursor = end + _DAY if end < attempted_end else None
    if cursor is not None and cursor <= attempted_end:
        gaps.append((cursor, attempted_end))
    return {
        'from': windows[0][0].isoformat() if windows else None,
        'to': windows[-1][1].isoformat() if windows else None,
        'attempted_from': attempted_start.isoformat(),
        'attempted_to': attempted_end.isoformat(),
        'windows': [{'from': start.isoformat(), 'to': end.isoformat()} for start, end in windows],
        'gaps': [{'from': start.isoformat(), 'to': end.isoformat()} for start, end in gaps],
    }


def effective_coverage(refreshes):
    sources = defaultdict(lambda: defaultdict(list))
    for row in refreshes:
        query = json.dumps(json.loads(row['query']), sort_keys=True)
        sources[row['source']][query].append(
            (date.fromisoformat(row['start']), date.fromisoformat(row['end']), row['status']))

    def summarize(rows):
        successful = [(start, end) for start, end, status in rows if status == 'COMPLETED']
        return _ranges(successful, min(start for start, _, _ in rows), max(end for _, end, _ in rows))

    return {source: {
        **summarize([row for rows in queries.values() for row in rows]),
        'queries': [{'query': json.loads(query), **summarize(rows)} for query, rows in sorted(queries.items())],
    } for source, queries in sorted(sources.items())}

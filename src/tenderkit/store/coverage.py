"""Effective publication windows for each recorded refresh query."""
from collections import defaultdict
from datetime import date, timedelta
import json

_DAY = timedelta(days=1)


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

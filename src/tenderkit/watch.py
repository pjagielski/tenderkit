"""Refresh a profile and report newly matching or revised notices."""
from datetime import date, datetime, timedelta, timezone
import json
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from tenderkit.queue import worker_lock
from tenderkit.serialization import dumps
from tenderkit.service import MAX_WINDOW_DAYS, matched_notice_record, refresh, today
from tenderkit.sources.bzp import AMENDMENT_TYPE
from tenderkit.sources.mapping import first


_BZP_NUMBER = re.compile(r'(\d{4}/BZP \d+)/(\d+)')
_TED_ID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
_TED_PUBLICATION = re.compile(r'(\d{1,10})-(\d{4})')


def _bzp_identity(number):
    match = _BZP_NUMBER.fullmatch(number)
    return (match[1], int(match[2])) if match else (number, 0)


def _ted_publication(number):
    match = _TED_PUBLICATION.fullmatch(number)
    return f'{match[2]}/{int(match[1]):010d}' if match else number


def _ted_identity(conn, profile, notice):
    publication = _ted_publication(notice.number)
    notice_id = str(first(notice.raw.get('notice-identifier')) or '').lower()
    notice_id = notice_id if re.fullmatch(_TED_ID, notice_id) else None
    reference = str(first(notice.raw.get('change-notice-version-identifier')) or '').lower()
    previous_id = re.fullmatch(rf'({_TED_ID})-\d{{1,2}}', reference)
    previous = previous_id[1] if previous_id else None
    if _TED_PUBLICATION.fullmatch(reference):
        previous = _ted_publication(reference)
    aliases = tuple(dict.fromkeys(key for key in (previous, notice_id, publication) if key))
    for alias in aliases:
        row = conn.execute('select identity from watch_ted_aliases where profile=? and alias=?',
                           (profile, alias)).fetchone()
        if row:
            identity = row['identity']
            break
    else:
        identity = previous or publication
    conn.executemany('insert or ignore into watch_ted_aliases(profile,alias,identity) values(?,?,?)',
                     [(profile, alias, identity) for alias in aliases])
    version = str(first(notice.raw.get('notice-version')) or '0').zfill(2)
    return identity, f'{notice.published}/{publication}/{version}'


def _latest_notices(conn, state, profile, source, start, end):
    notices = list(state.notices(start, end, (source,)))
    originals = {notice.number: notice for notice in notices if notice.notice_type != AMENDMENT_TYPE}
    latest = {}
    publication = _ted_publication if source == 'TED' else str
    for notice in sorted(notices, key=lambda item: (item.published, publication(item.number))):
        amendment = None
        if source == 'BZP':
            if notice.notice_type == AMENDMENT_TYPE:
                amendment = notice
                number = amendment.raw.get('amends')
                if not number:
                    continue
                identity, version = _bzp_identity(number)
                notice = originals.get(number)
                if notice is None:
                    seen = conn.execute('select 1 from watch_seen where profile=? and source=? and identity=?',
                                        (profile, source, identity)).fetchone()
                    notice = state.get(source, number) if seen else None
                if notice is None:
                    continue
            else:
                identity, version = _bzp_identity(notice.number)
            published_notice = amendment or notice
            revision = f'{version:08d}/{published_notice.published}/{published_notice.number}'
        elif source == 'TED':
            identity, revision = _ted_identity(conn, profile, notice)
        elif source == 'BK':
            identity, revision = notice.number, f"{notice.raw['version']:020d}"
        else:
            identity, revision = notice.number, notice.number
        prior = latest.get(identity)
        if prior is None or revision > prior[0]:
            latest[identity] = (revision, notice, amendment)
    return latest


def _record_events(conn, run_id, profile, source, latest, on):
    for identity, (revision, notice, amendment) in latest.items():
        key = (profile.name, source, identity)
        previous = conn.execute('''select revision, deadline from watch_seen
            where profile=? and source=? and identity=?''', key).fetchone()
        if previous and revision < previous['revision']:
            continue
        selected = profile.select(notice, today=on)
        if previous is None and selected is None:
            continue
        notice = selected or notice
        deadline = notice.deadline
        if deadline and deadline.tzinfo:
            deadline = deadline.astimezone(timezone.utc)
        deadline = deadline.isoformat() if deadline else None
        changes = []
        if previous:
            if revision > previous['revision']:
                changes.append('version')
            if deadline != previous['deadline']:
                changes.append('deadline')
            if not changes:
                continue
        conn.execute('''insert into watch_seen(profile,source,identity,revision,deadline) values(?,?,?,?,?)
            on conflict(profile,source,identity) do update set revision=excluded.revision, deadline=excluded.deadline''',
            (*key, revision, deadline))
        event = dict(profile=profile.name, event='updated' if previous else 'new',
                     changes=changes, notice=matched_notice_record(notice, profile))
        if amendment:
            event['amendment'] = dict(number=amendment.number, url=amendment.url)
        conn.execute('insert into watch_events(run_id,profile,payload) values(?,?,?)',
                     (run_id, profile.name, dumps(event)))


def _source_window(state, profile, source, start, catch_up):
    if not catch_up:
        return start
    with state.connection() as conn:
        row = conn.execute('select resume_from from watch_cursors where profile=? and source=?',
                           (profile, source)).fetchone()
        start = min(start, date.fromisoformat(row['resume_from'])) if row else start
        # Keep the whole attempted window recoverable until its events are staged.
        conn.execute('''insert into watch_cursors(profile,source,resume_from) values(?,?,?)
            on conflict(profile,source) do update set resume_from=excluded.resume_from''',
            (profile, source, start.isoformat()))
    return start


def _refresh_source(state, profile, source, adapter, start, end):
    first = start
    count = 0
    interval = 30 if source == 'BZP' else MAX_WINDOW_DAYS
    while True:
        last = first + timedelta(days=min((end - first).days, interval - 1))
        summary, = refresh(state, profile, first, last, {source: adapter}, include_amendments=True)
        if summary['status'] == 'FAILED':
            return {**summary, 'from': start.isoformat(), 'to': end.isoformat()}
        count += summary['notices']
        if last == end:
            break
        first = last + timedelta(days=1)
    return {'source': source, 'status': 'COMPLETED', 'notices': count, 'from': start.isoformat(), 'to': end.isoformat()}


def _event(row):
    return dict(event_id=row['id'], **json.loads(row['payload']))


def latest_updates(state, profile, *, limit=100, offset=0):
    if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or not 0 <= offset < 2**63:
        raise ValueError('Invalid result limit or offset')
    with state.connection() as conn:
        conn.execute('begin')
        run = conn.execute('''select id, finished_at, sources from watch_runs
            where profile=? order by id desc limit 1''', (profile,)).fetchone()
        if run is None:
            return dict(profile=profile, run_id=None, finished_at=None, sources=[], events=[], next_offset=None)
        rows = conn.execute('''select id,payload from watch_events where run_id=?
            order by id limit ? offset ?''', (run['id'], limit + 1, offset)).fetchall()
    return dict(profile=profile, run_id=run['id'], finished_at=run['finished_at'], sources=json.loads(run['sources']),
                events=[_event(row) for row in rows[:limit]], next_offset=offset + limit if len(rows) > limit else None)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _webhook_sender(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.fragment:
        raise ValueError('Webhook must be an HTTP(S) URL without credentials or a fragment')
    opener = build_opener(_NoRedirect())

    def send(events):
        request = Request(url, data=dumps({'events': events}).encode('utf-8'),
                          headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with opener.open(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise OSError('Unexpected webhook response')
        except (OSError, ValueError) as exc:
            raise RuntimeError(f'Webhook delivery failed ({type(exc).__name__}); events retained for retry') from None

    return send


def _deliver(state, run_id, output, send):
    while True:
        with state.connection() as conn:
            rows = conn.execute('''select id,payload from watch_events where run_id=? and delivered=0
                order by id limit 100''', (run_id,)).fetchall()
        if not rows:
            return
        events = [_event(row) for row in rows]
        if send:
            send(events)
        for event in events:
            print(dumps(event), file=output)
        output.flush()
        with state.connection() as conn:
            conn.executemany('update watch_events set delivered=1 where id=?', [(row['id'],) for row in rows])


def run_watch(state, profile, start, end, adapters, output, *, catch_up=True, webhook=None):
    """Refresh each source independently, stage events atomically, and deliver pending events."""
    send = _webhook_sender(webhook) if webhook is not None else None
    with worker_lock(state.path.resolve(), suffix='.watch.lock') as locked:
        if not locked:
            raise RuntimeError('Another watch run is already active for this database')
        summaries = []
        successful = []
        for source, adapter in adapters.items():
            first = _source_window(state, profile.name, source, start, catch_up)
            summary = _refresh_source(state, profile, source, adapter, first, end)
            summaries.append(summary)
            if summary['status'] == 'COMPLETED':
                successful.append((source, first))
        with state.connection() as conn:
            run_id = conn.execute('insert into watch_runs(profile,finished_at,sources) values(?,?,?)',
                                  (profile.name, datetime.now(timezone.utc).isoformat(), dumps(summaries))).lastrowid
            conn.execute('update watch_events set run_id=? where profile=? and delivered=0', (run_id, profile.name))
            on = today()
            for source, first in successful:
                latest = _latest_notices(conn, state, profile.name, source, first, end)
                _record_events(conn, run_id, profile, source, latest, on)
                if catch_up:
                    conn.execute('update watch_cursors set resume_from=? where profile=? and source=?',
                                 (end.isoformat(), profile.name, source))
        _deliver(state, run_id, output, send)
    return summaries

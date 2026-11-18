"""SQLite state: one transaction per successfully completed source window."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


from tenderkit.profile import fold, profile_from_dict
from tenderkit.serialization import compact_raw, dumps, notice_from_record, notice_record
from tenderkit.sources.bzp import AMENDMENT_TYPE, FRESH_NOTICE_TYPES
from tenderkit.store import search_index
from tenderkit.store.coverage import effective_coverage, search_coverage

SCHEMA = '''
create table if not exists saved_profiles (
    name text primary key, payload text not null
);
create table if not exists notices (
    source text not null, number text not null, published text,
    payload text not null, coverage text not null default 'PENDING',
    primary key(source, number)
);
create index if not exists notices_published on notices(published, source, number);
-- Result lookups: notices of one procedure, and only notices that carry awards. Expression indexes need no new
-- columns, so a read-only server uses them whenever a writer has created them and scans otherwise.
create index if not exists notices_procedure on notices(source, json_extract(payload, '$.procedure_id'));
create index if not exists notices_awarded on notices(published) where json_array_length(payload, '$.awards') > 0;
create table if not exists refreshes (
    id integer primary key, source text not null, start text not null, end text not null,
    query text not null, status text not null, count integer not null,
    error text, finished_at text not null
);
create table if not exists refresh_jobs (
    id integer primary key, request_key text not null, request text not null,
    status text not null, created_at text not null, finished_at text
);
create unique index if not exists active_refresh_request on refresh_jobs(request_key)
    where status in ('QUEUED','RUNNING');
create table if not exists refresh_job_sources (
    job_id integer not null, source text not null, status text not null,
    attempts integer not null default 0, observed integer not null default 0,
    saved integer not null default 0, error text, updated_at text,
    primary key(job_id,source)
);
create table if not exists document_refs (
    source text not null, number text not null, document_key text not null, payload text not null,
    primary key(source,number,document_key)
);
create table if not exists procedure_links (
    source text not null, number text not null, payload text not null,
    primary key(source,number)
);
create table if not exists notice_documents (
    source text not null, number text not null, document_key text not null, sha256 text not null,
    payload text not null, fetched_at text not null,
    primary key(source,number,document_key,sha256)
);
-- A document the platform refused with a permanent HTTP error (a withdrawn or replaced attachment). A whole-procedure
-- download skips it; asking for its key again retries it, and a later successful download clears it.
create table if not exists document_unavailable (
    source text not null, number text not null, document_key text not null, error text not null, checked_at text not null,
    primary key(source,number,document_key)
);
create table if not exists fetch_jobs (
    id integer primary key, source text not null, number text not null, selection text,
    status text not null, attempts integer not null default 0, error text, error_kind text,
    next_attempt real not null default 0, heartbeat real, summary text not null default '{}'
);
create unique index if not exists active_document_job on fetch_jobs(source,number)
    where status in ('QUEUED','RUNNING');
create table if not exists watch_cursors (
    profile text not null, source text not null, resume_from text not null,
    primary key(profile,source)
);
create table if not exists watch_seen (
    profile text not null, source text not null, identity text not null,
    revision text not null, deadline text, primary key(profile,source,identity)
);
create table if not exists watch_ted_aliases (
    profile text not null, alias text not null, identity text not null, primary key(profile,alias)
);
create table if not exists watch_runs (
    id integer primary key, profile text not null, finished_at text not null, sources text not null
);
create index if not exists watch_runs_profile on watch_runs(profile,id);
create table if not exists watch_events (
    id integer primary key, run_id integer not null references watch_runs(id),
    profile text not null, payload text not null, delivered integer not null default 0
);
create index if not exists watch_events_run on watch_events(run_id,id);
create index if not exists watch_events_pending on watch_events(profile) where delivered=0;
'''


class State:
    def __init__(self, path, *, read_only=False, keep_raw=True):
        """With keep_raw=False, saved notices keep only the source fields tenderkit reads (`RAW_FIELDS_USED`)."""
        self.path = Path(path).expanduser()
        self.read_only = read_only
        self.keep_raw = keep_raw
        if read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            conn.executescript(search_index.SCHEMA)
            # Index notices saved before the search index existed, once; later writes keep it in step.
            missing = conn.execute('select rowid, payload from notices where rowid not in (select id from notice_index)')
            for row in missing.fetchall():
                search_index.write(conn, row['rowid'], notice_from_record(json.loads(row['payload'])))
            self._initialize_amendments(conn)

    @contextmanager
    def connection(self):
        database = self.path.resolve().as_uri() + '?mode=ro' if self.read_only else self.path
        conn = sqlite3.connect(database, timeout=30, uri=self.read_only)
        conn.row_factory = sqlite3.Row
        conn.create_function('fold', 1, fold, deterministic=True)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _initialize_amendments(conn):
        # Existing installations can already have corrections fetched by watch.
        index_exists = "select 1 from sqlite_master where name='notices_amends'"
        if conn.execute(index_exists).fetchone():
            return
        if not conn.in_transaction:
            conn.execute('begin immediate')
        if conn.execute(index_exists).fetchone():
            return
        conn.execute('''create index notices_amends on notices(source, json_extract(payload, '$.raw.amends'))
                        where json_extract(payload, '$.raw.amends') is not null''')
        targets = conn.execute('''select distinct json_extract(payload, '$.raw.amends') as target
                                  from notices where source='BZP' and target is not null''').fetchall()
        State._apply_bzp_deadlines(conn, (row['target'] for row in targets))

    @staticmethod
    def _apply_bzp_deadlines(conn, targets):
        for number in targets:
            row = conn.execute("select rowid, payload from notices where source='BZP' and number=?",
                               (number,)).fetchone()
            if row is None:
                continue
            notice = notice_from_record(json.loads(row['payload']))
            if notice.notice_type is not None and notice.notice_type not in FRESH_NOTICE_TYPES:
                continue
            previous = notice.deadline_amendment
            original = previous['original_deadline'] if previous else notice.deadline
            original_precision = (previous.get('original_precision', 'datetime') if original else None) if previous else notice.deadline_precision
            if isinstance(original, str):
                original = datetime.fromisoformat(original)
            amendment = conn.execute('''
                select payload from notices where source='BZP'
                    and json_extract(payload, '$.raw.amends') = ?
                    and json_extract(payload, '$.notice_type') = ?
                    and json_extract(payload, '$.deadline') is not null
                    and published >= ?
                order by published desc, coalesce(
                    json_extract(payload, '$.raw.amendment_published_at'),
                    json_extract(payload, '$.raw.publicationDate'), '') desc, number desc limit 1
                ''', (number, AMENDMENT_TYPE, str(notice.published))).fetchone()
            evidence, deadline, precision = None, original, original_precision
            if amendment:
                correction = notice_from_record(json.loads(amendment['payload']))
                deadline, precision = correction.deadline, correction.deadline_precision
                evidence = dict(number=correction.number, url=correction.url,
                                original_deadline=original.isoformat() if original else None)
                if original_precision == 'date':
                    evidence['original_precision'] = original_precision
            if previous == evidence and notice.deadline == deadline and notice.deadline_precision == precision:
                continue
            notice = replace(notice, deadline=deadline, deadline_amendment=evidence, deadline_precision=precision)
            conn.execute('update notices set payload=? where rowid=?', (dumps(notice_record(notice)), row['rowid']))
            search_index.write(conn, row['rowid'], notice)

    def saved_profiles(self):
        with self.connection() as conn:
            # Read-only servers can still open databases created before saved profiles existed.
            if not conn.execute("select 1 from sqlite_master where name='saved_profiles'").fetchone():
                return {}
            return {row['name']: profile_from_dict(json.loads(row['payload']))
                    for row in conn.execute('select name,payload from saved_profiles order by name')}

    def save_profile(self, profile, *, overwrite=False):
        with self.connection() as conn:
            conn.execute('begin immediate')
            if not overwrite and conn.execute('select 1 from saved_profiles where name=?', (profile.name,)).fetchone():
                raise ValueError('Profile already exists; set overwrite=true to replace it')
            conn.execute('insert into saved_profiles(name,payload) values(?,?) '
                         'on conflict(name) do update set payload=excluded.payload',
                         (profile.name, dumps(asdict(profile))))

    def delete_profile(self, name):
        with self.connection() as conn:
            deleted = conn.execute('delete from saved_profiles where name=?', (name,)).rowcount
            if not deleted:
                raise ValueError('No saved profile with this name')

    def record_refresh(self, source, start, end, query, notices=None, error=None, *, job_id=None):
        with self.connection() as conn:
            if source == 'BZP' and notices:
                conn.execute('begin immediate')
            targets = set()
            for notice in notices or []:
                if notice.source == 'BZP':
                    if notice.notice_type is None or notice.notice_type in FRESH_NOTICE_TYPES:
                        targets.add(notice.number)
                    elif notice.notice_type == AMENDMENT_TYPE:
                        prior = conn.execute("select json_extract(payload, '$.raw.amends') from notices "
                                             "where source='BZP' and number=?", (notice.number,)).fetchone()
                        targets.update(target for target in (notice.raw.get('amends'), prior[0] if prior else None)
                                       if target)

                if not self.keep_raw:
                    notice = replace(notice, raw=compact_raw(notice.raw))
                conn.execute('''insert into notices(source,number,published,payload) values(?,?,?,?)
                    on conflict(source,number) do update set published=excluded.published,payload=excluded.payload''',
                    (notice.source, notice.number, str(notice.published) if notice.published else None,
                     dumps(notice_record(notice))))
                rowid = conn.execute('select rowid from notices where source=? and number=?',
                                     (notice.source, notice.number)).fetchone()[0]
                search_index.write(conn, rowid, notice)
            self._apply_bzp_deadlines(conn, targets)
            conn.execute('''insert into refreshes(source,start,end,query,status,count,error,finished_at)
                values(?,?,?,?,?,?,?,?)''', (source, str(start), str(end), dumps(query),
                    'FAILED' if error else 'COMPLETED', len(notices or []), error,
                    datetime.now(timezone.utc).isoformat()))
            if job_id is not None:
                timed_out = error in ('TimeoutError', 'Timeout', 'ReadTimeout', 'ConnectTimeout')
                conn.execute('''update refresh_job_sources set status=?,saved=?,error=?,updated_at=?
                    where job_id=? and source=?''',
                    ('TIMED_OUT' if timed_out else 'FAILED' if error else 'COMPLETED',
                     len(notices or []), error, datetime.now(timezone.utc).isoformat(), job_id, source))

    def notices(self, start=None, end=None, sources=(), *, awarded=False, procedure_id=None, cpv=(), buyer=None,
                profile=None, today=None):
        """Saved notices, newest first. The keyword filters narrow rows in SQLite before any record is decoded:
        awarded keeps notices with awards, cpv takes main codes or prefixes ending in *, buyer is a folded
        substring of the buyer name. profile with today narrows by the search index when it is complete; the caller
        still applies Profile.matches."""
        conditions, params = [], []
        if awarded:
            conditions.append("json_array_length(payload, '$.awards') > 0")
        if procedure_id is not None:
            conditions.append("json_extract(payload, '$.procedure_id') = ?")
            params.append(procedure_id)
        if cpv:
            terms = []
            for pattern in cpv:
                if pattern.endswith('*'):
                    terms.append("substr(value, 1, ?) = ?")
                    params.extend((len(pattern) - 1, pattern[:-1]))
                else:
                    terms.append('value = ?')
                    params.append(pattern)
            conditions.append("exists (select 1 from json_each(payload, '$.cpv') where " + ' or '.join(terms) + ')')
        if buyer:
            conditions.append("instr(fold(json_extract(payload, '$.buyer_name')), ?) > 0")
            params.append(fold(buyer))
        if start:
            conditions.append('published >= ?')
            params.append(str(start))
        if end:
            conditions.append('published <= ?')
            params.append(str(end))
        if sources:
            conditions.append('source in (' + ','.join('?' for _ in sources) + ')')
            params.extend(sources)
        with self.connection() as conn:
            if profile is not None and today is not None and search_index.complete(conn):
                narrowed, narrowed_params = search_index.prefilter(profile, today)
                conditions, params = conditions + narrowed, params + narrowed_params
            where = ' where ' + ' and '.join(conditions) if conditions else ''
            for row in conn.execute('select payload from notices' + where + ' order by published desc,source,number', params):
                yield notice_from_record(json.loads(row['payload']))

    def get(self, source, number):
        with self.connection() as conn:
            row = conn.execute('select payload from notices where source=? and number=?', (source, number)).fetchone()
            return notice_from_record(json.loads(row['payload'])) if row else None

    def search_coverage(self, profile, start=None, end=None, source_names=(), *, results=False):
        with self.connection() as conn:
            return search_coverage(conn.execute('select id,source,start,end,query,status,finished_at from refreshes'),
                                   profile, start, end, source_names, results=results)

    def coverage(self):
        with self.connection() as conn:
            counts = [dict(r) for r in conn.execute('select source,coverage,count(*) as count from notices group by source,coverage')]
            # Every source/query window is explicit; this is not a claim of complete catalogue coverage.
            refreshes = [dict(r) for r in conn.execute('select * from refreshes order by id desc limit 50')]
            for row in refreshes:
                row['query'] = json.loads(row['query'])
            jobs = [dict(r) for r in conn.execute('select id,source,number,status,attempts,error,error_kind,next_attempt,summary from fetch_jobs order by id desc limit 50')]
            for job in jobs:
                job['summary'] = json.loads(job['summary'])
            coverage = effective_coverage(conn.execute('select distinct source,start,end,query,status from refreshes'))
            return dict(counts=counts, refreshes=refreshes, jobs=jobs, effective_coverage=coverage)

    def set_coverage(self, source, number, status):
        if status not in ('PENDING', 'FETCHED', 'NO_DOCUMENTS', 'UNSUPPORTED_PLATFORM', 'NO_REFERENCE', 'FETCH_FAILED'):
            raise ValueError('Invalid coverage state')
        with self.connection() as conn:
            conn.execute('update notices set coverage=? where source=? and number=?', (status, source, number))

    def save_listing(self, source, number, refs, link=None):
        from dataclasses import asdict
        with self.connection() as conn:
            conn.execute('delete from document_refs where source=? and number=?', (source, number))
            conn.executemany('insert into document_refs values(?,?,?,?)',
                [(source, number, ref.key, dumps(asdict(ref))) for ref in refs])
            if link is not None:
                conn.execute('insert into procedure_links values(?,?,?) on conflict(source,number) do update set payload=excluded.payload',
                             (source, number, dumps(asdict(link))))

    def procedure_link(self, source, number):
        with self.connection() as conn:
            row = conn.execute('select payload from procedure_links where source=? and number=?', (source, number)).fetchone()
            return json.loads(row[0]) if row else None

    def listing(self, source, number):
        with self.connection() as conn:
            return [json.loads(row[0]) for row in conn.execute(
                'select payload from document_refs where source=? and number=? order by rowid', (source, number))]

    def record_document(self, source, number, record):
        with self.connection() as conn:
            conn.execute('''insert into notice_documents values(?,?,?,?,?,?)
                on conflict(source,number,document_key,sha256) do update set payload=excluded.payload,fetched_at=excluded.fetched_at''',
                (source, number, record['key'], record['sha256'], dumps(record), record['fetched_at']))

    def mark_unavailable(self, source, number, key, error):
        with self.connection() as conn:
            conn.execute('''insert into document_unavailable values(?,?,?,?,?)
                on conflict(source,number,document_key) do update set error=excluded.error,checked_at=excluded.checked_at''',
                (source, number, key, error, datetime.now(timezone.utc).isoformat()))

    def clear_unavailable(self, source, number, key):
        with self.connection() as conn:
            conn.execute('delete from document_unavailable where source=? and number=? and document_key=?', (source, number, key))

    def unavailable(self, source, number):
        """{document key: {error, checked_at}} of the documents the platform refused for good."""
        with self.connection() as conn:
            return {row['document_key']: dict(error=row['error'], checked_at=row['checked_at']) for row in conn.execute(
                'select document_key,error,checked_at from document_unavailable where source=? and number=?', (source, number))}

    def documents(self, source, number):
        with self.connection() as conn:
            # Return latest versions by key; earlier hashes stay registered and readable.
            found = {}
            for row in conn.execute('select document_key,payload from notice_documents where source=? and number=? order by fetched_at,rowid', (source, number)):
                found[row['document_key']] = json.loads(row['payload'])
            return found

    def document(self, digest):
        import re
        if not re.fullmatch(r'[a-f0-9]{64}', digest or ''):
            raise ValueError('Document id must be a SHA-256 digest')
        with self.connection() as conn:
            row = conn.execute('select source,number,payload from notice_documents where sha256=? order by fetched_at desc limit 1', (digest,)).fetchone()
            if row is None:
                raise ValueError('Document not found in local state')
            return dict(source=row['source'], number=row['number'], **json.loads(row['payload']))

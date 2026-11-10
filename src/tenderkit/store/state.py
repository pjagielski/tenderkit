"""SQLite state: one transaction per successfully completed source window."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from tenderkit.serialization import dumps, notice_from_record, notice_record

SCHEMA = '''
create table if not exists notices (
    source text not null, number text not null, published text,
    payload text not null, coverage text not null default 'PENDING',
    primary key(source, number)
);
create index if not exists notices_published on notices(published, source, number);
create table if not exists refreshes (
    id integer primary key, source text not null, start text not null, end text not null,
    query text not null, status text not null, count integer not null,
    error text, finished_at text not null
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
create table if not exists fetch_jobs (
    id integer primary key, source text not null, number text not null, selection text,
    status text not null, attempts integer not null default 0, error text, error_kind text,
    next_attempt real not null default 0, heartbeat real, summary text not null default '{}'
);
create unique index if not exists active_document_job on fetch_jobs(source,number)
    where status in ('QUEUED','RUNNING');
'''


class State:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def record_refresh(self, source, start, end, query, notices=None, error=None):
        with self.connection() as conn:
            for notice in notices or []:
                conn.execute('''insert into notices(source,number,published,payload) values(?,?,?,?)
                    on conflict(source,number) do update set published=excluded.published,payload=excluded.payload''',
                    (notice.source, notice.number, str(notice.published) if notice.published else None,
                     dumps(notice_record(notice))))
            conn.execute('''insert into refreshes(source,start,end,query,status,count,error,finished_at)
                values(?,?,?,?,?,?,?,?)''', (source, str(start), str(end), dumps(query),
                    'FAILED' if error else 'COMPLETED', len(notices or []), error,
                    datetime.now(timezone.utc).isoformat()))

    def notices(self, start=None, end=None, sources=()):
        conditions, params = [], []
        if start:
            conditions.append('published >= ?')
            params.append(str(start))
        if end:
            conditions.append('published <= ?')
            params.append(str(end))
        if sources:
            conditions.append('source in (' + ','.join('?' for _ in sources) + ')')
            params.extend(sources)
        where = ' where ' + ' and '.join(conditions) if conditions else ''
        with self.connection() as conn:
            for row in conn.execute('select payload from notices' + where + ' order by published desc,source,number', params):
                yield notice_from_record(json.loads(row['payload']))

    def get(self, source, number):
        with self.connection() as conn:
            row = conn.execute('select payload from notices where source=? and number=?', (source, number)).fetchone()
            return notice_from_record(json.loads(row['payload'])) if row else None

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
            return dict(counts=counts, refreshes=refreshes, jobs=jobs)

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

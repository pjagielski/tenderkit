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
            return dict(counts=counts, refreshes=refreshes)

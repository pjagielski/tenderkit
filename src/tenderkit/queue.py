"""Durable bounded download jobs. An OS lock releases automatically if a worker dies."""
from contextlib import contextmanager
import json
import os
import time

from tenderkit.errors import classify_error
from tenderkit.limits import MAX_ATTEMPTS, MAX_DOCUMENTS, MAX_JOBS
from tenderkit.serialization import dumps


@contextmanager
def worker_lock(path):
    with open(str(path) + '.documents.lock', 'a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Queue:
    def __init__(self, state):
        self.state = state

    def enqueue(self, source, number, keys=None):
        if self.state.get(source, number) is None:
            raise ValueError('Notice not found in local state')
        if keys is not None:
            if (not isinstance(keys, (tuple, list)) or not 1 <= len(keys) <= MAX_DOCUMENTS
                    or any(not isinstance(k, str) or not k or len(k) > 8000 for k in keys)):
                raise ValueError('Invalid document selection')
            keys = list(dict.fromkeys(keys))
        with self.state.connection() as conn:
            conn.execute('begin immediate')
            found = conn.execute("select * from fetch_jobs where source=? and number=? and status in ('QUEUED','RUNNING')", (source, number)).fetchone()
            if found:
                if json.loads(found['selection']) != keys:
                    raise ValueError('A job with a different document selection is already active')
                return found['id']
            return conn.execute('insert into fetch_jobs(source,number,selection,status) values(?,?,?,?) returning id',
                                 (source, number, dumps(keys), 'QUEUED')).fetchone()[0]

    def job(self, job_id):
        with self.state.connection() as conn:
            row = conn.execute('select * from fetch_jobs where id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('Download job not found')
            result = dict(row)
            result['selection'], result['summary'] = json.loads(result['selection']), json.loads(result['summary'])
            return result

    def recover(self):
        # Called only while holding the OS worker lock, so no live owner can be running.
        with self.state.connection() as conn:
            rows = conn.execute("select id,attempts from fetch_jobs where status='RUNNING'").fetchall()
            for row in rows:
                conn.execute('update fetch_jobs set status=?,error=?,error_kind=?,next_attempt=0 where id=?',
                    ('FAILED' if row['attempts'] >= MAX_ATTEMPTS else 'QUEUED', 'WORKER_LOST', 'TRANSIENT', row['id']))
                if row['attempts'] >= MAX_ATTEMPTS:
                    conn.execute("update notices set coverage='FETCH_FAILED' where (source,number)=(select source,number from fetch_jobs where id=?)", (row['id'],))
            return len(rows)

    def beat(self, job_id, summary):
        with self.state.connection() as conn:
            conn.execute('update fetch_jobs set heartbeat=?,summary=? where id=?', (time.time(), dumps(summary), job_id))

    def finish(self, job_id, error=None):
        job = self.job(job_id)
        kind = classify_error('FAILED' if error else 'DONE', error)
        retry = bool(error) and job['attempts'] < (MAX_ATTEMPTS if kind == 'TRANSIENT' else 2 if kind == 'UNCERTAIN' else 1)
        status = 'QUEUED' if retry else 'FAILED' if error else 'DONE'
        with self.state.connection() as conn:
            conn.execute('update fetch_jobs set status=?,error=?,error_kind=?,next_attempt=?,heartbeat=? where id=?',
                (status, error, kind, time.time() + 60 * job['attempts'] if retry else 0, time.time(), job_id))
        return self.job(job_id)

    def run(self, process, *, max_jobs=1, job_id=None):
        if type(max_jobs) is not int or not 1 <= max_jobs <= MAX_JOBS:
            raise ValueError('max_jobs must be between 1 and 20')
        results = []
        with worker_lock(self.state.path) as locked:
            if not locked:
                return dict(locked=False, jobs=[])
            recovered = self.recover()
            for _ in range(max_jobs):
                with self.state.connection() as conn:
                    conn.execute('begin immediate')
                    query = "select id from fetch_jobs where status='QUEUED' and next_attempt<=?"
                    args = [time.time()]
                    if job_id is not None:
                        query += ' and id=?'
                        args.append(job_id)
                    row = conn.execute(query + ' order by id limit 1', args).fetchone()
                    if row is None:
                        break
                    current = row[0]
                    conn.execute("update fetch_jobs set status='RUNNING',attempts=attempts+1,heartbeat=? where id=?", (time.time(), current))
                error = None
                try:
                    process(self.job(current))
                except Exception as exc:
                    error = getattr(exc, 'code', type(exc).__name__)
                results.append(self.finish(current, error))
        return dict(locked=True, recovered=recovered, jobs=results)

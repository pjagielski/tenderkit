"""Durable source-window refreshes with a process-local background runner.

The OS lock owns recovery across server instances. Completed sources and their
notice writes commit together; an interrupted source can safely be retried.
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import logging
from threading import Event, Lock, Thread

from tenderkit.profile import profile_from_dict
from tenderkit.queue import worker_lock
from tenderkit.serialization import dumps
from tenderkit.service import refresh_source, sources, window


def now():
    return datetime.now(timezone.utc).isoformat()


class RefreshJobs:
    def __init__(self, state, adapters=None, refresh_lock=None):
        self.state = state
        self.adapters = adapters
        self.refresh_lock = refresh_lock or Lock()
        self._guard, self._wake, self._thread = Lock(), Event(), None

    def start(self, profile, start=None, end=None, *, source_names=None, include_results=False):
        start, end = window(start, end)
        available = sources() if self.adapters is None else self.adapters
        selected = sorted(available if source_names is None else set(source_names))
        if not selected or len(selected) > 20 or any(name not in available for name in selected):
            raise ValueError('Select between 1 and 20 registered sources')
        request = dict(profile=asdict(profile), start=str(start), end=str(end),
                       sources=selected, include_results=include_results)
        payload = dumps(request)
        key = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        with self.state.connection() as conn:
            conn.execute('begin immediate')
            active = conn.execute("select id from refresh_jobs where request_key=? and status in ('QUEUED','RUNNING')", (key,)).fetchone()
            if active:
                return self.status(active['id'])
            pending = conn.execute("select count(*) from refresh_jobs where status in ('QUEUED','RUNNING')").fetchone()[0]
            if pending >= 20:
                raise ValueError('Refresh queue limit reached')
            job_id = conn.execute('''insert into refresh_jobs(request_key,request,status,created_at)
                values(?,?,?,?) returning id''', (key, payload, 'QUEUED', now())).fetchone()[0]
            conn.executemany('insert into refresh_job_sources(job_id,source,status) values(?,?,?)',
                             [(job_id, name, 'QUEUED') for name in selected])
        return self.status(job_id)

    def status(self, job_id):
        if type(job_id) is not int or job_id < 1:
            raise ValueError('Invalid refresh job id')
        with self.state.connection() as conn:
            job = conn.execute('select * from refresh_jobs where id=?', (job_id,)).fetchone()
            if job is None:
                raise ValueError('Refresh job not found')
            rows = [dict(row) for row in conn.execute(
                'select source,status,attempts,observed,saved,error,updated_at from refresh_job_sources where job_id=? order by source', (job_id,))]
        request = json.loads(job['request'])
        return dict(job_id=job_id, status=job['status'], profile=request['profile']['name'],
                    requested_window=dict(start=request['start'], end=request['end']),
                    include_results=request['include_results'], sources=rows,
                    completed_sources=sum(row['status'] == 'COMPLETED' for row in rows),
                    total_sources=len(rows), created_at=job['created_at'], finished_at=job['finished_at'])

    def resume(self, job_id):
        self.status(job_id)
        with worker_lock(self.state.path, suffix='.refresh.lock') as acquired:
            if acquired:
                self.recover()
        with self.state.connection() as conn:
            conn.execute('begin immediate')
            job = conn.execute('select * from refresh_jobs where id=?', (job_id,)).fetchone()
            if job['status'] in ('QUEUED', 'RUNNING', 'COMPLETED'):
                return self.status(job_id)
            active = conn.execute("select id from refresh_jobs where request_key=? and status in ('QUEUED','RUNNING')", (job['request_key'],)).fetchone()
            if active:
                raise ValueError(f'A matching refresh job is already active: {active[0]}')
            pending = conn.execute("select count(*) from refresh_jobs where status in ('QUEUED','RUNNING')").fetchone()[0]
            if pending >= 20:
                raise ValueError('Refresh queue limit reached')
            conn.execute("update refresh_job_sources set status='QUEUED' where job_id=? and status!='COMPLETED'", (job_id,))
            conn.execute("update refresh_jobs set status='QUEUED',finished_at=null where id=?", (job_id,))
        return self.status(job_id)

    def kick(self):
        """Never wait for an adapter in the MCP request thread."""
        with self._guard:
            self._wake.set()
            if self._thread is None:
                self._thread = Thread(target=self._background, name='tenderkit-refresh', daemon=True)
                self._thread.start()

    def _background(self):
        while True:
            self._wake.clear()
            try:
                self.run_pending()
            except Exception:
                logging.getLogger(__name__).exception('Background refresh stopped; poll or resume to recover')
            with self._guard:
                if not self._wake.is_set():
                    self._thread = None
                    return

    def recover(self):
        """Only call while owning the OS worker lock: RUNNING has no live owner."""
        with self.state.connection() as conn:
            conn.execute('begin immediate')
            conn.execute("""update refresh_job_sources set status='INTERRUPTED',error='WORKER_LOST',updated_at=?
                where status='RUNNING'""", (now(),))
            conn.execute("""update refresh_jobs set status=case when exists
                (select 1 from refresh_job_sources where job_id=refresh_jobs.id and status!='COMPLETED')
                then 'INTERRUPTED' else 'COMPLETED' end,finished_at=? where status='RUNNING'""", (now(),))

    def run_pending(self):
        with worker_lock(self.state.path, suffix='.refresh.lock') as acquired:
            if not acquired:
                return
            self.recover()
            available = sources() if self.adapters is None else self.adapters
            while True:
                with self.state.connection() as conn:
                    conn.execute('begin immediate')
                    row = conn.execute("select * from refresh_jobs where status='QUEUED' order by id limit 1").fetchone()
                    if row is None:
                        return
                    conn.execute("update refresh_jobs set status='RUNNING' where id=?", (row['id'],))
                request = json.loads(row['request'])
                profile = profile_from_dict(request['profile'])
                start, end = window(request['start'], request['end'])
                for source in request['sources']:
                    with self.state.connection() as conn:
                        changed = conn.execute("""update refresh_job_sources set status='RUNNING',attempts=attempts+1,
                            observed=0,error=null,updated_at=? where job_id=? and source=? and status='QUEUED'""",
                            (now(), row['id'], source)).rowcount
                    if not changed:
                        continue
                    query = replace(profile.source_query(), include_results=request['include_results'],
                                    include_amendments=source == 'BZP')
                    def progress(count):
                        with self.state.connection() as conn:
                            conn.execute('update refresh_job_sources set observed=?,updated_at=? where job_id=? and source=?',
                                         (count, now(), row['id'], source))
                    if source not in available:
                        self.state.record_refresh(source, start, end, asdict(query), error='SOURCE_UNAVAILABLE', job_id=row['id'])
                        continue
                    with self.refresh_lock:
                        refresh_source(self.state, source, available[source], start, end, query,
                                       progress=progress, job_id=row['id'])
                with self.state.connection() as conn:
                    failed = conn.execute("select 1 from refresh_job_sources where job_id=? and status!='COMPLETED'", (row['id'],)).fetchone()
                    completed = conn.execute("select 1 from refresh_job_sources where job_id=? and status='COMPLETED'", (row['id'],)).fetchone()
                    conn.execute('update refresh_jobs set status=?,finished_at=? where id=?',
                                 ('COMPLETED' if not failed else 'PARTIAL' if completed else 'FAILED', now(), row['id']))

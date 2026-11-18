"""Durable refresh lifecycle, partial results, recovery and nonblocking MCP requests."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import importlib.util
from pathlib import Path
import tempfile
from threading import Event
import unittest

from tenderkit.model import Award, Notice
from tenderkit.profile import Profile
from tenderkit.queue import worker_lock
from tenderkit.refresh_jobs import RefreshJobs
from tenderkit.service import search_results
from tenderkit.store.state import State

START = END = date(2026, 10, 1)


class Adapter:
    def __init__(self, source, error=None, gate=None):
        self.source, self.error, self.gate = source, error, gate
        self.calls, self.entered = [], Event()

    def fetch(self, start, end, query):
        self.calls.append((start, end, query))
        self.entered.set()
        yield Notice(self.source, self.source + '-result', published=start, cpv=('33600000',),
                     notice_type='TenderResultNotice' if self.source == 'BZP' else 'can-standard',
                     awards=(Award('1', 'AWARDED', contractor='Example'),))
        if self.gate is not None and not self.gate.wait(5):
            raise TimeoutError('test gate')
        if self.error:
            raise self.error


class RefreshJobTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = State(Path(temp.name) / 'state.sqlite3')
        self.bzp, self.ted = Adapter('BZP'), Adapter('TED', TimeoutError())
        self.adapters = {'BZP': self.bzp, 'TED': self.ted}
        self.jobs = RefreshJobs(self.state, self.adapters)
        self.profile = Profile('leki', cpv=('336*',))

    def start(self):
        return self.jobs.start(self.profile, START, END, include_results=True)['job_id']

    def test_partial_results_timeout_resume_and_completed_source_idempotence(self):
        job_id = self.start()
        self.assertEqual(self.start(), job_id)
        self.jobs.run_pending()
        job = self.jobs.status(job_id)
        self.assertEqual((job['status'], job['completed_sources']), ('PARTIAL', 1))
        self.assertEqual([(r['status'], r['observed'], r['saved']) for r in job['sources']],
                         [('COMPLETED', 1, 1), ('TIMED_OUT', 1, 0)])
        self.assertEqual(search_results(self.state)['total'], 1)
        self.assertIsNone(self.state.get('TED', 'TED-result'))
        self.jobs.run_pending()  # Polling never retries failures.
        self.assertEqual(len(self.ted.calls), 1)
        self.ted.error = None
        self.jobs.resume(job_id)
        self.jobs.run_pending()
        self.assertEqual(self.jobs.status(job_id)['status'], 'COMPLETED')
        self.assertEqual((len(self.bzp.calls), len(self.ted.calls)), (1, 2))
        self.assertEqual(search_results(self.state)['total'], 2)
        self.jobs.resume(job_id)
        self.jobs.run_pending()
        self.assertEqual((len(self.bzp.calls), len(self.ted.calls)), (1, 2))

    def test_interrupted_worker_recovery_preserves_success_and_request_snapshot(self):
        job_id = self.start()
        self.jobs.run_pending()
        with self.state.connection() as conn:
            conn.execute("update refresh_jobs set status='RUNNING' where id=?", (job_id,))
            conn.execute("update refresh_job_sources set status='RUNNING' where job_id=? and source='TED'", (job_id,))
        self.ted.error = None
        reopened = RefreshJobs(State(self.state.path), self.adapters)
        reopened.run_pending()
        self.assertEqual(reopened.status(job_id)['status'], 'INTERRUPTED')
        self.assertEqual(reopened.status(job_id)['sources'][1]['error'], 'WORKER_LOST')
        reopened.resume(job_id)
        reopened.run_pending()
        self.assertEqual(reopened.status(job_id)['status'], 'COMPLETED')
        self.assertEqual(len(self.bzp.calls), 1)
        self.assertEqual(self.ted.calls[-1], (START, END, self.ted.calls[0][2]))

    def test_resume_can_recover_lost_running_job_directly(self):
        job_id = self.start()
        with self.state.connection() as conn:
            conn.execute("update refresh_jobs set status='RUNNING' where id=?", (job_id,))
        self.assertEqual(self.jobs.resume(job_id)['status'], 'QUEUED')

    def test_os_lock_prevents_competing_worker_and_false_recovery(self):
        job_id = self.start()
        with worker_lock(self.state.path, suffix='.refresh.lock') as acquired:
            self.assertTrue(acquired)
            RefreshJobs(self.state, self.adapters).run_pending()
            self.assertEqual(self.jobs.status(job_id)['status'], 'QUEUED')
        self.assertEqual(len(self.bzp.calls), 0)

    def test_completed_source_and_notices_are_one_transaction(self):
        job_id = self.jobs.start(self.profile, START, END, source_names=['BZP'])['job_id']
        with self.state.connection() as conn:
            conn.execute("""create trigger fail_job_commit before update on refresh_job_sources
                when new.status='COMPLETED' begin select raise(abort, 'simulated commit failure'); end""")
        self.jobs.run_pending()
        self.assertEqual(self.jobs.status(job_id)['sources'][0]['status'], 'FAILED')
        self.assertEqual(self.jobs.status(job_id)['status'], 'FAILED')
        self.assertIsNone(self.state.get('BZP', 'BZP-result'))
        self.assertEqual(self.state.coverage()['refreshes'][0]['status'], 'FAILED')

    def test_missing_adapter_after_restart_fails_explicitly(self):
        job_id = self.start()
        reopened = RefreshJobs(self.state, {'BZP': self.bzp})
        reopened.run_pending()
        self.assertEqual(reopened.status(job_id)['sources'][1]['error'], 'SOURCE_UNAVAILABLE')

    def test_invalid_requests_do_not_enqueue(self):
        for names in ([], ['unknown']):
            with self.assertRaises(ValueError):
                self.jobs.start(self.profile, START, END, source_names=names)
        with self.assertRaises(ValueError):
            self.jobs.start(self.profile, date(2025, 1, 1), END)
        for job_id in (-1, 99, True):
            with self.assertRaises(ValueError):
                self.jobs.status(job_id)
        with self.state.connection() as conn:
            self.assertEqual(conn.execute('select count(*) from refresh_jobs').fetchone()[0], 0)

    def test_concurrent_identical_submissions_share_one_job(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: self.start(), range(2)))
        self.assertEqual(ids[0], ids[1])
        with self.state.connection() as conn:
            self.assertEqual(conn.execute('select count(*) from refresh_jobs').fetchone()[0], 1)

    def test_crash_after_last_source_commit_does_not_repeat_sources(self):
        self.ted.error = None
        job_id = self.start()
        self.jobs.run_pending()
        with self.state.connection() as conn:
            conn.execute("update refresh_jobs set status='RUNNING' where id=?", (job_id,))
        self.jobs.run_pending()
        self.assertEqual(self.jobs.status(job_id)['status'], 'COMPLETED')
        self.assertEqual((len(self.bzp.calls), len(self.ted.calls)), (1, 1))

    def test_wrapped_network_timeout_is_exposed_as_timeout(self):
        error = RuntimeError('source wrapper')
        error.__cause__ = TimeoutError()
        self.bzp.error = error
        job_id = self.jobs.start(self.profile, START, END, source_names=['BZP'])['job_id']
        self.jobs.run_pending()
        self.assertEqual(self.jobs.status(job_id)['sources'][0]['status'], 'TIMED_OUT')


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'Install the optional MCP extra')
class McpRefreshJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_source_does_not_block_polling_or_partial_search(self):
        from mcp.client import Client
        from tenderkit.mcp import create_server
        gate = Event()
        bzp, ted = Adapter('BZP'), Adapter('TED', gate=gate)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite3'
            server = create_server(path, adapters={'BZP': bzp, 'TED': ted})
            try:
                async with Client(server, read_timeout_seconds=2) as client:
                    response = await asyncio.wait_for(client.call_tool('start_refresh', {
                        'profile': 'leki', 'start': str(START), 'end': str(END), 'include_results': True}), 2)
                    self.assertFalse(response.is_error)
                    job_id = response.structured_content['job_id']
                    self.assertTrue(await asyncio.to_thread(ted.entered.wait, 2))
                    status = await client.call_tool('refresh_status', {'job_id': job_id})
                    self.assertEqual(status.structured_content['completed_sources'], 1)
                    self.assertEqual(status.structured_content['sources'][1]['status'], 'RUNNING')
                    resumed = await client.call_tool('resume_refresh', {'job_id': job_id})
                    self.assertEqual(resumed.structured_content['status'], 'RUNNING')
                    partial = await client.call_tool('search_results', {'profile': 'leki'})
                    self.assertEqual(partial.structured_content['total'], 1)
                    gate.set()
                    for _ in range(100):
                        status = await client.call_tool('refresh_status', {'job_id': job_id})
                        if status.structured_content['status'] == 'COMPLETED':
                            break
                        await asyncio.sleep(.02)
                    self.assertEqual(status.structured_content['status'], 'COMPLETED')
                    self.assertEqual(len(bzp.calls), 1)
                    self.assertTrue((await client.call_tool('start_refresh', {'profile': 'unknown'})).is_error)
                    readonly = create_server(path, read_only=True)
                    async with Client(readonly) as observer:
                        names = {t.name for t in (await observer.list_tools()).tools}
                        self.assertFalse(names & {'start_refresh', 'refresh_status', 'resume_refresh'})
            finally:
                gate.set()


if __name__ == '__main__':
    unittest.main()

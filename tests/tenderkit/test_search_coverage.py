"""Search coverage distinguishes absent data from empty results and respects acquisition scopes."""
from dataclasses import asdict
from datetime import date
from pathlib import Path
import tempfile
import unittest
import importlib.util

from tenderkit.interfaces import SourceQuery
from tenderkit.mcp import create_server
from tenderkit.profile import Profile
from tenderkit.service import search, search_results
from tenderkit.store.state import State


class SearchCoverageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = State(Path(temp.name) / 'state.sqlite3')
        self.profile = Profile('software', cpv=('72*',), notice_types=('ContractNotice',))

    def record(self, start=1, end=10, cpv=('72',), kinds=('ContractNotice',), source='BZP', error=None, results=False):
        self.state.record_refresh(source, date(2026, 10, start), date(2026, 10, end),
            asdict(SourceQuery(cpv, kinds, include_results=results)), [], error=error)

    def coverage(self, start=1, end=10, **kwargs):
        return search(self.state, self.profile, start=date(2026, 10, start), end=date(2026, 10, end),
                      **kwargs)['coverage']

    def test_empty_results_have_full_coverage_after_successful_empty_fetch(self):
        self.record()
        report = self.coverage()
        self.assertEqual(report['requested_window'], {'from': '2026-10-01', 'to': '2026-10-10'})
        self.assertEqual(report['sources']['BZP']['coverage_status'], 'full')
        self.assertEqual(report['sources']['TED']['coverage_status'], 'not_applicable')
        self.assertEqual(len(report['sources']['BZP']['covered_windows']), 1)

    def test_old_quarter_outside_current_cache(self):
        self.record()
        result = search(self.state, self.profile, start='2025-01-01', end='2025-03-31')
        self.assertEqual(result['notices'], [])
        self.assertEqual(result['coverage']['sources']['BZP']['coverage_status'], 'outside_coverage')

    def test_gaps_failures_and_latest_attempt(self):
        self.record(end=4)
        self.record(start=6)
        self.record(start=5, end=5, error='TimeoutError')
        bzp = self.coverage()['sources']['BZP']
        self.assertEqual(bzp['coverage_status'], 'partial')
        self.assertEqual(len(bzp['covered_windows']), 2)
        self.assertEqual(bzp['last_refresh']['status'], 'FAILED')
        self.record(start=5, end=5)
        self.assertEqual(self.coverage()['sources']['BZP']['coverage_status'], 'full')

    def test_cpv_scope_is_not_inferred_from_dates(self):
        self.record(cpv=('336',))
        self.assertEqual(self.coverage()['sources']['BZP']['coverage_status'], 'outside_coverage')
        self.record(cpv=('722',))
        bzp = self.coverage()['sources']['BZP']
        self.assertEqual(bzp['coverage_status'], 'partial')
        self.assertEqual(bzp['covered_windows'], [])
        self.record(cpv=())
        self.assertEqual(self.coverage()['sources']['BZP']['coverage_status'], 'full')

    def test_multiple_requested_prefixes_and_types_need_all_components(self):
        self.profile = Profile('two', cpv=('72*', '48*'), notice_types=('ContractNotice', 'TenderResultNotice'))
        self.record()
        self.record(cpv=('48',))
        self.assertEqual(self.coverage()['sources']['BZP']['coverage_status'], 'partial')
        self.record(cpv=(), kinds=('TenderResultNotice',))
        self.assertEqual(self.coverage()['sources']['BZP']['coverage_status'], 'full')

    def test_results_require_result_refresh_and_ignore_contract_only_profile_types(self):
        self.record()
        def result():
            return search_results(self.state, profile=self.profile, start='2026-10-01', end='2026-10-10')['coverage']
        self.assertEqual(result()['sources']['BZP']['coverage_status'], 'outside_coverage')
        self.record(results=True)
        self.assertEqual(result()['sources']['BZP']['coverage_status'], 'full')
        self.assertEqual(result()['sources']['TED']['coverage_status'], 'outside_coverage')

    def test_unbounded_search_never_claims_full_and_source_filter_is_respected(self):
        self.record()
        result = search(self.state, self.profile, source_names=('BZP',))['coverage']
        self.assertEqual(result['requested_window'], {'from': None, 'to': None})
        self.assertEqual(set(result['sources']), {'BZP'})
        self.assertEqual(result['sources']['BZP']['coverage_status'], 'partial')

    def test_ted_default_types_and_bk_are_reported_separately(self):
        self.record(source='TED', kinds=())
        self.record(source='BK', kinds=())
        self.profile = Profile('all-contracts', cpv=('72*',), notice_kinds=('contract',))
        report = self.coverage()['sources']
        self.assertEqual(report['TED']['coverage_status'], 'full')
        self.assertEqual(report['BK']['coverage_status'], 'full')
        self.assertEqual(report['BZP']['coverage_status'], 'outside_coverage')

    def test_clipping_wrong_notice_types_and_no_refresh_history(self):
        self.assertEqual(self.coverage()['sources']['BZP']['coverage_status'], 'outside_coverage')
        self.record(kinds=('TenderResultNotice',))
        self.assertEqual(self.coverage()['sources']['BZP']['coverage_status'], 'outside_coverage')
        self.record()
        self.assertEqual(self.coverage(3, 4)['sources']['BZP']['covered_windows'],
                         [{'from': '2026-10-03', 'to': '2026-10-04'}])
        self.assertEqual(self.coverage(1, 11)['sources']['BZP']['coverage_status'], 'partial')

    def test_history_is_not_limited_to_the_last_fifty_refreshes(self):
        self.record()
        for _ in range(51):
            self.record(cpv=('336',), error='TimeoutError')
        self.assertEqual(self.coverage()['sources']['BZP']['coverage_status'], 'full')


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'Install the optional MCP extra')
class McpSearchCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_response_uses_new_success_and_failure_records(self):
        from mcp.client import Client
        class Adapter:
            def __init__(self, fails=False):
                self.fails = fails

            def fetch(self, start, end, query):
                if self.fails:
                    raise TimeoutError('test failure')
                return []

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite3'
            server = create_server(path, adapters={'BZP': Adapter(), 'TED': Adapter(fails=True)})
            async with Client(server) as client:
                response = await client.call_tool('search_results', {'profile': 'leki', 'refresh': True,
                    'start': '2026-10-01', 'end': '2026-10-10'})
                self.assertFalse(response.is_error)
                report = response.structured_content['coverage']['sources']
                self.assertEqual(report['BZP']['coverage_status'], 'full')
                self.assertEqual(report['TED']['coverage_status'], 'outside_coverage')
                self.assertEqual(report['TED']['last_refresh']['status'], 'FAILED')

    async def test_read_only_mcp_exposes_coverage_on_empty_paginated_searches(self):
        from mcp.client import Client
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite3'
            state = State(path)
            state.record_refresh('BZP', date(2026, 10, 1), date(2026, 10, 10),
                                 asdict(SourceQuery(('336',), include_results=True)), [])
            server = create_server(path, read_only=True)
            async with Client(server) as client:
                for tool in ('search_notices', 'search_results'):
                    response = await client.call_tool(tool, {'profile': 'leki', 'start': '2025-01-01',
                                                           'end': '2025-03-31', 'offset': 10})
                    self.assertFalse(response.is_error)
                    coverage = response.structured_content['coverage']
                    self.assertEqual(coverage['sources']['BZP']['coverage_status'], 'outside_coverage')
                    self.assertEqual(coverage['sources']['BZP']['last_refresh']['status'], 'COMPLETED')


if __name__ == '__main__':
    unittest.main()

"""Official SDK client against the real server, including its stdio entry point."""
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tenderkit.model import Award, Lot, Notice
from tenderkit.profiles import builtin_profiles
from tenderkit.service import search
from tenderkit.store.state import State


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'Install the optional MCP extra')
class McpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.state = State(self.path)
        self.profile = builtin_profiles()['leki']
        self.notice = Notice('TED', 'one', procedure_id='proc-1', title='Dostawa leków', cpv=('33600000',),
            published=date(2026, 11, 1), notice_type='cn-standard',
            platform_url='https://platformazakupowa.pl/transakcja/1', lots=(Lot('LOT-0001'),))
        self.result = Notice('TED', 'two', procedure_id='proc-1', title='Wynik: dostawa leków', cpv=('33600000',),
            published=date(2026, 11, 10), notice_type='can-standard',
            awards=(Award('LOT-0001', 'AWARDED', contractor='Acme', contractor_id='111',
                          price=Decimal('1000'), currency='PLN', offers=2, date=date(2026, 11, 10)),))
        self.state.record_refresh('TED', date(2026, 11, 1), date(2026, 11, 10), asdict(self.profile.source_query()),
                                  [self.notice, self.result])

    async def check_client(self, client):
        tools = await client.list_tools()
        self.assertEqual({t.name for t in tools.tools}, {'search_notices', 'get_notice', 'procedure_results',
                                                         'search_results', 'list_documents', 'fetch_documents', 'read_document'})
        result = await client.call_tool('search_notices', {'profile': 'leki'})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content['notices'], search(self.state, self.profile)['notices'])
        notice = await client.call_tool('get_notice', {'source': 'TED', 'number': 'one'})
        self.assertEqual(notice.structured_content['notice']['lots'][0]['number'], 'LOT-0001')
        procedure = await client.call_tool('procedure_results', {'source': 'TED', 'number': 'two'})
        self.assertEqual(procedure.structured_content['awards'][0]['contractor'], 'Acme')
        self.assertEqual(procedure.structured_content['contracts'][0]['number'], 'one')
        awards = await client.call_tool('search_results', {'cpv': ['336*']})
        self.assertEqual(awards.structured_content['awards'][0]['contractor'], 'Acme')
        resources = await client.list_resources()
        self.assertEqual({str(r.uri) for r in resources.resources}, {'tenderkit://profiles', 'tenderkit://coverage'})
        coverage = await client.read_resource('tenderkit://coverage')
        self.assertEqual(json.loads(coverage.contents[0].text)['counts'][0]['count'], 2)

    async def test_in_memory_client_uses_the_cli_filter_and_profile_resources(self):
        from mcp.client import Client
        from tenderkit.mcp import create_server
        server = create_server(self.path)
        async with Client(server) as client:
            await self.check_client(client)
            for args in ({'profile': '../../private.toml'}, {'limit': 501}, {'regions': ['invalid']},
                         {'refresh': True, 'start': '2000-01-01', 'end': '2026-11-01'}):
                response = await client.call_tool('search_notices', args)
                self.assertTrue(response.is_error, args)

    async def test_stdio_client_initialization_discovery_tools_and_resources(self):
        from mcp.client import Client
        from mcp.client.stdio import StdioServerParameters
        root = Path(__file__).resolve().parents[2]
        params = StdioServerParameters(command=sys.executable,
            args=['-B', '-m', 'tenderkit', '--db', str(self.path), 'mcp'], cwd=root,
            env={**os.environ, 'PYTHONUTF8': '1'})
        async with Client(params, read_timeout_seconds=20, mode='legacy') as client:
            await self.check_client(client)

    async def test_refresh_is_bounded_and_records_source_failures(self):
        from mcp.client import Client
        from tenderkit.mcp import create_server
        adapter = mock.Mock()
        adapter.fetch.return_value = [self.notice]
        server = create_server(self.path, adapters={'TED': adapter})
        async with Client(server) as client:
            result = await client.call_tool('search_notices', dict(profile='leki', refresh=True,
                start='2026-11-01', end='2026-11-01'))
            self.assertEqual(result.structured_content['refreshes'][0]['status'], 'COMPLETED')
            adapter.fetch.side_effect = RuntimeError('private source detail')
            result = await client.call_tool('search_notices', dict(profile='leki', refresh=True,
                start='2026-11-01', end='2026-11-01'))
            self.assertEqual(result.structured_content['refreshes'][0], dict(source='TED', status='FAILED', error='RuntimeError'))
            self.assertEqual(len(result.structured_content['notices']), 1)

    async def test_search_results_refreshes_results_with_a_profile(self):
        from mcp.client import Client
        from tenderkit.mcp import create_server
        award = Notice('TED', 'three', procedure_id='proc-3', title='Wynik', cpv=('33600000',),
                       published=date(2026, 11, 2), notice_type='can-standard',
                       awards=(Award('1', 'AWARDED', contractor='Beta', price=Decimal('500'),
                                     currency='PLN', offers=1, date=date(2026, 11, 2)),))
        adapter = mock.Mock()
        adapter.fetch.return_value = [award]
        server = create_server(self.path, adapters={'TED': adapter})
        async with Client(server) as client:
            refused = await client.call_tool('search_results', {'refresh': True, 'cpv': ['336*']})
            self.assertTrue(refused.is_error)
            result = await client.call_tool('search_results', dict(profile='leki', refresh=True,
                start='2026-11-02', end='2026-11-02', cpv=['336*']))
            self.assertEqual(result.structured_content['refreshes'][0]['status'], 'COMPLETED')
            self.assertEqual(result.structured_content['awards'][0]['contractor'], 'Beta')

    async def test_f2_list_fetch_and_read_share_the_queue_and_store(self):
        from mcp.client import Client
        from tenderkit.mcp import create_server
        from tests.tenderkit import test_f2 as fixtures
        path = fixtures.docx(Path(self.temp.name) / 'source.docx')
        adapter = fixtures.FakePlatform()
        adapter.files = {'SWZ.docx': path.read_bytes()}
        server = create_server(self.path, document_adapters={adapter.name: adapter})
        async with Client(server) as client:
            listed = await client.call_tool('list_documents', {'source': 'TED', 'number': 'one'})
            self.assertEqual(listed.structured_content['documents'][0]['key'], 'SWZ.docx')
            fetched = await client.call_tool('fetch_documents', {'source': 'TED', 'number': 'one', 'keys': ['SWZ.docx']})
            self.assertEqual(fetched.structured_content['status'], 'DONE')
            record = self.state.documents('TED', 'one')['SWZ.docx']
            read = await client.call_tool('read_document', {'document_id': record['sha256']})
            self.assertIn('Public SWZ text', read.structured_content['pages'][0]['text'])
            self.assertIn('untrusted', read.structured_content['provenance'].lower())
            self.assertTrue((await client.call_tool('read_document', {'document_id': '../../outside'})).is_error)
            repeat = await client.call_tool('fetch_documents', {'source': 'TED', 'number': 'one'})
            self.assertEqual(repeat.structured_content['summary']['known'], 1)


if __name__ == '__main__':
    unittest.main()

"""Official SDK client against the real server, including its stdio entry point."""
from dataclasses import asdict, replace
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
            description='Preparaty onkologiczne',
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
                                                         'search_results', 'list_documents', 'fetch_documents', 'read_document',
                                                         'watch_updates', 'save_profile', 'delete_profile',
                                                         'start_refresh', 'refresh_status', 'resume_refresh'})
        by_name = {t.name: t for t in tools.tools}
        for tool in tools.tools:
            self.assertTrue(tool.title, tool.name)
            for name, schema in tool.input_schema['properties'].items():
                self.assertTrue(schema.get('description'), f'{tool.name}.{name}')
        self.assertTrue(by_name['get_notice'].annotations.read_only_hint)
        self.assertFalse(by_name['search_notices'].annotations.read_only_hint)   # refresh=true fetches
        self.assertTrue(by_name['fetch_documents'].annotations.open_world_hint)
        self.assertTrue(by_name['delete_profile'].annotations.destructive_hint)
        result = await client.call_tool('search_notices', {'profile': 'leki'})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content['notices'], search(self.state, self.profile)['notices'])
        matched = await client.call_tool('search_notices', {'profile': 'leki', 'keywords_any': ['onkologiczn']})
        self.assertFalse(matched.is_error)
        self.assertEqual(matched.structured_content['notices'][0]['keyword_matches'],
                         [{'keyword': 'onkologiczn', 'field': 'description'}])
        compact = await client.call_tool('search_notices', {'profile': 'leki', 'compact': True,
                                                             'keywords_any': ['onkologiczn'], 'limit': 1})
        self.assertFalse(compact.is_error)
        summary = compact.structured_content
        self.assertEqual((summary['total'], summary['next_offset']), (1, None))
        row, = summary['notices']
        self.assertEqual((row['number'], row['lot_count']), ('one', 1))
        self.assertFalse({'description', 'lots', 'lot_titles', 'awards'} & row.keys())
        self.assertEqual(row['keyword_matches'], [{'keyword': 'onkologiczn', 'field': 'description'}])
        beyond = await client.call_tool('search_notices', {'profile': 'leki', 'offset': 1})
        self.assertEqual((beyond.structured_content['notices'], beyond.structured_content['total']), ([], 1))
        notice = await client.call_tool('get_notice', {'source': 'TED', 'number': 'one'})
        self.assertEqual(notice.structured_content['notice']['lots'][0]['number'], 'LOT-0001')
        procedure = await client.call_tool('procedure_results', {'source': 'TED', 'number': 'two'})
        self.assertEqual(procedure.structured_content['awards'][0]['contractor'], 'Acme')
        self.assertEqual(procedure.structured_content['contracts'][0]['number'], 'one')
        awards = await client.call_tool('search_results', {'profile': 'leki'})
        self.assertEqual(awards.structured_content['awards'][0]['contractor'], 'Acme')
        prompts = await client.list_prompts()
        self.assertEqual({prompt.name for prompt in prompts.prompts}, {'analyze_it_tender', 'compare_public_bids'})
        for name, evidence in (('analyze_it_tender', 'evaluation criteria'),
                               ('compare_public_bids', 'net or gross')):
            prompt = await client.get_prompt(name, {'source': 'TED', 'number': 'one'})
            message, = prompt.messages
            self.assertIn('"number": "one"', message.content.text)
            self.assertIn(evidence, message.content.text)
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
            for args in ({'profile': '../../private.toml'}, {'limit': 501}, {'regions': ['invalid']}, {'keywords_any': ['on*log']},
                         {'refresh': True, 'start': '2000-01-01', 'end': '2026-11-01'}):
                response = await client.call_tool('search_notices', args)
                self.assertTrue(response.is_error, args)

    async def test_search_groups_procedure_publications_before_pagination(self):
        from dataclasses import replace
        from mcp.client import Client
        from tenderkit.mcp import create_server

        older = replace(self.notice, buyer_id='PL1234567890', reference='ABC 12/26')
        newer = replace(older, number='three', published=date(2026, 11, 12))
        bzp = replace(older, source='BZP', number='bzp-1', procedure_id='ocds-1',
                      notice_type='ContractNotice', buyer_id='123-456-78-90')
        independent = replace(older, number='four', procedure_id='proc-2', reference='ABC 13/26')
        for item in (older, newer, bzp, independent):
            self.state.record_refresh(item.source, item.published, item.published, {}, [item])

        async with Client(create_server(self.path)) as client:
            args = {'profile': 'leki', 'dedupe': 'procedure', 'compact': True, 'limit': 1}
            result = await client.call_tool('search_notices', args)
            self.assertFalse(result.is_error)
            page = result.structured_content
            self.assertEqual((page['total'], page['next_offset']), (2, 1))
            row, = page['notices']
            self.assertEqual(row['number'], 'three')
            self.assertCountEqual(row['other_publications'], [
                {'source': 'TED', 'number': 'one'}, {'source': 'BZP', 'number': 'bzp-1'},
            ])
            last = await client.call_tool('search_notices', dict(args, offset=1))
            self.assertEqual(last.structured_content['notices'][0]['number'], 'four')

    async def test_normalized_notice_kinds_apply_across_sources(self):
        from dataclasses import replace
        from mcp.client import Client
        from tenderkit.mcp import create_server

        bzp = replace(self.notice, source='BZP', number='bzp', notice_type='ContractNotice')
        correction = replace(self.notice, number='ted-change',
                             raw={'change-notice-version-identifier': ['previous-01']})
        for item in (bzp, correction):
            self.state.record_refresh(item.source, item.published, item.published, {}, [item])

        async with Client(create_server(self.path)) as client:
            for kind, expected in [('contract', {'one', 'bzp'}), ('corrigendum', {'ted-change'}), ('result', {'two'})]:
                result = await client.call_tool('search_notices', {'notice_kinds': [kind], 'compact': True})
                self.assertFalse(result.is_error)
                rows = result.structured_content['notices']
                self.assertEqual({row['number'] for row in rows}, expected)
                self.assertEqual({row['notice_kind'] for row in rows}, {kind})

    async def test_saved_profiles_update_live_clients_persist_and_restore_fallbacks(self):
        from mcp.client import Client
        from tenderkit.mcp import create_server

        writer = create_server(self.path)
        observer = create_server(self.path, read_only=True)
        async with Client(writer) as client, Client(observer) as other:
            criteria = {'cpv': ['336*'], 'keywords_any': ['lek'], 'notice_types': ['cn-standard']}
            created = await client.call_tool('save_profile', {'name': 'moje-leki', 'criteria': criteria})
            self.assertFalse(created.is_error)
            listed = await other.read_resource('tenderkit://profiles')
            self.assertIn('moje-leki', json.loads(listed.contents[0].text))
            found = await other.call_tool('search_notices', {'profile': 'moje-leki'})
            self.assertEqual(found.structured_content['notices'][0]['number'], 'one')
            awards = await other.call_tool('search_results', {'profile': 'moje-leki'})
            self.assertEqual(awards.structured_content['total'], 1)

            duplicate = await client.call_tool('save_profile', {'name': 'moje-leki', 'criteria': {}})
            self.assertTrue(duplicate.is_error)
            changed = await client.call_tool('save_profile', {
                'name': 'moje-leki', 'criteria': {'cpv': ['72*']}, 'overwrite': True})
            self.assertFalse(changed.is_error)
            found = await other.call_tool('search_notices', {'profile': 'moje-leki'})
            self.assertEqual(found.structured_content['notices'], [])
            # Replacing uses full criteria; previous inclusion terms have been cleared.
            listed = json.loads((await other.read_resource('tenderkit://profiles')).contents[0].text)
            self.assertEqual(listed['moje-leki']['keywords_any'], [])

            for name, invalid in [('invalid/name', {}), ('bad-cpv', {'cpv': ['invalid']}),
                                  ('bad-field', {'unexpected': True}), ('bad-name', {'name': 'different'})]:
                rejected = await client.call_tool('save_profile', {'name': name, 'criteria': invalid})
                self.assertTrue(rejected.is_error)

            refused = await client.call_tool('save_profile', {'name': 'leki', 'criteria': {'cpv': ['72*']}})
            self.assertTrue(refused.is_error)
            self.assertFalse((await client.call_tool('save_profile', {
                'name': 'leki', 'criteria': {'cpv': ['72*']}, 'overwrite': True})).is_error)
            removed = await client.call_tool('delete_profile', {'name': 'leki'})
            self.assertEqual(removed.structured_content['active_profile']['cpv'], ['336*'])
            found = await other.call_tool('search_notices', {'profile': 'leki'})
            self.assertEqual(found.structured_content['notices'][0]['number'], 'one')

        async with Client(create_server(self.path)) as reopened:
            found = await reopened.call_tool('search_notices', {'profile': 'moje-leki'})
            self.assertFalse(found.is_error)
            self.assertEqual(found.structured_content['notices'], [])
            self.assertFalse((await reopened.call_tool('delete_profile', {'name': 'moje-leki'})).is_error)
            self.assertTrue((await reopened.call_tool('search_notices', {'profile': 'moje-leki'})).is_error)
            self.assertTrue((await reopened.call_tool('delete_profile', {'name': 'moje-leki'})).is_error)
        self.assertEqual(State(self.path).saved_profiles(), {})

    async def test_watch_updates_reads_the_latest_run_without_consuming_events(self):
        from io import StringIO
        from mcp.client import Client
        from tenderkit.mcp import create_server
        from tenderkit.watch import run_watch

        adapter = mock.Mock()
        adapter.fetch.return_value = [self.notice]
        run_watch(self.state, self.profile, self.notice.published, self.notice.published,
                  {'TED': adapter}, StringIO())
        async with Client(create_server(self.path)) as client:
            for _ in range(2):
                response = await client.call_tool('watch_updates', {'profile': 'leki'})
                self.assertFalse(response.is_error)
                self.assertEqual(response.structured_content['events'][0]['notice']['number'], 'one')
        adapter.fetch.assert_called_once()

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
            self.assertEqual(adapter.fetch.call_args.args[2].cpv_prefixes, ('336',))
            for invalid in ({'profile': 'missing'}, {'profile': 'leki', 'cpv': ['invalid']},
                            {'profile': 'leki', 'limit': 501}):
                adapter.reset_mock()
                refused = await client.call_tool('search_results', dict(invalid, refresh=True,
                    start='2026-11-02', end='2026-11-02'))
                self.assertTrue(refused.is_error)
                adapter.fetch.assert_not_called()
            overridden = await client.call_tool('search_results', dict(profile='leki', cpv=['72*'], refresh=True,
                start='2026-11-02', end='2026-11-02'))
            self.assertFalse(overridden.is_error)
            self.assertEqual(overridden.structured_content['total'], 0)
            self.assertEqual(adapter.fetch.call_args.args[2].cpv_prefixes, ('72',))

    async def test_result_profiles_filter_awards_and_allow_explicit_overrides(self):
        from mcp.client import Client
        from tenderkit.mcp import create_server
        from tenderkit.profile import DeadlineRange, Profile

        for number, title, code in (
            ('software', 'Wynik: oprogramowanie', '72200000'),
            ('server', 'Wynik: serwer', '48820000'),
            ('reagent', 'Wynik: odczynniki', '33696500'),
            ('disinfectant', 'Wynik: dezynfekcja', '33631600'),
        ):
            item = replace(self.result, number=number, procedure_id=number, title=title, cpv=(code,))
            self.state.record_refresh(item.source, item.published, item.published, {}, [item])
        adapter = mock.Mock()
        configured = builtin_profiles()
        configured['custom'] = Profile('custom', cpv=('336*',), keywords_any=('lek',),
                                       exclude_keywords=('odczynnik', 'dezynfekcj'),
                                       notice_types=('ContractNotice',), notice_kinds=('contract',),
                                       deadline_days=DeadlineRange(min=1, max=5), open_now=True)
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                before = self.path.read_bytes()
                server = create_server(self.path, profiles=configured, adapters={'TED': adapter}, read_only=read_only)
                async with Client(server) as client:
                    for profile, expected in (('leki', {'two'}), ('oprogramowanie', {'software'}), ('custom', {'two'})):
                        result = await client.call_tool('search_results', {'profile': profile, 'limit': 1})
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content['total'], len(expected))
                        self.assertEqual({row['number'] for row in result.structured_content['awards']}, expected)
                    overridden = await client.call_tool('search_results', {'profile': 'leki', 'cpv': ['72*']})
                    self.assertEqual(overridden.structured_content['awards'][0]['number'], 'software')
                    cleared = await client.call_tool('search_results', {'profile': 'leki', 'exclude_cpv': []})
                    self.assertEqual(cleared.structured_content['total'], 3)
                    for invalid in ({'profile': 'missing'}, {'profile': 'leki', 'exclude_cpv': ['invalid']}):
                        self.assertTrue((await client.call_tool('search_results', invalid)).is_error)
                if read_only:
                    self.assertEqual(self.path.read_bytes(), before)
        adapter.fetch.assert_not_called()

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


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'Install the optional MCP extra')
class ReadOnlyMcpTests(unittest.IsolatedAsyncioTestCase):
    """A shared database served read-only: no refresh, watch, documents or prompts, and no writes."""
    setUp = McpTests.setUp

    async def check_read_only(self, client):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        self.assertEqual(set(tools), {'search_notices', 'get_notice', 'procedure_results', 'search_results'})
        self.assertNotIn('refresh', tools['search_notices'].input_schema['properties'])
        self.assertNotIn('refresh', tools['search_results'].input_schema['properties'])
        self.assertIn('profile', tools['search_results'].input_schema['properties'])
        self.assertNotIn('refresh', tools['search_notices'].description)
        for tool in tools.values():
            # Agents pick tools and arguments from these, and directories score them.
            self.assertTrue(tool.title, tool.name)
            self.assertTrue(tool.annotations.read_only_hint, tool.name)
            self.assertFalse(tool.annotations.open_world_hint, tool.name)
            for name, schema in tool.input_schema['properties'].items():
                self.assertTrue(schema.get('description'), f'{tool.name}.{name}')
        self.assertEqual((await client.list_prompts()).prompts, [])
        result = await client.call_tool('search_notices', {'profile': 'leki'})
        self.assertEqual(result.structured_content['notices'], search(self.state, self.profile)['notices'])
        self.assertEqual(result.structured_content['refreshes'], [])
        awards = await client.call_tool('search_results', {'profile': 'leki'})
        self.assertEqual(awards.structured_content['awards'][0]['contractor'], 'Acme')
        # The schema has no refresh; the SDK drops the stray argument before the tool runs.
        stray = await client.call_tool('search_notices', {'profile': 'leki', 'refresh': True})
        self.assertEqual(stray.structured_content['refreshes'], [])
        self.assertTrue((await client.call_tool('read_document', {'document_id': '0' * 64})).is_error)
        self.assertTrue((await client.call_tool('save_profile', {'name': 'remote', 'criteria': {}})).is_error)
        self.assertTrue((await client.call_tool('delete_profile', {'name': 'leki'})).is_error)
        coverage = await client.read_resource('tenderkit://coverage')
        self.assertEqual(json.loads(coverage.contents[0].text)['counts'][0]['count'], 2)

    async def test_read_only_server_publishes_only_search_and_result_tools(self):
        from mcp.client import Client
        from tenderkit.mcp import create_server
        adapter = mock.Mock()
        before = self.path.read_bytes()
        async with Client(create_server(self.path, adapters={'TED': adapter}, read_only=True)) as client:
            await self.check_read_only(client)
        adapter.fetch.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.path.parent / 'documents').exists())

    async def test_read_only_profiles_resource_accepts_database_without_saved_profiles_table(self):
        from mcp.client import Client
        from tenderkit.mcp import create_server
        with self.state.connection() as conn:
            conn.execute('drop table saved_profiles')
        before = self.path.read_bytes()
        async with Client(create_server(self.path, read_only=True)) as client:
            resource = await client.read_resource('tenderkit://profiles')
            self.assertIn('leki', json.loads(resource.contents[0].text))
        self.assertEqual(self.path.read_bytes(), before)

    def test_read_only_server_needs_an_existing_database(self):
        from tenderkit.mcp import create_server
        missing = self.path.parent / 'missing.sqlite3'
        with self.assertRaisesRegex(ValueError, 'Database not found'):
            create_server(missing, read_only=True)
        self.assertFalse(missing.exists())

    def test_cli_http_requires_read_only_and_http_options_require_http(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from tenderkit.cli import main
        for args in (['mcp', '--http'], ['mcp', '--port', '9000'], ['mcp', '--read-only', '--no-raw']):
            with redirect_stderr(StringIO()) as error:
                self.assertEqual(main(['--db', str(self.path), *args]), 2, args)
            self.assertIn('tenderkit:', error.getvalue())

    def test_http_options_allow_only_local_and_configured_hosts(self):
        from tenderkit.mcp import http_options
        security = http_options(host='0.0.0.0', allowed_hosts=['mcp.example.org'])['transport_security']
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertIn('mcp.example.org', security.allowed_hosts)
        self.assertIn('mcp.example.org:*', security.allowed_hosts)
        self.assertIn('https://mcp.example.org', security.allowed_origins)

    async def test_http_server_from_the_cli(self):
        import socket
        import subprocess
        import asyncio
        from mcp.client import Client
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        root = Path(__file__).resolve().parents[2]
        process = subprocess.Popen(
            [sys.executable, '-B', '-m', 'tenderkit', '--db', str(self.path), 'mcp', '--read-only', '--http',
             '--port', str(port)], cwd=root, env={**os.environ, 'PYTHONUTF8': '1'},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(process.wait, 10)
        self.addCleanup(process.terminate)
        for _ in range(100):
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', port)) == 0:
                    break
            await asyncio.sleep(0.1)
        async with Client(f'http://127.0.0.1:{port}/mcp', read_timeout_seconds=20) as client:
            await self.check_read_only(client)

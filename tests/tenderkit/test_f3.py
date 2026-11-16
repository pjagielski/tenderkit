"""A third industry travels through the existing source/filter/document interfaces."""
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from tenderkit.cli import main
from tenderkit.model import Notice
from tenderkit.profiles import resolve_profile
from tenderkit.service import refresh, search
from tenderkit.store.state import State
from tests.tenderkit.test_f2 import FakePlatform, docx


class MedicalDevicesTests(unittest.TestCase):
    def test_third_profile_includes_optics_but_not_drugs_or_substring_hits(self):
        profile = resolve_profile('wyroby-medyczne')
        self.assertEqual(profile.source_query().cpv_prefixes, ('331', '332', '334'))
        for cpv in ('33140000', '33200000', '33410000'):
            self.assertTrue(profile.matches(Notice('BZP', cpv, cpv=(cpv,), notice_type='ContractNotice')))
        for cpv in ('33600000', '71331000', '45331200'):
            self.assertFalse(profile.matches(Notice('BZP', cpv, cpv=(cpv,), notice_type='ContractNotice')))


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'Install the optional MCP extra')
class MedicalDevicesFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_to_cli_mcp_documents_and_idempotent_store(self):
        from mcp.client import Client
        from tenderkit.mcp import create_server
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = State(root / 'state.sqlite3')
            on = date(2026, 11, 1)
            accepted = Notice('BZP', 'medical-fixture', title='Dostawa rękawic diagnostycznych',
                published=on, notice_type='ContractNotice', cpv=('33141420',),
                platform_url='https://platformazakupowa.pl/transakcja/1')
            rejected = Notice('BZP', 'engineering-fixture', title='Usługi inżynierii mechanicznej',
                published=on, notice_type='ContractNotice', cpv=('71333000',))
            source = Mock()
            source.fetch.return_value = [accepted, rejected]
            profile = resolve_profile('wyroby-medyczne')
            result = refresh(state, profile, on, on, {'BZP': source})
            self.assertEqual(result[0]['status'], 'COMPLETED')
            self.assertEqual(source.fetch.call_args.args[2], replace(profile.source_query(), include_amendments=True))
            expected = search(state, profile)['notices']
            self.assertEqual([n['number'] for n in expected], ['medical-fixture'])
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--db', str(state.path), 'filter', '--profile', profile.name]), 0)
            self.assertEqual(json.loads(output.getvalue())['notices'], expected)
            adapter = FakePlatform()
            adapter.files = {'SWZ.docx': docx(root / 'swz.docx', 'Dostawa rękawic diagnostycznych').read_bytes()}
            server = create_server(state.path, document_root=root / 'documents',
                                   document_adapters={adapter.name: adapter})
            async with Client(server) as client:
                found = await client.call_tool('search_notices', {'profile': profile.name})
                self.assertEqual(found.structured_content['notices'], expected)
                args = {'source': accepted.source, 'number': accepted.number}
                listed = await client.call_tool('list_documents', args)
                self.assertEqual(listed.structured_content['documents'][0]['name'], 'SWZ.docx')
                fetched = await client.call_tool('fetch_documents', args)
                self.assertEqual(fetched.structured_content['status'], 'DONE')
                record = state.documents(accepted.source, accepted.number)['SWZ.docx']
                read = await client.call_tool('read_document', {'document_id': record['sha256']})
                self.assertIn('rękawic diagnostycznych', read.structured_content['pages'][0]['text'])
                again = await client.call_tool('fetch_documents', args)
                self.assertEqual(again.structured_content['summary']['known'], 1)
                self.assertEqual(len(adapter.calls), 1)
                self.assertEqual(state.coverage()['counts'][0]['coverage'], 'FETCHED')


if __name__ == '__main__':
    unittest.main()

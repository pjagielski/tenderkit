"""Public BK response fixtures with synthetic notice and attachment data."""
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tenderkit.http import Response
from tenderkit.platforms.backends import FetchError
from tenderkit.platforms.bk import BkDocuments
from tenderkit.profiles import builtin_profiles
from tenderkit.service import refresh, search, sources
from tenderkit.sources.bk import BASE, Bk, BkIncomplete, _list
from tenderkit.store.state import State

DAY = date(2026, 11, 12)
SUMMARY = dict(id=123, publication_date=str(DAY), advertiser_name='Example buyer')
FILE = dict(id=456, uri='/api/files/456', name='Specification.pdf')
DETAIL = dict(id=901, publication_date=str(DAY), submission_deadline='2026-11-20 10:00:00',
              title='Software development', advertisement=dict(id=123, number='2026-1-123'),
              orders=[dict(id=1, title='Integration', estimated_value='20000,00', is_variant=False,
                           order_items=[dict(description='<p>Data migration</p>',
                                             cpv_items=[dict(code='72000000-5')])])],
              attachments=[dict(id=789, file=FILE)], contact_persons=[dict(email='unused@example.invalid')])


def response(data):
    return Response(200, json.dumps({'data': data}).encode())


def page(*rows, total=None):
    return response(dict(advertisements=rows, meta=dict(total=len(rows) if total is None else total)))


class BkTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.profile = builtin_profiles()['oprogramowanie']
        self.dictionary = response(dict(cpv_items=[dict(code='72000000-5'), dict(code='03117200-6')],
                                        meta=dict(total=2, last_page=1)))
        self.empty = response(dict(cpv_items=[], meta=dict(total=0, last_page=1)))

    def test_source_is_explicit_and_profile_accepts_bk(self):
        self.assertNotIn('BK', sources())
        self.assertIsInstance(sources(include_bk=True)['BK'], Bk)
        self.session.get.side_effect = [self.dictionary, self.empty, page(SUMMARY), response(dict(advertisement=DETAIL))]
        with tempfile.TemporaryDirectory() as folder:
            state = State(Path(folder) / 'state.sqlite3')
            summary, = refresh(state, self.profile, DAY, DAY, {'BK': Bk(self.session)})
            self.assertEqual(summary, dict(source='BK', status='COMPLETED', notices=1))
            notice, = search(state, self.profile)['notices']
            self.assertEqual(notice['buyer_name'], 'Example buyer')
            self.assertEqual(notice['cpv'], ['72000000'])
            self.assertEqual(Decimal(notice['value']), Decimal('20000'))
            self.assertEqual(notice['deadline'], '2026-11-20T10:00:00+01:00')
            self.assertEqual(notice['description'], 'Data migration')
            self.assertEqual(notice['source_attribution']['publisher'], 'Ministerstwo Funduszy i Polityki Regionalnej')
            self.assertEqual(state.get('BK', '123').raw, {'version': 901})
        params = self.session.get.call_args_list[2].kwargs['params']
        self.assertEqual([value for key, value in params if key == 'cpvItem[]'], ['72000000-5'])

    @patch('tenderkit.sources.bk.PAGE_SIZE', 1)
    def test_duplicate_or_short_second_page_fails_before_detail_reads(self):
        for second in (page(SUMMARY, total=2), page(total=2), page(total=0)):
            with self.subTest(second=second.json()):
                self.session.reset_mock()
                self.session.get.side_effect = [self.dictionary, self.empty, page(SUMMARY, total=2), second]
                with self.assertRaises(BkIncomplete):
                    list(Bk(self.session).fetch(DAY, DAY, self.profile.source_query()))
                self.assertEqual(self.session.get.call_count, 4)

    def test_batch_larger_than_the_portal_ui_page_fits_one_request(self):
        rows = [dict(SUMMARY, id=identifier) for identifier in range(1, 26)]
        self.session.get.return_value = page(*rows)
        found = _list(self.session, DAY, DAY, ['72000000-5'])
        self.assertEqual(set(found), set(range(1, 26)))
        self.session.get.assert_called_once()
        self.assertGreaterEqual(dict(self.session.get.call_args.kwargs['params'])['limit'], len(rows))

    def test_documents_use_nested_file_id_and_reject_html(self):
        self.session.get.return_value = response(dict(advertisement=DETAIL))
        adapter = BkDocuments(self.session)
        from tenderkit.sources.bk import bk_notice
        link = adapter.procedure(bk_notice(SUMMARY, DETAIL, '2026-11-13'))
        ref, = adapter.documents(link)
        self.assertEqual(ref.key, '456')
        binary = b'%PDF-1.7\nfixture'
        with tempfile.TemporaryDirectory() as folder:
            self.session.get.return_value = Response(200, binary, {'Content-Type': 'application/pdf'}, ref.url)
            path = adapter.download(ref, Path(folder))
            self.assertEqual(path.name, hashlib.sha256(binary).hexdigest() + '.pdf')
            self.assertEqual(path.read_bytes(), binary)
            self.session.get.return_value = Response(200, b'<html>error</html>', {}, ref.url)
            with self.assertRaisesRegex(FetchError, 'DOWNLOAD_REJECTED'):
                adapter.download(ref, Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [path])

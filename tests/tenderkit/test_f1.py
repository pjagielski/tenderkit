"""F1 integration: source windows, durable state, shared filtering, CLI and MCP."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tenderkit.cli import main
from tenderkit.http import Session, HttpFailure
from tenderkit.interfaces import SourceQuery
from tenderkit.model import Lot, Notice
from tenderkit.profile import DeadlineRange, Profile, ValueRange, profile_from_dict
from tenderkit.profiles import builtin_profiles
from tenderkit.serialization import notice_from_record, notice_record
from tenderkit.service import filtered, refresh, search, window
from tenderkit.sources.bzp import Bzp, FetchFailure
from tenderkit.sources.mapping import bzp_notice, ted_notice
from tenderkit.sources.ted import Ted, TedIncomplete, fetch_notices
from tenderkit.store.state import State

START, END = date(2026, 10, 10), date(2026, 11, 8)


def bzp_raw(number='1', **extra):
    return dict(objectId=number, noticeNumber=number, publicationDate='2026-11-01T10:00:00Z',
                noticeType='ContractNotice', cpvCode='33600000-6 (Leki)', orderObject='Dostawa leków',
                **extra)


def ted_raw(number='1', **extra):
    return {'publication-number': number, 'publication-date': '2026-11-01+01:00',
            'classification-cpv': ['33600000'], 'notice-type': 'cn-standard', **extra}


class SourceTests(unittest.TestCase):
    def test_scope_is_read_from_sources_and_old_cached_payloads(self):
        bzp = bzp_notice(bzp_raw(htmlBody='<h3>Część 2</h3>'
            '<h3>4.2.1.) Nazwa części: <span>System obiegu dokumentów</span></h3>'
            '<h3>4.2.2.) Krótki opis przedmiotu zamówienia</h3><p>Integracja i wdrożenie.</p>'
            '<h3>4.2.6.) Główny kod CPV:</h3>72000000'))
        self.assertEqual(bzp.description, 'Integracja i wdrożenie.')
        self.assertEqual(bzp.lots, (Lot('2', 'System obiegu dokumentów'),))
        ted = ted_notice(ted_raw(**{'description-proc': {'pol': 'Zakup sprzętu'},
            'description-lot': {'pol': ['Dostawa strzykawek', 'Dostawa rękawic']},
            'title-lot': {'pol': ['Rękawice', 'Strzykawki'], 'eng': ['Gloves', 'Syringes']},
            'identifier-lot': ['LOT-0001', 'LOT-0002']}))
        self.assertEqual(ted.description, 'Zakup sprzętu\n\nDostawa strzykawek\n\nDostawa rękawic')
        self.assertEqual(ted.lot_titles, ('Rękawice', 'Strzykawki'))
        self.assertEqual(ted.lots, (Lot('LOT-0001'), Lot('LOT-0002')))
        for notice in (bzp, ted):
            record = notice_record(notice)
            self.assertEqual(notice_from_record(record), notice)
            del record['description'], record['lot_titles']
            if notice.source == 'BZP':
                record['lots'] = []
            self.assertEqual(notice_from_record(record), notice)

    def test_stdlib_transport_sets_identity_and_bounds_response_size(self):
        response = mock.MagicMock(code=200)
        response.__enter__.return_value = response
        response.read.return_value = b'[]'
        with mock.patch('tenderkit.http.urlopen', return_value=response) as open_url:
            self.assertEqual(Session().get('https://example.invalid', params={'a': 'b c'}).json(), [])
        request = open_url.call_args.args[0]
        self.assertIn('a=b+c', request.full_url)
        self.assertIn('tenderkit', request.get_header('User-agent'))
        response.code = 403
        with mock.patch('tenderkit.http.urlopen', return_value=response), self.assertRaises(HttpFailure):
            Session().get('https://example.invalid').raise_for_status()
        response.read.return_value = b'12345'
        with mock.patch('tenderkit.http.urlopen', return_value=response), mock.patch('tenderkit.http.MAX_RESPONSE_BYTES', 4), self.assertRaises(ValueError):
            Session().get('https://example.invalid')

    def test_bzp_profile_unions_cpv_without_duplicate_notices(self):
        session = mock.Mock()
        session.get.return_value.json.return_value = [bzp_raw()]
        with mock.patch('tenderkit.sources.bzp.time.sleep'):
            notices = Bzp(session).fetch(START, END, SourceQuery(('33', '336'), ('ContractNotice', 'cn-standard')))
        self.assertEqual([n.number for n in notices], ['1'])
        self.assertEqual([c.kwargs['params']['CpvCode'] for c in session.get.call_args_list], ['33', '336'])
        self.assertEqual(Bzp(session).fetch(START, END, SourceQuery((), ('cn-standard',))), [])

    def test_bzp_failure_is_not_an_empty_success(self):
        session = mock.Mock()
        session.get.return_value.json.return_value = {'message': 'blocked'}
        with mock.patch('tenderkit.sources.bzp.time.sleep'), self.assertRaises(FetchFailure):
            Bzp(session).fetch(START, END, SourceQuery(('336',), ('ContractNotice',)))

    def test_ted_uses_polish_buyer_and_both_profile_cpv(self):
        session = mock.Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'notices': [ted_raw()], 'totalNoticeCount': 1}
        result = Ted(session).fetch(START, END, SourceQuery(('72', '48'), ('ContractNotice', 'cn-standard')))
        query = session.post.call_args.kwargs['json']['query']
        self.assertIn('buyer-country=POL AND (classification-cpv=72* OR classification-cpv=48*)', query)
        self.assertEqual(result[0].number, '1')

    def test_ted_detects_duplicates_and_stalled_paging(self):
        session = mock.Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'notices': [ted_raw(), ted_raw()], 'totalNoticeCount': 2}
        with self.assertRaises(TedIncomplete):
            fetch_notices(session, START, END)
        session.post.return_value.json.return_value = {'notices': [ted_raw(str(i)) for i in range(250)], 'totalNoticeCount': 300}
        with mock.patch('tenderkit.sources.ted.time.sleep'), self.assertRaises(TedIncomplete):
            fetch_notices(session, START, END, max_pages=1)

    def test_ted_stops_on_a_full_final_page_and_rejects_oversized_total(self):
        session = mock.Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'notices': [ted_raw(str(i)) for i in range(250)], 'totalNoticeCount': 250}
        self.assertEqual(len(fetch_notices(session, START, END)), 250)
        self.assertEqual(session.post.call_count, 1)
        session.post.return_value.json.return_value = {'notices': [], 'totalNoticeCount': 15001}
        with self.assertRaises(TedIncomplete):
            fetch_notices(session, START, END)

    def test_mapping_keeps_region_identity_value_and_lots(self):
        raw = bzp_raw(organizationProvince='PL14', organizationNationalId='123',
            submittingOffersDate='2026-11-09T23:30:00Z', htmlBody='<p>Wartość zamówienia: 1 234,50 PLN Liczba części: 2</p>')
        raw['publicationDate'] = '2026-11-01T23:30:00Z'
        notice = bzp_notice(raw)
        self.assertEqual(notice.published, date(2026, 11, 2))
        self.assertEqual((notice.buyer_region, notice.buyer_id, notice.value, notice.lot_count), ('14', '123', Decimal('1234.50'), 2))
        notice = ted_notice(ted_raw(**{'organisation-country-sub-buyer': ['PL911'],
            'organisation-identifier-buyer': ['456'], 'identifier-lot': ['L1', 'L2'],
            'estimated-value-lot': ['100.01', '200.02'], 'estimated-value-cur-lot': ['PLN'],
            'deadline-receipt-tender-date-lot': ['2026-11-10+01:00']}))
        self.assertEqual((notice.buyer_region, notice.buyer_id, notice.value), ('14', '456', Decimal('300.03')))
        self.assertEqual(len(notice.lots), 2)
        self.assertEqual(notice.deadline.isoformat(), '2026-11-10T00:00:00+01:00')


class AdvancedProfileTests(unittest.TestCase):
    def test_ranges_unknowns_currency_and_warsaw_deadline(self):
        profile = profile_from_dict(dict(name='query', cpv=['336*'], regions=['14'],
            value=dict(min='100', max='200', currency='PLN'), deadline_days=dict(min=1, max=1), exclude_buyers=['weterynar']))
        notice = Notice('BZP', '1', cpv=('33600000',), buyer_region='14', value=Decimal('150'), currency='PLN',
                        deadline=datetime(2026, 11, 9, 23, 30, tzinfo=timezone.utc))
        self.assertTrue(profile.matches(notice, today=date(2026, 11, 9)))
        for fields in (dict(currency='EUR'), dict(value=None), dict(buyer_region=None), dict(deadline=None),
                       dict(buyer_name='Szpital weterynaryjny'), dict(value=Decimal('201'))):
            self.assertFalse(profile.matches(replace(notice, **fields), today=date(2026, 11, 9)), fields)

    def test_full_cpv_is_exact_and_invalid_profiles_fail_early(self):
        self.assertFalse(Profile('x', cpv=('33600000',)).matches_cpv(['336000001']))
        for data in (dict(name='x', cpv='336*'), dict(name='x', regions=['PL14']),
                     dict(name='x', value=dict(min='NaN')), dict(name='x', value=dict(min='bad')),
                     dict(name='x', value=dict(min=2, max=1)), dict(name='x', deadline_days=dict(min=-1)),
                     dict(name='x', cpv=['336*'] * 65)):
            with self.assertRaises(ValueError, msg=str(data)):
                profile_from_dict(data)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.state = State(self.path)
        self.profile = Profile('leki', cpv=('336*',))
        self.notice = replace(bzp_notice(bzp_raw()), value=Decimal('12.30'), lots=(Lot('1', value=Decimal('12.30')),))

    def adapter(self, rows):
        adapter = mock.Mock()
        adapter.fetch.return_value = rows
        return adapter

    def test_round_trip_idempotency_and_reopen(self):
        for _ in range(2):
            result = refresh(self.state, self.profile, START, END, {'BZP': self.adapter([self.notice])})
            self.assertEqual(result[0]['status'], 'COMPLETED')
        reopened = State(self.path)
        self.assertEqual(notice_record(reopened.get('BZP', '1')), notice_record(self.notice))
        self.assertEqual(len(list(reopened.notices())), 1)
        self.assertEqual(reopened.coverage()['counts'], [dict(source='BZP', coverage='PENDING', count=1)])

    def test_failed_iterator_keeps_previous_state_and_discards_partial_window(self):
        refresh(self.state, self.profile, START, END, {'BZP': self.adapter([self.notice])})
        def failed():
            yield replace(self.notice, number='partial')
            raise TedIncomplete('private endpoint detail must not be recorded')
        result = refresh(self.state, self.profile, START, END, {'BZP': self.adapter(failed())})
        self.assertEqual(result[0]['error'], 'TedIncomplete')
        self.assertIsNone(self.state.get('BZP', 'partial'))
        self.assertIsNotNone(self.state.get('BZP', '1'))
        self.assertEqual(self.state.coverage()['refreshes'][0]['status'], 'FAILED')

    def test_invalid_source_window_does_not_store_notice(self):
        result = refresh(self.state, self.profile, START, END, {'BZP': self.adapter([replace(self.notice, published=date(2020, 1, 1))])})
        self.assertEqual(result[0]['status'], 'FAILED')
        self.assertEqual(list(self.state.notices()), [])
        for start, end in ((END, START), (date(2026, 1, 1), END)):
            with self.assertRaises(ValueError):
                window(start, end)

    def test_source_qualified_identity_and_stable_paging(self):
        refresh(self.state, self.profile, START, END, {'BZP': self.adapter([self.notice, replace(self.notice, number='2')]),
                'TED': self.adapter([replace(self.notice, source='TED')])})
        page = search(self.state, self.profile, limit=2)
        self.assertEqual([(n['source'], n['number']) for n in page['notices']], [('BZP', '1'), ('BZP', '2')])
        self.assertEqual(page['next_offset'], 2)
        self.assertEqual(search(self.state, self.profile, offset=2)['notices'][0]['source'], 'TED')
        self.assertIsNone(self.state.get('TED', '2'))
        with self.assertRaises(ValueError):
            search(self.state, self.profile, limit=501)

    def test_cli_fetch_filter_export_and_errors(self):
        args = ['--db', str(self.path)]
        with mock.patch('tenderkit.cli.sources', return_value={'BZP': self.adapter([self.notice])}), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args + ['fetch', '--profile', 'leki', '--from', str(START), '--to', str(END)]), 0)
        self.assertEqual(json.loads(output.getvalue())['sources'][0]['notices'], 1)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args + ['filter', '--profile', 'leki']), 0)
        self.assertEqual(json.loads(output.getvalue())['notices'], search(self.state, builtin_profiles()['leki'])['notices'])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args + ['export', '--profile', 'leki', '--format', 'jsonl']), 0)
        self.assertEqual(json.loads(output.getvalue()), notice_record(self.notice))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(args + ['filter', '--profile', 'leki', '--limit', '501']), 2)


if __name__ == '__main__':
    unittest.main()

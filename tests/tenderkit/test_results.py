"""Formal per-lot results from BZP and TED, procedure linking, result search and the bid-opening marker."""
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tenderkit.cli import main
from tenderkit.documents import Documents, document_kind, with_kind
from tenderkit.http import Response
from tenderkit.interfaces import SourceQuery
from tenderkit.model import Lot, Award, DocumentRef, Notice, PlatformLink
from tenderkit.serialization import notice_from_record, notice_record
from tenderkit.service import procedure_results, search_results
from tenderkit.sources.bzp import Bzp
from tenderkit.sources.mapping import bzp_notice
from tenderkit.sources.ted import Ted
from tenderkit.sources.ted_xml import NS, parse_awards
from tenderkit.store.state import State

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures/tenderkit/results'
START, END = date(2026, 10, 1), date(2026, 10, 1)


def fixture(name):
    return FIXTURES / name


def bzp_raw(name):
    return json.loads(fixture(name).read_text(encoding='utf-8'))


class BzpAwardTests(unittest.TestCase):
    def test_multilot_notice_yields_contractor_and_price_per_lot(self):
        notice = bzp_notice(bzp_raw('bzp-awarded-multilot.json'))
        self.assertEqual([award.lot for award in notice.awards], ['1', '2'])
        first, second = notice.awards
        self.assertEqual((first.status, first.contractor, first.contractor_id),
                         ('AWARDED', 'Example Supplier', '0000000000'))
        self.assertEqual((first.price, first.currency, first.offers, first.date),
                         (Decimal('92250.00'), 'PLN', 1, date(2026, 7, 9)))
        self.assertEqual((second.price, second.offers), (Decimal('138137.78'), 2))
        self.assertEqual(second.date, date(2026, 7, 28))
        self.assertEqual((first.date_kind, first.date_source), ('contract', 'BZP:8.1'))

    def test_missing_invalid_and_ambiguous_contract_dates_stay_unknown(self):
        raw = bzp_raw('bzp-awarded-multilot.json')
        for value in ('', '2026-02-30', 'not a date'):
            with self.subTest(value=value):
                edited = dict(raw, htmlBody=raw['htmlBody'].replace('2026-07-09', value))
                award = bzp_notice(edited).awards[0]
                self.assertEqual((award.date, award.date_kind, award.date_source), (None, None, None))
        edited = dict(raw, htmlBody=raw['htmlBody'].replace('2026-07-09',
            '2026-07-09</h3><h3>8.1.) Data zawarcia umowy:<span>2026-07-10</span></h3>'))
        self.assertIsNone(bzp_notice(edited).awards[0].date)

    def test_legacy_cache_recovers_dates_without_changing_other_award_fields(self):
        record = notice_record(bzp_notice(bzp_raw('bzp-awarded-multilot.json')))
        for award in record['awards']:
            award.pop('date_kind')
            award.pop('date_source')
            award['date'] = record['published']
        restored = notice_from_record(record)
        self.assertEqual([a.date for a in restored.awards], [date(2026, 7, 9), date(2026, 7, 28)])
        self.assertEqual(restored.awards[0].contractor, record['awards'][0]['contractor'])
        record['raw'] = {}
        self.assertTrue(all(a.date is None for a in notice_from_record(record).awards))

    def test_cancelled_notice_has_no_contractor_or_price(self):
        notice = bzp_notice(bzp_raw('bzp-cancelled.json'))
        self.assertEqual(len(notice.awards), 1)
        award = notice.awards[0]
        self.assertEqual((award.lot, award.status, award.contractor, award.price),
                         ('__all__', 'CANCELLED', None, None))
        self.assertEqual(award.offers, 4)
        self.assertEqual((award.date, award.date_kind, award.date_source), (None, None, None))

    def test_non_result_notice_has_no_awards(self):
        raw = dict(bzp_raw('bzp-cancelled.json'), noticeType='ContractNotice')
        self.assertEqual(bzp_notice(raw).awards, ())

    def test_results_flag_asks_bzp_for_the_result_notice_type(self):
        session = mock.Mock()
        session.get.return_value.json.return_value = []
        with mock.patch('tenderkit.sources.bzp.time.sleep'):
            Bzp(session).fetch(START, END, SourceQuery(('336',), ('ContractNotice',), include_results=True))
        kinds = [call.kwargs['params']['NoticeType'] for call in session.get.call_args_list]
        self.assertEqual(kinds, ['ContractNotice', 'TenderResultNotice'])


class TedAwardTests(unittest.TestCase):
    def test_multilot_awards_come_from_the_notice_xml(self):
        awards = parse_awards(fixture('ted-awarded-multilot.xml').read_bytes())
        self.assertEqual([award.lot for award in awards], ['LOT-0001', 'LOT-0002'])
        first, second = awards
        self.assertEqual((first.contractor, first.contractor_id, first.price, first.offers, first.date),
                         ('Enigma Systemy Ochrony Informacji Sp. z o.o.', '5261029614',
                          Decimal('1764040'), 1, date(2026, 9, 24)))
        self.assertEqual((first.date_kind, first.date_source),
                         ('contract', 'TED:SettledContract/cbc:IssueDate'))
        self.assertEqual((second.contractor, second.price), ('COIG S.A.', Decimal('495000')))

    def test_equal_lot_values_and_framework_ceiling_have_distinct_scopes(self):
        def award_xml(rows, tenders, total):
            namespace = ' '.join(f'xmlns:{name}="{uri}"' for name, uri in NS.items())
            return f'<ContractAwardNotice {namespace}><efac:NoticeResult><cbc:TotalAmount currencyID="PLN">{total}</cbc:TotalAmount>{rows}{tenders}</efac:NoticeResult></ContractAwardNotice>'

        def result(lot, tender, limit=''):
            return (f'<efac:LotResult><cbc:TenderResultCode>selec-w</cbc:TenderResultCode>'
                    f'<efac:TenderLot><cbc:ID>{lot}</cbc:ID></efac:TenderLot>'
                    f'<efac:LotTender><cbc:ID>{tender}</cbc:ID></efac:LotTender>{limit}</efac:LotResult>')

        def tender(identifier, price):
            return (f'<efac:LotTender><cbc:ID>{identifier}</cbc:ID><cac:LegalMonetaryTotal>'
                    f'<cbc:PayableAmount currencyID="PLN">{price}</cbc:PayableAmount>'
                    '</cac:LegalMonetaryTotal></efac:LotTender>')

        lots = parse_awards(award_xml(result('L1', 'T1') + result('L2', 'T2'),
                                      tender('T1', 100) + tender('T2', 100), 200))
        self.assertEqual([award.price_scope for award in lots], ['lot', 'lot'])
        combined = parse_awards(award_xml(result('L1', 'T1') + result('L2', 'T1'), tender('T1', 200), 200))
        self.assertEqual([award.price_scope for award in combined], ['notice_total', 'notice_total'])
        cap = ('<efac:FrameworkAgreementValues><cbc:MaximumValueAmount currencyID="PLN">150'
               '</cbc:MaximumValueAmount></efac:FrameworkAgreementValues>')
        framework, = parse_awards(award_xml(result('L1', 'T1', cap), tender('T1', 150), 150))
        self.assertEqual((framework.price_scope, framework.framework_max, framework.framework_currency),
                         ('framework_max', Decimal('150'), 'PLN'))

    def test_cancelled_notice_keeps_status_without_a_winner(self):
        awards = parse_awards(fixture('ted-cancelled.xml').read_bytes())
        self.assertEqual([award.status for award in awards], ['CANCELLED', 'CANCELLED'])
        self.assertTrue(all(award.contractor is None and award.price is None for award in awards))

    def test_mixed_notice_marks_awarded_and_cancelled_lots_separately(self):
        awards = parse_awards(fixture('ted-mixed-multilot.xml').read_bytes())
        self.assertEqual(len(awards), 11)
        self.assertEqual([award.lot for award in awards if award.status == 'AWARDED'],
                         ['LOT-0003', 'LOT-0005', 'LOT-0007', 'LOT-0009'])
        self.assertEqual(awards[2].contractor, 'GISPRO SPÓŁKA AKCYJNA')
        self.assertEqual(awards[2].price, Decimal('453000'))
        self.assertEqual(awards[7].offers, 2)

    def test_other_notice_types_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_awards(b'<ContractNotice xmlns="urn:oasis:names:specification:ubl:schema:xsd:ContractNotice-2"/>')
        with self.assertRaises(ValueError):
            parse_awards(b'not xml')

    def test_ted_source_fetches_awards_xml_for_can_notices(self):
        raw = {'publication-number': '674771-2026', 'publication-date': '2026-10-01+02:00',
               'classification-cpv': ['72253200'], 'notice-type': 'can-standard',
               'procedure-identifier': ['proc-1'], 'buyer-name': {'pol': ['Test']},
               'identifier-lot': ['LOT-0001', 'LOT-0002']}
        search = Response(200, json.dumps({'notices': [raw], 'totalNoticeCount': 1}).encode(), {}, 'https://api.ted')
        session = mock.Mock()
        session.post.return_value = search
        session.get.return_value = Response(200, fixture('ted-awarded-multilot.xml').read_bytes(), {}, 'https://ted.europa.eu')
        notices = Ted(session, xml_delay=0).fetch(START, END, SourceQuery(('72',), ('cn-standard',), include_results=True))
        self.assertEqual(len(notices), 1)
        self.assertEqual([award.contractor for award in notices[0].awards],
                         ['Enigma Systemy Ochrony Informacji Sp. z o.o.', 'COIG S.A.'])

    def test_a_failed_xml_fetch_keeps_the_search_record(self):
        raw = {'publication-number': '674771-2026', 'publication-date': '2026-10-01+02:00',
               'classification-cpv': ['72253200'], 'notice-type': 'can-standard'}
        session = mock.Mock()
        session.post.return_value = Response(200, json.dumps({'notices': [raw], 'totalNoticeCount': 1}).encode(), {}, 'https://api.ted')
        session.get.return_value = Response(503, b'', {}, 'https://ted.europa.eu')
        notices = Ted(session, xml_delay=0).fetch(START, END, SourceQuery((), ('can-standard',)))
        self.assertEqual(notices[0].number, '674771-2026')
        self.assertEqual(notices[0].awards, ())


class ResultServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'state.sqlite3'
        self.state = State(self.path)
        self.contract = Notice('BZP', 'contract-1', procedure_id='proc-1', title='System obiegu dokumentów',
                               buyer_name='Gmina Kraków', buyer_id='123', cpv=('72200000',),
                               published=date(2026, 10, 1), notice_type='ContractNotice')
        self.result = Notice('BZP', 'result-1', procedure_id='proc-1', title='Wynik: system obiegu dokumentów',
                             buyer_name='Gmina Kraków', buyer_id='123', cpv=('72200000',),
                             published=date(2026, 10, 20), notice_type='TenderResultNotice',
                             awards=(Award('1', 'AWARDED', contractor='Acme sp. z o.o.', contractor_id='111',
                                           price=Decimal('1000'), currency='PLN', offers=2, date=date(2026, 10, 20)),
                                     Award('2', 'CANCELLED', offers=0, date=date(2026, 10, 20))))
        self.state.record_refresh('BZP', date(2026, 10, 1), date(2026, 10, 20), asdict(SourceQuery()),
                                  [self.contract, self.result])

    def test_awards_survive_the_state_round_trip(self):
        restored = self.state.get('BZP', 'result-1')
        self.assertEqual(restored.awards, self.result.awards)
        self.assertEqual(notice_from_record(notice_record(self.result)).awards, self.result.awards)

    def test_procedure_linking_works_from_the_contract_and_the_result(self):
        for number in ('contract-1', 'result-1'):
            with self.subTest(number=number):
                output = procedure_results(self.state, 'BZP', number)
                self.assertEqual(output['procedure_id'], 'proc-1')
                self.assertEqual([notice['number'] for notice in output['contracts']], ['contract-1'])
                self.assertEqual([notice['number'] for notice in output['results']], ['result-1'])
                self.assertEqual([(award['lot'], award['status'], award['contractor'], award['price'])
                                  for award in output['awards']],
                                 [('1', 'AWARDED', 'Acme sp. z o.o.', '1000'), ('2', 'CANCELLED', None, None)])

    def test_a_later_result_notice_replaces_an_earlier_award_for_the_same_lot(self):
        correction = Notice('BZP', 'result-2', procedure_id='proc-1', published=date(2026, 10, 25),
                            notice_type='TenderResultNotice',
                            awards=(Award('1', 'AWARDED', contractor='Nowy wykonawca', price=Decimal('900'),
                                          currency='PLN', offers=2, date=date(2026, 10, 25)),))
        self.state.record_refresh('BZP', date(2026, 10, 25), date(2026, 10, 25), asdict(SourceQuery()), [correction])
        output = procedure_results(self.state, 'BZP', 'result-1')
        lot_one = [award for award in output['awards'] if award['lot'] == '1']
        self.assertEqual(len(lot_one), 1)
        self.assertEqual((lot_one[0]['contractor'], lot_one[0]['number']), ('Nowy wykonawca', 'result-2'))
        self.assertEqual(len(output['results']), 2)

    def test_compact_results_carry_the_same_facts_once_per_notice(self):
        full = search_results(self.state, cpv=('72*',))
        compact = search_results(self.state, cpv=('72*',), compact=True)
        self.assertEqual((compact['total'], compact['next_offset']), (full['total'], full['next_offset']))
        notices = {(n['source'], n['number']): n for n in compact['notices']}
        self.assertEqual(list(notices), [('BZP', 'result-1')])
        rebuilt = [{**{k: None for k in full['awards'][0]}, **notices[(a['source'], a['number'])], **a}
                   for a in compact['awards']]
        self.assertEqual(rebuilt, full['awards'])
        self.assertNotIn('title', compact['awards'][0])
        self.assertNotIn('contractor', compact['awards'][1])           # cancelled lot: empty fields left out
        self.assertLess(len(json.dumps(compact)), len(json.dumps(full)))

    def test_compact_procedure_results_summarise_notices(self):
        full = procedure_results(self.state, 'BZP', 'result-1')
        compact = procedure_results(self.state, 'BZP', 'result-1', compact=True)
        self.assertEqual(compact['procedure_id'], 'proc-1')
        self.assertEqual([n['number'] for n in compact['contracts']], ['contract-1'])
        self.assertEqual([(n['number'], n['award_count']) for n in compact['results']], [('result-1', 2)])
        self.assertFalse({'description', 'lots', 'awards'} & compact['notice'].keys())
        self.assertEqual([(a['lot'], a['number'], a.get('contractor'), a.get('price')) for a in compact['awards']],
                         [(a['lot'], a['number'], a['contractor'], a['price']) for a in full['awards']])
        self.assertFalse({'source', 'title', 'buyer_name', 'url'} & compact['awards'][0].keys())

    def _add_lots(self):
        # TED: one bid for two lots (notice_total), a framework ceiling and a EUR lot; BZP: a lot without a bid count.
        ted = Notice('TED', 'ted-1', procedure_id='ted-proc', title='Dostawa ambulansów i opon', buyer_name='Szpital',
                     cpv=('72200000',), published=date(2026, 10, 22), notice_type='can-standard',
                     lots=(Lot('LOT-1', 'Ambulans typu C'), Lot('LOT-2', 'Opony zimowe'), Lot('LOT-3', 'Serwis'),
                           Lot('LOT-4', 'Ambulans typu B')),
                     awards=(Award('LOT-1', 'AWARDED', contractor='Beta', price=Decimal('500'), currency='PLN',
                                   offers=1, price_scope='notice_total', date=date(2026, 10, 10)),
                             Award('LOT-2', 'AWARDED', contractor='Beta', price=Decimal('500'), currency='PLN',
                                   offers=1, price_scope='notice_total'),
                             Award('LOT-3', 'AWARDED', contractor='Gamma', price=Decimal('80'), currency='PLN',
                                   offers=3, price_scope='framework_max'),
                             Award('LOT-4', 'AWARDED', contractor='Delta', contractor_id='999', price=Decimal('40'),
                                   currency='EUR', offers=4, price_scope='lot', date=date(2026, 10, 5))))
        bzp = Notice('BZP', 'result-9', procedure_id='proc-9', title='Ambulans', buyer_name='Gmina Kraków',
                     cpv=('72200000',), published=date(2026, 10, 23), notice_type='TenderResultNotice',
                     awards=(Award('__all__', 'AWARDED', contractor='Acme sp. z o.o.', contractor_id='111',
                                   price=Decimal('3000'), currency='PLN'),))
        for item in (ted, bzp):
            self.state.record_refresh(item.source, item.published, item.published, {}, [item])

    def test_summary_counts_procedures_lots_and_only_lot_prices(self):
        self._add_lots()
        result = search_results(self.state, cpv=('72*',), limit=0)
        self.assertEqual((result['awards'], result['total'], result['next_offset']), ([], 7, None))
        summary = result['summary']
        self.assertEqual({k: summary[k] for k in ('awards', 'procedures', 'notices', 'buyers', 'contractors')},
                         dict(awards=7, procedures=3, notices=3, buyers=2, contractors=4))
        self.assertEqual(summary['status'], {'AWARDED': 6, 'CANCELLED': 1})
        self.assertEqual(summary['missing'], dict(price=0, contractor=0, date=3, offers=1))
        self.assertEqual(summary['offers'], dict(known=6, single=2, min=0, median=1.5, max=4))
        self.assertEqual(summary['prices'], [
            dict(currency='PLN', lots=2, sum='4000', min='1000', median='2000', max='3000'),
            dict(currency='EUR', lots=1, sum='40', min='40', median='40', max='40')])
        self.assertEqual(summary['prices_left_out'], {'notice_total': 2, 'framework_max': 1})
        self.assertNotIn('keyword_scope', summary)

    def test_status_bid_filters_and_sorting_keep_missing_values_last(self):
        self._add_lots()

        def lots(**kw):
            return [(a['number'], a['lot']) for a in search_results(self.state, cpv=('72*',), **kw)['awards']]
        self.assertEqual(lots(status='CANCELLED'), [('result-1', '2')])
        self.assertEqual(lots(max_offers=1), [('result-1', '2'), ('ted-1', 'LOT-1'), ('ted-1', 'LOT-2')])
        self.assertEqual(lots(min_offers=3), [('ted-1', 'LOT-3'), ('ted-1', 'LOT-4')])
        self.assertEqual(lots(min_offers=0), [lot for lot in lots() if lot != ('result-9', '__all__')])
        # Only lot prices sort; notice totals, framework ceilings and cancelled lots follow in stored order.
        self.assertEqual(lots(sort='-price')[:3], [('result-9', '__all__'), ('result-1', '1'), ('ted-1', 'LOT-4')])
        self.assertEqual(lots(sort='price')[:3], [('ted-1', 'LOT-4'), ('result-1', '1'), ('result-9', '__all__')])
        self.assertEqual(lots(sort='date')[:4], [('ted-1', 'LOT-4'), ('ted-1', 'LOT-1'), ('result-1', '1'),
                                                 ('result-1', '2')])
        self.assertEqual(lots(sort='-published')[0], ('result-9', '__all__'))
        page = search_results(self.state, cpv=('72*',), sort='-offers', limit=2, offset=1)
        self.assertEqual(([a['offers'] for a in page['awards']], page['next_offset']), ([3, 2], 3))
        for bad in (dict(status='DONE'), dict(min_offers=-1), dict(max_offers='1'), dict(sort='value'),
                    dict(limit=-1), dict(keywords_in_lot=True)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                search_results(self.state, cpv=('72*',), **bad)

    def test_keyword_scope_tells_lot_matches_from_other_lots_of_the_notice(self):
        self._add_lots()
        result = search_results(self.state, keywords_any=['ambulans'])
        scopes = {(a['number'], a['lot']): a['keyword_scope'] for a in result['awards']}
        self.assertEqual(scopes, {('ted-1', 'LOT-1'): 'lot', ('ted-1', 'LOT-2'): 'notice', ('ted-1', 'LOT-3'): 'notice',
                                  ('ted-1', 'LOT-4'): 'lot', ('result-9', '__all__'): 'lot'})
        self.assertEqual(result['summary']['keyword_scope'], {'lot': 3, 'notice': 2})
        narrowed = search_results(self.state, keywords_any=['ambulans'], keywords_in_lot=True)
        self.assertEqual(sorted(a['lot'] for a in narrowed['awards']), ['LOT-1', 'LOT-4', '__all__'])

    def test_search_results_filters_by_buyer_and_cpv(self):
        found = search_results(self.state, buyer='krakow', cpv=('72*',))
        self.assertEqual({award['lot'] for award in found['awards']}, {'1', '2'})
        self.assertEqual(found['total'], 2)
        self.assertEqual(search_results(self.state, buyer='Warszawa')['awards'], [])
        self.assertEqual(search_results(self.state, cpv=('48*',))['awards'], [])

    def test_sqlite_filters_match_the_python_rules(self):
        other = Notice('BZP', 'result-3', procedure_id='proc-3', buyer_name='Szpital w Łodzi', cpv=('72212000', '48000000'),
                       published=date(2026, 10, 21), notice_type='TenderResultNotice',
                       awards=(Award('1', 'AWARDED', contractor='Beta', date=date(2026, 10, 21)),))
        self.state.record_refresh('BZP', date(2026, 10, 21), date(2026, 10, 21), asdict(SourceQuery()), [other])
        numbers = lambda **kw: sorted(n.number for n in self.state.notices(**kw))
        self.assertEqual(numbers(awarded=True), ['result-1', 'result-3'])
        self.assertEqual(numbers(cpv=('72200000',)), ['contract-1', 'result-1'])
        self.assertEqual(numbers(cpv=('7221*',)), ['result-3'])
        self.assertEqual(numbers(cpv=('48*', '722000*')), ['contract-1', 'result-1', 'result-3'])
        self.assertEqual(numbers(cpv=('722',)), [])
        self.assertEqual(numbers(buyer='KRAKÓW'), ['contract-1', 'result-1'])
        self.assertEqual(numbers(buyer='lodz'), ['result-3'])
        self.assertEqual(numbers(procedure_id='proc-1'), ['contract-1', 'result-1'])

    def test_search_filters_regions_and_keywords_and_returns_lot_metadata(self):
        from dataclasses import replace
        accepted = replace(self.result, buyer_region='12', url='https://example.org/result',
                           notice_type='TenderResultNotice', description='Dostawa ambulansów',
                           lots=(Lot(self.result.awards[0].lot, 'Ambulans B', cpv=('34114121',),
                                     quantity=Decimal('5'), quantity_unit='C62'),))
        rejected = replace(accepted, source='TED', number='other', buyer_region='14',
                           title='Pojazdy pożarnicze')
        for item in (accepted, rejected):
            self.state.record_refresh(item.source, item.published, item.published, {}, [item])
        result = search_results(self.state, keywords_any=['ambulans'], exclude_keywords=['pożarnicz'],
                                regions=['małopolskie'])
        award = result['awards'][0]
        self.assertEqual((award['url'], award['buyer_region'], award['notice_kind'], award['lot_title']),
                         ('https://example.org/result', '12', 'result', 'Ambulans B'))
        self.assertEqual((award['cpv'], award['quantity'], award['quantity_unit']), (['34114121'], '5', 'C62'))
        self.assertTrue(all(item['number'] == self.result.number for item in result['awards']))

    def test_a_read_only_database_without_result_indexes_still_answers(self):
        from contextlib import closing
        import sqlite3
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('drop index notices_procedure')
            conn.execute('drop index notices_awarded')
        reader = State(self.path, read_only=True)
        self.assertEqual(search_results(reader, buyer='krakow')['total'], 2)
        self.assertEqual(len(procedure_results(reader, 'BZP', 'contract-1')['results']), 1)
        State(self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            plan = ' '.join(row[-1] for row in conn.execute(
                "explain query plan select payload from notices where source=? and json_extract(payload, '$.procedure_id') = ?",
                ('BZP', 'proc-1')))
        self.assertIn('notices_procedure', plan)

    def test_search_results_rejects_invalid_cpv(self):
        with self.assertRaises(ValueError):
            search_results(self.state, cpv=('abc',))

    def test_cli_returns_contractor_and_price_for_every_lot(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--db', str(self.path), 'results', '--source', 'BZP', '--number', 'result-1']), 0)
        data = json.loads(output.getvalue())
        self.assertEqual([(award['lot'], award['contractor'], award['price']) for award in data['awards']],
                         [('1', 'Acme sp. z o.o.', '1000'), ('2', None, None)])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--db', str(self.path), 'results', '--buyer', 'krakow', '--cpv', '72*']), 0)
        self.assertEqual(json.loads(output.getvalue())['total'], 2)


class BidOpeningTests(unittest.TestCase):
    def test_filename_marks_bid_opening_without_parsing_content(self):
        self.assertEqual(document_kind('INFORMACJA Z OTWARCIA OFERT.pdf'), 'bid_opening')
        self.assertEqual(document_kind('Informacja z otwarcia ofert - część 2.docx'), 'bid_opening')
        self.assertIsNone(document_kind('SPECYFIKACJA WARUNKÓW ZAMÓWIENIA.pdf'))

    def test_listing_exposes_the_kind(self):
        ref = DocumentRef('INFORMACJA Z OTWARCIA OFERT.pdf', 'https://example.invalid/1', '1')
        marked = with_kind(ref)
        self.assertEqual(marked.metadata['kind'], 'bid_opening')
        self.assertNotIn('kind', ref.metadata)

    def test_documents_list_adds_kind_and_keeps_the_saved_list(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = State(Path(temp.name) / 'state.sqlite3')
        notice = Notice('TED', 'one', title='Test', platform_url='https://example.invalid/proc/1')
        state.record_refresh('TED', START, END, asdict(SourceQuery()), [notice])

        class Adapter:
            name = 'example.invalid'
            can_download = False

            def matches(self, url):
                return url.startswith('https://example.invalid/')

            def procedure(self, notice):
                return PlatformLink(self.name, notice.platform_url, 'proc-1')

            def documents(self, link):
                return [DocumentRef('INFORMACJA Z OTWARCIA OFERT.pdf', 'https://example.invalid/1', '1'),
                        DocumentRef('SWZ.docx', 'https://example.invalid/2', '2')]

        service = Documents(state, Path(temp.name) / 'documents', {Adapter.name: Adapter()})
        output = service.list('TED', 'one')
        self.assertEqual([(item['name'], item['kind']) for item in output['documents']],
                         [('INFORMACJA Z OTWARCIA OFERT.pdf', 'bid_opening'), ('SWZ.docx', None)])
        cached = service.list('TED', 'one', refresh=False)
        self.assertEqual(cached['documents'][0]['kind'], 'bid_opening')


if __name__ == '__main__':
    unittest.main()




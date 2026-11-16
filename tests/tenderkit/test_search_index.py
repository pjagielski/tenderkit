"""The search index narrows candidates in SQLite without changing what a profile matches."""
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from tenderkit.model import Lot, Notice
from tenderkit.profile import profile_from_dict
from tenderkit.service import filtered
from tenderkit.store import search_index
from tenderkit.store.state import State

TODAY = date(2026, 11, 16)
NOTICES = [
    Notice('BZP', 'n1', title='Dostawa leków onkologicznych', buyer_name='Szpital w Łodzi', buyer_region='10',
           cpv=('33600000',), published=date(2026, 11, 1), notice_type='ContractNotice',
           deadline=datetime(2026, 11, 20, 10), value=Decimal('250000'), currency='PLN'),
    Notice('BZP', 'n2', title='Remont budynku', description='Zakres:\n\ndostawę oraz montaż wind',
           buyer_name='Gmina Kraków', buyer_region='12', cpv=('45000000',), published=date(2026, 11, 2),
           notice_type='ContractNotice', deadline=datetime(2026, 12, 30, 10)),
    Notice('TED', 'n3', title='Wdrożenie systemu informatycznego e-Zamówienia', buyer_name='Urząd Miasta',
           buyer_region='14', cpv=('72200000', '48000000'), published=date(2026, 11, 3), notice_type='cn-standard',
           value=Decimal('1000000'), currency='EUR', lots=(Lot('1', title='Licencje serwerowe'),)),
    Notice('TED', 'n4', title='Usługi pocztowe', description='system  informatyczny_do obsługi', buyer_region='14',
           cpv=('64110000',), published=date(2026, 11, 4), notice_type='can-standard', lot_titles=('Część COVID-19',)),
    Notice('BZP', 'n5', title='Zakup paliw', buyer_name='Szpital Wojewódzki', cpv=('09100000',),
           published=date(2026, 11, 5), notice_type='TenderResultNotice'),
]
PROFILES = [
    {}, {'keywords_any': ['lek']}, {'keywords_any': ['Łódź', 'leków']}, {'keywords_any': ['dostaw']},
    {'keywords_any': ['system informatyczn']}, {'keywords_any': ['*system informat*']}, {'keywords_any': ['*serwer*']},
    {'keywords_any': ['e-zamówienia']}, {'keywords_any': ['covid-19']}, {'keywords_any': ['zzqx']},
    {'keywords_any': ['montaz'], 'exclude_keywords': ['wind']}, {'keywords_any': ['onkolog*', 'licencj*']},
    {'keywords_any': ['*kolog', 'remont*'], 'exclude_keywords': ['wind*']}, {'keywords_any': ['system informatyczn*']}, {'keywords_any': ['usług'], 'regions': ['14']},
    {'cpv': ['72*']}, {'cpv': ['48000000']}, {'cpv': ['336*'], 'exclude_cpv': ['33600000']},
    {'notice_types': ['ContractNotice']}, {'regions': ['10', '12']}, {'exclude_buyers': ['szpital']},
    {'value': {'min': '100000', 'max': '250000', 'currency': 'PLN'}}, {'value': {'min': '1000000', 'currency': 'EUR'}},
    {'deadline_days': {'min': 1, 'max': 10}}, {'deadline_days': {'min': 4, 'max': 4}}, {'deadline_days': {'min': 30, 'max': 60}},
]


class SearchIndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'state.sqlite3'
        self.state = State(self.path)
        self.state.record_refresh('BZP', date(2026, 11, 1), date(2026, 11, 5), {}, NOTICES)

    def numbers(self, state, profile, *, indexed=True):
        if indexed:
            return [n.number for n in filtered(state, profile, on=TODAY)]
        with mock.patch.object(search_index, 'complete', return_value=False):
            return [n.number for n in filtered(state, profile, on=TODAY)]

    def test_indexed_and_full_scans_match_for_every_rule(self):
        reader = State(self.path, read_only=True)
        for index, data in enumerate(PROFILES):
            profile = profile_from_dict({'name': f'p{index}', **data})
            with self.subTest(profile=data):
                self.assertEqual(self.numbers(reader, profile), self.numbers(reader, profile, indexed=False))

    def test_the_index_narrows_before_decoding(self):
        profile = profile_from_dict({'name': 'rare', 'keywords_any': ['onkologiczn']})
        with mock.patch('tenderkit.store.state.notice_from_record', wraps=__import__(
                'tenderkit.store.state', fromlist=['notice_from_record']).notice_from_record) as decode:
            self.assertEqual(self.numbers(State(self.path, read_only=True), profile), ['n1'])
        self.assertEqual(decode.call_count, 1)

    def test_a_bullet_glued_to_a_word_still_matches(self):
        profile = profile_from_dict({'name': 'b', 'keywords_any': ['dostaw']})
        self.assertIn('n2', self.numbers(State(self.path, read_only=True), profile))

    def test_rewrites_update_the_index(self):
        changed = Notice('BZP', 'n1', title='Dostawa sprzętu', cpv=('30200000',), published=date(2026, 11, 1),
                         notice_type='ContractNotice')
        self.state.record_refresh('BZP', date(2026, 11, 1), date(2026, 11, 1), {}, [changed])
        reader = State(self.path, read_only=True)
        self.assertEqual(self.numbers(reader, profile_from_dict({'name': 'x', 'keywords_any': ['lek']})), [])
        self.assertEqual(self.numbers(reader, profile_from_dict({'name': 'y', 'cpv': ['302*']})), ['n1'])

    def test_an_older_database_is_indexed_by_the_next_writer_and_read_by_scan_until_then(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('drop table notice_index')
            conn.execute('drop table notice_fts')
        profile = profile_from_dict({'name': 'lek', 'keywords_any': ['lek']})
        reader = State(self.path, read_only=True)
        self.assertEqual(self.numbers(reader, profile), ['n1'])
        State(self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('select count(*) from notice_index').fetchone()[0], len(NOTICES))
        self.assertEqual(self.numbers(reader, profile), ['n1'])

    def test_a_partial_index_is_not_used(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("delete from notice_index where id = (select rowid from notices where number='n1')")
        with closing(sqlite3.connect(self.path)) as conn:
            conn.create_function('fold', 1, lambda text: text, deterministic=True)
            self.assertFalse(search_index.complete(conn))
        profile = profile_from_dict({'name': 'lek', 'cpv': ['336*']})
        self.assertEqual(self.numbers(State(self.path, read_only=True), profile), ['n1'])


if __name__ == '__main__':
    unittest.main()

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tenderkit.cli import main
from tenderkit.model import Notice
from tenderkit.profile import DeadlineRange, Profile
from tenderkit.service import window
from tenderkit.sources.mapping import bzp_amendment
from tenderkit.store.state import State
from tenderkit.watch import latest_updates, run_watch


DAY = date(2026, 11, 9)


class Source:
    def __init__(self, *notices):
        self.notices = notices
        self.fail = False
        self.windows = []

    def fetch(self, start, end, query):
        self.windows.append((start, end))
        for notice in self.notices:
            if start <= notice.published <= end:
                yield notice
        if self.fail:
            raise OSError('Source failed after returning a partial page')


class WatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'notices.sqlite3'
        self.profile = Profile('watch-test')
        self.notice = Notice('BZP', '2026/BZP 00505877/01', published=DAY, title='Nowe ogłoszenie',
                             deadline=datetime(2026, 11, 16, 10), cpv=('72000000',))
        self.bzp = Source(self.notice)

    def watch(self, adapters=None, *, profile=None, end=DAY, webhook=None):
        output = StringIO()
        summaries = run_watch(State(self.path), profile or self.profile, *window(end=end),
                              adapters or {'BZP': self.bzp}, output, webhook=webhook)
        return [json.loads(line) for line in output.getvalue().splitlines()], summaries

    def test_cli_repeated_run_is_empty_and_uses_environment_database(self):
        with patch('tenderkit.cli.sources', return_value={'BZP': self.bzp}), \
             patch.dict('os.environ', {'TENDERKIT_DB': str(self.path)}):
            args = ['watch', '--profile', 'oprogramowanie', '--to', DAY.isoformat()]
            self.bzp.notices = (replace(self.notice, notice_type='ContractNotice',
                                        title='Wdrożenie systemu informatycznego'),)
            with redirect_stdout(StringIO()) as first:
                self.assertEqual(main(args), 0)
            with redirect_stdout(StringIO()) as second:
                self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(first.getvalue())['notice']['number'], self.notice.number)
        self.assertEqual(second.getvalue(), '')
        self.assertEqual(latest_updates(State(self.path), 'oprogramowanie')['events'], [])

    def test_deadline_and_version_are_tracked_separately_for_each_profile(self):
        first, _ = self.watch()
        self.assertEqual(first[0]['event'], 'new')
        version = replace(self.notice, number='2026/BZP 00505877/02')
        self.bzp.notices = (version, self.notice)
        updated, _ = self.watch()
        self.assertEqual(updated[0]['changes'], ['version'])
        self.bzp.notices = (replace(version, deadline=datetime(2026, 11, 18, 10)), self.notice)
        changed, _ = self.watch()
        self.assertEqual(changed[0]['changes'], ['deadline'])
        self.assertEqual(self.watch()[0], [])
        self.assertEqual(self.watch(profile=Profile('other'))[0][0]['event'], 'new')

    def test_failed_source_retries_initial_window_after_it_has_rolled_forward(self):
        older = DAY - timedelta(days=29)
        ted = Source(Notice('TED', 'old', published=older))
        ted.fail = True
        adapters = {'BZP': self.bzp, 'TED': ted}
        events, summaries = self.watch(adapters)
        self.assertEqual([event['notice']['source'] for event in events], ['BZP'])
        self.assertEqual(summaries[1]['status'], 'FAILED')
        self.assertIsNone(State(self.path).get('TED', 'old'))
        ted.fail = False
        events, _ = self.watch(adapters, end=DAY + timedelta(days=1))
        self.assertEqual(ted.windows[-1][0], older)
        self.assertEqual([event['notice']['number'] for event in events], ['old'])

    def test_revised_deadline_can_take_a_reported_notice_out_of_the_profile(self):
        profile = Profile('shortlist', deadline_days=DeadlineRange(min=1, max=10))
        with patch('tenderkit.watch.today', return_value=DAY):
            self.assertEqual(self.watch(profile=profile)[0][0]['event'], 'new')
            self.bzp.notices = (replace(self.notice, deadline=datetime(2026, 11, 9, 10)),)
            events, _ = self.watch(profile=profile)
        self.assertEqual(events[0]['changes'], ['deadline'])

    def test_bzp_amendment_updates_previously_reported_notice_outside_window(self):
        self.watch()
        self.assertEqual(self.watch(end=DAY + timedelta(days=31))[0], [])
        end = DAY + timedelta(days=45)
        raw = dict(objectId='correction', noticeType='NoticeUpdateNotice',
                   noticeNumber='2026/BZP 00528560/01', publicationDate=end.isoformat(),
                   htmlBody='<h3>3.2.) Numer zmienianego ogłoszenia w BZP: 2026/BZP 00505877</h3>'
                            '<h3>3.3.) Identyfikator ostatniej wersji zmienianego ogłoszenia: 01</h3>'
                            '<h3>3.4.1.) Opis zmiany, w tym tekst, który należy dodać lub zmienić: </h3>'
                            '8.1. Termin składania ofert <p>Przed zmianą: <br/>2026-11-16 10:00</p>'
                            '<p>Po zmianie: <br/>2026-12-30 10:00</p>')
        self.bzp.notices = (bzp_amendment(raw),)
        events, _ = self.watch(end=end)
        self.assertEqual(events[0]['changes'], ['version', 'deadline'])
        self.assertEqual(events[0]['notice']['number'], self.notice.number)
        self.assertEqual(events[0]['notice']['deadline'], '2026-12-30T10:00:00+01:00')
        self.assertEqual(events[0]['amendment']['number'], raw['noticeNumber'])
        self.assertEqual(self.watch(end=end)[0], [])

    def test_ted_amendment_chain_keeps_one_notice_identity(self):
        ids = [f'00000000-0000-4000-8000-{value:012d}' for value in range(3)]
        original = Notice('TED', '100-2026', published=DAY,
                          raw={'notice-identifier': ids[0], 'notice-version': '01'})
        ted = Source(original)
        self.assertEqual(self.watch({'TED': ted})[0][0]['event'], 'new')
        for index in (1, 2):
            end = DAY + timedelta(days=40 * index)
            reference = '00000100-2026' if index == 1 else ids[index - 1] + '-01'
            correction = replace(original, number=f'{100 + index}-2026', published=end,
                                 raw={'notice-identifier': ids[index], 'notice-version': '01',
                                      'change-notice-version-identifier': [reference]})
            ted.notices = (correction,)
            events, _ = self.watch({'TED': ted}, end=end)
            self.assertEqual(events[0]['event'], 'updated')
            self.assertEqual(events[0]['changes'], ['version'])

    def test_webhook_failure_keeps_stable_events_for_retry(self):
        with patch('tenderkit.watch.build_opener') as build:
            request = build.return_value.open
            request.side_effect = OSError('connection failed')
            with self.assertRaisesRegex(RuntimeError, 'retained for retry'):
                self.watch(webhook='http://localhost/hook')
            staged = latest_updates(State(self.path), self.profile.name)
            request.side_effect = None
            request.return_value.__enter__.return_value.status = 204
            events, _ = self.watch(webhook='http://localhost/hook')
            sent = request.call_args.args[0]
            self.assertEqual(sent.get_method(), 'POST')
            self.assertEqual(json.loads(sent.data), {'events': staged['events']})
        self.assertEqual(events, staged['events'])
        self.assertEqual(self.watch()[0], [])

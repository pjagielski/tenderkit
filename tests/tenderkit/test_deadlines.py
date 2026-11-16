from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tenderkit.model import Notice
from tenderkit.profile import Profile
from tenderkit.serialization import notice_from_record
from tenderkit.service import search
from tenderkit.sources.mapping import ted_notice
from tenderkit.store.state import State


class DeadlinePrecisionTests(unittest.TestCase):
    def test_ted_joins_single_lot_only_and_reopens_old_date_only_records(self):
        raw = {'publication-number': '123-2026', 'identifier-lot': ['LOT-1'],
               'deadline-receipt-tender-date-lot': ['2026-11-16+01:00'],
               'deadline-receipt-tender-time-lot': ['12:30:00.123']}
        timed = ted_notice(raw)
        self.assertEqual(timed.deadline.isoformat(), '2026-11-16T12:30:00.123000+01:00')
        self.assertEqual(timed.deadline_precision, 'datetime')
        multi = ted_notice(dict(raw, **{'identifier-lot': ['LOT-1', 'LOT-2']}))
        self.assertEqual(multi.deadline_precision, 'date')
        old = notice_from_record(dict(source='TED', number='123-2026',
                                      deadline='2026-11-16T00:00:00+01:00', raw=raw))
        self.assertEqual(old.deadline_precision, 'date')

    def test_open_now_excludes_elapsed_unknown_and_exact_boundary(self):
        local_now = datetime(2026, 11, 16, 11, tzinfo=timezone.utc)
        published = date(2026, 11, 15)
        with TemporaryDirectory() as tmp:
            state = State(Path(tmp) / 'db.sqlite3')
            deadlines = [
                ('elapsed', '2026-11-16T09:00:00+01:00', 'datetime'),
                ('boundary', '2026-11-16T12:00:00+01:00', 'datetime'),
                ('timed', '2026-11-16T12:00:01+01:00', 'datetime'),
                ('unknown-hour', '2026-11-16T00:00:00+03:00', 'date'),
                ('tomorrow', '2026-11-17T00:00:00+03:00', 'date'),
                ('unknown-day', None, None),
            ]
            notices = [Notice('BZP', number, published=published,
                              deadline=datetime.fromisoformat(value) if value else None,
                              deadline_precision=precision) for number, value, precision in deadlines]
            state.record_refresh('BZP', published, published, {}, notices)
            with patch('tenderkit.service.datetime') as clock:
                clock.now.return_value = local_now
                result = search(state, Profile('open', open_now=True))
            self.assertEqual([row['number'] for row in result['notices']], ['timed', 'tomorrow'])

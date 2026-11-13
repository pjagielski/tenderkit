from datetime import date
from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tenderkit.documents import Documents
from tenderkit.model import Notice
from tenderkit.pdf_ocr import _recognize
from tenderkit.read_document import TextReadError, _bounded_text, read_document
from tenderkit.store.state import State


@unittest.skipUnless(find_spec('pymupdf'), 'Install the documents extra')
class PdfOcrTests(unittest.TestCase):
    def setUp(self):
        import pymupdf

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'scan.pdf'
        with pymupdf.open() as original, pymupdf.open() as mixed:
            original.new_page(width=400, height=160).insert_text((25, 70), 'Dostawa lekow', fontsize=26)
            scan = mixed.new_page(width=400, height=160)
            scan.insert_image(scan.rect, pixmap=original[0].get_pixmap(dpi=150))
            mixed.new_page().insert_text((25, 70), 'Native PDF text')
            mixed.save(self.path, deflate=True)
        state = State(self.root / 'state.sqlite3')
        self.service = Documents(state, self.root / 'documents')
        record = self.service.store.put(self.path)
        self.digest = record['sha256']
        day = date(2026, 11, 13)
        state.record_refresh('BZP', day, day, {}, [Notice('BZP', '1', published=day)])
        state.record_document('BZP', '1', dict(record, key='scan', name='scan.pdf', fetched_at=day.isoformat()))

    def test_scan_marker_and_per_page_cache(self):
        plain = self.service.read(self.digest, pages=2)
        self.assertEqual([page['text_layer'] for page in plain['pages']], [False, True])
        self.assertIsNone(plain['pages'][0]['text_source'])
        with patch('tenderkit.pdf_ocr._recognize', return_value='Dostawa leków') as recognize:
            result = self.service.read(self.digest, pages=2, ocr=True)
            self.assertEqual(result['pages'][0]['text_source'], 'ocr')
            self.assertFalse(result['pages'][0]['text_layer'])
            self.assertEqual(result['pages'][0]['text'], 'Dostawa leków')
            self.assertEqual(result['pages'][1], plain['pages'][1])
            self.assertEqual(self.service.read(self.digest, ocr=True)['pages'][0], result['pages'][0])
            recognize.assert_called_once()
            self.assertEqual(recognize.call_args.args[1], 1)
        self.assertEqual(self.service.read(self.digest)['pages'][0], plain['pages'][0])

    def test_failed_ocr_can_be_retried_and_native_page_skips_ocr(self):
        with patch('tenderkit.pdf_ocr._recognize', side_effect=[TextReadError('Timeout'), 'Recovered']) as run:
            self.assertEqual(self.service.read(self.digest, page=2, ocr=True)['pages'][0]['text_source'], 'embedded')
            run.assert_not_called()
            with self.assertRaisesRegex(TextReadError, 'Timeout'):
                self.service.read(self.digest, ocr=True)
            self.assertEqual(self.service.read(self.digest, ocr=True)['pages'][0]['text'], 'Recovered')
            self.assertEqual(run.call_count, 2)

    @unittest.skipUnless((Path(os.environ.get('TESSDATA_PREFIX', '')) / 'pol.traineddata').is_file(),
                         'Set TESSDATA_PREFIX to a directory with pol.traineddata')
    def test_real_ocr_on_image_only_page(self):
        result = read_document(self.path, ocr=True)
        self.assertIn('Dostawa lekow', result['pages'][0]['text'])
        self.assertEqual(result['pages'][0]['text_source'], 'ocr')
        self.assertFalse(result['pages'][0]['text_layer'])

    def test_ocr_extra_is_optional(self):
        with patch('tenderkit.pdf_ocr.importlib.util.find_spec', return_value=None):
            with self.assertRaisesRegex(TextReadError, r'tenderkit\[ocr\]'):
                _recognize(self.path, 1, time.monotonic() + 5)


class PdfProcessTests(unittest.TestCase):
    def test_child_is_terminated_on_timeout(self):
        with patch('tenderkit.read_document.subprocess.Popen') as popen, \
             patch('tenderkit.read_document.time.monotonic', side_effect=[0, 2]):
            process = popen.return_value
            process.poll.return_value = None
            with self.assertRaisesRegex(TextReadError, 'exceeded time'):
                _bounded_text(['worker'], Path('unused'), deadline=1, limit=100, operation='PDF OCR')
            process.kill.assert_called_once()
            process.wait.assert_called_once()

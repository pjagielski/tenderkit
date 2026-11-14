"""Legacy Office formats: XLS through xlrd, DOC through antiword or LibreOffice (issue #22)."""
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from zipfile import ZipFile

from tenderkit import read_document as reader
from tenderkit.read_document import read_document, TextReadError

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures/tenderkit/documents'

try:
    import xlrd  # noqa: F401
except ImportError:
    xlrd = None


def fake_tool(directory, name, body):
    """An executable that runs a Python body with the converter's arguments in sys.argv."""
    script = Path(directory) / (name + '.py')
    script.write_text(textwrap.dedent(body), encoding='utf-8')
    if os.name == 'nt':
        launcher = Path(directory) / (name + '.cmd')
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
    else:
        launcher = Path(directory) / name
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding='utf-8')
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return str(launcher)


ANTIWORD = '''
    import sys
    assert sys.argv[1:5] == ['-m', 'UTF-8.txt', '-w', '0'], sys.argv
    sys.stdout.buffer.write('Formularz oferty – zażółć\\fStrona druga'.encode('utf-8'))
'''
SOFFICE = '''
    import sys
    from pathlib import Path
    args = sys.argv[1:]
    assert '--headless' in args and any(a.startswith('-env:UserInstallation=') for a in args), args
    out = Path(args[args.index('--outdir') + 1]); out.mkdir(parents=True, exist_ok=True)
    source = Path(args[-1])
    (out / (source.stem + '.txt')).write_text('\\ufeffZ LibreOffice: ' + source.name, encoding='utf-8')
'''
FAILING = 'import sys; sys.exit(1)\n'


@unittest.skipIf(xlrd is None, 'xlrd (documents extra) is not installed')
class XlsTests(unittest.TestCase):
    def test_sheets_cells_numbers_dates_and_booleans(self):
        result = read_document(FIXTURES / 'price-form.xls')
        self.assertEqual((result['format'], result['engine'], result['total_pages']), ('xls', 'xlrd', 2))
        page = result['pages'][0]
        self.assertEqual(page['label'], 'Pakiet 1')
        self.assertIn('C1: ilość', page['text'])
        self.assertIn('A2: 1\tB2: Nusinersen 28 mg/5 ml\tC2: 18\tD2: 1234.56\tE2: 2026-11-10', page['text'])
        self.assertEqual(result['next_page'], 2)
        second = read_document(FIXTURES / 'price-form.xls', page=2)['pages'][0]
        self.assertEqual((second['label'], second['text']), ('Pakiet 2', 'A1: Żółć\tB1: TRUE'))

    def test_malformed_workbook_is_a_read_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'broken.xls'
            path.write_bytes(b'\xd0\xcf\x11\xe0 not a workbook')
            with self.assertRaisesRegex(TextReadError, 'XLS'):
                read_document(path)

    def test_text_limit_applies(self):
        with mock.patch.object(reader, 'MAX_TEXT', 20), self.assertRaisesRegex(TextReadError, 'limit'):
            read_document(FIXTURES / 'price-form.xls')


class XlsWithoutXlrdTests(unittest.TestCase):
    def test_missing_extra_names_it(self):
        with mock.patch.dict(sys.modules, {'xlrd': None}), self.assertRaisesRegex(TextReadError, r'tenderkit\[documents\]'):
            read_document(FIXTURES / 'price-form.xls')


class DisguisedOoxmlTests(unittest.TestCase):
    def test_docx_saved_as_doc_reads_as_docx(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'formularz.doc'
            with ZipFile(path, 'w') as archive:
                archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                                 'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Treść</w:t></w:r></w:p></w:body></w:document>')
            result = read_document(path)
            self.assertEqual((result['format'], result['engine']), ('docx', 'stdlib-ooxml'))
            self.assertIn('Treść', result['pages'][0]['text'])


class DocTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.doc = self.root / 'załącznik.doc'
        self.doc.write_bytes(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\0' * 504)
        patcher = mock.patch.object(reader.shutil, 'which', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, **values):
        clean = {k: v for k, v in os.environ.items() if k not in ('TENDERKIT_ANTIWORD', 'TENDERKIT_SOFFICE')}
        return mock.patch.dict(os.environ, {**clean, **values}, clear=True)

    def test_antiword_text_split_on_page_breaks(self):
        with self.env(TENDERKIT_ANTIWORD=fake_tool(self.root, 'antiword', ANTIWORD)):
            result = read_document(self.doc, pages=2)
        self.assertEqual((result['format'], result['engine'], result['total_pages']), ('doc', 'antiword', 2))
        self.assertEqual([p['text'] for p in result['pages']], ['Formularz oferty – zażółć', 'Strona druga'])

    def test_libreoffice_when_antiword_fails(self):
        with self.env(TENDERKIT_ANTIWORD=fake_tool(self.root, 'antiword', FAILING),
                      TENDERKIT_SOFFICE=fake_tool(self.root, 'soffice', SOFFICE)):
            result = read_document(self.doc)
        self.assertEqual((result['engine'], result['pages'][0]['text']), ('libreoffice', 'Z LibreOffice: document.doc'))

    def test_failing_antiword_without_libreoffice_is_a_read_error(self):
        with self.env(TENDERKIT_ANTIWORD=fake_tool(self.root, 'antiword', FAILING)), \
                mock.patch.object(reader, '_soffice', return_value=None), \
                self.assertRaisesRegex(TextReadError, 'DOC text extraction failed'):
            read_document(self.doc)

    def test_no_converter_names_both_options(self):
        with self.env(), mock.patch.object(reader, '_soffice', return_value=None), \
                self.assertRaisesRegex(TextReadError, 'antiword or LibreOffice'):
            read_document(self.doc)

    def test_ocr_is_refused_for_doc(self):
        with self.assertRaisesRegex(TextReadError, 'OCR'):
            read_document(self.doc, ocr=True)


if __name__ == '__main__':
    unittest.main()

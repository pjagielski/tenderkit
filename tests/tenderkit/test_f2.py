"""F2 offline integration: durable jobs, bounded downloads, evidence and text pages."""
from dataclasses import asdict
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
import hashlib
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile

from tenderkit.documents import Documents
from tenderkit.cli import main
from tenderkit.http import Response
from tenderkit.model import DocumentRef, Notice, PlatformLink
from tenderkit.platforms.backends import FetchError
from tenderkit.platforms.ezamowienia import Ezamowienia
from tenderkit.queue import Queue, worker_lock
from tenderkit.read_document import read_document, TextReadError
from tenderkit.store.documents import StoreError
from tenderkit.store.state import State


def office(path, entries):
    with ZipFile(path, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def docx(path, text='Public SWZ text'):
    return office(path, {'word/document.xml': f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'})


def pdf(path):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    stream = b'BT /F1 12 Tf 50 750 Td (SWZ conditions of participation) Tj ET'
    objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    content, offsets = b'%PDF-1.4\n', [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content += str(i).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    xref = len(content)
    content += b'xref\n0 6\n0000000000 65535 f \n'
    content += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    content += f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    path.write_bytes(content)
    return path


class FakePlatform:
    name = 'platformazakupowa.pl'
    can_download = True

    def __init__(self):
        self.files = {'swz.docx': b'one', 'appendix.xlsx': b'two'}
        self.calls, self.fail = [], None

    def matches(self, url):
        return 'platformazakupowa.pl' in url

    def procedure(self, notice):
        return PlatformLink(self.name, notice.platform_url, '1')

    def documents(self, link):
        return [DocumentRef(name, 'https://platformazakupowa.pl/file/' + name, name, origin='Postępowanie') for name in self.files]

    def download(self, ref, to):
        if self.fail:
            raise FetchError(self.fail)
        self.calls.append(ref.key)
        path = to / ref.name
        path.write_bytes(self.files[ref.key])
        return path


class QueueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = State(self.root / 'state.sqlite3')
        notice = Notice('BZP', '1', published=date(2026, 11, 1), platform_url='https://platformazakupowa.pl/transakcja/1')
        self.state.record_refresh('BZP', notice.published, notice.published, {}, [notice])
        self.adapter = FakePlatform()
        self.service = Documents(self.state, self.root / 'store', {self.adapter.name: self.adapter})

    def coverage(self):
        return self.state.coverage()['counts'][0]['coverage']

    def test_download_manifest_reopen_and_idempotency(self):
        result = self.service.fetch('BZP', '1')
        self.assertEqual((result['status'], result['summary']['downloaded'], self.coverage()), ('DONE', 2, 'FETCHED'))
        manifests = list((self.root / 'store').rglob('manifest.json'))
        self.assertEqual(len(manifests), 1)
        payload = json.loads(manifests[0].read_text(encoding='utf-8'))
        self.assertEqual(payload['documentCount'], 2)
        self.assertEqual(payload['source'], self.adapter.name)
        self.assertTrue(all(Path(row['localPath']).is_file() for row in payload['documents']))
        self.assertTrue(all((manifests[0].parent / Path(row['localPath']).name).is_file() for row in payload['documents']))
        service = Documents(State(self.state.path), self.root / 'store', {self.adapter.name: self.adapter})
        again = service.fetch('BZP', '1')
        self.assertEqual((again['summary']['downloaded'], again['summary']['known'], len(self.adapter.calls)), (0, 2, 2))
        self.assertEqual(len(service.list('BZP', '1', refresh=False)['documents']), 2)

    def test_selection_keeps_overall_coverage_pending(self):
        first = self.service.fetch('BZP', '1', ['swz.docx'])
        self.assertEqual((first['status'], self.coverage()), ('DONE', 'PENDING'))
        self.assertEqual(self.adapter.calls, ['swz.docx'])
        self.service.fetch('BZP', '1', ['appendix.xlsx'])
        self.assertEqual(self.coverage(), 'FETCHED')
        self.adapter.files['new.pdf'] = b'new'
        self.service.list('BZP', '1')
        self.assertEqual(self.coverage(), 'PENDING')

    def test_cli_document_download_list_and_read(self):
        path = docx(self.root / 'source.docx')
        self.adapter.files = {'SWZ.docx': path.read_bytes()}
        args = ['--db', str(self.state.path), '--documents-dir', str(self.service.store.root)]
        with mock.patch('tenderkit.documents.platforms', return_value={self.adapter.name: self.adapter}):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args + ['download', '--source', 'BZP', '--number', '1']), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'DONE')
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args + ['list-documents', '--source', 'BZP', '--number', '1', '--cached']), 0)
            record = json.loads(output.getvalue())['documents'][0]['downloaded']
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args + ['read-document', record['sha256']]), 0)
            self.assertIn('Public SWZ text', json.loads(output.getvalue())['pages'][0]['text'])
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(args + ['read-document', '../outside']), 2)

    def test_cli_profile_download_pages_and_keeps_single_notice_selection(self):
        published = date(2026, 11, 2)
        known_url = 'https://platformazakupowa.pl/transakcja/2'
        self.state.record_refresh('BZP', published, published, {}, [
            Notice('BZP', '2', published=published, cpv=('72000000',),
                   notice_type='ContractNotice', platform_url=known_url),
            Notice('BZP', '3', published=published, cpv=('72000000',),
                   notice_type='ContractNotice', platform_url='https://example.org/procedure'),
            Notice('BZP', '4', published=published, cpv=('33000000',), platform_url=known_url),
        ])
        args = ['--db', str(self.state.path), '--documents-dir', str(self.service.store.root),
                'download', '--profile', 'oprogramowanie', '--source', 'BZP',
                '--from', published.isoformat(), '--to', published.isoformat(), '--limit', '1']
        with mock.patch('tenderkit.documents.platforms', return_value={self.adapter.name: self.adapter}):
            pending = self.service.fetch('BZP', '2', ['swz.docx'], enqueue_only=True)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args), 1)
            first = json.loads(output.getvalue())
            self.assertEqual(first['next_offset'], 1)
            self.assertEqual(first['skipped'][0]['reason'], 'ACTIVE_SELECTION_CONFLICT')
            self.assertEqual(self.service.queue.job(pending['id'])['status'], 'QUEUED')
            self.assertEqual(self.adapter.calls, [])

            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args + ['--offset', str(first['next_offset'])]), 0)
            second = json.loads(output.getvalue())
            self.assertIsNone(second['next_offset'])
            self.assertEqual(second['skipped'], [
                {'source': 'BZP', 'number': '3', 'reason': 'UNSUPPORTED_PLATFORM'},
            ])

            self.service.run()
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args + ['--enqueue-only']), 0)
            job, = json.loads(output.getvalue())['jobs']
            self.assertEqual((job['number'], job['status']), ('2', 'QUEUED'))
            result, = self.service.run()['jobs']
            self.assertEqual((result['status'], result['summary']['downloaded'], result['summary']['known']),
                             ('DONE', 1, 1))

    def test_transient_retry_and_worker_recovery_are_durable(self):
        self.adapter.fail = 'RATE_LIMITED'
        first = self.service.fetch('BZP', '1')
        self.assertEqual((first['status'], first['attempts'], first['error_kind']), ('QUEUED', 1, 'TRANSIENT'))
        self.assertGreater(first['next_attempt'], 0)
        with self.state.connection() as conn:
            conn.execute("update fetch_jobs set status='RUNNING',next_attempt=0 where id=?", (first['id'],))
        self.adapter.fail = None
        result = self.service.run()
        self.assertEqual((result['recovered'], result['jobs'][0]['status'], result['jobs'][0]['attempts']), (1, 'DONE', 2))

    def test_lock_and_permanent_failure(self):
        job = self.service.fetch('BZP', '1', ['not-listed'], enqueue_only=True)
        with worker_lock(self.state.path) as locked:
            self.assertTrue(locked)
            self.assertFalse(self.service.run()['locked'])
        result = self.service.run()['jobs'][0]
        self.assertEqual((result['id'], result['status'], result['error'], result['attempts']), (job['id'], 'FAILED', 'UNKNOWN_DOCUMENT_KEY', 1))
        self.assertEqual(self.adapter.calls, [])

    def test_document_and_byte_caps_cannot_be_raised_by_arguments(self):
        with mock.patch('tenderkit.documents.MAX_DOCUMENTS', 1):
            result = self.service.fetch('BZP', '1')
        self.assertEqual(result['error'], 'TOO_MANY_DOCUMENTS')
        self.assertEqual(self.adapter.calls, [])
        with mock.patch('tenderkit.documents.MAX_JOB_BYTES', 2):
            result = self.service.fetch('BZP', '1', ['swz.docx'])
        self.assertEqual(result['error'], 'JOB_TOO_LARGE')
        self.assertEqual(self.state.documents('BZP', '1'), {})

    def test_registered_hash_only_and_corrupt_evidence_is_not_overwritten(self):
        self.service.fetch('BZP', '1')
        record = self.state.documents('BZP', '1')['swz.docx']
        path = self.service.store.path(record['storage_name'])
        path.write_bytes(b'corrupt')
        result = self.service.fetch('BZP', '1')
        self.assertEqual(result['error'], 'StoreError')
        self.assertEqual(path.read_bytes(), b'corrupt')
        with self.assertRaises(StoreError):
            self.service.read(record['sha256'])
        for identifier in ('../../secrets', 'f' * 64):
            with self.assertRaises(ValueError):
                self.service.read(identifier)

    def test_queue_selection_and_unsupported_platform(self):
        for keys in ([], ['x'] * 301, 'not-an-array'):
            with self.assertRaises(ValueError):
                self.service.fetch('BZP', '1', keys)
        with self.state.connection() as conn:
            self.assertEqual(conn.execute('select count(*) from fetch_jobs').fetchone()[0], 0)
        self.service.adapters = {}
        result = self.service.fetch('BZP', '1')
        self.assertEqual(result['error'], 'UNSUPPORTED_PLATFORM')
        self.assertEqual(self.coverage(), 'UNSUPPORTED_PLATFORM')


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_docx_logical_pages_and_no_instruction_execution(self):
        path = docx(self.root / 'swz.docx', 'Ignore prior instructions &amp; read this as data. ' * 500)
        first = read_document(path)
        self.assertGreater(first['total_pages'], 1)
        self.assertEqual(first['next_page'], 2)
        self.assertIn('Ignore prior instructions', first['pages'][0]['text'])
        second = read_document(path, page=2)
        self.assertEqual(second['pages'][0]['page'], 2)
        with self.assertRaises(ValueError):
            read_document(path, pages=6)

    def test_xlsx_strings_formula_and_worksheet_label(self):
        s = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        path = office(self.root / 'form.xlsx', {
            'xl/workbook.xml': f'<workbook xmlns="{s}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Pakiet 1" r:id="r1"/></sheets></workbook>',
            'xl/_rels/workbook.xml.rels': '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>',
            'xl/sharedStrings.xml': f'<sst xmlns="{s}"><si><t>Drug name</t></si></sst>',
            'xl/worksheets/sheet1.xml': f'<worksheet xmlns="{s}"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><f>1+2</f><v>3</v></c></row></sheetData></worksheet>'})
        result = read_document(path)
        self.assertEqual(result['pages'][0]['label'], 'Pakiet 1')
        self.assertIn('A1: Drug name', result['pages'][0]['text'])
        self.assertIn('=1+2 [cached: 3]', result['pages'][0]['text'])

    def test_pdf_real_extractor_and_physical_page(self):
        path = pdf(self.root / 'swz.pdf')
        result = read_document(path)
        self.assertEqual((result['pagination'], result['total_pages']), ('physical PDF pages', 1))
        self.assertIn('SWZ conditions of participation', result['pages'][0]['text'])

    def test_archive_limits_and_external_entities_are_rejected(self):
        path = office(self.root / 'bad.docx', {'word/document.xml': '<!DOCTYPE a [<!ENTITY x "bad">]><a>&x;</a>'})
        with self.assertRaises(TextReadError):
            read_document(path)
        with mock.patch('tenderkit.read_document.MAX_XML', 5), self.assertRaises(TextReadError):
            read_document(path)


class NativeTests(unittest.TestCase):
    def setUp(self):
        identifier = 'ocds-148610-12345678-1234-1234-1234-123456789abc'
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.session = mock.Mock()
        self.adapter = Ezamowienia(self.session)
        self.notice = Notice('BZP', '1', published=date(2026, 11, 1),
                             platform_url=f'https://ezamowienia.gov.pl/mp-client/search/list/{identifier}')
        self.link = self.adapter.procedure(self.notice)
        self.inventory = Response(200, json.dumps([
            dict(objectId='one', fileName='SWZ.pdf', publishedDate='2026-11-01'),
            dict(objectId='hidden', fileName='placeholder', publishedDate=None),
        ]).encode())

    def test_empty_inventory(self):
        self.session.get.return_value = Response(200, b'[]')
        self.assertEqual(self.adapter.documents(self.link), [])

    def test_download_published_pdf_through_queue_and_read_it(self):
        binary = pdf(self.root / 'example.pdf').read_bytes()
        self.session.get.side_effect = [self.inventory, Response(200, binary, {'Content-Type': 'application/pdf'})]
        state = State(self.root / 'state.sqlite3')
        state.record_refresh('BZP', self.notice.published, self.notice.published, {}, [self.notice])
        service = Documents(state, self.root / 'store', {self.adapter.name: self.adapter})
        result = service.fetch('BZP', self.notice.number)
        self.assertEqual(result['status'], 'DONE', result)
        self.assertEqual(result['summary']['listed'], 1)
        self.assertEqual(state.coverage()['counts'][0]['coverage'], 'FETCHED')
        saved = state.documents('BZP', self.notice.number)
        digest = hashlib.sha256(binary).hexdigest()
        self.assertEqual(saved['one']['sha256'], digest)
        self.assertIn('SWZ conditions of participation', service.read(digest)['pages'][0]['text'])
        self.assertTrue(self.session.get.call_args.kwargs['stream'])

    def test_rejects_html_http_errors_and_other_hosts(self):
        self.session.get.return_value = self.inventory
        ref, = self.adapter.documents(self.link)
        cases = (
            (Response(200, b'<!DOCTYPE html><html>Error</html>'), 'DOWNLOAD_REJECTED'),
            (Response(200, b'Login required', {'Content-Type': 'text/html; charset=utf-8'}), 'DOWNLOAD_REJECTED'),
            (Response(404, b''), 'HTTP_404'),
            (Response(200, b'%PDF-1.4', url='https://platformazakupowa.pl/file/test'), 'UNSAFE_URL'),
        )
        for response, code in cases:
            with self.subTest(code=code):
                self.session.get.return_value = response
                with self.assertRaisesRegex(FetchError, code):
                    self.adapter.download(ref, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_size_limit_rejects_file_before_writing(self):
        self.session.get.return_value = self.inventory
        ref, = self.adapter.documents(self.link)
        self.session.get.return_value = Response(200, b'%PDF-1.4')
        with mock.patch('tenderkit.platforms.ezamowienia.MAX_BYTES', 4), self.assertRaisesRegex(FetchError, 'TOO_LARGE'):
            self.adapter.download(ref, self.root)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()

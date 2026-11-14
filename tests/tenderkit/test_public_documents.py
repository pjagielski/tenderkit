import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from tenderkit.http import Response
from tenderkit.model import Notice
from tenderkit.platforms import detect, platforms
from tenderkit.platforms.backends import FetchError
from tenderkit.platforms.session import PlatformRedirect


EB2B = 'https://example.eb2b.com.pl'
SIGNED_FILE = 'https://eb2b-platforma.s3.eu-central-1.amazonaws.com/file?X-Amz-Signature=test'
PROCEDURE_ID = 'aaaaaaaa-1111-2222-3333-000000000000'
DOCUMENT_ID = 'bbbbbbbb-1111-2222-3333-000000000000'
PDF = b'%PDF-1.7\nexample'


class PublicDocumentsTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.adapters = platforms(self.session)

    def procedure(self, url, reference=None):
        notice = Notice('BZP', '1', platform_url=url, reference=reference)
        adapter = detect(notice, self.adapters)
        return adapter, adapter.procedure(notice)

    def test_epropublico_lists_documents_by_procedure_uuid(self):
        page = 'https://e-propublico.pl/Ogloszenia/Details/' + PROCEDURE_ID + '?example=1'
        adapter, link = self.procedure(page)
        html = f"""<h5 class="section-header">SWZ</h5><table><tr>
            <td><a href="/Dokumenty/Download/{DOCUMENT_ID}">SWZ</a></td>
            <td></td><td>.pdf</td><td></td><td>2026-11-14</td>
            </tr></table>"""
        self.session.get.return_value = Response(200, html.encode())
        ref, = adapter.documents(link)
        self.assertEqual((ref.key, ref.name, ref.ext, ref.published),
                         (DOCUMENT_ID, 'SWZ', '.pdf', '2026-11-14'))
        self.assertEqual(self.session.get.call_args.kwargs['params'], {'przetargId': PROCEDURE_ID})

    def test_logintrade_lists_and_downloads_public_file(self):
        page = 'https://example.logintrade.net/zapytania_email,123,abcdef.html'
        adapter, link = self.procedure(page)
        html = """<div class="attachments"><div class="attachment">
            <a href="DocumentService,getAttachmentUnlogged,ab%3D.html">
            <span class="attachment-name">SWZ.pdf</span></a>
            <span class="attachment-date">2026-11-14</span></div></div>"""
        self.session.get.return_value = Response(200, html.encode())
        ref, = adapter.documents(link)
        self.assertEqual((ref.key, ref.name, ref.published), ('ab=', 'SWZ.pdf', '2026-11-14'))
        self.session.get.return_value = Response(200, PDF, {'Content-Type': 'application/pdf'}, ref.url)
        with tempfile.TemporaryDirectory() as tmp:
            path = adapter.download(ref, Path(tmp))
            self.assertEqual(path.name, hashlib.sha256(PDF).hexdigest() + '.pdf')
            self.assertEqual(path.read_bytes(), PDF)

            for body, limit, error in ((b'<html>Login</html>', 1024, 'DOWNLOAD_REJECTED'),
                                       (PDF, 1, 'TOO_LARGE')):
                self.session.get.return_value = Response(200, body, url=ref.url)
                with patch('tenderkit.platforms.public_documents.MAX_BYTES', limit):
                    with self.assertRaisesRegex(FetchError, error):
                        adapter.download(ref, Path(tmp))
                self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_eb2b_uses_exact_reference_and_excludes_other_attachments(self):
        public = dict(id=12, is_active=True, is_current=True, is_public=True, contractor_id=None,
                      name='SWZ.pdf', extension='pdf')
        rows = [public, dict(public, id=13, contractor_id=2), dict(public, id=14, is_public=False),
                dict(public, id=15, is_current=False), dict(public, id=16, is_active=False)]
        matches = [dict(id=1, auction_number='ZZ/123-extra'), dict(id=2, auction_number='ZZ/123')]
        self.session.get.side_effect = [Response(200, json.dumps({'success': True, 'data': data}).encode())
                                        for data in (matches, rows)]
        adapter, link = self.procedure(EB2B, 'ZZ/123')
        self.assertEqual((link.key, link.url), ('2', EB2B + '/open-preview-auction.html/2'))
        ref, = adapter.documents(link)
        self.assertEqual(ref.key, '12')
        self.session.get.side_effect = None
        self.session.get.return_value = Response(200, PDF, url=SIGNED_FILE)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(adapter.download(ref, Path(tmp)).read_bytes(), PDF)

    def test_redirect_only_allows_expected_signed_file(self):
        handler = PlatformRedirect()
        file = EB2B + '/auction/auction/download-attachment/id/12'
        alien_targets = (
            'https://different.eb2b.com.pl/file',
            'https://other.s3.eu-central-1.amazonaws.com/file?X-Amz-Signature=test',
            SIGNED_FILE.split('?')[0],
        )
        self.assertEqual(handler.redirect_request(Request(file), None, 302, '', {}, SIGNED_FILE).full_url,
                         SIGNED_FILE)
        for target in alien_targets:
            with self.subTest(target=target), self.assertRaisesRegex(FetchError, 'UNSAFE_URL'):
                handler.redirect_request(Request(file), None, 302, '', {}, target)
        with self.assertRaisesRegex(FetchError, 'UNSAFE_URL'):
            handler.redirect_request(Request(SIGNED_FILE), None, 302, '', {}, file)

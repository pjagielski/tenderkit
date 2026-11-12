"""Frozen anonymous public-response fragments, with the legacy parser's expected inventories."""
from dataclasses import asdict
import json
from pathlib import Path
import unittest
from urllib.request import Request

from tenderkit.http import Response
from tenderkit.model import Notice
from tenderkit.platforms import platforms, detect
from tenderkit.platforms.backends import FetchError
from tenderkit.platforms.session import PlatformRedirect

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures/tenderkit/platforms'


class Frozen:
    def __init__(self, responses):
        self.responses = responses

    def get(self, url, **kwargs):
        return Response(200, self.responses[url].encode('utf-8'))


class PlatformFixturesTests(unittest.TestCase):
    def test_public_document_sets_match_frozen_legacy_inventories(self):
        for path in sorted(FIXTURES.glob('*.json')):
            with self.subTest(platform=path.stem):
                fixture = json.loads(path.read_text(encoding='utf-8'))
                notice = Notice('FIXTURE', path.stem, platform_url=fixture['url'])
                adapter = detect(notice, platforms(Frozen(fixture['responses'])))
                result = [asdict(ref) for ref in adapter.documents(adapter.procedure(notice))]
                self.assertEqual(result, fixture['expected'])
                self.assertGreater(len(result), 0)
        self.assertEqual(len(list(FIXTURES.glob('*.json'))), 3)

    def test_anonymous_sso_probe_is_allowed_but_login_and_other_hosts_are_rejected(self):
        redirect = PlatformRedirect()
        request = Request('https://platformazakupowa.pl/transakcja/1')
        base = 'https://accounts.opennexus.com/sso/v1/oauth/authorize?redirect_uri=https%3A%2F%2Fplatformazakupowa.pl%2Fsso%2Fauthorize&prompt='
        next_request = redirect.redirect_request(request, None, 302, '', {}, base + 'none')
        self.assertEqual(next_request.full_url, base + 'none')
        for url in (base + 'login', 'https://evil.invalid/file', base.replace('platformazakupowa.pl%2Fsso', 'evil.invalid%2Fsso') + 'none'):
            with self.assertRaises(FetchError):
                redirect.redirect_request(request, None, 302, '', {}, url)

    def test_native_redirect_stays_on_the_same_https_host(self):
        redirect = PlatformRedirect()
        request = Request('https://ezamowienia.gov.pl/mp-readmodels/api/Tender/DownloadDocument/example')
        for url in ('https://platformazakupowa.pl/file/example', 'http://ezamowienia.gov.pl/file/example'):
            with self.subTest(url=url), self.assertRaisesRegex(FetchError, 'UNSAFE_URL'):
                redirect.redirect_request(request, None, 302, '', {}, url)


if __name__ == '__main__':
    unittest.main()

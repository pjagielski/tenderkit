"""Cookie-preserving, paced public platform HTTP; redirects stay on platform hosts."""
from http.cookiejar import CookieJar
import time
import re
from urllib.parse import urlsplit, parse_qs
from urllib.request import build_opener, HTTPCookieProcessor, HTTPRedirectHandler

from tenderkit.http import Session
from tenderkit.platforms.backends import FetchError

BK_HOST = 'bazakonkurencyjnosci.funduszeeuropejskie.gov.pl'
DOMAINS = ('platformazakupowa.pl', 'ezamawiajacy.pl', 'ezamowienia.gov.pl')
DOCUMENT_DOMAINS = ('e-propublico.pl', 'logintrade.net', 'eb2b.com.pl')


def tenant_url(url, domain):
    try:
        parts = urlsplit(url)
        host = parts.hostname or ''
        return (parts.scheme == 'https' and parts.username is None and parts.port in (None, 443)
                and (host == domain or host.endswith('.' + domain)))
    except ValueError:
        return False


def eb2b_file_redirect(source, target):
    if not tenant_url(source, 'eb2b.com.pl') or not tenant_url(target, 's3.eu-central-1.amazonaws.com'):
        return False
    original, destination = urlsplit(source), urlsplit(target)
    return (re.fullmatch(r'/auction/auction/download-attachment/id/[1-9][0-9]*', original.path) is not None
            and destination.hostname == 'eb2b-platforma.s3.eu-central-1.amazonaws.com'
            and bool(parse_qs(destination.query).get('X-Amz-Signature')))


def bk_platform_url(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.hostname == BK_HOST
                and parts.username is None and parts.port in (None, 443))
    except ValueError:
        return False


def native_platform_url(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.hostname == 'ezamowienia.gov.pl'
                and parts.username is None and parts.port in (None, 443))
    except ValueError:
        return False


def public_platform_url(url):
    if bk_platform_url(url):
        return True
    if any(tenant_url(url, domain) for domain in DOCUMENT_DOMAINS):
        return True
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        return (parts.scheme in ('http', 'https') and not parts.username and not parts.password
                and parts.port in (None, 80, 443)
                and any(host == domain or host.endswith('.' + domain) for domain in DOMAINS))
    except ValueError:
        return False


class PlatformRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        source = request.full_url
        if eb2b_file_redirect(source, newurl):
            return super().redirect_request(request, response, code, message, headers, newurl)
        if urlsplit(source).hostname == 'eb2b-platforma.s3.eu-central-1.amazonaws.com':
            raise FetchError('UNSAFE_URL')
        for domain in DOCUMENT_DOMAINS:
            if tenant_url(source, domain) and (not tenant_url(newurl, domain)
                    or urlsplit(source).hostname != urlsplit(newurl).hostname):
                raise FetchError('UNSAFE_URL')
        if bk_platform_url(request.full_url) and not bk_platform_url(newurl):
            raise FetchError('UNSAFE_URL')
        if urlsplit(request.full_url).hostname == 'ezamowienia.gov.pl' and not native_platform_url(newurl):
            raise FetchError('UNSAFE_URL')
        if not public_platform_url(newurl):
            target = urlsplit(newurl)
            query = parse_qs(target.query)
            callback = query.get('redirect_uri', [''])[0]
            anonymous_probe = (
                request.get_method() == 'GET' and urlsplit(request.full_url).hostname in ('platformazakupowa.pl', 'www.platformazakupowa.pl')
                and target.scheme == 'https' and target.hostname == 'accounts.opennexus.com'
                and not target.username and target.port in (None, 443)
                and target.path == '/sso/v1/oauth/authorize' and query.get('prompt') == ['none']
                and public_platform_url(callback) and urlsplit(callback).hostname in ('platformazakupowa.pl', 'www.platformazakupowa.pl'))
            if not anonymous_probe:
                raise FetchError('UNSAFE_URL')
        return super().redirect_request(request, response, code, message, headers, newurl)


class PlatformSession(Session):
    def __init__(self, delay=2.1, sleep=time.sleep):
        super().__init__(build_opener(HTTPCookieProcessor(CookieJar()), PlatformRedirect()))
        self.delay, self.sleep = delay, sleep

    def request(self, method, url, **kwargs):
        if not public_platform_url(url):
            raise FetchError('UNSAFE_URL')
        self.sleep(self.delay)
        response = super().request(method, url, **kwargs)
        if response.url and not (public_platform_url(response.url) or eb2b_file_redirect(url, response.url)):
            response.close()
            raise FetchError('LOGIN_REQUIRED')
        if response.status_code >= 400:
            response.close()
            raise FetchError(f'HTTP_{response.status_code}')
        return response

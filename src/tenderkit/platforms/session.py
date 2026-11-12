"""Cookie-preserving, paced public platform HTTP; redirects stay on platform hosts."""
from http.cookiejar import CookieJar
import time
from urllib.parse import urlsplit, parse_qs
from urllib.request import build_opener, HTTPCookieProcessor, HTTPRedirectHandler

from tenderkit.http import Session
from tenderkit.platforms.backends import FetchError

DOMAINS = ('platformazakupowa.pl', 'ezamawiajacy.pl', 'ezamowienia.gov.pl')


def native_platform_url(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.hostname == 'ezamowienia.gov.pl'
                and parts.username is None and parts.port in (None, 443))
    except ValueError:
        return False


def public_platform_url(url):
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
        if response.url and not public_platform_url(response.url):
            response.close()
            raise FetchError('LOGIN_REQUIRED')
        if response.status_code >= 400:
            response.close()
            raise FetchError(f'HTTP_{response.status_code}')
        return response

"""Clean notice-supplied public URLs and recognize known procedure routes."""
import re
from urllib.parse import unquote, urlsplit, urlunsplit

_DOMAIN = re.compile(r'(?:[\w-]+\.)+[\w-]{2,63}', re.UNICODE)


def normalize_platform_url(value):
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip('.,;')
    while value.endswith(')') and value.count(')') > value.count('('):
        value = value[:-1].rstrip('.,;')
    if not value or any(char.isspace() or ord(char) < 32 for char in value) or '\\' in value:
        return None
    if value.startswith('//'):
        value = 'https:' + value
    elif not re.match(r'^[a-z][a-z0-9+.-]*://', value, re.I):
        value = 'https://' + value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').rstrip('.')
        if (parts.scheme not in ('http', 'https') or parts.username is not None
                or not _DOMAIN.fullmatch(host) or '%' in host):
            return None
        port = parts.port
    except ValueError:
        return None
    return urlunsplit((parts.scheme, f'{host}:{port}' if port is not None else host,
                       parts.path, parts.query, parts.fragment))


def platform_url_specific(url):
    """True for a recognized direct procedure route; directory and unknown routes return False."""
    if not url:
        return False
    from tenderkit.platforms.backends import Marketplanet
    from tenderkit.platforms.ezamowienia import TENDER_ID
    from tenderkit.platforms.public_documents import Eb2b, EProPublico, LogIntrade

    parts = urlsplit(url)
    if parts.port not in (None, 80 if parts.scheme == 'http' else 443):
        return False
    host, path = parts.hostname or '', unquote(parts.path).rstrip('/')
    if host == 'ezamowienia.gov.pl':
        return bool(re.fullmatch(fr'/mp-client/search/list/{TENDER_ID}', path))
    if host in ('platformazakupowa.pl', 'www.platformazakupowa.pl'):
        return bool(re.fullmatch(r'/transakcja/[1-9][0-9]*', path))
    if host == 'ezamawiajacy.pl' or host.endswith('.ezamawiajacy.pl'):
        return bool(Marketplanet.NOTICE.fullmatch(path))
    if host == 'bazakonkurencyjnosci.funduszeeuropejskie.gov.pl':
        return bool(re.fullmatch(r'/ogloszenia/[1-9][0-9]*', path))
    for adapter in (Eb2b, EProPublico, LogIntrade):
        if host == adapter.domain or host.endswith('.' + adapter.domain):
            return parts.scheme == 'https' and bool(adapter.procedure_path.fullmatch(path))
    return False

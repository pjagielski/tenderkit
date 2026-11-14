"""Fetch failure categories shared by standalone queue policy."""
import re

TRANSIENT, UNCERTAIN, PERMANENT, NONE = 'TRANSIENT', 'UNCERTAIN', 'PERMANENT', 'NONE'

ACTIVE = ('QUEUED', 'RUNNING')

PERMANENT_ERRORS = frozenset({'NO_REFERENCE', 'NOTICE_MISSING', 'TOO_LARGE', 'REFERENCE_MISMATCH',
    'UNSAFE_URL', 'UNSAFE_PATH', 'LOGIN_REQUIRED', 'LIST_ONLY', 'UNSUPPORTED_PLATFORM',
    'TOO_MANY_DOCUMENTS', 'JOB_TOO_LARGE', 'UNKNOWN_DOCUMENT_KEY', 'StoreError', 'PATH_TOO_LONG'})

UNCERTAIN_ERRORS = frozenset({'NOT_FOUND'})

TRANSIENT_ERRORS = frozenset({
    'RATE_LIMITED', 'DOWNLOAD_REJECTED', 'WORKER_LOST', 'URLError',
    'Timeout', 'ReadTimeout', 'ConnectTimeout', 'ConnectionError', 'ChunkedEncodingError', 'SSLError', 'ProxyError',
    'ReadTimeoutError', 'ProtocolError', 'IncompleteRead', 'RemoteDisconnected',
    'TimeoutError', 'ConnectionResetError', 'ConnectionAbortedError', 'ConnectionRefusedError', 'BrokenPipeError'})

HTTP_ERROR = re.compile(r'HTTP_(\d{3})')

def classify_error(status, error=None):
    """The kind of a finished job: TRANSIENT (try again soon), UNCERTAIN (one more try later), PERMANENT (never again) or
    NONE (it did not fail); None while the job is still QUEUED or RUNNING.

    A platform without an adapter (UNSUPPORTED) is permanent. For FAILED the `error` code decides: HTTP 408, 429 and 5xx
    are transient, 401 and 403 uncertain (a block may lift), the other 4xx permanent (the buyer's page is gone: 404 on
    szpzlowesola); NOT_FOUND is uncertain; the codes that say the notice gives nothing to search by are permanent.
    A code or exception this does not know is uncertain: it may be fixed by a deploy, so it gets one more try.
    """
    if status in ACTIVE:
        return None
    if status in ('DONE', 'CANCELLED'):
        return NONE
    if status == 'UNSUPPORTED':
        return PERMANENT
    if error in PERMANENT_ERRORS:
        return PERMANENT
    if error in UNCERTAIN_ERRORS:
        return UNCERTAIN
    if error in TRANSIENT_ERRORS:
        return TRANSIENT
    found = HTTP_ERROR.fullmatch(error or '')
    if found:
        code = int(found[1])
        if code in (408, 429) or code >= 500:
            return TRANSIENT
        if code in (401, 403):
            return UNCERTAIN
        if 400 <= code < 500:
            return PERMANENT
    return UNCERTAIN

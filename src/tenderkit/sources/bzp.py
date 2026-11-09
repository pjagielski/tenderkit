"""Bounded BZP pagination shared by ingestion and read-only discovery."""
from datetime import datetime, timedelta, timezone
import time
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo('Europe/Warsaw')
FRESH_NOTICE_TYPES = ('ContractNotice', 'SmallContractNotice', 'CompetitionNotice')
ENDPOINT = 'https://ezamowienia.gov.pl/mo-board/api/v1/notice'


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Publication timestamp has no timezone')
    return result.astimezone(timezone.utc)


def bounds(date_from, date_to):
    if date_from > date_to:
        raise ValueError('Invalid date interval')
    start = datetime.combine(date_from, datetime.min.time(), WARSAW)
    end = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), WARSAW)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


class FetchFailure(RuntimeError):
    def __init__(self, message, audit, notices):
        super().__init__(message)
        self.audit, self.notices = audit, notices


def fetch_window(session, date_from, date_to, kind, cpv_code='336', page_size=500,
                 delay=0.3, observed_at=None, save_page=None, max_pages=100,
                 retries=3):
    """Fetch broad API date bounds, then enforce exact Warsaw bounds locally.

    save_page is called before validation, preserving malformed/error evidence.
    Failed windows carry partial notices but must never be marked complete.
    """
    if not 1 <= page_size <= 500 or max_pages < 1 or retries < 1:
        raise ValueError('Invalid pagination configuration')
    start, end = bounds(date_from, date_to)
    observed_at = observed_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError('Observation timestamp has no timezone')
    cutoff = min(end, observed_at)
    params = dict(NoticeType=kind, PageSize=page_size,
                  PublicationDateFrom=start.strftime('%Y-%m-%dT00:00:00'),
                  PublicationDateTo=end.strftime('%Y-%m-%dT23:59:59'))
    if cpv_code:
        params['CpvCode'] = cpv_code
    audit = dict(notice_type=kind, from_date=str(date_from), to_date=str(date_to),
                 start_inclusive=start.isoformat(), end_exclusive=end.isoformat(),
                 observed_at=observed_at.isoformat(), pages=0, returned_unique=0,
                 included=0, duplicates=0, outside_window=0, complete=False,
                 provisional=date_to >= observed_at.astimezone(WARSAW).date())
    seen, accepted = {}, {}
    try:
        for page_number in range(1, max_pages + 1):
            for attempt in range(retries):
                if delay:
                    time.sleep(delay * (2 ** attempt))
                try:
                    response = session.get(ENDPOINT, params=params,
                                           headers={'Accept': 'application/json'}, timeout=40)
                    response.raise_for_status()
                    page = response.json()
                    break
                except Exception:
                    if attempt == retries - 1:
                        raise
            if save_page:
                save_page(kind, page_number, dict(params), page)
            audit['pages'] += 1
            if not isinstance(page, list):
                raise ValueError('Expected notice array, not an empty/error object')
            before = len(seen)
            for notice in page:
                if not isinstance(notice, dict) or not notice.get('objectId'):
                    raise ValueError('Notice is missing objectId')
                nid = notice['objectId']
                if nid in seen:
                    if seen[nid] != notice:
                        raise ValueError('Conflicting content for the same objectId')
                    audit['duplicates'] += 1
                    continue
                seen[nid] = notice
                audit['returned_unique'] = len(seen)
                if notice.get('noticeType') != kind:
                    raise ValueError('Unexpected notice type')
                published = timestamp(notice['publicationDate'])
                if not start <= published < cutoff:
                    audit['outside_window'] += 1
                    continue
                accepted[nid] = notice
                audit['included'] = len(accepted)
            if len(page) < page_size:
                audit['complete'] = not audit['provisional']
                audit['fetch_succeeded'] = True
                return list(accepted.values()), audit
            cursor = page[-1]['objectId']
            if len(seen) == before or cursor == params.get('SearchAfter'):
                raise ValueError('Pagination stalled')
            params['SearchAfter'] = cursor
        raise ValueError('Pagination safety limit reached')
    except Exception as exc:
        audit.update(error_type=type(exc).__name__, fetch_succeeded=False,
                     http_status=getattr(getattr(exc, 'response', None), 'status_code', None))
        # Avoid printing connection strings or response bodies from exceptions.
        raise FetchFailure(type(exc).__name__, audit, list(accepted.values())) from exc


class Bzp:
    name = 'BZP'

    def __init__(self, session=None, delay=2.1):
        from tenderkit.http import Session
        self.session, self.delay = session or Session(), delay

    def fetch(self, start, end, query):
        from tenderkit.sources.mapping import bzp_notice
        kinds = tuple(k for k in query.notice_types if k in FRESH_NOTICE_TYPES) if query.notice_types else FRESH_NOTICE_TYPES
        found = {}
        for kind in kinds:
            for prefix in query.cpv_prefixes or ('',):
                rows, _ = fetch_window(self.session, start, end, kind, cpv_code=prefix, delay=self.delay)
                for raw in rows:
                    notice = bzp_notice(raw)
                    found[notice.number] = notice
        return list(found.values())

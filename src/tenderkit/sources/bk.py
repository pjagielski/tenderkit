"""Bounded reads of public BK2021 notices from the portal's backend."""
from datetime import date, datetime, timezone
import re
from zoneinfo import ZoneInfo

from tenderkit.model import Lot, Notice
from tenderkit.platforms.session import BK_HOST, PlatformSession
from tenderkit.sources.mapping import deadline_precision, CPV, amount, html_to_text, instant

BASE = f'https://{BK_HOST}'
WARSAW = ZoneInfo('Europe/Warsaw')
NOTICE_TYPE = 'BKAnnouncement'
STATUSES = ('PUBLISHED', 'CLOSED', 'RESOLVED', 'CANCELLED')
MAX_TOTAL = 1000
# Publication-date ordering can repeat records across page boundaries.
# Request the bounded batch in one response when the portal permits it.
PAGE_SIZE = MAX_TOTAL
CPV_BATCH = 100


class BkIncomplete(RuntimeError):
    """The returned page or detail did not match the public search."""


def _data(session, path, params=None):
    response = session.get(BASE + path, params=params, timeout=30)
    response.raise_for_status()
    result = response.json()['data']
    if not isinstance(result, dict):
        raise BkIncomplete('Expected a BK data object')
    return result


def detail(session, identifier):
    row = _data(session, f'/api/announcements/{identifier}')['advertisement']
    if row['advertisement']['id'] != identifier or type(row['id']) is not int or row['id'] < 1:
        raise BkIncomplete('BK detail does not match the search result')
    return row


def _codes(session, prefixes):
    codes = set()
    for prefix in dict.fromkeys(prefixes):
        data = _data(session, '/api/dictionaries/cpv', {'search_key': prefix, 'request_id': 1})
        rows = data['cpv_items']
        if data['meta']['last_page'] != 1 or data['meta']['total'] != len(rows):
            raise BkIncomplete('Incomplete BK CPV dictionary')
        for row in rows:
            code = row['code']
            if not re.fullmatch(r'\d{8}-\d', code):
                raise BkIncomplete('Invalid BK CPV code')
            if code.startswith(prefix):
                codes.add(code)
    return sorted(codes)


def _list(session, start, end, codes):
    params = [('limit', PAGE_SIZE), ('sort', 'publicationDate'),
              ('publicationDateRange[from]', str(start)), ('publicationDateRange[to]', str(end))]
    params += [(f'status[{index}]', status) for index, status in enumerate(STATUSES)]
    params += [('cpvItem[]', code) for code in codes]
    found, expected, page = {}, None, 1
    while True:
        data = _data(session, '/api/announcements/search', [*params, ('page', page)])
        total, rows = data['meta']['total'], data['advertisements']
        if (type(total) is not int or not 0 <= total <= MAX_TOTAL
                or expected is not None and total != expected
                or not isinstance(rows, list) or len(rows) != min(PAGE_SIZE, total - len(found))):
            raise BkIncomplete('BK count changed or exceeded the fetch limit; narrow the window')
        expected = total
        for row in rows:
            identifier = row['id']
            published = date.fromisoformat(row['publication_date'])
            if type(identifier) is not int or identifier < 1 or identifier in found or not start <= published <= end:
                raise BkIncomplete('Duplicate or invalid BK search result')
            found[identifier] = row
        if len(found) == total:
            return found
        page += 1


def bk_notice(summary, row, acquired):
    identifier = summary['id']
    published = date.fromisoformat(row['publication_date'])
    if published != date.fromisoformat(summary['publication_date']):
        raise BkIncomplete('BK publication date changed during the fetch')
    descriptions, lots = [], []
    orders = row['orders']
    for order in orders:
        codes = []
        for item in order['order_items']:
            descriptions.append(html_to_text(item.get('description')).strip())
            codes.extend(match for cpv in item['cpv_items'] for match in CPV.findall(cpv['code']))
        estimate = amount(order.get('estimated_value'))
        lots.append(Lot(str(order['id']), title=order.get('title'), cpv=tuple(dict.fromkeys(codes)),
                        value=estimate, currency='PLN' if estimate is not None else None))
    # A single order's estimate can be used directly. Variants and partial orders
    # cannot be added safely without an explicit whole-notice total.
    value = lots[0].value if len(orders) == 1 and not orders[0].get('is_variant') else None
    deadline_value = row.get('submission_deadline')
    deadline = instant(deadline_value)
    if deadline is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=WARSAW)
    address = row.get('advertiser_address_details') or {}
    url = f'{BASE}/ogloszenia/{identifier}'
    return Notice(
        'BK', str(identifier), procedure_id=str(identifier), title=row['title'], published=published,
        deadline=deadline, deadline_precision=deadline_precision(deadline_value), buyer_name=summary.get('advertiser_name') or address.get('name'),
        buyer_city=(address.get('address') or {}).get('locality'),
        cpv=tuple(dict.fromkeys(code for lot in lots for code in lot.cpv)), lots=tuple(lots),
        description='\n\n'.join(dict.fromkeys(text for text in descriptions if text)) or None,
        value=value, currency='PLN' if value is not None else None, url=url, platform_url=url,
        reference=row['advertisement'].get('number'), notice_type=NOTICE_TYPE, lot_count=len(lots),
        raw={'version': row['id']}, source_attribution={
            'publisher': 'Ministerstwo Funduszy i Polityki Regionalnej',
            'acquired_at': acquired, 'processing': 'Selected and normalized from the public BK2021 notice',
            'url': url,
        })


class Bk:
    name = 'BK'

    def __init__(self, session=None):
        self.session = session or PlatformSession()

    def fetch(self, start, end, query):
        if query.notice_types and NOTICE_TYPE not in query.notice_types:
            return
        if any(not re.fullmatch(r'\d{1,8}', prefix) for prefix in query.cpv_prefixes):
            raise ValueError('Invalid BK CPV prefix')
        codes = _codes(self.session, query.cpv_prefixes)
        batches = [codes[i:i + CPV_BATCH] for i in range(0, len(codes), CPV_BATCH)] if query.cpv_prefixes else [()]
        found = {}
        for batch in batches:
            for identifier, row in _list(self.session, start, end, batch).items():
                if identifier in found and any(
                    found[identifier].get(key) != row.get(key)
                    for key in ('publication_date', 'submission_deadline', 'title', 'advertiser_name')
                ):
                    raise BkIncomplete('BK search result changed between CPV batches')
                found[identifier] = row
            if len(found) > MAX_TOTAL:
                raise BkIncomplete('BK detail request limit exceeded; narrow the window')
        for identifier, row in found.items():
            yield bk_notice(row, detail(self.session, identifier), datetime.now(timezone.utc).isoformat())

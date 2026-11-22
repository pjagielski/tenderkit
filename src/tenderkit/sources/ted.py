"""TED Search API v3 with bounded pagination and completeness checks."""
from dataclasses import replace
import re
import time

from tenderkit.http import Session
from tenderkit.sources.mapping import ted_notice

TED_SEARCH = 'https://api.ted.europa.eu/v3/notices/search'
TED_XML = 'https://ted.europa.eu/en/notice/{number}/xml'
# Search API fields only; the award XML below carries the per-lot join.
TED_FIELDS = ['publication-number', 'publication-date', 'procedure-identifier', 'title-proc', 'notice-title',
              'buyer-name', 'organisation-city-buyer', 'internal-identifier-proc', 'document-url-lot',
              'deadline-receipt-tender-date-lot', 'deadline-receipt-tender-time-lot', 'classification-cpv', 'identifier-lot', 'estimated-value-proc',
              'estimated-value-cur-proc', 'estimated-value-lot', 'estimated-value-cur-lot',
              'organisation-country-sub-buyer', 'organisation-identifier-buyer', 'notice-type',
              'notice-identifier', 'notice-version', 'change-notice-version-identifier',
              'description-proc', 'description-lot', 'title-lot']
NOTICE_KINDS = ('cn-standard', 'cn-social', 'pin-cfc-standard')
RESULT_KINDS = ('can-standard',)
SEARCH_KINDS = NOTICE_KINDS + RESULT_KINDS + ('pin-only', 'pin-rtl', 'corr')
MAX_AWARD_XML = 1000


class TedIncomplete(RuntimeError):
    """The source did not return exactly its declared set of publications."""


def cpv_condition(codes):
    if any(not re.fullmatch(r'\d{1,8}\*?', code) for code in codes):
        raise ValueError('Invalid CPV')
    terms = [f'classification-cpv={code}' for code in codes]
    return terms[0] if len(terms) == 1 else '(' + ' OR '.join(terms) + ')' if terms else ''


def fetch_notices(session, start, end, codes=(), kinds=('cn-standard',), *, mapper=ted_notice,
                  max_pages=100, delay=1.0):
    if start > end or not 1 <= max_pages <= 100:
        raise ValueError('Invalid TED window or pagination limit')
    if not kinds:
        return []
    if any(not re.fullmatch(r'[a-z][a-z0-9-]{0,50}', k) for k in kinds):
        raise ValueError('Invalid TED notice type')
    terms = ['buyer-country=POL', cpv_condition(codes)]
    kinds_query = [f'notice-type={k}' for k in kinds]
    terms.append(kinds_query[0] if len(kinds_query) == 1 else '(' + ' OR '.join(kinds_query) + ')')
    terms += [f'publication-date>={start:%Y%m%d}', f'publication-date<={end:%Y%m%d}']
    query = ' AND '.join(t for t in terms if t)
    rows, seen, expected, repeats = [], {}, None, 0
    for page in range(1, max_pages + 1):
        for attempt in range(6):
            response = session.post(TED_SEARCH, json=dict(query=query, fields=TED_FIELDS, limit=250, page=page), timeout=90)
            if response.status_code != 429:
                break
            time.sleep(10 * (attempt + 1))
        response.raise_for_status()
        body = response.json()
        batch, total = body['notices'], body.get('totalNoticeCount')
        if not isinstance(batch, list):
            raise ValueError('Expected TED notices array')
        if total is not None:
            if not isinstance(total, int) or total < 0 or expected is not None and total != expected:
                raise TedIncomplete('TED total changed during pagination')
            expected = total
            if expected > 15000:
                raise TedIncomplete('TED pagination supports 15000 notices; use a narrower window or profile')
        for raw in batch:
            number = raw['publication-number']
            if number in seen:
                # The TED index can hold one notice as identical rows, both counted in the total; differing rows
                # mean paging shifted under us.
                if seen[number] != raw:
                    raise TedIncomplete('TED repeated a publication during pagination')
                repeats += 1
                continue
            seen[number] = raw
            rows.append(mapper(raw))
        if expected is not None and len(rows) + repeats == expected:
            return rows
        if len(batch) < 250:
            if expected is not None and len(rows) + repeats != expected:
                raise TedIncomplete(f'{len(rows) + repeats} notices fetched, TED counts {expected}')
            return rows
        time.sleep(delay)
    raise TedIncomplete('TED pagination safety limit reached')


class Ted:
    name = 'TED'

    def __init__(self, session=None, xml_delay=1.0):
        self.session = session or Session()
        self.xml_delay = xml_delay

    def fetch(self, start, end, query):
        known = tuple(k for k in query.notice_types if k in SEARCH_KINDS)
        kinds = known or NOTICE_KINDS
        if query.include_results:
            kinds = kinds + tuple(k for k in RESULT_KINDS if k not in kinds)
        # SourceQuery carries prefixes; append * to short codes, keep full codes exact.
        codes = tuple(p + '*' if len(p) < 8 else p for p in query.cpv_prefixes)
        notices = fetch_notices(self.session, start, end, codes, kinds)
        if not any(kind in kinds for kind in RESULT_KINDS):
            return notices
        results = [notice for notice in notices if notice.notice_type in RESULT_KINDS]
        if len(results) > MAX_AWARD_XML:
            raise TedIncomplete(f'{len(results)} TED award notices exceed the {MAX_AWARD_XML} XML budget; narrow the window')
        return [self.awards(notice) if notice.notice_type in RESULT_KINDS else notice for notice in notices]

    def awards(self, notice):
        """Fill per-lot awards from the notice XML; a failed fetch keeps the search-API record intact."""
        try:
            if self.xml_delay:
                time.sleep(self.xml_delay)
            response = self.session.get(TED_XML.format(number=notice.number),
                                        headers={'Accept': 'application/xml'}, timeout=90)
            response.raise_for_status()
            from tenderkit.sources.ted_xml import parse_award_notice
            awards, lots = parse_award_notice(response.content)
            return replace(notice, awards=awards, lots=lots or notice.lots)
        except (OSError, ValueError, RuntimeError):
            return notice

"""One application path for CLI and MCP: fetch to SQLite, then filter the same notices."""
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib.metadata import entry_points
import json
import re
from statistics import median
from zoneinfo import ZoneInfo

from tenderkit.interfaces import Source
from tenderkit.notice_kinds import notice_kind
from tenderkit.profile import Profile, fold
from tenderkit.serialization import compact_raw, dumps, notice_record
from tenderkit.sources.bzp import AMENDMENT_TYPE, Bzp
from tenderkit.sources.bk import Bk
from tenderkit.sources.ted import Ted

MAX_WINDOW_DAYS = 120
MAX_NOTICES = 20000
MAX_RESULTS = 500


def today():
    return datetime.now(ZoneInfo('Europe/Warsaw')).date()


def window(start=None, end=None):
    end = date.fromisoformat(end) if isinstance(end, str) else end or today()
    start = date.fromisoformat(start) if isinstance(start, str) else start or end - timedelta(days=29)
    if not 0 <= (end - start).days < MAX_WINDOW_DAYS:
        raise ValueError(f'Publication window must contain 1 to {MAX_WINDOW_DAYS} days')
    return start, end


def sources(*, include_bk=False):
    registered = {'BZP': Bzp(), 'TED': Ted()}
    if include_bk:
        registered['BK'] = Bk()
    for entry in entry_points(group='tenderkit.sources'):
        source = entry.load()()
        if not isinstance(source, Source) or source.name in registered:
            raise ValueError(f'Invalid or duplicate source entry point: {entry.name}')
        registered[source.name] = source
    return registered


def refresh(state, profile, start, end, adapters=None, include_results=False, include_amendments=False):
    start, end = window(start, end)
    adapters = sources() if adapters is None else adapters
    summaries = []
    base_query = profile.source_query()
    for source, adapter in adapters.items():
        query = replace(base_query, include_results=include_results,
                        include_amendments=include_amendments or source == 'BZP')
        summaries.append(refresh_source(state, source, adapter, start, end, query))
    return summaries


def refresh_source(state, source, adapter, start, end, query, *, progress=None, job_id=None):
    """One atomic source window, shared by synchronous and background acquisition."""
    try:
        notices = []
        for notice in adapter.fetch(start, end, query):
            if len(notices) >= MAX_NOTICES:
                raise ValueError('Notice safety limit reached')
            if notice.source != source or notice.published is None or not start <= notice.published <= end:
                raise ValueError('Source returned a notice outside its declared window')
            notices.append(notice)
            if progress and (len(notices) == 1 or len(notices) % 25 == 0):
                progress(len(notices))
        if progress:
            progress(len(notices))
        state.record_refresh(source, start, end, asdict(query), notices, job_id=job_id)
        return dict(source=source, status='COMPLETED', notices=len(notices))
    except Exception as exc:
        error = type(exc).__name__
        cause, seen = exc, set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if isinstance(cause, TimeoutError) or type(cause).__name__ in ('Timeout', 'ReadTimeout', 'ConnectTimeout'):
                error = 'TimeoutError'
                break
            cause = cause.__cause__ or cause.__context__
        state.record_refresh(source, start, end, asdict(query), error=error, job_id=job_id)
        return dict(source=source, status='FAILED', error=error)


def filtered(state, profile, *, start=None, end=None, source_names=(), on=None):
    if start is not None or end is not None:
        start, end = window(start, end)
    now = datetime.now(ZoneInfo("Europe/Warsaw")) if profile.open_now else None
    on = on or (now.date() if now is not None else today())
    # The search index narrows candidates in SQLite; Profile.matches still decides every notice.
    for notice in state.notices(start, end, source_names, profile=profile, today=on):
        if (notice.source == 'BZP' and notice.notice_type == AMENDMENT_TYPE
                and AMENDMENT_TYPE not in profile.notice_types and 'corrigendum' not in profile.notice_kinds):
            continue
        selected = profile.select(notice, today=on, now=now)
        if selected is not None:
            yield selected


def procedure_publications(notices):
    """Group matching publications using source IDs and unambiguous buyer references."""
    by_procedure = defaultdict(list)
    for notice in notices:
        key = (notice.source, notice.procedure_id or notice.number, notice.procedure_id is not None)
        by_procedure[key].append(notice)

    by_reference = defaultdict(list)
    for key, group in by_procedure.items():
        if key[0] not in ('BZP', 'TED'):
            continue
        identities = set()
        for notice in group:
            buyer = re.sub(r'^PL|[ -]', '', (notice.buyer_id or '').upper())
            reference = ' '.join((notice.reference or '').upper().split())
            if re.fullmatch(r'[0-9]{10}', buyer) and reference:
                identities.add((buyer, reference))
        if len(identities) == 1:
            by_reference[identities.pop()].append(key)

    for keys in by_reference.values():
        if len(keys) == 2 and keys[0][0] != keys[1][0]:
            by_procedure[keys[0]].extend(by_procedure.pop(keys[1]))

    def newest(notice):
        digits = tuple(int(part) for part in re.findall(r'[0-9]+', notice.number))
        return (notice.published or date.min, notice.source, digits, notice.number)

    groups = [sorted(group, key=newest, reverse=True) for group in by_procedure.values()]
    groups.sort(key=lambda group: newest(group[0]), reverse=True)
    for representative, *related in groups:
        yield representative, [dict(source=notice.source, number=notice.number) for notice in related]


def search(state, profile, *, start=None, end=None, limit=100, offset=0, source_names=(),
           compact=False, include_total=False, dedupe=None):
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS or type(offset) is not int or not 0 <= offset <= MAX_NOTICES:
        raise ValueError('Invalid result limit or offset')
    if dedupe not in (None, 'procedure'):
        raise ValueError('dedupe must be procedure')
    if start is not None or end is not None:
        start, end = window(start, end)
    notices = filtered(state, profile, start=start, end=end, source_names=source_names)
    rows = procedure_publications(notices) if dedupe else ((notice, None) for notice in notices)
    found, total = [], 0
    for index, (notice, other_publications) in enumerate(rows):
        total = index + 1
        if offset <= index < offset + limit:
            record = compact_notice_record(notice, profile) if compact else matched_notice_record(notice, profile)
            if other_publications is not None:
                record['other_publications'] = other_publications
            found.append(record)
        elif index >= offset + limit and not include_total:
            break
    result = dict(notices=found, offset=offset, next_offset=offset + limit if total > offset + limit else None)
    if include_total:
        result['total'] = total
    result['coverage'] = state.search_coverage(profile, start, end, source_names)
    return result


def matched_notice_record(notice, profile, *, raw=False):
    record = notice_record(notice, raw=raw)
    if profile.keywords_any:
        record['keyword_matches'] = profile.keyword_matches(notice)
    return record


def compact_notice_record(notice, profile):
    # Compute keyword evidence against the complete notice before omitting its long text.
    evidence = profile.keyword_matches(notice) if profile.keywords_any else None
    summary = replace(notice, description=None, lots=(), lot_titles=(), awards=(), raw=compact_raw(notice.raw))
    record = notice_record(summary, raw=False)
    for key in ('description', 'lots', 'lot_titles', 'awards'):
        record.pop(key)
    record['lot_count'] = notice.lot_count if notice.lot_count is not None else max(len(notice.lots), len(notice.lot_titles))
    if evidence is not None:
        # Keep a compact summary even when a keyword matches hundreds of lots.
        matches = {(hit['keyword'], hit['field']): dict(keyword=hit['keyword'], field=hit['field'])
                   for hit in evidence}
        record['keyword_matches'] = list(matches.values())
    return record


def _award_record(award, notice):
    record = json_roundtrip(asdict(award))
    lot = next((item for item in notice.lots if item.number == award.lot), None)
    lot_title = lot.title if lot else None
    if not lot_title and len(notice.lots) == len(notice.lot_titles) == 1:
        lot_title = notice.lot_titles[0]
    return dict(record, source=notice.source, number=notice.number, procedure_id=notice.procedure_id, url=notice.url,
                buyer_region=notice.buyer_region, cpv=list(lot.cpv if lot and lot.cpv else notice.cpv),
                notice_kind=notice_kind(notice), lot_title=lot_title,
                quantity=str(lot.quantity) if lot and lot.quantity is not None else None,
                quantity_unit=lot.quantity_unit if lot else None,
                published=str(notice.published) if notice.published else None,
                title=notice.title, buyer_name=notice.buyer_name)


# Notice fields an award record repeats; a compact response lists them once per notice.
AWARD_NOTICE_FIELDS = ('procedure_id', 'url', 'buyer_region', 'notice_kind', 'published', 'title', 'buyer_name')
AWARD_STATUSES = ('AWARDED', 'CANCELLED')
# Sort keys for result awards: date is the award or contract date, published the notice date; '-' sorts descending.
RESULT_SORTS = ('price', '-price', 'date', '-date', 'published', '-published', 'offers', '-offers')


def lot_price(award):
    """The award's price when it prices this one lot, else None.

    TED states the scope: 'lot' is one lot's bid, while 'notice_total' (one bid for several lots) and 'framework_max'
    (a framework ceiling) would be counted once per lot, and an unmarked TED price is of unknown scope. A BZP award
    comes from its own lot section, so its price is that lot's."""
    if award.get('price') is None or not award.get('currency'):
        return None
    scope = award.get('price_scope')
    if scope == 'lot' or (scope is None and award['source'] != 'TED'):
        return Decimal(award['price'])
    return None


def keyword_scope(profile, award, notice):
    """'lot' when a keywords_any term names this award's subject: the lot title, or any searched field of a notice
    with a single lot; 'notice' when the terms matched only elsewhere in a multi-lot notice."""
    lots = {item.lot for item in notice.awards} | {lot.number for lot in notice.lots}
    if award.lot == '__all__' or len(lots) <= 1:
        return 'lot'
    lot = next((item for item in notice.lots if item.number == award.lot), None)
    return 'lot' if lot and lot.title and profile.mentions(lot.title) else 'notice'


def _sort_awards(awards, sort):
    field = sort.lstrip('-')
    value = {'price': lot_price, 'date': lambda a: a.get('date'), 'published': lambda a: a.get('published'),
             'offers': lambda a: a.get('offers')}[field]
    known = [award for award in awards if value(award) is not None]
    known.sort(key=value, reverse=sort.startswith('-'))
    return known + [award for award in awards if value(award) is None]       # missing values always last


def _number(value):
    return int(value) if value == int(value) else float(value)


def results_summary(awards):
    """Counts over every filtered award, not only the returned page.

    awards counts lot results (one latest result per source, procedure and lot); procedures counts distinct
    procedures; missing counts awarded lots without a price, contractor or date, and any lot without a bid count;
    prices sum only lot prices (see lot_price), per currency, and price_scope counts the awarded prices left out."""
    awarded = [a for a in awards if a['status'] == 'AWARDED']
    offers = sorted(a['offers'] for a in awards if a.get('offers') is not None)
    prices, excluded = defaultdict(list), defaultdict(int)
    for award in awarded:
        price = lot_price(award)
        if price is not None:
            prices[award['currency']].append(price)
        elif award.get('price') is not None:
            excluded[award.get('price_scope') or ('unknown' if award.get('currency') else 'no_currency')] += 1
    summary = dict(
        awards=len(awards),
        procedures=len({(a['source'], a.get('procedure_id') or a['number']) for a in awards}),
        notices=len({(a['source'], a['number']) for a in awards}),
        buyers=len({fold(a['buyer_name']) for a in awards if a.get('buyer_name')}),
        contractors=len({a.get('contractor_id') or fold(a['contractor']) for a in awarded if a.get('contractor')}),
        status={status: sum(a['status'] == status for a in awards) for status in AWARD_STATUSES},
        missing=dict(price=sum(a.get('price') is None for a in awarded),
                     contractor=sum(not a.get('contractor') for a in awarded),
                     date=sum(a.get('date') is None for a in awarded),
                     offers=len(awards) - len(offers)),
        offers=dict(known=len(offers), single=offers.count(1), min=offers[0], median=_number(median(offers)),
                    max=offers[-1]) if offers else dict(known=0),
        prices=[dict(currency=currency, lots=len(values), sum=str(sum(values)), min=str(min(values)),
                     median=str(median(values)), max=str(max(values)))
                for currency, values in sorted(prices.items(), key=lambda item: -len(item[1]))],
        prices_left_out=dict(excluded))
    scopes = [a['keyword_scope'] for a in awards if 'keyword_scope' in a]
    if scopes:
        summary['keyword_scope'] = {scope: scopes.count(scope) for scope in ('lot', 'notice')}
    return summary


def _without_empty(record):
    return {key: value for key, value in record.items() if value not in (None, [], '')}


def compact_awards(awards):
    """Awards that keep source and number, plus each of their notices once, both without empty fields."""
    notices, slim = {}, []
    for award in awards:
        key = (award['source'], award['number'])
        if key not in notices:
            notices[key] = _without_empty(dict(source=award['source'], number=award['number'],
                                               **{name: award.get(name) for name in AWARD_NOTICE_FIELDS}))
        slim.append(_without_empty({k: v for k, v in award.items() if k not in AWARD_NOTICE_FIELDS}))
    return slim, list(notices.values())


def _notice_summary(notice):
    record = compact_notice_record(notice, Profile('summary'))
    record['award_count'] = len(notice.awards)
    return _without_empty(record)


def json_roundtrip(value):
    return json.loads(dumps(value))


def latest_awards(notices):
    """Latest formal result per source, procedure and lot; a later notice replaces an earlier one."""
    latest = {}
    for notice in sorted(notices, key=lambda n: (n.published or date.min, n.number)):
        if not notice.awards:
            continue
        key = notice.procedure_id or notice.number
        for award in notice.awards:
            latest[(notice.source, key, award.lot)] = (award, notice)
    return [latest[key] for key in sorted(latest, key=lambda k: (k[0], k[1], k[2]))]


def procedure_results(state, source, number, *, compact=False):
    """One procedure's contract and result notices, linked by procedure_id, with the latest award per lot."""
    notice = state.get(source, number)
    if notice is None:
        raise ValueError('Notice not found in local state')
    related = [notice]
    if notice.procedure_id:
        related = list(state.notices(sources=(source,), procedure_id=notice.procedure_id))
        if not any(n.number == notice.number for n in related):
            related.append(notice)
    related.sort(key=lambda n: (n.published or date.min, n.number))
    if compact:
        awards, _ = compact_awards([_award_record(award, owner) for award, owner in latest_awards(related)])
        for award in awards:
            award.pop('source', None)
        return dict(
            notice=_notice_summary(notice),
            procedure_id=notice.procedure_id,
            contracts=[_notice_summary(n) for n in related if not n.awards and n.notice_type != AMENDMENT_TYPE],
            results=[_notice_summary(n) for n in related if n.awards],
            awards=awards)
    return dict(
        notice=notice_record(notice, raw=False),
        procedure_id=notice.procedure_id,
        contracts=[notice_record(n, raw=False) for n in related if not n.awards and n.notice_type != AMENDMENT_TYPE],
        results=[notice_record(n, raw=False) for n in related if n.awards],
        awards=[_award_record(award, owner) for award, owner in latest_awards(related)])


def search_results(state, *, buyer=None, cpv=None, start=None, end=None, limit=100, offset=0, source_names=(),
                   keywords_any=None, exclude_keywords=None, regions=None, exclude_cpv=None, profile=None, compact=False,
                   status=None, min_offers=None, max_offers=None, keywords_in_lot=False, sort=None):
    """Earlier awards by buyer text and CPV, latest per procedure and lot, paginated like notice search.

    summary describes every filtered award; limit=0 returns only the summary."""
    if type(limit) is not int or not 0 <= limit <= MAX_RESULTS or type(offset) is not int or not 0 <= offset <= MAX_NOTICES:
        raise ValueError('Invalid result limit or offset')
    if status is not None and status not in AWARD_STATUSES:
        raise ValueError('status must be AWARDED or CANCELLED')
    for bound in (min_offers, max_offers):
        if bound is not None and (type(bound) is not int or bound < 0):
            raise ValueError('min_offers and max_offers must be non-negative integers')
    if sort is not None and sort not in RESULT_SORTS:
        raise ValueError('sort must be one of ' + ', '.join(RESULT_SORTS))
    if start is not None or end is not None:
        start, end = window(start, end)
    overrides = {name: value for name, value in (
        ('cpv', cpv), ('exclude_cpv', exclude_cpv), ('keywords_any', keywords_any),
        ('exclude_keywords', exclude_keywords), ('regions', regions)) if value is not None}
    # Historical awards use the profile's subject criteria. Contract-only notice types and
    # submission deadline constraints describe opportunities, so they do not constrain awards.
    profile = replace(profile or Profile('results'), notice_types=(), notice_kinds=(),
                      deadline_days=None, open_now=False, **overrides)
    patterns = profile.cpv
    folded_buyer = fold(buyer) if buyer else None
    candidates = []
    # SQLite narrows by awards, buyer and CPV; the checks below keep the exact matching rules.
    for notice in state.notices(start, end, source_names, awarded=True, cpv=patterns, buyer=buyer):
        if not notice.awards:
            continue
        selected = profile.select(notice)
        if selected is None:
            continue
        if folded_buyer and folded_buyer not in fold(notice.buyer_name):
            continue
        candidates.append(selected)
    if keywords_in_lot and not profile.keywords_any:
        raise ValueError('keywords_in_lot requires keywords_any')
    awards = []
    for award, owner in latest_awards(candidates):
        if status is not None and award.status != status:
            continue
        if (min_offers is not None or max_offers is not None) and award.offers is None:
            continue
        if min_offers is not None and award.offers < min_offers or max_offers is not None and award.offers > max_offers:
            continue
        record = _award_record(award, owner)
        if profile.keywords_any:
            record['keyword_scope'] = keyword_scope(profile, award, owner)
            if keywords_in_lot and record['keyword_scope'] != 'lot':
                continue
        awards.append(record)
    if sort is not None:
        awards = _sort_awards(awards, sort)
    page = awards[offset:offset + limit]
    result = dict(awards=page, offset=offset, total=len(awards),
                  next_offset=offset + len(page) if limit and offset + len(page) < len(awards) else None,
                  summary=results_summary(awards),
                  coverage=state.search_coverage(profile, start, end, source_names, results=True))
    if compact:
        result['awards'], result['notices'] = compact_awards(page)
    return result

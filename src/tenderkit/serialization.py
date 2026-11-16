"""Lossless JSON notice records shared by SQLite, CLI and MCP."""
from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal
import json

from tenderkit.model import Award, Lot, Notice
from tenderkit.notice_kinds import notice_kind
from tenderkit.sources.mapping import bzp_scope, ted_scope


def json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f'Cannot serialize {type(value).__name__}')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=json_default, allow_nan=False)


# The source fields tenderkit reads after mapping: watch identities and versions (TED, BZP corrections, BK) and
# TED lot deadlines in the browser UI. Everything else in `raw` is the source's record kept for the user.
RAW_FIELDS_USED = ('notice-identifier', 'change-notice-version-identifier', 'notice-version',
                   'deadline-receipt-tender-date-lot', 'amends', 'amendment_published_at', 'version')


def compact_raw(raw):
    """Keep only the source fields tenderkit itself reads."""
    return {key: raw[key] for key in RAW_FIELDS_USED if key in raw}


def notice_record(notice, *, raw=True):
    record = asdict(notice)
    record['notice_kind'] = notice_kind(notice)
    if record['deadline_amendment'] is None:
        record.pop('deadline_amendment')
    if not raw:
        record.pop('raw')
    return json.loads(dumps(record))


def notice_from_record(record):
    data = dict(record)
    data.pop('notice_kind', None)
    data['published'] = date.fromisoformat(data['published']) if data.get('published') else None
    data['deadline'] = datetime.fromisoformat(data['deadline']) if data.get('deadline') else None
    data['value'] = Decimal(data['value']) if data.get('value') is not None else None
    data['cpv'] = tuple(data.get('cpv') or [])
    lots = []
    for lot in data.get('lots') or []:
        lot = dict(lot)
        lot['cpv'] = tuple(lot.get('cpv') or [])
        lot['value'] = Decimal(lot['value']) if lot.get('value') is not None else None
        lots.append(Lot(**lot))
    data['lots'] = tuple(lots)
    data['lot_titles'] = tuple(data.get('lot_titles') or [])
    if 'description' not in data:
        raw = data.get('raw') or {}
        if data['source'] == 'BZP':
            description, titled_lots = bzp_scope(raw.get('htmlBody'))
            merged = {lot.number: lot for lot in lots}
            for lot in titled_lots:
                existing = merged.get(lot.number)
                merged[lot.number] = replace(existing, title=existing.title or lot.title) if existing else lot
            data['description'] = description
            data['lots'] = tuple(merged.values())
        elif data['source'] == 'TED':
            data['description'], data['lot_titles'] = ted_scope(raw)
    awards = []
    for award in data.get('awards') or []:
        award = dict(award)
        award['price'] = Decimal(award['price']) if award.get('price') is not None else None
        award['date'] = date.fromisoformat(award['date']) if award.get('date') else None
        awards.append(Award(**award))
    data['awards'] = tuple(awards)
    return Notice(**data)

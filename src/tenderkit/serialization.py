"""Lossless JSON notice records shared by SQLite, CLI and MCP."""
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import json

from tenderkit.model import Award, Lot, Notice


def json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f'Cannot serialize {type(value).__name__}')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=json_default, allow_nan=False)


def notice_record(notice, *, raw=True):
    record = asdict(notice)
    if not raw:
        record.pop('raw')
    return json.loads(dumps(record))


def notice_from_record(record):
    data = dict(record)
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
    awards = []
    for award in data.get('awards') or []:
        award = dict(award)
        award['price'] = Decimal(award['price']) if award.get('price') is not None else None
        award['date'] = date.fromisoformat(award['date']) if award.get('date') else None
        awards.append(Award(**award))
    data['awards'] = tuple(awards)
    return Notice(**data)

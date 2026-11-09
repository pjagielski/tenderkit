"""Source-specific fields to the industry-independent Notice model."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import html
import re

from tenderkit.model import Lot, Notice

REFERENCE = re.compile(r'4\.1\.2\.\)\s*Numer referencyjny:\s*(.+?)\s+\d\.\d\.\d+\.\)')
PLATFORM_URL = re.compile(r'Adres strony internetowej prowadzonego post.powania.{0,300}?(https?://\s*\S+|www\.\S+)')
CPV = re.compile(r'\b(\d{8})(?:-\d)?\b')
# NUTS level 2 -> TERYT voivodeship. Warsaw and the surrounding region share 14.
NUTS_TERYT = dict(zip(
    ('PL21', 'PL22', 'PL41', 'PL42', 'PL43', 'PL51', 'PL52', 'PL61', 'PL62', 'PL63', 'PL71', 'PL72', 'PL81', 'PL82', 'PL84', 'PL91', 'PL92'),
    ('12', '24', '30', '32', '08', '02', '16', '04', '28', '22', '10', '26', '06', '18', '20', '14', '14')))


def region(value):
    if not value:
        return None
    value = str(value)
    if re.fullmatch(r'PL\d{2}', value):
        return value[2:]
    if re.fullmatch(r'\d{2}', value):
        return value
    return None


def ted_region(value):
    value = first(value)
    return NUTS_TERYT.get(str(value)[:4]) if value else None


def html_to_text(value):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html.unescape(value or '')))


def amount(value):
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        number = Decimal(re.sub(r'\s', '', str(value)).replace(',', '.'))
        return number if number.is_finite() and number > 0 else None
    except InvalidOperation:
        return None


def day(value):
    return date.fromisoformat(value[:10]) if value else None


def instant(value):
    if not value:
        return None
    # TED returns date-only fields with a timezone suffix, e.g. 2026-10-21+02:00.
    if len(value) <= 16 and 'T' not in value:
        return datetime.fromisoformat(value[:10] + 'T00:00:00' + value[10:])
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def polish(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or not value:
        return None
    text = value.get('pol') or next(iter(value.values()))
    return text[0] if isinstance(text, list) and text else text or None


def first(value):
    return value[0] if isinstance(value, list) and value else value if not isinstance(value, list) else None


def bzp_notice(raw):
    text = html_to_text(raw.get('htmlBody'))
    reference, platform = REFERENCE.search(text), PLATFORM_URL.search(text)
    value = re.search(r'Wartość zamówienia:\s*(\d[\d\s]*(?:,\d+)?)\s*([A-Z]{3})\b', text)
    return Notice(
        source='BZP', number=raw['noticeNumber'], procedure_id=raw.get('tenderId'),
        published=day(raw.get('publicationDate')), deadline=instant(raw.get('submittingOffersDate')),
        title=raw.get('orderObject'), buyer_name=raw.get('organizationName'),
        buyer_id=raw.get('organizationNationalId'), buyer_city=raw.get('organizationCity'),
        buyer_region=region(raw.get('organizationProvince')),
        cpv=tuple(dict.fromkeys(CPV.findall(raw.get('cpvCode') or ''))),
        value=amount(value[1]) if value else None, currency=value[2] if value and amount(value[1]) else None,
        reference=reference[1].strip() if reference else None,
        platform_url=re.sub(r'\s+', '', platform[1]) if platform else None,
        url=f"https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/{raw['objectId']}",
        notice_type=raw.get('noticeType'), raw=raw,
        lot_count=int(re.search(r'Liczba części:\s*(\d+)', text)[1]) if re.search(r'Liczba części:\s*(\d+)', text)
        else 1 if re.search(r'Możliwe jest składanie ofert częściowych:\s*Nie', text) else None,
    )


def ted_notice(raw):
    ids = raw.get('identifier-lot') or []
    values, currencies = raw.get('estimated-value-lot') or [], raw.get('estimated-value-cur-lot') or []
    value, currency = amount(raw.get('estimated-value-proc')), first(raw.get('estimated-value-cur-proc'))
    if value is None and values and all(amount(v) is not None for v in values) and len(set(currencies)) == 1:
        value, currency = sum(amount(v) for v in values), currencies[0]
    deadlines = sorted(d for d in raw.get('deadline-receipt-tender-date-lot') or [] if d)
    urls = [u for u in raw.get('document-url-lot') or [] if u]
    return Notice(
        source='TED', number=raw['publication-number'], procedure_id=first(raw.get('procedure-identifier')),
        published=day(raw.get('publication-date')), deadline=instant(deadlines[0]) if deadlines else None,
        title=polish(raw.get('title-proc')) or polish(raw.get('notice-title')),
        buyer_name=polish(raw.get('buyer-name')), buyer_city=first(raw.get('organisation-city-buyer')),
        buyer_id=first(raw.get('organisation-identifier-buyer')),
        buyer_region=ted_region(raw.get('organisation-country-sub-buyer')),
        cpv=tuple(dict.fromkeys(str(c).split('-')[0] for c in raw.get('classification-cpv') or [])),
        value=value, currency=currency if value else None,
        # Search API arrays are flattened, without a join between each amount
        # and its lot id. Keep the IDs; never infer per-lot amounts by position.
        lots=tuple(Lot(str(number)) for number in ids),
        reference=first(raw.get('internal-identifier-proc')), platform_url=urls[0] if urls else None,
        url=f"https://ted.europa.eu/pl/notice/-/detail/{raw['publication-number']}",
        notice_type=first(raw.get('notice-type')) or 'cn-standard', raw=raw,
        lot_count=len(ids) or None,
    )

"""Source-specific fields to the industry-independent Notice model."""
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import html
import re
from zoneinfo import ZoneInfo

from tenderkit.model import Award, Lot, Notice
from tenderkit.sources.bzp import timestamp

REFERENCE = re.compile(r'4\.1\.2\.\)\s*Numer referencyjny:\s*(.+?)\s+\d\.\d\.\d+\.\)')
PLATFORM_URL = re.compile(r'Adres strony internetowej prowadzonego post.powania.{0,300}?(https?://\s*\S+|www\.\S+)')
CPV = re.compile(r'\b(\d{8})(?:-\d)?\b')
WARSAW = ZoneInfo('Europe/Warsaw')
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


def bzp_publication_day(value):
    published = instant(value)
    if published is None:
        return None
    if published.tzinfo is not None:
        published = published.astimezone(WARSAW)
    return published.date()


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
    body = raw.get('htmlBody')
    text = html_to_text(body)
    reference, platform = REFERENCE.search(text), PLATFORM_URL.search(text)
    value = re.search(r'Wartość zamówienia:\s*(\d[\d\s]*(?:,\d+)?)\s*([A-Z]{3})\b', text)
    description, lots = bzp_scope(body)
    return Notice(
        source='BZP', number=raw['noticeNumber'], procedure_id=raw.get('tenderId'),
        published=bzp_publication_day(raw.get('publicationDate')), deadline=instant(raw.get('submittingOffersDate')),
        title=raw.get('orderObject'), buyer_name=raw.get('organizationName'),
        buyer_id=raw.get('organizationNationalId'), buyer_city=raw.get('organizationCity'),
        buyer_region=region(raw.get('organizationProvince')),
        cpv=tuple(dict.fromkeys(CPV.findall(raw.get('cpvCode') or ''))),
        value=amount(value[1]) if value else None, currency=value[2] if value and amount(value[1]) else None,
        reference=reference[1].strip() if reference else None,
        platform_url=re.sub(r'\s+', '', platform[1]) if platform else None,
        url=f"https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/{raw['objectId']}",
        notice_type=raw.get('noticeType'), raw=raw,
        description=description, lots=lots,
        awards=bzp_awards(raw),
        lot_count=int(re.search(r'Liczba części:\s*(\d+)', text)[1]) if re.search(r'Liczba części:\s*(\d+)', text)
        else 1 if re.search(r'Możliwe jest składanie ofert częściowych:\s*Nie', text) else None,
    )


def bzp_amendment(raw):
    text = html_to_text(raw.get('htmlBody'))
    target = re.search(r'Numer zmienianego ogłoszenia w BZP:\s*(\d{4}/BZP \d+)(?:/(\d+))?', text)
    if not target:
        return None
    version = re.search(r'Identyfikator ostatniej wersji zmienianego ogłoszenia:\s*(\d+)', text)
    target_number = f'{target[1]}/{target[2] or (version[1] if version else "01")}'
    deadline = None
    for label, value in bzp_blocks(raw.get('htmlBody')):
        if label.startswith('3.4.1.') and re.match(r'8\.1\.\)?\s*Termin składania ofert\b', value):
            updated = re.search(r'Po zmianie:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)', value)
            if updated:
                deadline = datetime.fromisoformat(updated[1]).replace(tzinfo=WARSAW)
    return Notice('BZP', raw['noticeNumber'], procedure_id=raw.get('tenderId'),
                  published=bzp_publication_day(raw.get('publicationDate')), deadline=deadline, title=raw.get('orderObject'),
                  url=f"https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/{raw['objectId']}",
                  notice_type=raw.get('noticeType'), raw={'amends': target_number,
                      'amendment_published_at': timestamp(raw['publicationDate']).isoformat()})


def bzp_blocks(body, labels=None):
    """(label, value) pairs of one BZP HTML body, the same h3 layout the reader uses."""
    if not body:
        return []
    main = re.search(r'<main\b[^>]*>(.*?)</main>', body, re.S | re.I)
    if main:
        body = main[1]
    found = []
    for heading, value in re.findall(r'<h3\b[^>]*>(.*?)</h3>(.*?)(?=<h[23]\b|</main>|</body>|$)', body, re.S | re.I):
        if labels is not None and not labels.search(heading):
            continue
        inline = re.findall(r'<span\b[^>]*>(.*?)</span>', heading, re.S | re.I)
        label = re.sub(r'<span\b[^>]*>.*?</span>', '', heading, flags=re.S | re.I)
        found.append((html_to_text(label).strip(), html_to_text(' '.join(inline) + ' ' + value).strip()))
    return found


_SCOPE_LABELS = re.compile(r'Krótki opis przedmiotu zamówienia|Część\s+\d|Nazwa części', re.I)


def bzp_scope(body):
    descriptions, titles, lot_descriptions = [], {}, defaultdict(list)
    lot_number = None
    for label, value in bzp_blocks(body, _SCOPE_LABELS):
        lot = re.fullmatch(r'Część\s+(\d+)(?:\s*[-–—:]\s*(.+))?', label, re.I)
        if lot:
            lot_number = lot[1]
            if lot[2]:
                titles[lot_number] = lot[2]
        elif 'krótki opis przedmiotu zamówienia' in label.casefold() and value:
            descriptions.append(value)
            if lot_number is not None and label.startswith('4.2.2'):
                lot_descriptions[lot_number].append(value)
        elif lot_number is not None and 'nazwa części' in label.casefold() and value:
            titles[lot_number] = value
    return '\n\n'.join(dict.fromkeys(descriptions)) or None, tuple(
        Lot(number, title=titles.get(number), description='\n\n'.join(lot_descriptions[number]) or None)
        for number in dict.fromkeys((*titles, *lot_descriptions)))


def bzp_fields(body):
    fields = defaultdict(list)
    for label, value in bzp_blocks(body):
        code = re.match(r'^(\d+(?:\.\d+)*)\.?\)', label)
        if code:
            fields[code[1]].append((label, value))
    return fields


def _one(fields, code):
    entries = fields.get(code, [])
    return entries[0][1] if len(entries) == 1 else None


def _bid_amount(fields, code):
    value = _one(fields, code)
    matched = re.fullmatch(r'\s*([\d\s\u00a0]+,\d{2})\s+(PLN)\s*', value or '')
    if not matched:
        return None, None
    return Decimal(re.sub(r'\s+', '', matched[1]).replace(',', '.')), matched[2]


def bzp_awards(raw):
    """Formal per-lot results from a TenderResultNotice; unparsed sections yield no award, never a guess."""
    if raw.get('noticeType') != 'TenderResultNotice':
        return ()
    body = raw.get('htmlBody') or ''
    sections = [match for match in re.finditer(r'<h2\b[^>]*>(.*?)</h2>', body, re.I | re.S)
                if re.search(r'\bSEKCJA\s+V\b', match[1])]
    published = bzp_publication_day(raw.get('publicationDate'))
    awards = []
    for index, section in enumerate(sections):
        heading = html_to_text(section[1]).strip()
        content = body[section.end():sections[index + 1].start() if index + 1 < len(sections) else len(body)]
        fields = bzp_fields(content)
        status = _one(fields, '5.1')
        if not status:
            continue
        lot = re.search(r'dla części\s+(\d+)(?![\w])', heading, re.I)
        lot = lot[1] if lot else '__all__'
        normalized = status.casefold()
        if 'unieważn' in normalized and 'zawarciem umowy' not in normalized:
            awards.append(Award(lot=lot, status='CANCELLED', offers=_offer_count(fields), date=published))
        elif 'zawarciem umowy' in normalized:
            winner = _one(fields, '7.3.1')
            if _one(fields, '7.1') != 'Nie':
                continue    # consortium or unknown structure: do not invent a single contractor
            price, currency = _bid_amount(fields, '6.4')
            awards.append(Award(lot=lot, status='AWARDED', contractor=winner,
                                contractor_id=_one(fields, '7.3.2'), price=price, currency=currency,
                                offers=_offer_count(fields), date=published))
    return tuple(awards)


def _offer_count(fields):
    count = _one(fields, '6.1')
    return int(count) if count and count.isdigit() else None


def _ted_texts(value):
    if isinstance(value, dict):
        value = value.get('pol') or next(iter(value.values()), None)
    texts = value if isinstance(value, list) else [value]
    return tuple(text.strip() for text in texts if isinstance(text, str) and text.strip())


def ted_scope(raw):
    descriptions = _ted_texts(raw.get('description-proc')) + _ted_texts(raw.get('description-lot'))
    return '\n\n'.join(dict.fromkeys(descriptions)) or None, tuple(dict.fromkeys(_ted_texts(raw.get('title-lot'))))


def ted_notice(raw):
    ids = raw.get('identifier-lot') or []
    values, currencies = raw.get('estimated-value-lot') or [], raw.get('estimated-value-cur-lot') or []
    value, currency = amount(raw.get('estimated-value-proc')), first(raw.get('estimated-value-cur-proc'))
    if value is None and values and all(amount(v) is not None for v in values) and len(set(currencies)) == 1:
        value, currency = sum(amount(v) for v in values), currencies[0]
    deadlines = sorted(d for d in raw.get('deadline-receipt-tender-date-lot') or [] if d)
    urls = [u for u in raw.get('document-url-lot') or [] if u]
    description, lot_titles = ted_scope(raw)
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
        description=description, lot_titles=lot_titles,
        reference=first(raw.get('internal-identifier-proc')), platform_url=urls[0] if urls else None,
        url=f"https://ted.europa.eu/pl/notice/-/detail/{raw['publication-number']}",
        notice_type=first(raw.get('notice-type')) or 'cn-standard', raw=raw,
        lot_count=len(ids) or None,
    )

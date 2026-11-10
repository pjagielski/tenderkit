"""platformazakupowa.pl public HTML: attachments, messages and offer folders."""
import re
from urllib.parse import urljoin

from tenderkit.platforms.html import Tree

BASE_URL = 'https://platformazakupowa.pl'
RATE_LIMIT_MARKER = 'Wykryto nietypowy ruch'
PROCEEDING_STATUSES = ('active', 'ended', 'canceled', 'closed')


def resolve_transaction_url(org_slug, reference_number, session):
    for status in PROCEEDING_STATUSES:
        response = session.get(f'{BASE_URL}/pn/{org_slug}/proceedings/ajax',
            params=dict(status=status, page=1, query=reference_number, **{'sort-field': 'endsAt', 'sort-type': 'DESC'}), timeout=30)
        response.raise_for_status()
        auctions = response.json().get('auctions') or []
        if len(auctions) == 1:
            return f"{BASE_URL}/transakcja/{auctions[0]['id']}"
        if len(auctions) > 1:
            raise LookupError('Ambiguous procedure reference')
    raise LookupError('Procedure not found')


def parse_attachments(html):
    table = Tree(html).root.find('table', attr='class', value='all-attachments-table')
    if table is None:
        return []
    body = table.find('tbody') or table
    documents = []
    for row in body.find_all('tr'):
        cells = row.find_all('td')
        link = row.find('a', attr='class', value='proceeding-file-download')
        if len(cells) < 6 or link is None or not link.attrs.get('href'):
            continue
        documents.append(dict(name=cells[0].text(), extension=cells[1].text(), sizeKb=cells[2].text(),
            publishedDate=cells[3].text(' '), source=cells[4].text(), downloadUrl=urljoin(BASE_URL, link.attrs['href'])))
    return documents


def parse_offers(html):
    root = Tree(html).root
    offers = []
    for head in root.find_all(attr='id', value=lambda v: v.startswith('offer-attachment-accordion-heading-')):
        offer_id = head.attrs['id'].rsplit('-', 1)[-1]
        text = re.sub(r'\s+', ' ', head.text(' '))
        name = re.search(r'Nazwa firmy:\s*(.*?)\s*NIP:', text)
        submitted = re.search(r'Data z[łl]o[żz]enia oferty:\s*([\d-]+ [\d:]+)', text)
        body = root.find(attr='id', value='offer-attachment-accordion-collapse-' + offer_id)
        files = []
        for row in body.find_all('tr') if body else []:
            link = row.find('a', attr='href', value=lambda v: 'get_new' in v)
            cells = row.find_all('td')
            if link is not None and cells:
                files.append(dict(name=cells[0].text(), sizeKb=cells[2].text() if len(cells)>2 else None,
                                  downloadUrl=urljoin(BASE_URL, link.attrs['href'])))
        offers.append(dict(id=offer_id, name=(name[1].strip(' -') or None) if name else None,
                           submitted=submitted[1] if submitted else None, files=files))
    return offers


def parse_messages(html):
    table = Tree(html).root.find(attr='id', value='comments_info')
    messages = {}
    for row in table.find_all('tr') if table else []:
        cells = row.find_all('td', direct=True)
        stamp = re.search(r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})', cells[0].text(' ')) if cells else None
        if stamp and len(cells) >= 3:
            messages[f'{stamp[1]} {stamp[2]}'] = re.sub(r'\s+', ' ', cells[2].text(' ')).strip()[:500]
    return messages

OPENING = re.compile(r'zestawienie\s+(z[łl]o[żz]onych\s+|otwartych\s+)?ofert|z\s+otwarcia\s+ofert|protok[oó][łl]\s+z\s+otwarcia|informacja\s+z\s+otwarcia|informacja\s+o\s+z[łl]o[żz]onych\s+ofert|art\.?\s*222\s*(ust\.?\s*)?5|art\.?\s*222_5', re.I)

RESULT = re.compile(r'wyb[oó]r|wybor|wynik|uniewa[żz]ni|rozstrzygni|najkorzystniejsz', re.I)

NEEDED = re.compile(r'otwarci|zestawieni', re.I)

CHECK = re.compile(r'formularz|cenow|ofert|kwot|zestawien|wykonawc|cen[ay]\b', re.I)

OFFER_FILE = re.compile(r'\.(zip|7z|rar|pdf|xlsx?|docx?|ods|odt)$', re.I)

NOT_OFFER = re.compile(r'swz|specyfikacj|ogłosz|ogloszen|wyjaśni|wyjasni|pytan|odpowied|modyfikac|zmian|umow|kwot|unieważn|protok', re.I)

ATTACHED = 'Publiczna Sesja Otwarcia Ofert - Oferta z załącznika '

ARCHIVE = re.compile(r'\.(zip|7z|rar)$', re.I)

def is_opening(name, text=None):
    return not ARCHIVE.search(name) and bool(OPENING.search(name if text is None else text))

def attached_offers(documents):
    """Mark attachments that are offers when the offers section has no files: published at or after the first
    opening summary, not a letter, notice or SWZ. Each becomes an anonymous offer, identified later by its amounts."""
    if any(d.get('offerId') for d in documents):
        return
    opened = min((d['publishedDate'] or '' for d in documents if d['folder'] == 'Informacja z otwarcia ofert'), default=None)
    if not opened:
        return
    for i, d in enumerate(documents, 1):
        if (d['folder'] is None and (d['publishedDate'] or '') >= opened and OFFER_FILE.search(d['name'])
                and not NOT_OFFER.search(d['name']) and not RESULT.search(d['name']) and not RESULT.search(d.get('message') or '')):
            d['folder'] = ATTACHED + str(i)
            d['attachedOffer'] = True

def documents_from_page(html):
    """Every public document of a transaction page, classified as in the manifest: the attachments table (the opening
    summary gets the folder 'Informacja z otwarcia ofert'), the files of each offer ('Publiczna Sesja Otwarcia Ofert -
    <firma>'; the platform hides the name of most) and, when the offers section has no files, the attachments published
    with the opening summary. Also used by the download queue (tools/fetch_worker.py)."""
    documents = [dict(name=a['name'] if a['name'].lower().endswith('.'+a['extension'].lower()) else f"{a['name']}.{a['extension']}",
                      folder='Informacja z otwarcia ofert' if is_opening(a['name']) else None,
                      publishedDate=a['publishedDate'], sizeText=a['sizeKb']+' KB', downloadUrl=a['downloadUrl'], kind=a['source'])
                 for a in parse_attachments(html)]
    # An attachment shares its publication time with the message it came with.
    messages = parse_messages(html)
    for d in documents:
        message = messages.get(re.sub(r'\s+', ' ', d['publishedDate'] or '').strip())
        if message:
            d['message'] = message
            if d['folder'] is None and is_opening(d['name'], message) and not RESULT.search(message):
                d['folder'] = 'Informacja z otwarcia ofert'
    for offer in parse_offers(html):
        folder = f"Publiczna Sesja Otwarcia Ofert - {offer['name'] or 'Oferta '+offer['id']}"
        documents += [dict(name=f['name'], folder=folder, publishedDate=offer['submitted'], sizeText=f"{f['sizeKb']} KB",
                           downloadUrl=f['downloadUrl'], offerId=offer['id'], hiddenName=offer['name'] is None) for f in offer['files']]
    attached_offers(documents)
    return documents

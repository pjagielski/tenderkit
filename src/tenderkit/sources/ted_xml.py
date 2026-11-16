"""Per-lot awards from the public TED notice XML (eForms UBL).

The Search API arrays are flattened and carry no join between a lot and its winner, so multi-lot
awards come from the authoritative XML behind `https://ted.europa.eu/en/notice/{number}/xml`.
Only the NoticeResult section is read; missing fields stay empty instead of being inferred.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET

from tenderkit.model import Award

NS = {
    'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
    'efac': 'http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1',
    'efbc': 'http://data.europa.eu/p27/eforms-ubl-extension-basic-components/1',
}
AWARDED_STATUS = 'selec-w'


def text(element, path):
    found = element.find(path, NS) if element is not None else None
    value = (found.text or '').strip() if found is not None and found.text else None
    return value or None


def money(element, path):
    node = element.find(path, NS) if element is not None else None
    if node is None or not (node.text or '').strip():
        return None, None
    try:
        value = Decimal(node.text.strip())
    except InvalidOperation:
        return None, None
    return (value, node.get('currencyID')) if value.is_finite() and value >= 0 else (None, None)


def parse_awards(document):
    """Award tuples for one ContractAwardNotice XML document; raises ValueError when it is not one."""
    try:
        root = ET.fromstring(document)
    except ET.ParseError as exc:
        raise ValueError('Unreadable TED notice XML') from exc
    if root.tag.rsplit('}', 1)[-1] not in ('ContractAwardNotice', 'ContractAwardNoticeType'):
        raise ValueError('Not a TED contract award notice')
    organizations = {}
    for company in root.findall('.//efac:Organizations/efac:Organization/efac:Company', NS):
        key = text(company, 'cac:PartyIdentification/cbc:ID')
        if key:
            organizations[key] = (text(company, 'cac:PartyName/cbc:Name'),
                                  text(company, 'cac:PartyLegalEntity/cbc:CompanyID'))
    notice_result = root.find('.//efac:NoticeResult', NS)
    notice_total = money(notice_result, 'cbc:TotalAmount')
    results = root.findall('.//efac:NoticeResult/efac:LotResult', NS)
    all_lots = {text(result, 'efac:TenderLot/cbc:ID') for result in results}
    referenced_lots = {}
    for result in results:
        tender_id = text(result, 'efac:LotTender/cbc:ID')
        lot_id = text(result, 'efac:TenderLot/cbc:ID')
        if tender_id and lot_id:
            referenced_lots.setdefault(tender_id, set()).add(lot_id)

    tenders = {}
    for tender in root.findall('.//efac:NoticeResult/efac:LotTender', NS):
        key = text(tender, 'cbc:ID')
        if key:
            price, currency = money(tender, 'cac:LegalMonetaryTotal/cbc:PayableAmount')
            lots = {node.text for node in tender.findall('efac:TenderLot/cbc:ID', NS)}
            tenders[key] = dict(tenderer=text(tender, 'efac:TenderingParty/cbc:ID'),
                                price=price, currency=currency, lots=lots)
    contracts, framework_contracts = {}, set()
    for contract in root.findall('.//efac:NoticeResult/efac:SettledContract', NS):
        key, issued = text(contract, 'cbc:ID'), text(contract, 'cbc:IssueDate')
        if key and text(contract, 'efbc:ContractFrameworkIndicator') in ('true', '1'):
            framework_contracts.add(key)
        if key and issued:
            try:
                contracts[key] = date.fromisoformat(issued[:10])
            except ValueError:
                pass
    parties = {}
    for party in root.findall('.//efac:NoticeResult/efac:TenderingParty', NS):
        key, organization = text(party, 'cbc:ID'), text(party, 'efac:Tenderer/cbc:ID')
        if key and organization:
            parties[key] = organization
    awards = []
    for result in results:
        lot = text(result, 'efac:TenderLot/cbc:ID')
        if not lot:
            continue
        tender_id = text(result, 'efac:LotTender/cbc:ID')
        tender = tenders.get(tender_id, {})
        contract_id = text(result, 'efac:SettledContract/cbc:ID')
        contractor, contractor_id = organizations.get(parties.get(tender.get('tenderer')), (None, None))
        awarded = text(result, 'cbc:TenderResultCode') == AWARDED_STATUS
        price, currency = (tender.get('price'), tender.get('currency')) if awarded else (None, None)
        cap, cap_currency = money(result, 'efac:FrameworkAgreementValues/cbc:MaximumValueAmount')
        scope = None
        if price is not None:
            lots = tender.get('lots', set()) | referenced_lots.get(tender_id, set())
            if cap is not None and cap_currency and (price, currency) == (cap, cap_currency):
                scope = 'framework_max'
            elif contract_id in framework_contracts:
                scope = None
            elif len(lots) > 1 and lots == all_lots and currency and (price, currency) == notice_total:
                scope = 'notice_total'
            elif lots == {lot}:
                scope = 'lot'
        awards.append(Award(
            lot=lot, status='AWARDED' if awarded else 'CANCELLED',
            contractor=contractor if awarded else None,
            contractor_id=contractor_id if awarded else None,
            price=price, currency=currency, price_scope=scope, framework_max=cap, framework_currency=cap_currency,
            offers=subscription_count(result),
            date=contracts.get(contract_id) if awarded else None))
    return tuple(awards)


def subscription_count(result):
    for statistics in result.findall('efac:ReceivedSubmissionsStatistics', NS):
        if text(statistics, 'efbc:StatisticsCode') == 'tenders':
            value = text(statistics, 'efbc:StatisticsNumeric')
            if value and value.isdigit():
                return int(value)
    return None

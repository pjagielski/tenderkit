"""Per-lot awards from the public TED notice XML (eForms UBL).

The Search API arrays are flattened and carry no join between a lot and its winner, so multi-lot
awards come from the authoritative XML behind `https://ted.europa.eu/en/notice/{number}/xml`.
Only the NoticeResult section is read; missing fields stay empty instead of being inferred.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET

from tenderkit.model import Award, Lot

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


def money(element):
    node = element.find('cac:LegalMonetaryTotal/cbc:PayableAmount', NS) if element is not None else None
    if node is None or not (node.text or '').strip():
        return None, None
    try:
        value = Decimal(node.text.strip())
    except InvalidOperation:
        return None, None
    return (value, node.get('currencyID')) if value.is_finite() and value >= 0 else (None, None)


def parse_awards(document):
    """Award tuples for one ContractAwardNotice XML document."""
    return parse_award_notice(document)[0]


def parse_award_notice(document):
    """Awards and keyed lot metadata from a ContractAwardNotice XML document."""
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
    tenders = {}
    for tender in root.findall('.//efac:NoticeResult/efac:LotTender', NS):
        key = text(tender, 'cbc:ID')
        if key:
            price, currency = money(tender)
            tenders[key] = dict(tenderer=text(tender, 'efac:TenderingParty/cbc:ID'), price=price, currency=currency)
    contracts = {}
    for contract in root.findall('.//efac:NoticeResult/efac:SettledContract', NS):
        key, issued = text(contract, 'cbc:ID'), text(contract, 'cbc:IssueDate')
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
    for result in root.findall('.//efac:NoticeResult/efac:LotResult', NS):
        lot = text(result, 'efac:TenderLot/cbc:ID')
        if not lot:
            continue
        tender = tenders.get(text(result, 'efac:LotTender/cbc:ID')) or {}
        contractor, contractor_id = organizations.get(parties.get(tender.get('tenderer')), (None, None))
        awarded = text(result, 'cbc:TenderResultCode') == AWARDED_STATUS
        awards.append(Award(
            lot=lot, status='AWARDED' if awarded else 'CANCELLED',
            contractor=contractor if awarded else None,
            contractor_id=contractor_id if awarded else None,
            price=tender.get('price') if awarded else None,
            currency=tender.get('currency') if awarded else None,
            offers=subscription_count(result),
            date=contracts.get(text(result, 'efac:SettledContract/cbc:ID')) if awarded else None))
    lots = []
    for lot in root.findall('cac:ProcurementProjectLot', NS):
        number = text(lot, 'cbc:ID')
        project = lot.find('cac:ProcurementProject', NS)
        if not number or project is None:
            continue
        quantity = project.find('cbc:EstimatedOverallContractQuantity', NS)
        value = None
        if quantity is not None:
            try:
                value = Decimal(quantity.text or '')
                value = value if value.is_finite() and value >= 0 else None
            except InvalidOperation:
                pass
        code = text(project, 'cac:MainCommodityClassification/cbc:ItemClassificationCode')
        lots.append(Lot(number, title=text(project, 'cbc:Name'), description=text(project, 'cbc:Description'),
                        cpv=(code,) if code else (), quantity=value,
                        quantity_unit=quantity.get('unitCode') if value is not None else None))
    return tuple(awards), tuple(lots)


def subscription_count(result):
    for statistics in result.findall('efac:ReceivedSubmissionsStatistics', NS):
        if text(statistics, 'efbc:StatisticsCode') == 'tenders':
            value = text(statistics, 'efbc:StatisticsNumeric')
            if value and value.isdigit():
                return int(value)
    return None

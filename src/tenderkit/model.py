"""The data every source and platform speaks: a notice, its lots, the platform page it points to, a document on it."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


@dataclass(frozen=True)
class Lot:
    number: str
    title: str | None = None
    cpv: tuple[str, ...] = ()
    value: Decimal | None = None
    currency: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    quantity_unit: str | None = None


@dataclass(frozen=True)
class Award:
    """One lot's formal result: the selected contractor and price, or the cancellation."""
    lot: str                                     # the source's lot number, or '__all__' for a whole procedure
    status: str                                  # 'AWARDED' or 'CANCELLED'
    contractor: str | None = None
    contractor_id: str | None = None             # NIP or another identifier, as the source gives it
    price: Decimal | None = None                 # the selected bid, not the buyer's estimate
    currency: str | None = None
    offers: int | None = None                    # bids received for this lot, when the source counts them
    date: date | None = None                     # award or contract date, when the source gives one
    price_scope: str | None = None
    framework_max: Decimal | None = None
    framework_currency: str | None = None


@dataclass(frozen=True)
class Notice:
    """One published notice, as its source gave it; `raw` keeps the source's own record."""
    source: str                                  # 'BZP', 'TED', 'BK'
    number: str                                  # the source's notice number, e.g. '2026/BZP 00495988/01'
    procedure_id: str | None = None              # the procedure several notice versions share (BZP OCID, TED procedure id)
    title: str | None = None
    published: date | None = None
    deadline: datetime | None = None             # offers due
    buyer_name: str | None = None
    buyer_id: str | None = None                  # NIP or REGON, as the source gives it
    buyer_city: str | None = None
    buyer_region: str | None = None
    cpv: tuple[str, ...] = ()                    # eight-digit codes, main first
    value: Decimal | None = None
    currency: str | None = None
    lots: tuple[Lot, ...] = ()
    awards: tuple[Award, ...] = ()               # formal per-lot results, when this is a result notice
    platform_url: str | None = None              # where the documents are, from the notice text
    reference: str | None = None                 # the buyer's own case number
    url: str | None = None                       # the notice itself
    raw: dict = field(default_factory=dict, compare=False, repr=False)
    notice_type: str | None = None
    lot_count: int | None = None
    description: str | None = None
    lot_titles: tuple[str, ...] = ()              # titles from sources that do not link them to lot IDs
    source_attribution: dict[str, str] | None = None
    deadline_amendment: dict[str, str | None] | None = None
    value_source: str | None = None
    value_tax_basis: str | None = None
    deadline_precision: Literal['date', 'datetime'] | None = None

    def __post_init__(self):
        if self.deadline is not None and self.deadline_precision is None:
            # Older TED records retain the original date array, even with --no-raw.
            dates = self.raw.get('deadline-receipt-tender-date-lot') or []
            first_date = min((item for item in dates if item), default='')
            date_only = self.source == 'TED' and first_date and 'T' not in first_date
            object.__setattr__(self, 'deadline_precision', 'date' if date_only else 'datetime')

    def local_deadline(self):
        if self.deadline is None:
            return None
        if self.deadline_precision == 'date' or self.deadline.tzinfo is None:
            return self.deadline.replace(tzinfo=WARSAW)
        return self.deadline.astimezone(WARSAW)

    def deadline_status(self, now):
        deadline = self.local_deadline()
        if deadline is None:
            return 'missing'
        if self.deadline_precision == 'date' and deadline.date() == now.astimezone(WARSAW).date():
            return 'today'
        return 'upcoming' if deadline.astimezone(timezone.utc) > now else 'passed'



@dataclass(frozen=True)
class PlatformLink:
    """A procurement platform page a notice points to; `key` identifies the procedure there (e.g. a transaction id)."""
    platform: str                                # 'platformazakupowa.pl', 'eZamawiający (Marketplanet)', ...
    url: str
    key: str | None = None
    procedure_page: bool = True                  # False for a buyer's profile page, which lists many procedures


@dataclass(frozen=True)
class DocumentRef:
    """A document a platform lists: `url` downloads it, `key` identifies it across visits (a one-time link does not)."""
    name: str
    url: str
    key: str
    ext: str | None = None
    published: str | None = None
    origin: str | None = None                    # 'attachment', 'message', 'offer folder', ...
    metadata: dict = field(default_factory=dict, compare=False, repr=False)

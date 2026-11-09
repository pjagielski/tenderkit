"""The data every source and platform speaks: a notice, its lots, the platform page it points to, a document on it."""
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class Lot:
    number: str
    title: str | None = None
    cpv: tuple[str, ...] = ()
    value: Decimal | None = None
    currency: str | None = None


@dataclass(frozen=True)
class Notice:
    """One published contract notice, as its source gave it; `raw` keeps the source's own record."""
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
    platform_url: str | None = None              # where the documents are, from the notice text
    reference: str | None = None                 # the buyer's own case number
    url: str | None = None                       # the notice itself
    raw: dict = field(default_factory=dict, compare=False, repr=False)
    notice_type: str | None = None
    lot_count: int | None = None


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

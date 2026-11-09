"""What a notice source and a procurement platform have to provide; projects register their own as entry points
(tenderkit.sources, tenderkit.platforms) from stage F1 on."""
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from tenderkit.model import DocumentRef, Notice, PlatformLink


@dataclass(frozen=True)
class SourceQuery:
    """The part of a profile a source can filter on its side; the rest is filtered by Profile after fetching."""
    cpv_prefixes: tuple[str, ...] = ()           # '336' for CPV 336*
    notice_types: tuple[str, ...] = ()


@runtime_checkable
class Source(Protocol):
    name: str                                    # 'BZP', 'TED'

    def fetch(self, start: date, end: date, query: SourceQuery) -> Iterable[Notice]:
        """Notices published from start to end (inclusive) that the source's own filters let through."""


@runtime_checkable
class Platform(Protocol):
    name: str                                    # 'platformazakupowa.pl'

    def matches(self, url: str) -> bool:
        """Whether a URL from a notice belongs to this platform."""

    def procedure(self, notice: Notice) -> PlatformLink | None:
        """The procedure's page on this platform, or None when the notice does not identify one."""

    def documents(self, link: PlatformLink) -> list[DocumentRef]:
        """The public documents of the procedure, each with a key that is stable across visits."""

    def download(self, ref: DocumentRef, to: Path) -> Path:
        """Save one document under the directory `to`; returns the file written."""

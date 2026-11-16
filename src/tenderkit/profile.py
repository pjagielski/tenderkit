"""An industry profile: which notices a project wants, as a TOML file (tomllib, standard library).

    name = "leki"
    cpv = ["336*"]                         # a code, or a prefix ending in *
    exclude_cpv = ["33696*", "336316*"]    # codes that never count, even inside cpv
    keywords_any = ["lek", "farmaceut"]    # empty: no keyword condition
    exclude_keywords = ["weterynar"]

Keywords match from the start of a word in the title, description or lot titles; a trailing * changes nothing.
Use *term* (or *term) for a substring anywhere in a word.
Matching is case- and diacritics-insensitive. Profile.matches applies the filters the source cannot handle.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from zoneinfo import ZoneInfo
from pathlib import Path
import re
import tomllib
import unicodedata

from tenderkit.interfaces import SourceQuery
from tenderkit.model import Notice

_CPV = re.compile(r'^\d{1,8}\*?$')


@dataclass(frozen=True)
class ValueRange:
    min: Decimal | None = None
    max: Decimal | None = None
    currency: str = 'PLN'

    def __post_init__(self):
        for name in ('min', 'max'):
            value = getattr(self, name)
            if value is not None:
                try:
                    value = Decimal(str(value))
                except InvalidOperation as exc:
                    raise ValueError('Invalid value bound') from exc
                if not value.is_finite() or value < 0:
                    raise ValueError('Value bounds must be finite and nonnegative')
                object.__setattr__(self, name, value)
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError('Value min exceeds max')
        if not re.fullmatch(r'[A-Z]{3}', self.currency):
            raise ValueError('Currency must be a three-letter code')


@dataclass(frozen=True)
class DeadlineRange:
    min: int = 0
    max: int = 120

    def __post_init__(self):
        if type(self.min) is not int or type(self.max) is not int or not 0 <= self.min <= self.max <= 3650:
            raise ValueError('Invalid deadline_days bounds')


def fold(text):
    """Lower case without diacritics ('Leków' -> 'lekow'), the form keywords are matched in."""
    text = unicodedata.normalize('NFKD', (text or '').replace('ł', 'l').replace('Ł', 'L'))
    return ''.join(c for c in text if not unicodedata.combining(c)).casefold()


def keyword_term(keyword):
    """A keyword's folded text and whether it matches anywhere in a word.

    'lek' and 'lek*' match from a word start (the default, so a trailing * is optional); '*lek*' and '*lek'
    match anywhere. Profile validation rejects a * anywhere else."""
    term = fold(keyword.strip())
    return term.strip('*').strip(), term.startswith('*')


@lru_cache(maxsize=1024)
def _keyword_pattern(keyword):
    term, substring = keyword_term(keyword)
    phrase = r'\s+'.join(re.escape(word) for word in term.split())
    return re.compile(('' if substring else r'(?<!\w)') + phrase) if phrase else re.compile(r'(?!)')


def _keyword_fields(notice):
    yield 'title', None, notice.title
    for paragraph in (notice.description or '').split('\n\n'):
        yield 'description', None, paragraph
    for lot in notice.lots:
        if lot.title:
            yield 'lot_title', lot.number, lot.title
    for title in notice.lot_titles:
        yield 'lot_title', None, title


def keyword_texts(notice):
    """The texts keywords are matched in: title, description paragraphs and lot titles."""
    return [text for _, _, text in _keyword_fields(notice) if text]


@dataclass(frozen=True)
class Profile:
    name: str
    cpv: tuple[str, ...] = ()
    exclude_cpv: tuple[str, ...] = ()
    keywords_any: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    notice_types: tuple[str, ...] = ()
    value: ValueRange | None = None
    regions: tuple[str, ...] = ()
    deadline_days: DeadlineRange | None = None
    exclude_buyers: tuple[str, ...] = ()
    documents: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('Profile name must be nonempty')
        for name in ('cpv', 'exclude_cpv', 'keywords_any', 'exclude_keywords', 'notice_types', 'regions', 'exclude_buyers', 'documents'):
            values = getattr(self, name)
            if (not isinstance(values, (list, tuple)) or len(values) > 64
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values)):
                raise ValueError(f'{name} must be an array of nonempty strings')
            object.__setattr__(self, name, tuple(values))
        for name in ('keywords_any', 'exclude_keywords'):
            for keyword in getattr(self, name):
                inner = keyword.strip().strip('*')
                if '*' in inner or not inner.strip():
                    raise ValueError(f'{name}: * may only start or end a keyword, as in lek* or *lek*: {keyword!r}')
        bad = [c for c in self.cpv + self.exclude_cpv if not _CPV.match(c)]
        if bad:
            raise ValueError(f'profile {self.name}: CPV must be digits, optionally ending in *: {bad}')
        if self.value is not None and not isinstance(self.value, ValueRange):
            raise ValueError('value must be a ValueRange')
        if self.deadline_days is not None and not isinstance(self.deadline_days, DeadlineRange):
            raise ValueError('deadline_days must be a DeadlineRange')
        if any(not re.fullmatch(r'\d{2}', r) for r in self.regions):
            raise ValueError('regions must contain two-digit TERYT voivodeship codes')

    def cpv_prefixes(self):
        """'336*' -> '336'; a full code stays as it is."""
        return tuple(c.rstrip('*') for c in self.cpv)

    def source_query(self):
        return SourceQuery(cpv_prefixes=self.cpv_prefixes(), notice_types=self.notice_types)

    @staticmethod
    def _cpv_hit(code, patterns):
        return any(code.startswith(p[:-1]) if p.endswith('*') else code == p for p in patterns)

    def matches_cpv(self, codes):
        """Whether one of the notice's codes (main or lot) is wanted: inside cpv (or cpv is empty) and not in exclude_cpv.
        A notice whose every code is excluded does not match; one with medicines beside reagents still does."""
        codes = [str(code) for code in codes]
        if self.exclude_cpv:
            kept = [code for code in codes if not self._cpv_hit(code, self.exclude_cpv)]
            if codes and not kept:
                return False
            codes = kept
        return not self.cpv or any(self._cpv_hit(code, self.cpv) for code in codes)

    def matches_text(self, text):
        return self._matches_keywords((text,))

    def _matches_keywords(self, texts):
        if not (self.keywords_any or self.exclude_keywords):
            return True
        folded = tuple(fold(text) for text in texts if text)
        if any(_keyword_pattern(term).search(text) for term in self.exclude_keywords for text in folded):
            return False
        return not self.keywords_any or any(
            _keyword_pattern(term).search(text) for term in self.keywords_any for text in folded)

    def keyword_matches(self, notice):
        """Return the configured inclusion terms that matched and their source fields."""
        if not self.keywords_any:
            return []
        fields = [(field, lot, fold(text)) for field, lot, text in _keyword_fields(notice) if text]
        hits = dict.fromkeys(
            (term, field, lot)
            for term in self.keywords_any for field, lot, text in fields
            if _keyword_pattern(term).search(text)
        )
        return [dict(keyword=term, field=field, lot=lot) if lot is not None else dict(keyword=term, field=field)
                for term, field, lot in hits]

    def matches(self, notice: Notice, *, today=None):
        if not self.matches_cpv(notice.cpv):
            return False
        if self.notice_types and notice.notice_type not in self.notice_types:
            return False
        if self.regions and notice.buyer_region not in self.regions:
            return False
        if any(fold(b) in fold(notice.buyer_name) or b == notice.buyer_id for b in self.exclude_buyers):
            return False
        if self.value:
            if notice.value is None or notice.currency != self.value.currency:
                return False
            if self.value.min is not None and notice.value < self.value.min:
                return False
            if self.value.max is not None and notice.value > self.value.max:
                return False
        if self.deadline_days:
            if notice.deadline is None:
                return False
            deadline = notice.deadline.astimezone(ZoneInfo('Europe/Warsaw')) if notice.deadline.tzinfo else notice.deadline
            days = (deadline.date() - (today or datetime.now(ZoneInfo('Europe/Warsaw')).date())).days
            if not self.deadline_days.min <= days <= self.deadline_days.max:
                return False
        return self._matches_keywords(text for _, _, text in _keyword_fields(notice))


def load_profile(path):
    source = path if hasattr(path, 'read_text') else Path(path)     # a path or an importlib.resources file
    data = tomllib.loads(source.read_text(encoding='utf-8'))
    return profile_from_dict(data, origin=str(path))


def profile_from_dict(data, *, origin='profile'):
    unknown = set(data) - set(Profile.__dataclass_fields__)
    if unknown:
        raise ValueError(f'{origin}: unknown profile keys {sorted(unknown)}')
    data = dict(data)
    for key, cls in (('value', ValueRange), ('deadline_days', DeadlineRange)):
        if key in data and data[key] is not None:
            if not isinstance(data[key], dict):
                raise ValueError(f'{key} must be a table')
            try:
                data[key] = cls(**data[key])
            except TypeError as exc:
                raise ValueError(f'Invalid {key} fields') from exc
    try:
        return Profile(**data)
    except TypeError as exc:
        raise ValueError('Invalid profile fields') from exc

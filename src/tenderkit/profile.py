"""An industry profile: which notices a project wants, as a TOML file (tomllib, standard library).

    name = "leki"
    cpv = ["336*"]                         # a code, or a prefix ending in *
    keywords_any = ["lek", "farmaceut"]    # empty: no keyword condition
    exclude_keywords = ["weterynar"]

Matching is case- and diacritics-insensitive on the title. A source gets only what it can filter itself (SourceQuery);
Profile.matches decides the rest.
"""
from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
import unicodedata

from tenderkit.interfaces import SourceQuery
from tenderkit.model import Notice

_CPV = re.compile(r'^\d{1,8}\*?$')


def fold(text):
    """Lower case without diacritics ('Leków' -> 'lekow'), the form keywords are matched in."""
    text = unicodedata.normalize('NFKD', (text or '').replace('ł', 'l').replace('Ł', 'L'))
    return ''.join(c for c in text if not unicodedata.combining(c)).casefold()


@dataclass(frozen=True)
class Profile:
    name: str
    cpv: tuple[str, ...] = ()
    keywords_any: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    notice_types: tuple[str, ...] = ()

    def __post_init__(self):
        bad = [c for c in self.cpv if not _CPV.match(c)]
        if bad:
            raise ValueError(f'profile {self.name}: CPV must be digits, optionally ending in *: {bad}')

    def cpv_prefixes(self):
        """'336*' -> '336'; a full code stays as it is."""
        return tuple(c.rstrip('*') for c in self.cpv)

    def source_query(self):
        return SourceQuery(cpv_prefixes=self.cpv_prefixes(), notice_types=self.notice_types)

    def matches_cpv(self, codes):
        prefixes = self.cpv_prefixes()
        return not prefixes or any(str(code).startswith(prefixes) for code in codes)

    def matches_text(self, text):
        folded = fold(text)
        if any(fold(k) in folded for k in self.exclude_keywords):
            return False
        return not self.keywords_any or any(fold(k) in folded for k in self.keywords_any)

    def matches(self, notice: Notice):
        return self.matches_cpv(notice.cpv) and self.matches_text(notice.title)


def load_profile(path):
    source = path if hasattr(path, 'read_text') else Path(path)     # a path or an importlib.resources file
    data = tomllib.loads(source.read_text(encoding='utf-8'))
    unknown = set(data) - {'name', 'cpv', 'keywords_any', 'exclude_keywords', 'notice_types'}
    if unknown:
        raise ValueError(f'{path}: unknown profile keys {sorted(unknown)}')
    return Profile(name=data['name'], **{k: tuple(v) for k, v in data.items() if k != 'name'})

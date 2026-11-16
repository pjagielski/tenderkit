"""A search index beside `notices`: filter columns and folded text in SQLite FTS5.

The index only narrows candidates. Every condition here keeps a superset of what `Profile.matches` accepts, and
the caller still applies `Profile.matches` to each decoded notice, so results never depend on the index.
"""
from datetime import timedelta
import re
from zoneinfo import ZoneInfo

from tenderkit.profile import fold, keyword_texts

SCHEMA = '''
create table if not exists notice_index (
    id integer primary key,              -- notices.rowid
    notice_type text, region text, deadline_day text, value real, currency text,
    cpv text not null default ' '        -- ' code code ', for prefix and exact lookups
);
create virtual table if not exists notice_fts using fts5(text, tokenize = 'unicode61 remove_diacritics 0');
'''
_WARSAW = ZoneInfo('Europe/Warsaw')
# unicode61 splits on everything that is not a letter or a digit; underscore separates as well.
_TOKEN = re.compile(r'[^\W_]+')
# Private-use characters (Wingdings bullets pasted into BZP text) are token characters for unicode61 but not word
# characters for the keyword rule, so a bullet glued to a word would hide it from FTS. They become spaces.
_PRIVATE_USE = re.compile('[-\U000f0000-\U000ffffd\U00100000-\U0010fffd]')


def _searchable(text):
    return _PRIVATE_USE.sub(' ', fold(text))


def _deadline_day(deadline):
    if deadline is None:
        return None
    return str((deadline.astimezone(_WARSAW) if deadline.tzinfo else deadline).date())


def write(conn, rowid, notice):
    conn.execute('insert or replace into notice_index(id,notice_type,region,deadline_day,value,currency,cpv) '
                 'values(?,?,?,?,?,?,?)',
                 (rowid, notice.notice_type, notice.buyer_region, _deadline_day(notice.deadline),
                  float(notice.value) if notice.value is not None else None, notice.currency,
                  ' ' + ' '.join(str(code) for code in notice.cpv) + ' '))
    conn.execute('delete from notice_fts where rowid=?', (rowid,))
    conn.execute('insert into notice_fts(rowid,text) values(?,?)', (rowid, '\n'.join(_searchable(t) for t in keyword_texts(notice))))


def complete(conn):
    """Whether every saved notice has an index row; a partial index must not narrow anything."""
    if not conn.execute("select 1 from sqlite_master where name='notice_index'").fetchone():
        return False
    return conn.execute('select (select count(*) from notices) = (select count(*) from notice_index)').fetchone()[0] == 1


def _fts_term(keyword):
    """One keyword as an FTS5 query: its words as a phrase whose last word is a prefix, or None if FTS cannot
    express it (a *substring* or no word characters)."""
    term = fold(keyword.strip())
    if len(term) > 2 and term.startswith('*') and term.endswith('*'):
        return None
    tokens = _TOKEN.findall(term)
    if not tokens:
        return None
    return '"' + ' '.join(tokens) + '"*'


def prefilter(profile, today):
    """SQL conditions on notices.rowid and their parameters, each a superset of the profile's own rule."""
    index, params = [], []
    if profile.notice_types:
        index.append('notice_type in (' + ','.join('?' * len(profile.notice_types)) + ')')
        params += profile.notice_types
    if profile.regions:
        index.append('region in (' + ','.join('?' * len(profile.regions)) + ')')
        params += profile.regions
    if profile.cpv:
        terms = []
        for pattern in profile.cpv:
            terms.append('instr(cpv, ?) > 0')
            params.append(' ' + pattern[:-1] if pattern.endswith('*') else ' ' + pattern + ' ')
        index.append('(' + ' or '.join(terms) + ')')
    if profile.value:
        index.append('currency = ? and value is not null')
        params.append(profile.value.currency)
        if profile.value.min is not None:
            index.append('value >= ?')
            params.append(float(profile.value.min))
        if profile.value.max is not None:
            index.append('value <= ?')
            params.append(float(profile.value.max))
    if profile.deadline_days:
        index.append('deadline_day between ? and ?')
        params += [str(today + timedelta(days=profile.deadline_days.min)),
                   str(today + timedelta(days=profile.deadline_days.max))]
    conditions = ['rowid in (select id from notice_index where ' + ' and '.join(index) + ')'] if index else []
    if profile.keywords_any:
        fts = [_fts_term(keyword) for keyword in profile.keywords_any]
        if all(fts):
            conditions.append('rowid in (select rowid from notice_fts where notice_fts match ?)')
            params.append(' OR '.join(fts))
        else:
            # A substring keyword: scan the folded text with instr for those, FTS for the rest.
            parts, part_params = [], []
            for keyword, query in zip(profile.keywords_any, fts):
                if query:
                    parts.append('rowid in (select rowid from notice_fts where notice_fts match ?)')
                    part_params.append(query)
                else:
                    # The rule allows any whitespace between words, so look for the first word alone.
                    words = _searchable(keyword.strip()).strip('*').split()
                    needle = words[0] if words else ''
                    if not needle:
                        parts = []
                        break
                    parts.append('rowid in (select rowid from notice_fts where instr(text, ?) > 0)')
                    part_params.append(needle)
            if parts:
                conditions.append('(' + ' or '.join(parts) + ')')
                params += part_params
    return conditions, params

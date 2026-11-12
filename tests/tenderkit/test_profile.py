"""Industry profiles: loading, CPV prefixes, keyword matching without diacritics, and refusing what they do not know."""
from pathlib import Path
import tempfile
import unittest

from tenderkit import Notice, Profile, load_profile
from tenderkit.interfaces import Platform, Source, SourceQuery
from tenderkit.model import Lot
from tenderkit.profile import fold


def write(text):
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name)/'p.toml'
    path.write_text(text, encoding='utf-8')
    return directory, path


class ProfileTests(unittest.TestCase):
    def test_a_profile_loads_from_toml(self):
        directory, path = write('name = "oprogramowanie"\ncpv = ["72*", "48*"]\nexclude_keywords = ["licencj"]\n')
        with directory:
            profile = load_profile(path)
        self.assertEqual(profile, Profile('oprogramowanie', cpv=('72*', '48*'), exclude_keywords=('licencj',)))
        self.assertEqual(profile.cpv_prefixes(), ('72', '48'))
        self.assertEqual(profile.source_query(), SourceQuery(cpv_prefixes=('72', '48')))

    def test_unknown_keys_and_malformed_cpv_are_refused(self):
        directory, path = write('name = "x"\ncpv = ["336*"]\nkeywords = ["lek"]\n')
        with directory, self.assertRaisesRegex(ValueError, "unknown profile keys \\['keywords'\\]"):
            load_profile(path)
        with self.assertRaisesRegex(ValueError, 'CPV must be digits'):
            Profile('x', cpv=('33*6',))

    def test_cpv_matches_by_prefix_and_an_empty_list_matches_all(self):
        profile = Profile('leki', cpv=('336*',))
        self.assertTrue(profile.matches_cpv(['33600000']))
        self.assertTrue(profile.matches_cpv(['33100000', '33690000']))
        self.assertFalse(profile.matches_cpv(['33100000']))
        self.assertTrue(Profile('all').matches_cpv(['45000000']))

    def test_keywords_ignore_case_and_polish_diacritics_and_exclusions_win(self):
        self.assertEqual(fold('Dostawa LEKÓW i szczepionek, Łódź'), 'dostawa lekow i szczepionek, lodz')
        profile = Profile('leki', cpv=('336*',), keywords_any=('lekow', 'szczepion'), exclude_keywords=('weterynar',))
        self.assertTrue(profile.matches(Notice('BZP', '1', title='Dostawa leków', cpv=('33600000',))))
        self.assertFalse(profile.matches(Notice('BZP', '2', title='Leki weterynaryjne', cpv=('33600000',))))
        self.assertFalse(profile.matches(Notice('BZP', '3', title='Dostawa rękawic', cpv=('33600000',))))
        self.assertFalse(profile.matches(Notice('BZP', '4', title='Dostawa leków', cpv=('33100000',))))

    def test_keywords_match_word_starts_and_allow_explicit_substrings(self):
        prefix = Profile('prefix', keywords_any=('lek',))
        substring = Profile('substring', keywords_any=('*lek*',))
        for title in ('Dostawa sprzętu elektrycznego', 'Wymiana kolektorów'):
            self.assertFalse(prefix.matches_text(title), title)
            self.assertTrue(substring.matches_text(title), title)
        for title in ('Dostawa leków', 'Leki', 'Zakup (lekami)'):
            self.assertTrue(prefix.matches_text(title), title)
        self.assertTrue(Profile('exclude', exclude_keywords=('lek',)).matches_text('Sprzęt elektryczny'))

    def test_phrases_and_exclusions_use_each_searchable_field(self):
        notice = Notice('BZP', '1', title='Dostawa', description='Utrzymanie SYSTEMU\nINFORMATYCZNEGO',
                        lots=(Lot('2', 'Systemu informatycznego i oprogramowania'),))
        profile = Profile('it', keywords_any=('systemu informatyczn', 'oprogramowan'))
        self.assertTrue(profile.matches(notice))
        self.assertEqual(profile.keyword_matches(notice), [
            {'keyword': 'systemu informatyczn', 'field': 'description'},
            {'keyword': 'systemu informatyczn', 'field': 'lot_title', 'lot': '2'},
            {'keyword': 'oprogramowan', 'field': 'lot_title', 'lot': '2'},
        ])
        self.assertFalse(Profile('excluded', exclude_keywords=('oprogramowan',)).matches(notice))
        self.assertFalse(Profile('split', keywords_any=('system informatyczn',)).matches(
            Notice('TED', '2', title='System', description='informatyczny')))

    def test_the_interfaces_are_checkable_protocols(self):
        class Bzp:
            name = 'BZP'
            def fetch(self, start, end, query): return []
        self.assertIsInstance(Bzp(), Source)
        self.assertNotIsInstance(Bzp(), Platform)


if __name__ == '__main__':
    unittest.main()

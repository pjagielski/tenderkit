"""Industry profiles: loading, CPV prefixes, keyword matching without diacritics, and refusing what they do not know."""
from pathlib import Path
import tempfile
import unittest

from tenderkit import Notice, Profile, load_profile
from tenderkit.interfaces import Platform, Source, SourceQuery
from tenderkit.model import Lot
from tenderkit.profile import fold
from tenderkit.profiles import builtin_profiles


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

    def test_excluded_cpv_codes_never_count(self):
        profile = Profile('leki', cpv=('336*',), exclude_cpv=('33696*', '336316*'))
        self.assertTrue(profile.matches_cpv(['33600000']))
        self.assertFalse(profile.matches_cpv(['33696500']))                  # reagents only
        self.assertFalse(profile.matches_cpv(['33631600', '33696300']))      # disinfectants and reagents
        self.assertTrue(profile.matches_cpv(['33696500', '33690000']))       # a medicines lot beside reagents
        self.assertFalse(profile.matches(Notice('TED', '1', title='Odczynniki', cpv=('33696500',))))
        self.assertTrue(Profile('all', exclude_cpv=('45*',)).matches_cpv(['72000000', '45000000']))
        self.assertFalse(Profile('all', exclude_cpv=('45*',)).matches_cpv(['45000000']))
        with self.assertRaisesRegex(ValueError, 'CPV must be digits'):
            Profile('x', exclude_cpv=('33696-5',))

    def test_the_builtin_medicines_profile_leaves_out_reagents_and_disinfectants(self):
        leki = builtin_profiles()['leki']
        self.assertEqual(leki.exclude_cpv, ('33696*', '336316*'))
        self.assertEqual(leki.source_query().cpv_prefixes, ('336',))
        self.assertFalse(leki.matches_cpv(['33696500']))

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

    def test_a_trailing_star_is_a_prefix_and_never_silently_matches_nothing(self):
        # Regression: 'onkolog*' found nothing and exclude_keywords=['szczepion*'] excluded nothing.
        onco = Profile('onco', cpv=('336*',), keywords_any=('onkolog*', 'cytostat*'), exclude_keywords=('szczepion*', 'dezynfek*'))
        self.assertTrue(onco.matches(Notice('TED', '1', title='Dostawa leków onkologicznych', cpv=('33652000',))))
        self.assertTrue(onco.matches(Notice('TED', '2', title='Leki cytostatyczne', cpv=('33652000',))))
        self.assertFalse(onco.matches(Notice('TED', '3', title='Leki onkologiczne i szczepionki', cpv=('33652000',))))
        self.assertFalse(onco.matches(Notice('TED', '4', title='Leki onkologiczne', description='Pakiet 7: środki dezynfekcyjne',
                                             cpv=('33652000',))))
        self.assertEqual(Profile('a', keywords_any=('lek*',)).keyword_matches(Notice('BZP', '5', title='Leki')),
                         [{'keyword': 'lek*', 'field': 'title'}])
        self.assertTrue(Profile('b', keywords_any=('*ektry',)).matches_text('Sprzęt elektryczny'))

    def test_a_star_inside_a_keyword_is_rejected(self):
        for keyword in ('on*log', '*', '**', ' * '):
            for field in ('keywords_any', 'exclude_keywords'):
                with self.subTest(keyword=keyword, field=field), self.assertRaisesRegex(ValueError, 'may only start or end'):
                    Profile('bad', **{field: (keyword,)})

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

    def test_regions_accept_voivodeship_names_and_teryt_codes(self):
        for value in ('mazowieckie', 'Mazowieckie', 'woj. mazowieckie', 'Województwo Mazowieckie', '14'):
            self.assertEqual(Profile('x', regions=(value,)).regions, ('14',), value)
        self.assertEqual(Profile('x', regions=('Łódzkie', 'slaskie', 'swietokrzyskie')).regions, ('10', '24', '26'))
        self.assertEqual(Profile('x', regions=('mazowieckie', '14')).regions, ('14',))
        with self.assertRaisesRegex(ValueError, 'mazowieckie'):
            Profile('x', regions=('mazowiekie',))
        for value in ('99', '15', 'PL14', '1'):
            with self.assertRaises(ValueError, msg=value):
                Profile('x', regions=(value,))

    def test_the_interfaces_are_checkable_protocols(self):
        class Bzp:
            name = 'BZP'
            def fetch(self, start, end, query): return []
        self.assertIsInstance(Bzp(), Source)
        self.assertNotIsInstance(Bzp(), Platform)


if __name__ == '__main__':
    unittest.main()


class LotExclusionTests(unittest.TestCase):
    def test_lot_exclusion_keeps_other_lots_and_removes_matched_text(self):
        notice = Notice('BZP', 'example', title='Leki i szczepionki', lot_count=2,
                        description='Szczepionki dla dzieci\n\nPreparaty onkologiczne',
                        lots=(Lot('1', 'Szczepionki', description='Szczepionki dla dzieci'),
                              Lot('2', 'Preparaty onkologiczne', description='Preparaty onkologiczne')))
        self.assertFalse(Profile('whole', exclude_keywords=('szczepionk',)).matches(notice))
        profile = Profile('lots', exclude_scope='lot', exclude_keywords=('szczepionk',))
        kept = profile.select(notice)
        self.assertEqual(([item.number for item in kept.lots], kept.description, kept.lot_count),
                         (['2'], 'Preparaty onkologiczne', 1))
        self.assertEqual(len(notice.lots), 2)
        self.assertIsNone(Profile('none', exclude_scope='lot',
                                  exclude_keywords=('szczepionk', 'onkologiczn')).select(notice))


class BuiltInIndustryProfileTests(unittest.TestCase):
    def test_software_excludes_server_only_and_keeps_mixed_notices(self):
        profile = builtin_profiles()['oprogramowanie']
        server = Notice('TED', 'server', notice_type='cn-standard', cpv=('48823000',))
        self.assertFalse(profile.matches(server))
        mixed = Notice('TED', 'mixed', notice_type='cn-standard', cpv=('48820000', '72200000'))
        self.assertTrue(profile.matches(mixed))

    def test_ambulances_profile_excludes_fire_and_police_vehicles(self):
        profile = builtin_profiles()['ambulanse']
        for number, title, cpv, expected in (
            ('1', 'Dostawa ambulansów typu B', '34114121', True),
            ('2', 'Dostawa karetki', '34114100', True),
            ('3', 'Zakup samochodu pożarniczego', '34114110', False),
            ('4', 'Radiowóz policyjny', '34114200', False),
        ):
            with self.subTest(number=number):
                notice = Notice('BZP', number, title=title, notice_type='ContractNotice', cpv=(cpv,))
                self.assertEqual(profile.matches(notice), expected)

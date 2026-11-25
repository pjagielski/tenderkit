"""The topic pages of tenderkit.pl (scripts/build_topic_pages.py) render public notice data safely."""
import importlib.util
from datetime import date
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('build_topic_pages', ROOT/'scripts'/'build_topic_pages.py')
pages = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pages)

TOPIC = dict(slug='karetki', name='karetki', title='Przetargi na karetki', cpv=['3411412*'], intro='Ambulanse.',
             prompt='Pokaż przetargi na karetki.')


def data(title='Zakup karetki', url='https://example.org/n/1'):
    return dict(
        notices=dict(total=1, notices=[dict(source='BZP', number='1', title=title, buyer_name='SZPITAL W X',
                                            buyer_city='WYSZKÓW', buyer_region='14', deadline='2026-12-10T10:00:00+01:00',
                                            published='2026-11-25', value='1048780.49', currency='PLN',
                                            platform_url=url, platform_url_specific=True, url='https://ted.example/1')]),
        results=dict(
            summary=dict(awards=3, procedures=3, buyers=2, offers=dict(known=3, single=2),
                         prices=[dict(currency='PLN', median='889893.00')]),
            awards=[dict(source='BZP', number='2', contractor='AUTO-FORM Distribution Sp. z o.o.', price='883586.55',
                         currency='PLN', offers=1, date='2026-11-19'),
                    dict(source='BZP', number='3', contractor='AUTO-FORM DISTRIBUTION SPÓŁKA Z OGRANICZONĄ '
                         'ODPOWIEDZIALNOŚCIĄ', price='1', currency='PLN', offers=1),
                    dict(source='BZP', number='4', contractor='AUTO-FORM Distribution Sp. z o.o.', price='2',
                         currency='PLN', offers=2)],
            notices=[dict(source='BZP', number='2', title='Zakup karetki 4x4', buyer_name='Powiat',
                          url='https://ezamowienia.gov.pl/x')]))


class TopicPageTests(unittest.TestCase):
    def render(self, **kw):
        return pages.topic_page(TOPIC, pages.summarize(data(**kw), date(2026, 11, 25)), [TOPIC], date(2026, 11, 25))

    def test_page_shows_open_notices_and_award_statistics(self):
        page = self.render()
        self.assertIn('id="d-2026-12-10"', page)
        self.assertIn('Czwartek, 10 grudnia<small>za 15 dni, <span class="cnt">1 termin</span></small>', page)
        self.assertIn('Wyszków, woj. mazowieckie', page)
        self.assertIn('889 893 zł', page)
        self.assertIn('<strong>67%</strong>', page)
        self.assertIn('<link rel="canonical" href="https://tenderkit.pl/przetargi/karetki">', page)

    def test_list_carries_what_the_filters_need_and_works_without_them(self):
        page = self.render()
        self.assertIn('<li data-region="14" data-left="15" data-day="2026-12-10">', page)
        self.assertIn('<div class="filters" id="filtry" hidden>', page)
        self.assertIn('<option value="14">mazowieckie (1)</option>', page)
        self.assertIn("M=[\"stycznia\"", page)
        self.assertNotIn('__MONTHS__', page)

    def test_spellings_of_one_company_are_counted_together(self):
        page = self.render()
        self.assertIn('<span>AUTO-FORM Distribution Sp. z o.o.</span><b>3</b>', page)

    def test_notice_text_is_escaped_and_only_web_links_are_kept(self):
        page = self.render(title='<script>alert(1)</script>', url='javascript:alert(1)')
        self.assertNotIn('<script>alert', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('javascript:', page)
        self.assertIn('https://ted.example/1', page)

    def test_feed_lists_recent_open_notices_as_valid_rss(self):
        import xml.etree.ElementTree as ET
        s = pages.summarize(data(title='Zakup karetki <4x4> & noszy'), date(2026, 11, 25))
        root = ET.fromstring(pages.feed(TOPIC, s, date(2026, 11, 25)))
        item = root.find('channel/item')
        self.assertEqual(item.find('title').text, 'Zakup karetki <4x4> & noszy')
        self.assertEqual(item.find('guid').text, 'BZP:1')
        self.assertIn('Termin składania ofert: 10 grudnia 2026', item.find('description').text)
        old = pages.summarize(data(), date(2027, 1, 25))
        self.assertIsNone(ET.fromstring(pages.feed(TOPIC, old, date(2027, 1, 25))).find('channel/item'))
        self.assertIn('href="https://tenderkit.pl/przetargi/karetki.xml"', self.render())

    def test_sitemap_keeps_hand_written_entries(self):
        existing = ('<urlset><url><loc>https://tenderkit.pl/</loc><lastmod>2026-11-18</lastmod></url>'
                    '<url><loc>https://tenderkit.pl/przetargi/stare</loc></url></urlset>')
        out = pages.sitemap(existing, [TOPIC], date(2026, 11, 25))
        self.assertIn('<loc>https://tenderkit.pl/</loc>\n    <lastmod>2026-11-18</lastmod>', out)
        self.assertIn('<loc>https://tenderkit.pl/przetargi/karetki</loc>', out)
        self.assertNotIn('stare', out)

    def test_topics_file_is_complete(self):
        topics = tomllib.loads((ROOT/'scripts'/'topics.toml').read_text(encoding='utf-8'))['topic']
        self.assertEqual(len({t['slug'] for t in topics}), len(topics))
        for t in topics:
            self.assertTrue({'slug', 'name', 'title', 'cpv', 'intro', 'prompt'} <= t.keys(), t['slug'])
            self.assertTrue(all(c.rstrip('*').isdigit() for c in t['cpv']), t['slug'])


if __name__ == '__main__':
    unittest.main()

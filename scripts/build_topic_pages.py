"""Build the topic pages of tenderkit.pl: /przetargi/ and /przetargi/<slug>, one per topic in topics.toml.

Each page lists the notices open today and sums up the awards of the last 90 days, read from the public MCP server
(https://mcp.tenderkit.pl/mcp) with tenderkit's own tools. The Pages workflow runs this daily before upload; the
output is not committed.

    python scripts/build_topic_pages.py --site site [--topics scripts/topics.toml] [--mcp URL]

Standard library only. Notice fields are public source data and are escaped like any untrusted text.
"""
import argparse
import html
import json
import re
import sys
import time
import tomllib
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE = 'https://tenderkit.pl'
MCP = 'https://mcp.tenderkit.pl/mcp'
CLIENT = 'tenderkit-pages'
RESULT_DAYS = 90
OPEN_LIMIT = 500
SHOWN = 25  # notices visible before "Pokaż kolejne"; the rest stay in the page for the filters
AWARD_LIMIT = 300
MONTHS = ['stycznia', 'lutego', 'marca', 'kwietnia', 'maja', 'czerwca', 'lipca', 'sierpnia', 'września',
          'października', 'listopada', 'grudnia']
REGIONS = {'02': 'dolnośląskie', '04': 'kujawsko-pomorskie', '06': 'lubelskie', '08': 'lubuskie', '10': 'łódzkie',
           '12': 'małopolskie', '14': 'mazowieckie', '16': 'opolskie', '18': 'podkarpackie', '20': 'podlaskie',
           '22': 'pomorskie', '24': 'śląskie', '26': 'świętokrzyskie', '28': 'warmińsko-mazurskie',
           '30': 'wielkopolskie', '32': 'zachodniopomorskie'}


# --- MCP over Streamable HTTP ---------------------------------------------------------------------------------------

class Mcp:
    def __init__(self, url):
        self.url, self.session, self.next_id = url, None, 1

    def _post(self, message):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'User-Agent': f'{CLIENT}/1 (+{BASE}/przetargi/)'}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        request = urllib.request.Request(self.url, json.dumps(message).encode(), headers)
        with urllib.request.urlopen(request, timeout=120) as response:
            self.session = response.headers.get('Mcp-Session-Id') or self.session
            body = response.read().decode('utf-8')
            kind = response.headers.get('Content-Type', '')
        if 'id' not in message:
            return None
        if 'text/event-stream' in kind:
            for line in body.splitlines():
                if line.startswith('data:'):
                    data = json.loads(line[5:])
                    if data.get('id') == message['id']:
                        return data
            raise RuntimeError('no response in the event stream')
        return json.loads(body)

    def request(self, method, params):
        message = dict(jsonrpc='2.0', id=self.next_id, method=method, params=params)
        self.next_id += 1
        reply = self._post(message)
        if 'error' in reply:
            raise RuntimeError(f'{method}: {reply["error"]}')
        return reply['result']

    def start(self):
        self.request('initialize', dict(protocolVersion='2025-06-18', capabilities={},
                                        clientInfo=dict(name=CLIENT, version='1')))
        self._post(dict(jsonrpc='2.0', method='notifications/initialized'))

    def call(self, tool, **arguments):
        result = self.request('tools/call', dict(name=tool, arguments=arguments))
        if result.get('isError'):
            raise RuntimeError(f'{tool}: {result["content"][0].get("text", "")[:300]}')
        if result.get('structuredContent'):
            return result['structuredContent']
        return json.loads(result['content'][0]['text'])


def fetch(mcp, topic, today):
    notices, offset = None, 0
    while offset is not None and offset < OPEN_LIMIT:
        page = mcp.call('search_notices', cpv=topic['cpv'], open_now=True, dedupe='procedure', compact=True,
                        limit=100, offset=offset)
        if notices is None:
            notices = page
        else:
            notices['notices'] += page.get('notices') or []
        offset = page.get('next_offset')
    # The server caps one response at 140k characters, so awards come in pages of 100.
    results, offset = None, 0
    while offset is not None and offset < AWARD_LIMIT:
        page = mcp.call('search_results', cpv=topic['cpv'], status='AWARDED', sort='-date', compact=True,
                        start=(today - timedelta(days=RESULT_DAYS)).isoformat(), end=today.isoformat(),
                        limit=100, offset=offset)
        if results is None:
            results = page
        else:
            results['awards'] += page.get('awards') or []
            results['notices'] = (results.get('notices') or []) + (page.get('notices') or [])
        offset = page.get('next_offset')
    return dict(notices=notices, results=results)


# --- formatting -----------------------------------------------------------------------------------------------------

def e(value):
    return html.escape(str(value if value is not None else ''), quote=True)


def safe_url(url):
    return url if isinstance(url, str) and re.match(r'https?://', url) else None


def day(value):
    try:
        d = datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return ''
    return f'{d.day} {MONTHS[d.month - 1]} {d.year}'


def money(value, currency='PLN'):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return ''
    whole = f'{amount.quantize(Decimal(1)):,}'.replace(',', ' ')
    return f'{whole} {"zł" if currency in (None, "PLN") else e(currency)}'


def name_case(name):
    """Buyer names often come in capitals; long words get sentence case, short ones (SPZOZ, ZOZ) stay."""
    name = (name or '').strip()
    if not name.isupper():
        return name
    small = {'W': 'w', 'IM.': 'im.', 'I': 'i', 'NA': 'na', 'DLA': 'dla', 'Z': 'z', 'ORAZ': 'oraz'}
    return ' '.join(small.get(w, w.capitalize() if len(w) >= 6 or i == 0 and len(w) >= 4 else w)
                    for i, w in enumerate(name.split()))


def place(notice):
    city = (notice.get('buyer_city') or '').strip().title() if (notice.get('buyer_city') or '').isupper() \
        else (notice.get('buyer_city') or '').strip()
    region = REGIONS.get(str(notice.get('buyer_region') or '')[:2])
    return ', '.join(x for x in (city, region and f'woj. {region}') if x)


LEGAL_FORMS = [(r'sp[óo][łl]ka z ograniczon[ąa] odpowiedzialno[śs]ci[ąa]', 'sp z o o'), (r'sp[óo][łl]ka akcyjna', 's a'),
               (r'sp[óo][łl]ka komandytowa', 'sp k'), (r'sp[óo][łl]ka jawna', 'sp j')]


def company_key(name):
    key = name.casefold()
    for pattern, short in LEGAL_FORMS:
        key = re.sub(pattern, short, key)
    return ' '.join(re.sub(r'[^\w]+', ' ', key).split())


def plural(n, one, few, many):
    if n == 1:
        return one
    return few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


# --- pages ----------------------------------------------------------------------------------------------------------

RAIL_DAYS = 28
# Filters on a topic page: region, deadline range, or one day picked on the rail. State lives in the query string
# (?woj=14&termin=0-7&dzien=2026-12-01), so a filtered list can be shared. Matching notices show in pages of SHOWN.
FILTER_JS = (r'''
(()=>{const list=document.getElementById('lista'),bar=document.getElementById('filtry');if(!list||!bar)return;
const items=[...list.querySelectorAll('li')],woj=document.getElementById('f-woj'),ile=document.getElementById('f-ile'),
dzien=document.getElementById('f-dzien'),next=document.getElementById('dalej'),none=document.getElementById('brak'),
M=__MONTHS__,STEP=__SHOWN__;bar.hidden=false;
const q=new URLSearchParams(location.search);let st={woj:q.get('woj')||'',termin:q.get('termin')||'',dzien:q.get('dzien')||''},limit=STEP;
const R=__REGIONS__,bars=[...document.querySelectorAll('.rail .bars [data-day]')],sum=document.getElementById('rail-sum'),sum0=sum&&sum.textContent;
const forms=(n,a,b,c)=>n==1?a:(n%10>=2&&n%10<=4&&(n%100<12||n%100>14)?b:c);
function fits(li){if(st.woj&&li.dataset.region!==st.woj)return false;if(st.dzien)return li.dataset.day===st.dzien;
if(st.termin){const [a,b]=st.termin.split('-').map(x=>x===''?Infinity:+x),d=li.dataset.left===''?Infinity:+li.dataset.left;if(d<a||d>b)return false}return true}
function rail(){const c={};let all=0;for(const li of items)if(!st.woj||li.dataset.region===st.woj){all++;c[li.dataset.day]=(c[li.dataset.day]||0)+1}
const top=Math.max(1,...bars.map(b=>c[b.dataset.day]||0));bars.forEach((b,i)=>{const n=c[b.dataset.day]||0,h=Math.round(100*n/top)+'%',d=new Date(b.dataset.day+'T12:00');
b.querySelector('i').style.setProperty('--h',h);const l=b.querySelector('b');l.style.setProperty('--h',h);l.textContent=n||'';
b.classList.toggle('none',!n);b.classList.toggle('soon',!!n&&i<=3);const t=d.getDate()+' '+M[d.getMonth()]+': '+n+' '+forms(n,'termin','terminy','terminów');
b.title=t;if(b.tagName==='A'){b.setAttribute('aria-label',t);b.tabIndex=n?0:-1}});
const lt=document.getElementById('rail-later');if(lt){const k=all-bars.reduce((x,b)=>x+(c[b.dataset.day]||0),0);lt.textContent='Terminy na najbliższe '+lt.dataset.days+' dni'+(k>0?', później '+k:'')+'.'}
if(sum)sum.textContent=st.woj?'Woj. '+R[st.woj]+': '+all+' '+forms(all,'otwarte postępowanie','otwarte postępowania','otwartych postępowań')+'.':sum0}
function render(){rail();let n=0;for(const li of items){const ok=fits(li);li.hidden=!ok||n>=limit;if(ok)n++}
for(const g of list.querySelectorAll('.day')){const v=[...g.querySelectorAll('li')].filter(li=>!li.hidden).length;g.hidden=!v;
const c=g.querySelector('.cnt');if(c)c.textContent=v+' '+forms(v,'termin','terminy','terminów')}
const shown=Math.min(n,limit);ile.textContent=shown===n?n+' '+forms(n,'postępowanie','postępowania','postępowań'):'Pokazano '+shown+' z '+n;
none.hidden=n>0;next.hidden=n<=limit;next.textContent='Pokaż kolejne '+Math.min(STEP,n-limit);
woj.value=st.woj;for(const b of bar.querySelectorAll('[data-range]'))b.setAttribute('aria-pressed',String(!st.dzien&&b.dataset.range===st.termin));
dzien.hidden=!st.dzien;if(st.dzien){const d=new Date(st.dzien+'T12:00');dzien.firstChild.textContent='Termin '+d.getDate()+' '+M[d.getMonth()]}
const p=new URLSearchParams();for(const k in st)if(st[k])p.set(k,st[k]);const qs=p.toString();history.replaceState(null,'',qs?'?'+qs:location.pathname)}
function set(ch){Object.assign(st,ch);limit=STEP;render()}
woj.addEventListener('change',()=>set({woj:woj.value}));
bar.querySelectorAll('[data-range]').forEach(b=>b.addEventListener('click',()=>set({termin:b.dataset.range,dzien:''})));
dzien.querySelector('button').addEventListener('click',()=>set({dzien:''}));
document.querySelectorAll('[data-reset]').forEach(b=>b.addEventListener('click',()=>set({woj:'',termin:'',dzien:''})));
next.addEventListener('click',()=>{limit+=STEP;render()});
document.querySelectorAll('.rail a[data-day]').forEach(a=>a.addEventListener('click',ev=>{ev.preventDefault();set({dzien:a.dataset.day,termin:''});
bar.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'})}));
render()})();
'''
             .replace('__MONTHS__', json.dumps(MONTHS, ensure_ascii=False)).replace('__SHOWN__', str(SHOWN))
             .replace('__REGIONS__', json.dumps(REGIONS, ensure_ascii=False)))
WEEKDAYS = ['poniedziałek', 'wtorek', 'środa', 'czwartek', 'piątek', 'sobota', 'niedziela']

# One family (Manrope, as on the landing page) so the topic pages read as the same site. The single bold element is
# the deadline rail under the heading: one column per day, a bar per day's closing notices. Amber marks only deadlines
# within three days; it carries meaning, not decoration.
STYLE = '''
:root{--urgent:#b4530a;--urgent-soft:#fdf0e4;--ink-2:#41536f}
.topic{width:min(1080px,100% - 32px);margin:0 auto;padding:28px 0 88px;line-height:1.6}
.topic a{color:var(--blue)}
.site-header nav a[aria-current]{color:var(--blue)}
.crumbs{font-size:.84rem;color:var(--muted);margin:0 0 26px}.crumbs a{color:var(--muted);text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:3px}
.topic h1{font-size:clamp(2.2rem,6vw,4.1rem);font-weight:800;line-height:1.02;letter-spacing:-.055em;color:var(--navy);max-width:28ch;margin:0;white-space:normal;text-wrap:balance}
.lead{font-size:1.06rem;color:var(--ink-2);max-width:62ch;margin:18px 0 0}
.topic h2{font-size:clamp(1.45rem,3vw,2.05rem);font-weight:800;letter-spacing:-.04em;line-height:1.15;color:var(--navy);margin:0;max-width:30ch}
.topic section{margin-top:72px}
.sub{color:var(--muted);font-size:.92rem;margin:8px 0 0;max-width:70ch}
/* deadline rail */
.rail{margin-top:40px;padding:22px 22px 14px;background:var(--ice);border-radius:18px}
.rail-head{display:flex;flex-wrap:wrap;justify-content:space-between;gap:6px 20px;align-items:baseline;margin-bottom:18px}
.rail-head p{margin:0;font-size:1rem;color:var(--navy);font-weight:600}.rail-head p b{font-weight:600}.rail-head p span{color:var(--muted);font-weight:500}
.rail-head small{color:var(--muted);font-size:.82rem}
.bars{display:grid;grid-template-columns:repeat(var(--days),1fr);gap:4px;height:132px;align-items:end}
.bars a,.bars span{position:relative;display:block;height:100%;border-radius:4px}
.bars i{position:absolute;left:0;right:0;bottom:0;height:var(--h);min-height:3px;border-radius:4px 4px 2px 2px;background:var(--blue);transform-origin:bottom}
.bars .soon i{background:var(--urgent)}.bars .none i{background:var(--line);min-height:2px}.bars .none b{display:none}
.bars a.none{pointer-events:none}
.bars .we{background:repeating-linear-gradient(135deg,#0000 0 6px,#e6ecf8 6px 7px)}
.bars a:hover i,.bars a:focus-visible i{filter:brightness(1.15)}
.bars b{position:absolute;left:50%;bottom:calc(var(--h) + 4px);transform:translateX(-50%);font-size:.72rem;font-weight:700;color:var(--navy);font-variant-numeric:tabular-nums}
.ticks{display:grid;grid-template-columns:repeat(var(--days),1fr);gap:4px;margin-top:8px;font-size:.72rem;color:var(--muted);font-variant-numeric:tabular-nums}
.ticks span{white-space:nowrap;overflow:visible}
/* One moment of motion: the rail on a topic page grows once on load. */
@media (prefers-reduced-motion:no-preference){.rail .bars i{animation:grow .7s cubic-bezier(.2,.7,.2,1) both;animation-delay:calc(var(--n) * 18ms)}@keyframes grow{from{transform:scaleY(0)}}}
@media (max-width:640px){.bars{height:96px;gap:2px}.bars b{display:none}.ticks{gap:2px}.rail{padding:16px 12px 10px}}
/* the list, grouped by deadline day */
.day{margin-top:30px;scroll-margin-top:90px}
.day h3{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px;font-size:1.05rem;color:var(--navy);margin:0;padding-bottom:10px;border-bottom:2px solid var(--navy)}
.day h3 small{font-size:.85rem;font-weight:600;color:var(--muted)}
.day.soon h3{border-color:var(--urgent)}.day.soon h3 small{color:var(--urgent)}
.day ul{list-style:none;margin:0;padding:0}
.day li{display:grid;grid-template-columns:1fr auto;gap:2px 24px;padding:14px 0;border-bottom:1px solid var(--line)}
.day .t{font-weight:650;color:var(--ink);text-decoration:none;overflow-wrap:anywhere;max-width:75ch}
.day a.t:hover{color:var(--blue);text-decoration:underline;text-underline-offset:3px}
.day .who{grid-column:1;font-size:.88rem;color:var(--muted)}
.day .val{grid-column:2;grid-row:1 / span 2;text-align:right;font-weight:700;color:var(--navy);font-variant-numeric:tabular-nums;white-space:nowrap}
.day .val small{display:block;font-weight:500;font-size:.75rem;color:var(--muted)}
.src{font-size:.72rem;font-weight:700;color:var(--blue);margin-left:8px;letter-spacing:.02em}
.more{margin-top:18px;color:var(--muted);font-size:.9rem}
.filters{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;margin:22px 0 4px;padding:12px 0;background:#fffffff2;backdrop-filter:blur(6px);border-bottom:1px solid var(--line);scroll-margin-top:12px}
.filters[hidden]{display:none}
.filters label{display:flex;align-items:center;gap:8px;font-size:.88rem;font-weight:600;color:var(--navy)}
.filters select{font:inherit;font-weight:500;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink);max-width:62vw}
.range{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:3px;background:var(--ice)}
.range button{border:0;background:none;padding:6px 13px;border-radius:999px;font-size:.86rem;font-weight:600;color:var(--ink-2)}
.range button[aria-pressed=true]{background:var(--navy);color:#fff}
.dayf{display:inline-flex;align-items:center;gap:6px;background:var(--navy);color:#fff;border-radius:999px;padding:5px 6px 5px 13px;font-size:.86rem;font-weight:600}
.dayf[hidden]{display:none}
.dayf button{border:0;background:#ffffff26;color:#fff;width:22px;height:22px;border-radius:50%;line-height:1}
.count{margin:0 0 0 auto;font-size:.86rem;color:var(--muted);font-variant-numeric:tabular-nums}
.next{display:block;margin:22px auto 0;padding:11px 22px;border:1px solid var(--navy);border-radius:999px;background:#fff;color:var(--navy);font-weight:700}
.next[hidden]{display:none}
.next:hover{background:var(--navy);color:#fff}
button.link{border:0;background:none;padding:0;color:var(--blue);font:inherit;text-decoration:underline}
.day[hidden],.day li[hidden]{display:none}
@media (max-width:640px){.count{margin:0;width:100%}.range button{padding:6px 10px}}
@media (max-width:640px){.day li{grid-template-columns:1fr}.day .val{grid-column:1;grid-row:auto;text-align:left}}
/* results */
.facts{font-size:clamp(1.05rem,2vw,1.25rem);line-height:1.55;color:var(--ink);max-width:60ch;margin:16px 0 0}
.facts strong{color:var(--navy);font-weight:800;font-variant-numeric:tabular-nums}
.prices{position:relative;height:64px;margin:30px 0 0;border-bottom:1px solid var(--line)}
.prices i{position:absolute;bottom:14px;width:9px;height:9px;margin-left:-4.5px;border-radius:50%;background:#274de433}
.prices .med{position:absolute;bottom:0;top:0;width:2px;margin-left:-1px;background:var(--navy)}
.prices .med span{position:absolute;top:-2px;left:8px;white-space:nowrap;font-size:.78rem;font-weight:700;color:var(--navy)}
.prices .med.end span{left:auto;right:8px}
.scale{display:flex;justify-content:space-between;font-size:.75rem;color:var(--muted);margin-top:6px;font-variant-numeric:tabular-nums}
.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.6fr);gap:48px;margin-top:40px}
.split h3{font-size:1rem;color:var(--navy);margin:0 0 12px}
.rank{list-style:none;margin:0;padding:0;counter-reset:r}
.rank li{counter-increment:r;display:grid;grid-template-columns:22px 1fr auto;gap:10px;align-items:baseline;padding:8px 0;border-bottom:1px solid var(--line);font-size:.9rem}
.rank li::before{content:counter(r);color:var(--muted);font-weight:700;font-variant-numeric:tabular-nums}
.rank em{grid-column:2 / span 2;height:4px;border-radius:2px;background:var(--pale);font-style:normal;overflow:hidden}
.rank em::after{content:"";display:block;height:100%;width:var(--w);background:var(--blue)}
.rank b{font-variant-numeric:tabular-nums;color:var(--navy)}
.awards{overflow-x:auto}
.awards table{border-collapse:collapse;width:100%;font-size:.86rem;min-width:560px}
.awards th{text-align:left;font-size:.78rem;font-weight:600;color:var(--muted);padding:0 8px 8px;border-bottom:2px solid var(--navy)}
.awards td{padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.awards td small{display:block;color:var(--muted)}
.awards .n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.awards .one{color:var(--urgent);font-weight:700}
@media (max-width:820px){.split{grid-template-columns:1fr;gap:36px}}
/* MCP */
.mcp{background:var(--navy);color:#dfe7f6;border-radius:22px;padding:clamp(24px,4vw,44px);display:grid;grid-template-columns:1fr 1fr;gap:36px;align-items:center}
.mcp h2{color:#fff}.mcp p{margin:12px 0 0;max-width:46ch}
.mcp code{display:inline-block;margin-top:14px;padding:8px 12px;border-radius:8px;background:#ffffff14;color:#fff;font-size:.95rem;user-select:all;overflow-wrap:anywhere}
.mcp a{color:#a9bdf7}
.chat{display:grid;gap:10px}
.chat p{margin:0;padding:12px 16px;border-radius:16px;font-size:.95rem;line-height:1.5;max-width:none}
.chat .q{justify-self:end;background:var(--blue);color:#fff;border-bottom-right-radius:4px;max-width:90%}
.chat .a{background:#ffffff12;border-bottom-left-radius:4px;color:#c9d5f0;max-width:90%}
@media (max-width:820px){.mcp{grid-template-columns:1fr}}
.others ul{display:flex;flex-wrap:wrap;gap:8px;list-style:none;padding:0;margin:16px 0 0}
.others a{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:6px 14px;font-size:.9rem;color:var(--ink);text-decoration:none}
.others a:hover{border-color:var(--blue);color:var(--blue)}
.others a[aria-current]{background:var(--navy);border-color:var(--navy);color:#fff}
.note{color:var(--muted);font-size:.82rem;max-width:75ch;margin:48px 0 0}
.empty{color:var(--muted);margin-top:16px}
/* hub */
.hub{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;list-style:none;padding:0;margin:40px 0 0}
.hub a{display:grid;gap:10px;height:100%;padding:18px 20px 16px;border-radius:16px;background:var(--ice);color:var(--ink);text-decoration:none;border:1px solid transparent}
.hub a:hover,.hub a:focus-visible{border-color:var(--blue)}
.hub b{font-size:1.08rem;color:var(--navy);letter-spacing:-.02em}
.hub small{color:var(--muted);font-size:.85rem}
.hub .bars{height:44px;gap:2px}.hub .bars b{display:none}
@media (max-width:900px){.hub{grid-template-columns:repeat(2,minmax(0,1fr))}}@media (max-width:600px){.hub{grid-template-columns:1fr}}
'''

HEADER = '''<header class="site-header">
    <div class="container header-inner">
      <a class="brand" href="/" aria-label="tenderkit, strona główna">
        <img class="brand-mark" src="/assets/tenderkit-mark.svg" width="32" height="32" alt=""><span class="brand-wordmark">tenderkit</span>
      </a>
      <nav aria-label="Nawigacja główna">
        <a href="/#jak-to-dziala">Jak to działa</a>
        <a href="/#mcp">Serwer MCP</a>
        <a href="/#dla-programistow">Dla programistów</a>
        <a href="/#przyklady">Przykładowe implementacje</a>
        <a href="/przetargi/" aria-current="page">Przetargi</a>
      </nav>
      <a class="github-link" href="https://github.com/pjagielski/tenderkit" target="_blank" rel="noopener noreferrer">
        <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2a10 10 0 0 0-3.16 19.49c.5.09.68-.22.68-.48v-1.69c-2.78.6-3.37-1.18-3.37-1.18-.46-1.16-1.11-1.47-1.11-1.47-.91-.62.07-.61.07-.61 1 .07 1.53 1.03 1.53 1.03.89 1.53 2.34 1.09 2.91.83.09-.65.35-1.09.64-1.34-2.22-.25-4.56-1.11-4.56-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.65 0 0 .84-.27 2.75 1.02A9.55 9.55 0 0 1 12 6.99c.85 0 1.7.11 2.5.34 1.91-1.29 2.75-1.02 2.75-1.02.55 1.38.2 2.4.1 2.65.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.69-4.58 4.94.36.31.68.92.68 1.85v2.58c0 .27.18.58.69.48A10 10 0 0 0 12 2Z"/></svg>
        <span>GitHub</span>
        <svg class="external-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 12 12 4M4 4h8v8" stroke="currentColor" stroke-width="1.5"/></svg>
      </a>
    </div>
  </header>'''

FOOTER = '''<footer class="site-footer"><div class="container footer-inner"><a class="brand" href="/"><img class="brand-mark" src="/assets/tenderkit-mark.svg" width="32" height="32" alt=""><span class="brand-wordmark">tenderkit</span></a><p>Otwarty framework do polskich przetargów.<br><span class="concept-note">Apache-2.0</span></p><div><a href="/przetargi/">Przetargi</a><a href="https://github.com/pjagielski/tenderkit" target="_blank" rel="noopener noreferrer">GitHub</a><a href="https://pypi.org/project/tenderkit/" target="_blank" rel="noopener noreferrer">PyPI</a><a href="/#kontakt">Kontakt</a><a href="/privacy">Prywatność</a></div></div></footer>
  <!-- Cloudflare Web Analytics: cookieless page views; the token is public by design. -->
  <script type='module' src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{"token": "c05c0c48f7bf408a8ad6604d905de210"}'></script>'''


def document(*, path, title, description, body, ld):
    url = f'{BASE}{path}'
    return f'''<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#f3f6fc">
  <title>{e(title)}</title>
  <meta name="description" content="{e(description)}">
  <link rel="canonical" href="{url}">
  <meta property="og:type" content="website">
  <meta property="og:locale" content="pl_PL">
  <meta property="og:site_name" content="tenderkit">
  <meta property="og:title" content="{e(title)}">
  <meta property="og:description" content="{e(description)}">
  <meta property="og:url" content="{url}">
  <meta property="og:image" content="{BASE}/assets/og-image.png">
  <meta name="twitter:card" content="summary_large_image">
  <link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/assets/fonts.css">
  <link rel="stylesheet" href="/styles.css">
  <style>{STYLE}</style>
  <script type="application/ld+json">{json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")}</script>
</head>
<body>
  <a class="skip-link" href="#main">Przejdź do treści</a>
  {HEADER}
  <main id="main" class="topic">
{body}
  </main>
  {FOOTER}
</body>
</html>
'''


def deadline_day(notice):
    try:
        return datetime.fromisoformat(str(notice.get('deadline'))[:10]).date()
    except ValueError:
        return None


def summarize(data, today):
    notices = sorted(data['notices'].get('notices') or [], key=lambda n: n.get('deadline') or '9999')
    results = data['results']
    summary = results.get('summary') or {}
    prices = next((p for p in summary.get('prices') or [] if p.get('currency') == 'PLN'), None)
    offers = summary.get('offers') or {}
    awards = results.get('awards') or []
    by_notice = {(n.get('source'), n.get('number')): n for n in results.get('notices') or []}
    # One company is spelled several ways ("Sp. z o.o.", "SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ"); count it once
    # and show its most common spelling.
    counts, spellings = Counter(), {}
    for a in awards:
        if a.get('contractor'):
            key = company_key(a['contractor'])
            counts[key] += 1
            spellings.setdefault(key, Counter())[a['contractor'].strip()] += 1
    contractors = [(spellings[k].most_common(1)[0][0], n) for k, n in counts.most_common(6)]
    per_day = Counter(d for d in map(deadline_day, notices) if d)
    award_prices = []
    for a in awards:
        if a.get('currency') in (None, 'PLN'):
            try:
                value = float(a.get('price'))
            except (TypeError, ValueError):
                continue
            if value > 0:
                award_prices.append(value)
    return dict(open=notices, open_total=data['notices'].get('total', len(notices)), awards=awards,
                award_total=summary.get('awards', len(awards)), procedures=summary.get('procedures'),
                buyers=summary.get('buyers'), median=prices and prices.get('median'),
                single=offers.get('single'), offers_known=offers.get('known'), by_notice=by_notice,
                contractors=contractors, sample=len(awards), per_day=per_day, prices=award_prices, today=today)


def when(d, today):
    left = (d - today).days
    if left <= 0:
        return 'dziś'
    if left == 1:
        return 'jutro'
    return f'za {left} {plural(left, "dzień", "dni", "dni")}'


def rail(s, today, *, days=RAIL_DAYS, links=True):
    counts = [s['per_day'].get(today + timedelta(days=i), 0) for i in range(days)]
    top = max(counts + [1])
    bars, ticks = [], []
    for i, n in enumerate(counts):
        d = today + timedelta(days=i)
        kind = ' none' if not n else (' soon' if i <= 3 else '')
        we = ' we' if d.weekday() >= 5 else ''
        label = f'{d.day} {MONTHS[d.month - 1]}: {n} {plural(n, "termin", "terminy", "terminów")}'
        inner = f'<i style="--h:{round(100 * n / top)}%;--n:{i}"></i><b style="--h:{round(100 * n / top)}%">{n or ""}</b>'
        if links and n:
            bars.append(f'<a class="{kind.strip()}{we}" href="#d-{d.isoformat()}" data-day="{d.isoformat()}" title="{e(label)}" aria-label="{e(label)}">{inner}</a>')
        else:
            bars.append(f'<span class="{(kind + we).strip()}" data-day="{d.isoformat()}" title="{e(label)}">{inner}</span>')
        ticks.append(f'<span>{d.day if i == 0 or d.weekday() == 0 else ""}</span>')
    return (f'<div class="bars" style="--days:{days}">{"".join(bars)}</div>'
            + (f'<div class="ticks" style="--days:{days}" aria-hidden="true">{"".join(ticks)}</div>' if links else ''))


def price_strip(prices, median):
    if len(prices) < 5 or not median:
        return ''
    import math
    # The scale spans the middle 90% of prices; a few odd awards (a framework total, a typo) would squash the rest.
    ranked = sorted(prices)
    low, high = ranked[int(len(ranked) * .05)], ranked[min(len(ranked) - 1, int(len(ranked) * .95))]
    lo, hi = math.log10(low), math.log10(high)
    if hi - lo < 0.3:
        return ''
    pos = lambda v: round(min(100, max(0, 100 * (math.log10(v) - lo) / (hi - lo))), 2)
    dots = ''.join(f'<i style="left:{pos(v)}%"></i>' for v in prices)
    m = float(median)
    edge = ' end' if pos(m) > 70 else ''
    return (f'<div class="prices" role="img" aria-label="Ceny wybranych ofert: 90% mieści się między {e(money(low))} a '
            f'{e(money(high))}, mediana {e(money(m))}">{dots}<span class="med{edge}" style="left:{pos(m)}%">'
            f'<span>mediana {money(m)}</span></span></div>'
            f'<div class="scale" aria-hidden="true"><span>{money(low)}</span><span>90% cen w tym przedziale</span>'
            f'<span>{money(high)}</span></div>')


def filters(s):
    """The filter bar. It starts hidden and the script shows it, so without scripts the whole list stays readable."""
    regions = Counter(str(n.get('buyer_region') or '')[:2] for n in s['open'])
    options = ''.join(f'<option value="{code}">{REGIONS[code]} ({n})</option>'
                      for code, n in sorted(((c, n) for c, n in regions.items() if c in REGIONS),
                                            key=lambda cn: REGIONS[cn[0]]))
    ranges = [('', 'Wszystkie'), ('0-7', 'Do 7 dni'), ('8-14', '8–14 dni'), ('15-', 'Później')]
    buttons = ''.join(f'<button type="button" data-range="{v}" aria-pressed="{"true" if not v else "false"}">{k}</button>'
                      for v, k in ranges)
    return (f'    <div class="filters" id="filtry" hidden>'
            f'<label>Województwo<select id="f-woj"><option value="">wszystkie ({len(s["open"])})</option>{options}</select></label>'
            f'<div class="range" role="group" aria-label="Termin składania ofert">{buttons}</div>'
            f'<span class="dayf" id="f-dzien" hidden><span></span><button type="button" aria-label="Usuń filtr dnia">×</button></span>'
            f'<p class="count" id="f-ile" aria-live="polite"></p></div>')


def topic_page(topic, s, topics, today):
    slug, name = topic['slug'], topic['name']
    open_n, award_n = s['open_total'], s['award_total']
    description = (f'{open_n} {plural(open_n, "otwarty przetarg", "otwarte przetargi", "otwartych przetargów")} '
                   f'na {name} z BZP i TED, z terminami składania ofert. Rozstrzygnięcia z {RESULT_DAYS} dni: '
                   f'wykonawcy, ceny i liczba ofert. Aktualizacja codziennie.')
    first = deadline_day(s['open'][0]) if s['open'] else None
    in_rail = sum(s['per_day'].get(today + timedelta(days=i), 0) for i in range(RAIL_DAYS))
    later = len(s['open']) - in_rail
    out = [f'    <p class="crumbs"><a href="/">tenderkit</a> / <a href="/przetargi/">Przetargi</a> / {e(name)}</p>',
           f'    <h1>{e(topic["title"])}</h1>',
           f'    <p class="lead">{e(topic["intro"])}</p>']
    if s['open']:
        head = (f'<p><b id="rail-sum">{open_n} {plural(open_n, "otwarte postępowanie", "otwarte postępowania", "otwartych postępowań")}'
                f'{f", najbliższy termin {when(first, today)}" if first else ""}.</b>'
                f' <span id="rail-later" data-days="{RAIL_DAYS}">Terminy na najbliższe {RAIL_DAYS} dni{f", później {later}" if later > 0 else ""}.</span></p>')
        out.append(f'    <div class="rail"><div class="rail-head">{head}<small>Stan na {day(today)}</small></div>'
                   f'{rail(s, today)}</div>')

    out.append(f'    <section aria-labelledby="otwarte"><h2 id="otwarte">Do kiedy można złożyć ofertę</h2>')
    if s['open']:
        out.append('    <p class="sub">Według terminu składania ofert, najbliższe na górze. Tytuł prowadzi do '
                   'ogłoszenia na platformie zamawiającego.</p>')
        out.append(filters(s))
        groups = {}
        for n in s['open'][:OPEN_LIMIT]:
            groups.setdefault(deadline_day(n), []).append(n)
        out.append('    <div id="lista">')
        for d, items in groups.items():
            soon = d is not None and (d - today).days <= 3
            heading = (f'{WEEKDAYS[d.weekday()].capitalize()}, {d.day} {MONTHS[d.month - 1]}'
                       f'<small>{when(d, today)}, <span class="cnt">{len(items)} '
                       f'{plural(len(items), "termin", "terminy", "terminów")}</span></small>' if d else 'Termin nieznany')
            anchor = f' id="d-{d.isoformat()}"' if d else ''
            out.append(f'    <div class="day{" soon" if soon else ""}"{anchor}><h3>{heading}</h3><ul>')
            for n in items:
                link = safe_url(n.get('platform_url')) if n.get('platform_url_specific') else None
                link = link or safe_url(n.get('url'))
                title = (f'<a class="t" href="{e(link)}" rel="nofollow noopener" target="_blank">{e(n.get("title"))}</a>'
                         if link else f'<span class="t">{e(n.get("title"))}</span>')
                where = place(n)
                value = (f'<span class="val">{money(n["value"], n.get("currency"))}<small>wartość szacunkowa</small></span>'
                         if n.get('value') else '')
                region = str(n.get('buyer_region') or '')[:2]
                left = (d - today).days if d else ''
                out.append(f'      <li data-region="{e(region)}" data-left="{left}" data-day="{d.isoformat() if d else ""}">'
                           f'<span>{title}<span class="src" title="Źródło ogłoszenia">{e(n.get("source"))}</span></span>'
                           f'{value}<span class="who">{e(name_case(n.get("buyer_name")))}{", " + e(where) if where else ""}</span></li>')
            out.append('    </ul></div>')
        out.append('    </div>')
        out.append('    <p class="empty" id="brak" hidden>Żadne otwarte postępowanie nie pasuje do tych filtrów. '
                   '<button type="button" class="link" data-reset>Pokaż wszystkie</button></p>')
        out.append('    <button type="button" class="next" id="dalej" hidden></button>')
        if open_n > len(s['open']):
            out.append(f'    <p class="more">Na stronie jest {len(s["open"])} z {open_n} postępowań. Pełną listę z '
                       f'filtrami zwróci serwer MCP poniżej.</p>')
        out.append(f'    <script>{FILTER_JS}</script>')
    else:
        out.append('    <p class="empty">Dziś nie ma otwartych postępowań w tej kategorii. Lista odświeża się '
                   'codziennie rano.</p>')
    out.append('    </section>')

    out.append(f'    <section aria-labelledby="wyniki"><h2 id="wyniki">Kto wygrywa i za ile</h2>')
    if s['awards']:
        facts = [f'W ostatnich {RESULT_DAYS} dniach rozstrzygnięto <strong>{award_n}</strong> '
                 f'{plural(award_n, "część", "części", "części")} w {s["procedures"] or "?"} postępowaniach '
                 f'{s["buyers"] or "?"} zamawiających.']
        if s['median']:
            facts.append(f'Połowa wybranych ofert kosztowała mniej niż <strong>{money(s["median"])}</strong>.')
        if s['offers_known']:
            share = round(100 * (s['single'] or 0) / s['offers_known'])
            facts.append(f'W <strong>{share}%</strong> części wpłynęła tylko jedna oferta '
                         f'({s["single"]} z {s["offers_known"]} ze znaną liczbą ofert).')
        out.append(f'    <p class="facts">{" ".join(facts)}</p>')
        out.append('    ' + price_strip(s['prices'], s['median']))
        out.append('    <div class="split">')
        if s['contractors']:
            top = s['contractors'][0][1]
            basis = f' (z {s["sample"]} najnowszych rozstrzygnięć)' if award_n > s['sample'] else ''
            out.append(f'      <div><h3>Najczęściej wybierani wykonawcy{basis}</h3><ol class="rank">' + ''.join(
                f'<li><span>{e(c)}</span><b>{k}</b><em style="--w:{round(100 * k / top)}%"></em></li>'
                for c, k in s['contractors']) + '</ol></div>')
        out.append('      <div class="awards"><h3>Ostatnie rozstrzygnięcia</h3><table><thead><tr><th>Postępowanie</th>'
                   '<th>Wykonawca</th><th class="n">Cena</th><th class="n">Ofert</th></tr></thead><tbody>')
        for a in s['awards'][:12]:
            notice = s['by_notice'].get((a.get('source'), a.get('number')), {})
            link = safe_url(notice.get('url'))
            title = e(notice.get('title') or a.get('number'))
            title = f'<a href="{e(link)}" rel="nofollow noopener" target="_blank">{title}</a>' if link else title
            when_ = day(a.get('date') or notice.get('published'))
            offers_n = a.get('offers')
            out.append(f'      <tr><td>{title}<small>{e(name_case(notice.get("buyer_name")))}{", " + when_ if when_ else ""}</small></td>'
                       f'<td>{e(a.get("contractor"))}</td><td class="n">{money(a.get("price"), a.get("currency"))}</td>'
                       f'<td class="n{" one" if offers_n == 1 else ""}">{e(offers_n or "")}</td></tr>')
        out.append('      </tbody></table></div>')
        out.append('    </div>')
    else:
        out.append('    <p class="empty">W tym okresie nie opublikowano rozstrzygnięć w tej kategorii.</p>')
    out.append('    </section>')

    out.append(f'''    <section class="mcp" aria-labelledby="cta">
      <div>
        <h2 id="cta">Zapytaj o te przetargi swój model AI</h2>
        <p>Dodaj publiczny serwer MCP tenderkit jako konektor w Claude, ChatGPT, Codex albo Cursorze. Bez instalacji i bez konta.</p>
        <code>https://mcp.tenderkit.pl/mcp</code>
        <p><a href="/#mcp">Jak podłączyć serwer</a>, albo <a href="https://github.com/pjagielski/tenderkit#readme" target="_blank" rel="noopener">uruchom tenderkit u siebie</a>.</p>
      </div>
      <div class="chat" aria-label="Przykładowa rozmowa">
        <p class="q">{e(topic["prompt"])}</p>
        <p class="a">Model przeszuka ogłoszenia z BZP i TED, poda zamawiających, terminy i linki do dokumentów, a na życzenie porówna je z wcześniejszymi rozstrzygnięciami.</p>
      </div>
    </section>''')
    current = ' aria-current="page"'
    out.append('    <section class="others" aria-labelledby="inne"><h2 id="inne">Inne kategorie</h2><ul>' + ''.join(
        f'<li><a href="/przetargi/{e(t["slug"])}"{current if t["slug"] == slug else ""}>{e(t["name"])}</a></li>'
        for t in topics) + '</ul></section>')
    out.append('    <p class="note">Lista powstaje automatycznie z publicznych ogłoszeń według kodów CPV '
               f'{e(", ".join(topic["cpv"]))}. Może być niepełna albo zawierać pozycje spoza tematu. Zanim złożysz '
               'ofertę, sprawdź ogłoszenie i dokumenty u źródła.</p>')
    ld = {'@context': 'https://schema.org', '@graph': [
        {'@type': 'CollectionPage', 'name': topic['title'], 'url': f'{BASE}/przetargi/{slug}', 'inLanguage': 'pl-PL',
         'dateModified': today.isoformat(), 'description': description, 'isPartOf': {'@id': f'{BASE}/#website'}},
        {'@type': 'BreadcrumbList', 'itemListElement': [
            {'@type': 'ListItem', 'position': 1, 'name': 'tenderkit', 'item': f'{BASE}/'},
            {'@type': 'ListItem', 'position': 2, 'name': 'Przetargi', 'item': f'{BASE}/przetargi/'},
            {'@type': 'ListItem', 'position': 3, 'name': topic['title'], 'item': f'{BASE}/przetargi/{slug}'}]}]}
    return document(path=f'/przetargi/{slug}', title=f'{topic["title"]}: {open_n} otwartych, terminy i wyniki | tenderkit',
                    description=description, body='\n'.join(out), ld=ld)


def hub_page(topics, summaries, today):
    items = []
    for t in topics:
        s = summaries[t['slug']]
        line = f'{s["open_total"]} otwartych'
        if s['median']:
            line += f', mediana ceny {money(s["median"])}'
        items.append(f'<li><a href="/przetargi/{e(t["slug"])}"><b>{e(t["name"][:1].upper() + t["name"][1:])}</b>{rail(s, today, days=21, links=False)}'
                     f'<small>{e(line)}</small></a></li>')
    body = f'''    <p class="crumbs"><a href="/">tenderkit</a> / Przetargi</p>
    <h1>Przetargi według tematu</h1>
    <p class="lead">Otwarte postępowania i rozstrzygnięcia z Biuletynu Zamówień Publicznych i TED w {len(topics)} kategoriach. Słupki pokazują, ile terminów składania ofert mija w każdym z najbliższych 21 dni. Stan na {day(today)}.</p>
    <ul class="hub">{"".join(items)}</ul>
    <p class="note">Własne kategorie, regiony i słowa kluczowe ustawisz w profilu tenderkit albo zapytasz o nie model przez <a href="/#mcp">publiczny serwer MCP</a>.</p>'''
    ld = {'@context': 'https://schema.org', '@type': 'CollectionPage', 'name': 'Przetargi według tematu',
          'url': f'{BASE}/przetargi/', 'inLanguage': 'pl-PL', 'dateModified': today.isoformat(),
          'hasPart': [{'@type': 'WebPage', 'name': t['title'], 'url': f'{BASE}/przetargi/{t["slug"]}'} for t in topics]}
    return document(path='/przetargi/', title='Przetargi według tematu: terminy i wyniki | tenderkit',
                    description='Otwarte przetargi i rozstrzygnięcia z BZP i TED w popularnych kategoriach, '
                                'aktualizowane codziennie: IT, leki, drogi, projektowanie, energia i inne.',
                    body=body, ld=ld)


def sitemap(existing, topics, today):
    """Keep the hand-written entries and add the topic pages."""
    locs = [loc for loc in re.findall(r'<loc>([^<]+)</loc>', existing) if '/przetargi/' not in loc]
    lastmods = dict(re.findall(r'<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>', existing))
    rows = [(loc, lastmods.get(loc)) for loc in locs]
    rows += [(f'{BASE}/przetargi/', today.isoformat())]
    rows += [(f'{BASE}/przetargi/{t["slug"]}', today.isoformat()) for t in topics]
    body = ''.join(f'  <url>\n    <loc>{e(loc)}</loc>\n' + (f'    <lastmod>{mod}</lastmod>\n' if mod else '') + '  </url>\n'
                   for loc, mod in rows)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{body}</urlset>\n'


def main(argv=None):
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--site', type=Path, default=here.parent/'site')
    parser.add_argument('--topics', type=Path, default=here/'topics.toml')
    parser.add_argument('--mcp', default=MCP)
    parser.add_argument('--cache', type=Path, help='reuse the server answers saved here (for working on the layout)')
    args = parser.parse_args(argv)
    topics = tomllib.loads(args.topics.read_text(encoding='utf-8'))['topic']
    today = date.today()
    mcp = None
    summaries = {}
    out = args.site/'przetargi'
    out.mkdir(parents=True, exist_ok=True)
    for topic in topics:
        cached = args.cache and args.cache/f'{topic["slug"]}.json'
        if cached and cached.exists():
            data = json.loads(cached.read_text(encoding='utf-8'))
        else:
            if mcp is None:
                mcp = Mcp(args.mcp)
                mcp.start()
            data = fetch(mcp, topic, today)
            time.sleep(0.5)
            if cached:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        summaries[topic['slug']] = summarize(data, today)
    for topic in topics:
        (out/f'{topic["slug"]}.html').write_text(topic_page(topic, summaries[topic['slug']], topics, today),
                                                 encoding='utf-8')
        s = summaries[topic['slug']]
        print(f'{topic["slug"]}: {s["open_total"]} open, {s["award_total"]} awards', file=sys.stderr)
    (out/'index.html').write_text(hub_page(topics, summaries, today), encoding='utf-8')
    map_path = args.site/'sitemap.xml'
    map_path.write_text(sitemap(map_path.read_text(encoding='utf-8'), topics, today), encoding='utf-8')


if __name__ == '__main__':
    main()

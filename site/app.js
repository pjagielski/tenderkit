'use strict';

const examples = {
  install: {
    filename: 'Instalacja z PyPI',
    text: '# Środowisko wirtualne i pakiet z PyPI\npython -m venv .venv\n.venv/bin/python -m pip install "tenderkit[mcp,documents]"\n\n# Wbudowane profile: leki, oprogramowanie, wyroby-medyczne\n.venv/bin/tenderkit profiles',
    explanation: 'Python 3.11+ na Windows, macOS i Linuksie.'
  },
  profile: {
    filename: 'oprogramowanie.toml',
    text: 'name = "oprogramowanie"\ncpv = ["72*", "48*"]\n\nkeywords_any = ["system", "oprogramowanie"]\nexclude_keywords = ["dostawa sprzętu"]\nregions = ["14"]\n\ndeadline_days = { min = 1, max = 30 }',
    explanation: 'Jeden plik definiuje, które ogłoszenia trafią do Twojego procesu.'
  },
  cli: {
    filename: 'Wiersz poleceń',
    text: '# Wyszukaj i odsień postępowania\ntenderkit fetch --profile oprogramowanie\n\n# Pobierz materiały z platform zakupowych\ntenderkit download --profile oprogramowanie --limit 10\n\n# Przekaż dane do własnego procesu\ntenderkit export --profile oprogramowanie --format jsonl\n\n# Formalne wyniki: wykonawca, cena i liczba ofert\ntenderkit results --buyer Kraków --cpv "72*"',
    explanation: 'Pobieranie korzysta z zapisanych ogłoszeń. Aby przejść do kolejnej partii, podaj next_offset jako --offset.'
  },
  mcp: {
    filename: 'Narzędzia dla Twojego AI',
    text: '# Uruchom lokalny serwer MCP\ntenderkit mcp\n\n# Ogłoszenia i dokumenty dla Twojego AI\nsearch_notices(profile="oprogramowanie")\nlist_documents(source="BZP", number="…")\nfetch_documents(source="BZP", number="…", keys=["…"])\nprocedure_results(source="BZP", number="…")\nread_document(document_id="<downloaded.sha256>")',
    explanation: 'Przekaż source i number z wyniku wyszukiwania oraz klucze załączników z listy dokumentów. Do analizy użyj promptu MCP analyze_it_tender lub compare_public_bids.'
  }
};
let activeCode = 'install';
let activeProfile = 'biezace';
let activeSector = 'it';
let activeSource = 'all';
let data = [];
let toastTimer;

function notify(message) {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('visible'), 2600);
}

function activateTab(button, selector) {
  document.querySelectorAll(selector).forEach(tab => {
    const active = tab === button;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
  });
}

function tabKeyboard(event, selector) {
  const tabs = Array.from(document.querySelectorAll(selector));
  let next = tabs.indexOf(event.currentTarget);
  if (event.key === 'ArrowRight') next = (next + 1) % tabs.length;
  else if (event.key === 'ArrowLeft') next = (next - 1 + tabs.length) % tabs.length;
  else if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = tabs.length - 1;
  else return;
  event.preventDefault();
  tabs[next].focus();
  tabs[next].click();
}

function renderCode() {
  const example = examples[activeCode];
  document.getElementById('code-filename').textContent = example.filename;
  const code = document.getElementById('code-text');
  code.replaceChildren();
  example.text.split('\n').forEach((line, index, lines) => {
    const span = document.createElement('span');
    if (line.startsWith('#')) span.className = 'code-comment';
    const parts = line.split(/("[^"]*")/g);
    parts.forEach((part, i) => {
      if (i % 2 === 1) {
        const string = document.createElement('span');
        string.className = 'code-string';
        string.textContent = part;
        span.append(string);
      } else span.append(document.createTextNode(part));
    });
    code.append(span);
    if (index < lines.length - 1) code.append(document.createTextNode('\n'));
  });
  document.getElementById('code-explanation').textContent = example.explanation;
  document.getElementById('code-panel').setAttribute('aria-labelledby', 'code-' + activeCode);
}

function fold(text) {
  return String(text).normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ł/g, 'l').replace(/Ł/g, 'L').toLocaleLowerCase('pl');
}

function renderNotices() {
  const search = fold(document.getElementById('demo-search').value.trim());
  const results = data.filter(n => n.sector === activeSector && n.profile === activeProfile && (activeSource === 'all' || n.source === activeSource) && fold(n.title + ' ' + n.buyer_city + ' ' + n.buyer_name).includes(search));
  const container = document.getElementById('notice-results');
  container.replaceChildren();
  container.setAttribute('aria-busy', 'false');
  document.getElementById('results-count').textContent = (results.length > 3 ? '3 z ' + results.length + ' przykładowych ogłoszeń' : results.length + ' przykładowe ogłoszenia');
  if (results.length === 1) document.getElementById('results-count').textContent = '1 przykładowe ogłoszenie';
  if (!results.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-results';
    const text = document.createElement('p');
    text.textContent = 'Brak dopasowań w tym zestawie przykładów.';
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.textContent = 'Wyczyść filtry';
    clear.addEventListener('click', () => {
      document.getElementById('demo-search').value = '';
      activeSource = 'all';
      document.querySelectorAll('[data-source]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.source === 'all')));
      renderNotices();
    });
    empty.append(text, clear);
    container.append(empty);
    return;
  }
  results.slice(0, 3).forEach(notice => {
    const row = document.createElement('article');
    row.className = 'notice-row';
    const link = document.createElement('button');
    link.className = 'notice-copy';
    link.type = 'button';
    link.setAttribute('aria-label', 'Zobacz przykład: ' + notice.title);
    link.addEventListener('click', () => showExample(notice));
    const meta = document.createElement('div');
    meta.className = 'notice-meta';
    const badge = document.createElement('span');
    badge.className = 'source-badge' + (notice.source === 'TED' ? ' ted' : notice.source === 'BK' ? ' bk' : '');
    badge.textContent = notice.source;
    const number = document.createElement('span');
    number.textContent = notice.number;
    meta.append(badge, number);
    const title = document.createElement('h3');
    title.textContent = notice.title;
    const city = document.createElement('p');
    city.className = 'notice-city';
    city.textContent = notice.buyer_city + ' · ' + notice.buyer_name;
    link.append(meta, title, city);
    const arrow = document.createElement('span');
    arrow.className = 'notice-open';
    arrow.setAttribute('aria-hidden', 'true');
    arrow.innerHTML = '<svg viewBox="0 0 16 16" fill="none"><path d="M4 12 12 4M4 4h8v8" stroke="currentColor" stroke-width="1.5"/></svg>';
    row.append(link, arrow);
    container.append(row);
  });
}

document.querySelectorAll('[data-code]').forEach(button => {
  button.addEventListener('click', () => {
    activeCode = button.dataset.code;
    activateTab(button, '[data-code]');
    renderCode();
  });
  button.addEventListener('keydown', event => tabKeyboard(event, '[data-code]'));
});
document.querySelectorAll('[data-profile]').forEach(button => {
  button.addEventListener('click', () => {
    activeProfile = button.dataset.profile;
    activateTab(button, '[data-profile]');
    updateDemoCriteria();
    activeSource = 'all';
    document.querySelectorAll('[data-source]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.source === 'all')));
    document.getElementById('notice-results').setAttribute('aria-labelledby', button.id);
    document.getElementById('demo-search').value = '';
    renderNotices();
  });
  button.addEventListener('keydown', event => tabKeyboard(event, '[data-profile]'));
});
document.querySelectorAll('[data-source]').forEach(button => {
  button.addEventListener('click', () => {
    activeSource = button.dataset.source;
    document.querySelectorAll('[data-source]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
    renderNotices();
  });
});
document.getElementById('demo-search').addEventListener('input', renderNotices);
document.getElementById('copy-code').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(examples[activeCode].text);
    notify('Przykład skopiowany.');
  } catch {
    const code = document.getElementById('code-text');
    const range = document.createRange();
    range.selectNodeContents(code);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    notify('Kod zaznaczony. Użyj Ctrl+C lub ⌘C, aby go skopiować.');
  }
});
renderCode();
fetch('assets/notices.json')
  .then(response => {
    if (!response.ok) throw new Error('Nie udało się pobrać przykładów');
    return response.json();
  })
  .then(notices => { data = notices; renderNotices(); })
  .catch(() => {
    document.getElementById('notice-results').setAttribute('aria-busy', 'false');
    document.getElementById('notice-results').textContent = 'Przykłady są chwilowo niedostępne. Możesz sprawdzić projekt w repozytorium GitHub.';
    document.getElementById('notice-results').classList.add('empty-results');
  });

function showExample(notice) {
  let dialog = document.getElementById('example-dialog');
  if (!dialog) {
    dialog = document.createElement('dialog');
    dialog.id = 'example-dialog';
    dialog.setAttribute('aria-labelledby', 'example-title');
    dialog.innerHTML = '<form method="dialog"><button class="example-close" aria-label="Zamknij przykład">×</button></form><p class="example-kicker">Fikcyjne ogłoszenie</p><h2 id="example-title"></h2><p id="example-meta"></p><h3>Dlaczego pasuje do profilu?</h3><p id="example-reason"></p><p class="example-disclaimer">Przykład pokazuje dobór ogłoszeń do wewnętrznego profilu firmy. Nie jest rzeczywistym postępowaniem.</p>';
    document.body.append(dialog);
    dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  }
  dialog.querySelector('#example-title').textContent = notice.title;
  dialog.querySelector('#example-meta').textContent = notice.source + ' · ' + notice.buyer_city + ' · ' + notice.buyer_name;
  dialog.querySelector('#example-reason').textContent = notice.reason;
  dialog.showModal();
}
function drawProductFlow() {
  const map = document.querySelector('.product-map');
  const svg = map.querySelector('.product-map-lines');
  const bounds = map.getBoundingClientRect();
  if (!bounds.width || !bounds.height) return;
  svg.setAttribute('viewBox', '0 0 ' + bounds.width + ' ' + bounds.height);
  const ns = 'http://www.w3.org/2000/svg';
  svg.replaceChildren();
  const defs = document.createElementNS(ns, 'defs');
  const marker = document.createElementNS(ns, 'marker');
  Object.entries({id:'flow-arrow',viewBox:'0 0 10 10',refX:'9',refY:'5',markerWidth:'7',markerHeight:'7',orient:'auto'}).forEach(([k,v]) => marker.setAttribute(k,v));
  const head = document.createElementNS(ns, 'path');
  head.setAttribute('d','M2 1 8 5 2 9'); head.setAttribute('stroke','#7292ce'); head.setAttribute('stroke-width','1.5'); head.setAttribute('fill','none');
  marker.append(head); defs.append(marker); svg.append(defs);
  function point(el) { const r=el.getBoundingClientRect(); return {x:r.left+r.width/2-bounds.left,top:r.top-bounds.top,bottom:r.bottom-bounds.top}; }
  function line(d,arrow=false) { const path=document.createElementNS(ns,'path'); path.setAttribute('d',d); path.setAttribute('stroke','#91abd9'); path.setAttribute('stroke-width','1.5'); path.setAttribute('stroke-linejoin','round'); path.setAttribute('stroke-linecap','round'); if(arrow)path.setAttribute('marker-end','url(#flow-arrow)'); svg.append(path); }
  const core=point(map.querySelector('.product-map-core'));
  const sources=Array.from(map.querySelectorAll('.source-floating')).map(point);
  const inputY=(Math.max(...sources.map(p=>p.bottom))+core.top)/2;
  sources.forEach(p=>line('M'+p.x+' '+p.bottom+'V'+inputY));
  line('M'+sources[0].x+' '+inputY+'H'+sources[sources.length-1].x);
  line('M'+core.x+' '+inputY+'V'+(core.top-5),true);
  const outputs=Array.from(map.querySelectorAll('.product-map-outputs>span')).map(point);
  const outputY=(core.bottom+Math.min(...outputs.map(p=>p.top)))/2;
  line('M'+core.x+' '+core.bottom+'V'+outputY);
  line('M'+outputs[0].x+' '+outputY+'H'+outputs[outputs.length-1].x);
  outputs.forEach(p=>line('M'+p.x+' '+outputY+'V'+(p.top-5),true));
}
const flowObserver = new ResizeObserver(drawProductFlow);
flowObserver.observe(document.querySelector('.product-map'));
flowObserver.observe(document.querySelector('.product-map-core'));
document.fonts.ready.then(drawProductFlow);

function updateDemoCriteria() {
  const criteria = {
    it: {'biezace':['Integracje i automatyzacje','Do 500 tys. zł'], 'nice-to-have':['AI i platformy danych','Projekty rozwojowe']},
    pharma: {'biezace':['Leki i odczynniki','CPV 336*'], 'nice-to-have':['Nowe obszary terapii','Cała Polska']}
  };
  const selected = criteria[activeSector][activeProfile];
  document.getElementById('cpv-chip').textContent = selected[0];
  document.getElementById('scope-chip').textContent = selected[1];
}

const heroSlides = {
  it: { title:['Przetargi IT.','Twój nowy projekt.'], genre:'Dla zespołów technologicznych', description:'Znajdź projekty pasujące do Twojego zespołu. Połącz ogłoszenia, SWZ i wcześniejsze wyniki, żeby ocenić szanse przed przygotowaniem oferty.', footnote:'Integracje i automatyzacje → analiza SWZ → ocena szans', heading:'Twój przegląd projektów IT', insight:'Czy ten projekt pasuje do zespołu?', detail:'Zakres, referencje i warunki udziału — z materiałami dla Twojego AI.', label:'Od ogłoszenia do decyzji', backdrop:'IT', counter:'01 / 02'},
  pharma: { title:['Rynek leków.','Dane do decyzji.'], genre:'Dla firm z rynku farmaceutycznego', description:'Śledź dostawy leków i nowe obszary terapii. Zestaw formularze cenowe, oferty i rozstrzygnięcia, żeby zobaczyć pełniejszy obraz rynku.', footnote:'Leki i pakiety → formularze cenowe → porównanie ofert', heading:'Twój przegląd przetargów Pharma', insight:'Jak wypada Twoja oferta?', detail:'Produkty, pakiety i ceny w jednym miejscu — z odniesieniem do źródeł.', label:'Od formularza do porównania', backdrop:'Pharma', counter:'02 / 02'}
};
const hero = document.querySelector('.hero-showcase');
const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
let carouselPlaying = false;
let carouselVisible = false;
let carouselHovered = false;
let carouselFocused = false;
let carouselTimer;
const demoScene = document.querySelector('.demo-scene');
const autoplayButton = document.getElementById('sector-autoplay');
function setSector(sector) {
  activeSector = sector;
  const slide = heroSlides[sector];
  hero.dataset.sector = sector;
  document.querySelectorAll('button[data-sector]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.sector === sector)));
  document.getElementById('hero-title').replaceChildren(document.createTextNode(slide.title[0]), document.createElement('br'), document.createTextNode(slide.title[1]));
  document.getElementById('hero-genre').textContent = slide.genre;
  document.querySelector('.hero-description').textContent = slide.description;
  document.querySelector('.hero-footnote').textContent = slide.footnote;
  document.querySelector('.hero-backdrop').textContent = slide.backdrop;
  document.getElementById('demo-sector-heading').textContent = slide.heading;
  document.getElementById('hero-insight-label').textContent = slide.label;
  document.getElementById('hero-insight-title').textContent = slide.insight;
  document.getElementById('hero-insight-detail').textContent = slide.detail;
  document.getElementById('hero-slide-counter').textContent = slide.counter;
  if (!motionPreference.matches) {
    [document.querySelector('.hero-copy'), demoScene].forEach(element => element.animate([{opacity:0,transform:'translateX(22px)'},{opacity:1,transform:'translateX(0)'}],{duration:550,easing:'cubic-bezier(.2,.7,.3,1)'}));
  }
  activeSource = 'all';
  document.querySelectorAll('[data-source]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.source === 'all')));
  document.getElementById('demo-search').value = '';
  updateDemoCriteria();
  renderNotices();
}
function syncCarousel() {
  clearInterval(carouselTimer);
  autoplayButton.setAttribute('aria-pressed', String(carouselPlaying));
  autoplayButton.setAttribute('aria-label', carouselPlaying ? 'Wstrzymaj automatyczną zmianę branży' : 'Włącz automatyczną zmianę branży');
  autoplayButton.innerHTML = carouselPlaying
    ? '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M7 5v10M13 5v10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'
    : '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="m7 4 9 6-9 6V4Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  if (carouselPlaying && carouselVisible && !carouselHovered && !carouselFocused && !document.hidden) {
    carouselTimer = setInterval(() => setSector(activeSector === 'it' ? 'pharma' : 'it'), 7000);
  }
}
function chooseSector(sector) {
  carouselPlaying = false;
  setSector(sector);
  syncCarousel();
}
document.querySelectorAll('button[data-sector]').forEach(button => button.addEventListener('click', () => chooseSector(button.dataset.sector)));
['sector-prev','sector-next'].forEach(id => document.getElementById(id).addEventListener('click', () => chooseSector(activeSector === 'it' ? 'pharma' : 'it')));
autoplayButton.addEventListener('click', () => { carouselPlaying = !carouselPlaying; syncCarousel(); });
demoScene.addEventListener('mouseenter', () => { carouselHovered = true; syncCarousel(); });
demoScene.addEventListener('mouseleave', () => { carouselHovered = false; syncCarousel(); });
demoScene.addEventListener('focusin', () => { carouselFocused = true; syncCarousel(); });
demoScene.addEventListener('focusout', event => { if (!demoScene.contains(event.relatedTarget)) { carouselFocused = false; syncCarousel(); } });
demoScene.querySelector('.demo-window').addEventListener('pointerdown', () => { carouselPlaying = false; syncCarousel(); });
document.addEventListener('visibilitychange', syncCarousel);
motionPreference.addEventListener('change', () => { if (motionPreference.matches) carouselPlaying = false; syncCarousel(); });
new IntersectionObserver(entries => { carouselVisible = entries[0].isIntersecting; syncCarousel(); }, {threshold:0.25}).observe(hero);
syncCarousel();

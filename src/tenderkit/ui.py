"""Loopback HTTP view over the same saved notices and profiles as the CLI."""
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import sqlite3
from string import Template
from urllib.parse import parse_qs, urlencode, urlsplit

from tenderkit.profile import fold
from tenderkit.service import filtered
from tenderkit.store.state import State

PAGE_SIZE = 30
PROFILE_LABELS = {
    'oprogramowanie': 'Oprogramowanie',
    'leki': 'Leki',
    'wyroby-medyczne': 'Wyroby medyczne',
}


def page_link(profile, query, source='', page=1):
    return '/?' + escape(urlencode(dict(profile=profile, q=query, source=source, page=page)))


def tab(label, href, active):
    current = ' aria-current="page"' if active else ''
    return f'<a href="{href}"{current}>{escape(label)}</a>'


def notice_row(notice):
    title = escape(notice.title or 'Ogłoszenie bez tytułu')
    buyer = escape(notice.buyer_name or 'Nie podano zamawiającego')
    published = escape(str(notice.published or ''))
    link = ''
    for url in (notice.url, notice.platform_url):
        if not url:
            continue
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme in ('http', 'https') and parts.hostname:
            link = (f'<a class="open" href="{escape(url)}" target="_blank" rel="noopener noreferrer"'
                    f' aria-label="Otwórz ogłoszenie: {title}">↗</a>')
            break
    return f'''<li class="notice">
      <div>
        <div class="metadata"><span class="badge">{escape(notice.source)}</span><time>{published}</time></div>
        <h2>{title}</h2>
        <p>{buyer}</p>
      </div>
      {link}
    </li>'''


def render(template, state, profiles, active, query, source, page):
    profile = profiles[active]
    terms = fold(query).split()
    found_sources, visible = set(), []
    total = 0
    offset = (page - 1) * PAGE_SIZE
    for notice in filtered(state, profile):
        found_sources.add(notice.source)
        if source and notice.source != source:
            continue
        if terms:
            haystack = fold(f'{notice.title or ""} {notice.buyer_name or ""}')
            if not all(term in haystack for term in terms):
                continue
        if offset <= total < offset + PAGE_SIZE:
            visible.append(notice)
        total += 1

    profile_tabs = ''.join(tab(PROFILE_LABELS.get(name, name), page_link(name, query), name == active)
                           for name in profiles)
    source_tabs = ''.join(tab(label, page_link(active, query, name), source == name)
                          for name, label in [('', 'Wszystkie')] +
                          [(name, name) for name in sorted(found_sources | ({source} if source else set()))])
    count = f'{offset + 1}–{offset + len(visible)} z {total} ogłoszeń' if visible else f'{total} ogłoszeń'
    pagination = []
    if page > 1:
        pagination.append(tab('← Poprzednia', page_link(active, query, source, page - 1), False))
    if offset + PAGE_SIZE < total:
        pagination.append(tab('Następna →', page_link(active, query, source, page + 1), False))
    rows = ''.join(notice_row(notice) for notice in visible)
    return template.substitute(
        profile_tabs=profile_tabs, source_tabs=source_tabs, profile=escape(active),
        source=escape(source), query=escape(query), count=count,
        criteria=''.join(f'<span>CPV {escape(code)}</span>' for code in profile.cpv),
        rows=f'<ul class="notices">{rows}</ul>' if rows else
             '<p class="empty">Brak wyników na tej stronie. Zmień profil, źródło lub wpisaną frazę.</p>',
        pagination=''.join(pagination),
    )


def serve(path, profiles, initial, port=8765):
    if not 0 <= port <= 65535:
        raise ValueError('Port must be between 0 and 65535')
    state = State(path, read_only=True)
    try:
        with state.connection() as connection:
            connection.execute('select 1 from notices limit 1').fetchone()
    except sqlite3.Error as exc:
        raise ValueError(f'Nie można otworzyć bazy ogłoszeń: {state.path}') from exc

    assets = files('tenderkit').joinpath('web')
    template = Template(assets.joinpath('index.html').read_text(encoding='utf-8'))
    css = assets.joinpath('style.css').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            port = self.server.server_port
            if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
                return self.send_error(403)
            path, _, params = self.path.partition('?')
            if path == '/style.css':
                return self.respond(css, 'text/css')
            if path != '/':
                return self.send_error(404)
            try:
                values = parse_qs(params, max_num_fields=10)
                active = values.get('profile', [initial])[0]
                query = values.get('q', [''])[0].strip()
                source = values.get('source', [''])[0]
                page = int(values.get('page', ['1'])[0])
                if active not in profiles or len(query) > 200 or len(source) > 100 or page < 1:
                    raise ValueError
            except ValueError:
                return self.send_error(400, 'Invalid filter')
            try:
                body = render(template, state, profiles, active, query, source, page)
            except sqlite3.Error:
                return self.send_error(503, 'Database unavailable')
            self.respond(body.encode('utf-8'), 'text/html')

        def respond(self, body, content_type):
            self.send_response(200)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy',
                             "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'")
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', port), Handler) as server:
        print(f'tenderkit UI: http://127.0.0.1:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

"""Loopback HTTP view over the same saved notices and profiles as the CLI."""
from dataclasses import replace
from datetime import date, datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import re
import sqlite3
from string import Template
from urllib.parse import parse_qsl, urlencode, urlsplit

from tenderkit.profile import fold
from tenderkit.service import filtered
from tenderkit.store.state import State

PAGE_SIZE = 30
PROFILE_LABELS = {
    'oprogramowanie': 'Oprogramowanie',
    'leki': 'Leki',
    'wyroby-medyczne': 'Wyroby medyczne',
}
DEADLINE_LABELS = {
    '': 'Dowolny',
    'upcoming': 'Przed terminem',
    'passed': 'Po terminie',
    'today': 'Dziś, godzina nieznana',
    'missing': 'Brak terminu',
}
SORT_LABELS = {
    'newest': 'Najnowsze',
    'oldest': 'Najstarsze',
    'deadline': 'Termin rosnąco',
    'title': 'Tytuł A–Z',
    'buyer': 'Zamawiający A–Z',
}


def page_link(params):
    return '/?' + escape(urlencode({key: value for key, value in params.items() if value != ''}))


def options(labels, selected):
    return ''.join(f'<option value="{key}"{" selected" if key == selected else ""}>{label}</option>'
                   for key, label in labels.items())


def tab(label, href, active):
    current = ' aria-current="page"' if active else ''
    return f'<a href="{href}"{current}>{escape(label)}</a>'


def notice_row(notice, deadline, date_only):
    title = escape(notice.title or 'Ogłoszenie bez tytułu')
    buyer = escape(notice.buyer_name or 'Nie podano zamawiającego')
    published = escape(str(notice.published or ''))
    due = ''
    if deadline:
        date_text = deadline.strftime('%d.%m.%Y' if date_only else '%d.%m.%Y, %H:%M')
        due = f'<span>Termin: {date_text}</span>'
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
        <div class="metadata"><span class="badge">{escape(notice.source)}</span><time>{published}</time>{due}</div>
        <h2>{title}</h2>
        <p>{buyer}</p>
      </div>
      {link}
    </li>'''


def render(template, state, profiles, params):
    active, query, source = (params[key] for key in ('profile', 'q', 'source'))
    cpv, deadline_filter, order = (params[key] for key in ('cpv', 'deadline', 'sort'))
    start = date.fromisoformat(params['from']) if params['from'] else None
    end = date.fromisoformat(params['to']) if params['to'] else None
    page = int(params['page'])
    profile = profiles[active]
    terms = fold(query).split()
    now = datetime.now(timezone.utc)
    found_sources, matches = set(), []
    offset = (page - 1) * PAGE_SIZE
    for notice in filtered(state, profile):
        found_sources.add(notice.source)
        if source and notice.source != source:
            continue
        if start and (not notice.published or notice.published < start):
            continue
        if end and (not notice.published or notice.published > end):
            continue
        if cpv and not any(code.startswith(cpv) for code in notice.cpv):
            continue
        if terms:
            haystack = fold(f'{notice.title or ""} {notice.buyer_name or ""}')
            if not all(term in haystack for term in terms):
                continue
        deadline = notice.local_deadline()
        date_only = notice.deadline_precision == 'date'
        deadline_utc = deadline.astimezone(timezone.utc) if deadline else None
        status = notice.deadline_status(now)
        if deadline_filter and status != deadline_filter:
            continue
        matches.append((replace(notice, raw={}, lots=()), deadline, deadline_utc, date_only))

    if order == 'oldest':
        matches.sort(key=lambda item: (item[0].published is None, item[0].published))
    elif order == 'deadline':
        matches.sort(key=lambda item: (item[2] is None, item[2]))
    elif order in ('title', 'buyer'):
        field = 'title' if order == 'title' else 'buyer_name'
        matches.sort(key=lambda item: (not getattr(item[0], field), fold(getattr(item[0], field) or '')))
    total = len(matches)
    visible = matches[offset:offset + PAGE_SIZE]

    def link(**changes):
        return page_link({**params, 'page': 1, **changes})

    profile_tabs = ''.join(tab(PROFILE_LABELS.get(name, name), link(profile=name, source=''), name == active)
                           for name in profiles)
    source_tabs = ''.join(tab(label, link(source=name), source == name)
                          for name, label in [('', 'Wszystkie')] +
                          [(name, name) for name in sorted(found_sources | ({source} if source else set()))])
    count = f'{offset + 1}–{offset + len(visible)} z {total} ogłoszeń' if visible else f'{total} ogłoszeń'
    pagination = []
    if page > 1:
        pagination.append(tab('← Poprzednia', link(page=page - 1), False))
    if offset + PAGE_SIZE < total:
        pagination.append(tab('Następna →', link(page=page + 1), False))
    rows = ''.join(notice_row(notice, deadline, date_only) for notice, deadline, _, date_only in visible)
    return template.substitute(
        profile_tabs=profile_tabs, source_tabs=source_tabs, profile=escape(active),
        source=escape(source), query=escape(query), count=count,
        published_from=escape(params['from']), published_to=escape(params['to']), cpv=escape(cpv),
        deadline_options=options(DEADLINE_LABELS, deadline_filter), sort_options=options(SORT_LABELS, order),
        reset_link=page_link({'profile': active}),
        criteria=''.join(f'<span>CPV {escape(code)}</span>' for code in profile.cpv)
        + ''.join(f'<span>bez CPV {escape(code)}</span>' for code in profile.exclude_cpv),
        rows=f'<ul class="notices">{rows}</ul>' if rows else
             '<p class="empty">Brak wyników na tej stronie. Zmień filtry lub wybierz inny profil.</p>',
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
                values = dict(parse_qsl(params, max_num_fields=12))
                filters = dict(profile=initial, q='', source='', cpv='', deadline='', sort='newest', page='1')
                filters.update({'from': '', 'to': ''})
                filters = {key: values.get(key, default).strip() for key, default in filters.items()}
                for key in ('from', 'to'):
                    if filters[key]:
                        filters[key] = date.fromisoformat(filters[key]).isoformat()
                if (filters['profile'] not in profiles or len(filters['q']) > 200 or len(filters['source']) > 100
                        or not re.fullmatch(r'[0-9]{0,8}', filters['cpv']) or int(filters['page']) < 1
                        or filters['deadline'] not in DEADLINE_LABELS or filters['sort'] not in SORT_LABELS
                        or filters['to'] and filters['from'] > filters['to']):
                    raise ValueError
            except ValueError:
                return self.send_error(400, 'Invalid filter')
            try:
                body = render(template, state, profiles, filters)
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

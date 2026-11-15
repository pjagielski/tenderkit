"""tenderkit CLI. Diagnostics go to stderr; stdout is JSON, JSONL or MCP stdio."""
import argparse
from dataclasses import asdict
import os
from pathlib import Path
import sys

from tenderkit.limits import MAX_JOBS
from tenderkit.profiles import builtin_profiles, resolve_profile
from tenderkit.serialization import dumps
from tenderkit.service import (filtered, matched_notice_record, procedure_results, refresh, search, search_results,
                               sources, window)
from tenderkit.store.state import State
from tenderkit.platforms.backends import FetchError


def default_database():
    if sys.platform == 'win32':
        configured = Path(os.environ.get('LOCALAPPDATA', ''))
        root = configured if configured.is_absolute() else Path.home() / 'AppData' / 'Local'
    elif sys.platform == 'darwin':
        root = Path.home() / 'Library' / 'Application Support'
    else:
        configured = Path(os.environ.get('XDG_DATA_HOME', ''))
        root = configured if configured.is_absolute() else Path.home() / '.local' / 'share'
    return root / 'tenderkit' / 'state.sqlite3'


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(
        prog='tenderkit',
        description='Fetch, browse and export Polish public procurement notices.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Examples:
  tenderkit fetch --profile oprogramowanie
  tenderkit filter --profile oprogramowanie --limit 10
  tenderkit ui --profile oprogramowanie
  tenderkit export --profile oprogramowanie > notices.jsonl
  tenderkit watch --profile oprogramowanie
  tenderkit fetch --profile oprogramowanie --results
  tenderkit results --buyer Kraków --cpv 72*

Run tenderkit COMMAND --help for command options.''',
    )
    parser.add_argument('--db', type=Path, metavar='PATH',
                        help='SQLite database path. Overrides TENDERKIT_DB. '
                             'The default is state.sqlite3 in the tenderkit user data directory.')
    parser.add_argument('--documents-dir', type=Path, default=os.environ.get('TENDERKIT_DOCUMENT_DIR'),
                        metavar='PATH', help='Document store (TENDERKIT_DOCUMENT_DIR).')
    commands = parser.add_subparsers(dest='command', title='commands', metavar='COMMAND')

    def add_command(name, description):
        return commands.add_parser(name, help=description, description=description)

    for name, description in (
        ('fetch', 'Fetch notices from sources into the SQLite database.'),
        ('filter', 'List saved notices that match a profile as JSON.'),
        ('export', 'Write matching saved notices as JSON Lines.'),
        ('watch', 'Refresh a profile and print new or revised matches as JSON Lines.'),
    ):
        command = add_command(name, description)
        command.add_argument('--profile', required=True, help='Built-in profile name or TOML path.')
        command.add_argument('--from', dest='start', metavar='YYYY-MM-DD', help='First publication date, inclusive.')
        command.add_argument('--to', dest='end', metavar='YYYY-MM-DD', help='Last publication date, inclusive.')
        command.add_argument('--source', action='append', dest='source_names', metavar='NAME',
                             help='Limit to a source such as BZP or TED. Repeat to include more sources.')
        if name in ('fetch', 'watch'):
            command.add_argument('--no-raw', action='store_true',
                             help='Save notices without the full source record. Keeps only the few source fields '
                                  'tenderkit reads (watch versions, TED lot deadlines); export then omits the rest.')
        if name == 'fetch':
            command.epilog = 'With no date options, fetch covers the last 30 calendar days including today.'
            command.add_argument('--results', action='store_true',
                                 help='Also fetch formal result notices (BZP TenderResultNotice, TED can-standard).')
        if name == 'filter':
            command.add_argument('--limit', type=int, default=100, help='Notices per page (default: 100, max: 500).')
            command.add_argument('--offset', type=int, default=0, help='Skip this many matching notices (default: 0).')
        if name == 'export':
            command.add_argument('--format', choices=('jsonl',), default='jsonl', help='Output format (default: jsonl).')
        if name == 'watch':
            command.epilog = ('Run once per invocation. The default window covers 30 days and includes missed '
                              'days since the last successful run. Keep the same database for each run.')
            command.add_argument('--webhook', metavar='URL', help='Also POST events to this HTTP(S) webhook.')
    add_command('profiles', 'List built-in profiles and their criteria.')
    add_command('status', 'Show database coverage, refresh history and download jobs.')
    results = add_command('results', 'Show formal results: contractor, price and bid count per lot.')
    results.add_argument('--source', help='Notice source such as BZP or TED; required with --number, optional for search.')
    results.add_argument('--number', help='Notice number; returns this procedure and its latest award per lot.')
    results.add_argument('--buyer', help='Buyer name fragment for searching stored results.')
    results.add_argument('--cpv', action='append', dest='cpv', metavar='CODE',
                         help='CPV code or prefix ending in *; repeat to search several.')
    results.add_argument('--from', dest='start', metavar='YYYY-MM-DD', help='First publication date, inclusive.')
    results.add_argument('--to', dest='end', metavar='YYYY-MM-DD', help='Last publication date, inclusive.')
    results.add_argument('--limit', type=int, default=100, help='Awards per page (default: 100, max: 500).')
    results.add_argument('--offset', type=int, default=0, help='Skip this many awards (default: 0).')
    ui = add_command('ui', 'Browse saved notices at http://127.0.0.1:8765/.')
    ui.add_argument('--port', type=int, default=8765, help='Local HTTP port (default: 8765).')
    ui.add_argument('--profile', action='append', required=True, metavar='NAME_OR_PATH',
                    help='Profile name or TOML path. Repeat to add tabs. The first profile opens by default.')
    download = add_command('download', 'Download documents for a notice or profile, or run queued jobs.')
    download.add_argument('--source', help='Notice source, such as BZP or TED.')
    selection = download.add_mutually_exclusive_group()
    selection.add_argument('--number', help='Notice number from filter.')
    selection.add_argument('--profile', help='Download documents for saved notices matching a profile.')
    selection.add_argument('--run-queued', action='store_true', help='Process jobs already in the queue.')
    download.add_argument('--from', dest='start', metavar='YYYY-MM-DD', help='First publication date for profile downloads.')
    download.add_argument('--to', dest='end', metavar='YYYY-MM-DD', help='Last publication date for profile downloads.')
    download.add_argument('--limit', type=int, help=f'Notices per profile download (default and max: {MAX_JOBS}).')
    download.add_argument('--offset', type=int, help='Skip this many profile matches (default: 0).')
    download.add_argument('--key', action='append', dest='keys',
                          help='Document key from list-documents. Repeat to select multiple documents.')
    download.add_argument('--enqueue-only', action='store_true', help='Queue the download for later.')
    download.add_argument('--max-jobs', type=int, default=1, help='Limit jobs for --run-queued (default: 1).')
    listing = add_command('list-documents', 'List the documents attached to a notice.')
    listing.add_argument('--source', required=True, help='Notice source, such as BZP or TED.')
    listing.add_argument('--number', required=True, help='Notice number from filter.')
    listing.add_argument('--limit', type=int, default=100, help='Documents per page (default: 100).')
    listing.add_argument('--offset', type=int, default=0, help='Skip this many documents (default: 0).')
    listing.add_argument('--cached', action='store_true', help='Read the saved list without refreshing it.')
    reader = add_command('read-document', 'Extract text from a downloaded document.')
    reader.add_argument('document_id', help='SHA-256 of the downloaded document.')
    reader.add_argument('--page', type=int, default=1, help='First page to read (default: 1).')
    reader.add_argument('--pages', type=int, default=1, help='Number of pages to read (default: 1).')
    reader.add_argument('--ocr', action='store_true', help='Run local Polish OCR on PDF pages without embedded text.')
    mcp = add_command('mcp', 'Serve tenderkit tools over MCP stdio.')
    mcp.add_argument('--profile-file', type=Path, action='append', default=[],
                     help='trusted profile to expose alongside the built-ins')
    mcp.add_argument('--no-raw', action='store_true',
                     help='Save notices without the full source record. Keeps only the few source fields '
                          'tenderkit reads (watch versions, TED lot deadlines); export then omits the rest.')
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == 'profiles':
            print(dumps({name: asdict(p) for name, p in builtin_profiles().items()}))
            return 0
        if args.db is None:
            configured = os.environ.get('TENDERKIT_DB')
            args.db = Path(configured) if configured else default_database()
        if args.command == 'ui':
            from tenderkit.ui import serve
            profiles = {}
            for name_or_path in args.profile:
                profile = resolve_profile(name_or_path)
                if profile.name in profiles:
                    raise ValueError(f'Duplicate UI profile name: {profile.name}')
                profiles[profile.name] = profile
            serve(args.db, profiles, next(iter(profiles)), args.port)
            return 0
        if args.command == 'mcp':
            from tenderkit.mcp import create_server
            profiles = builtin_profiles()
            for path in args.profile_file:
                profile = resolve_profile(str(path))
                profiles[profile.name] = profile
            create_server(args.db, profiles=profiles, document_root=args.documents_dir,
                          keep_raw=not args.no_raw).run(transport='stdio')
            return 0
        # Validate inputs before creating or opening the state file.
        if args.command == 'results':
            if args.number and not args.source:
                raise ValueError('results --number requires --source')
            if args.number and (args.buyer or args.cpv):
                raise ValueError('--number cannot be combined with --buyer or --cpv')
            if not args.number and not (args.buyer or args.cpv):
                raise ValueError('results requires --number with --source, or --buyer and/or --cpv')
            if args.start or args.end:
                window(args.start, args.end)
        if args.command in ('fetch', 'filter', 'export', 'watch'):
            profile = resolve_profile(args.profile)
            if args.start or args.end or args.command in ('fetch', 'watch'):
                start, end = window(args.start, args.end)
            else:
                start = end = None
        if args.command == 'download':
            if args.profile:
                if args.keys:
                    raise ValueError('--key requires a single notice')
                profile = resolve_profile(args.profile)
                start, end = window(args.start, args.end) if args.start or args.end else (None, None)
            elif any(value is not None for value in (args.start, args.end, args.limit, args.offset)):
                raise ValueError('--from, --to, --limit and --offset require --profile')
        state = State(args.db, keep_raw=not getattr(args, 'no_raw', False))
        if args.command == 'results':
            if args.number:
                print(dumps(procedure_results(state, args.source, args.number)))
            else:
                print(dumps(search_results(state, buyer=args.buyer, cpv=args.cpv or (),
                                           start=args.start, end=args.end, limit=args.limit, offset=args.offset,
                                           source_names=(args.source,) if args.source else ())))
            return 0
        if args.command in ('download', 'list-documents', 'read-document'):
            from tenderkit.documents import Documents
            service = Documents(state, args.documents_dir or state.path.parent / 'documents')
            if args.command == 'list-documents':
                print(dumps(service.list(args.source, args.number, limit=args.limit, offset=args.offset, refresh=not args.cached)))
            elif args.command == 'read-document':
                print(dumps(service.read(args.document_id, page=args.page, pages=args.pages, ocr=args.ocr)))
            elif args.profile:
                result = service.fetch_profile(
                    profile, start=start, end=end, sources=(args.source,) if args.source else (),
                    limit=MAX_JOBS if args.limit is None else args.limit,
                    offset=0 if args.offset is None else args.offset, enqueue_only=args.enqueue_only)
                print(dumps(result))
                conflict = any(row['reason'] == 'ACTIVE_SELECTION_CONFLICT' for row in result['skipped'])
                return int(conflict or (not args.enqueue_only and any(job['status'] != 'DONE' for job in result['jobs'])))
            elif args.run_queued:
                if args.source or args.number or args.keys or args.enqueue_only:
                    raise ValueError('--run-queued cannot be combined with a notice selection')
                result = service.run(args.max_jobs)
                print(dumps(result))
                return int(not result['locked'] or any(j['status'] != 'DONE' for j in result['jobs']))
            else:
                if not args.source or not args.number:
                    raise ValueError('download requires --source and --number, --profile, or --run-queued')
                result = service.fetch(args.source, args.number, args.keys, enqueue_only=args.enqueue_only)
                print(dumps(result))
                return 0 if args.enqueue_only or result['status'] == 'DONE' else 1
            return 0
        if args.command in ('fetch', 'watch'):
            adapters = sources(include_bk=True) if 'BK' in (args.source_names or ()) else sources()
            if args.source_names:
                if set(args.source_names) - set(adapters):
                    raise ValueError('Unknown source')
                adapters = {name: adapters[name] for name in dict.fromkeys(args.source_names)}
            if args.command == 'watch':
                from tenderkit.watch import run_watch
                summaries = run_watch(state, profile, start, end, adapters, sys.stdout,
                                      catch_up=args.start is None and args.end is None, webhook=args.webhook)
                for summary in summaries:
                    if summary['status'] == 'FAILED':
                        print(f"tenderkit: {summary['source']} refresh failed ({summary['error']})", file=sys.stderr)
                return int(any(summary['status'] == 'FAILED' for summary in summaries))
            summaries = refresh(state, profile, start, end, adapters, include_results=args.results)
            print(dumps(dict(window=[str(start), str(end)], sources=summaries)))
            return int(any(r['status'] == 'FAILED' for r in summaries))
        if args.command == 'filter':
            print(dumps(search(state, profile, start=start, end=end, limit=args.limit,
                               offset=args.offset, source_names=args.source_names or ())))
        elif args.command == 'export':
            for notice in filtered(state, profile, start=start, end=end, source_names=args.source_names or ()):
                print(dumps(matched_notice_record(notice, profile, raw=True)))
        elif args.command == 'status':
            print(dumps(state.coverage()))
        return 0
    except (ValueError, OSError, RuntimeError, FetchError) as exc:
        print(f'tenderkit: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

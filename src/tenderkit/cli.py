"""tenderkit CLI. Diagnostics go to stderr; stdout is JSON, JSONL or MCP stdio."""
import argparse
from dataclasses import asdict
import os
from pathlib import Path
import sys

from tenderkit.profiles import builtin_profiles, resolve_profile
from tenderkit.serialization import dumps, notice_record
from tenderkit.service import filtered, refresh, search, sources, window
from tenderkit.store.state import State
from tenderkit.platforms.backends import FetchError


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(
        prog='tenderkit',
        description='Fetch, browse and export Polish public procurement notices.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Examples:
  tenderkit --db .notices.sqlite3 fetch --profile oprogramowanie
  tenderkit --db .notices.sqlite3 filter --profile oprogramowanie --limit 10
  tenderkit --db .notices.sqlite3 ui --profile oprogramowanie
  tenderkit --db .notices.sqlite3 export --profile oprogramowanie > notices.jsonl

Run tenderkit COMMAND --help for command options.''',
    )
    parser.add_argument('--db', type=Path, default=os.environ.get('TENDERKIT_DB'), metavar='PATH',
                        help='SQLite database (TENDERKIT_DB). Defaults to .notices.sqlite3 for ui '
                             'and .tenderkit/state.sqlite3 for other commands.')
    parser.add_argument('--documents-dir', type=Path, default=os.environ.get('TENDERKIT_DOCUMENT_DIR'),
                        metavar='PATH', help='Document store (TENDERKIT_DOCUMENT_DIR).')
    commands = parser.add_subparsers(dest='command', title='commands', metavar='COMMAND')

    def add_command(name, description):
        return commands.add_parser(name, help=description, description=description)

    for name, description in (
        ('fetch', 'Fetch notices from sources into the SQLite database.'),
        ('filter', 'List saved notices that match a profile as JSON.'),
        ('export', 'Write matching saved notices as JSON Lines.'),
    ):
        command = add_command(name, description)
        command.add_argument('--profile', required=True, help='Built-in profile name or TOML path.')
        command.add_argument('--from', dest='start', metavar='YYYY-MM-DD', help='First publication date, inclusive.')
        command.add_argument('--to', dest='end', metavar='YYYY-MM-DD', help='Last publication date, inclusive.')
        command.add_argument('--source', action='append', dest='source_names', metavar='NAME',
                             help='Limit to a source such as BZP or TED. Repeat to include more sources.')
        if name == 'fetch':
            command.epilog = 'With no date options, fetch covers the last 30 calendar days including today.'
        if name == 'filter':
            command.add_argument('--limit', type=int, default=100, help='Notices per page (default: 100, max: 500).')
            command.add_argument('--offset', type=int, default=0, help='Skip this many matching notices (default: 0).')
        if name == 'export':
            command.add_argument('--format', choices=('jsonl',), default='jsonl', help='Output format (default: jsonl).')
    add_command('profiles', 'List built-in profiles and their criteria.')
    add_command('status', 'Show database coverage, refresh history and download jobs.')
    ui = add_command('ui', 'Browse saved notices at http://127.0.0.1:8765/.')
    ui.add_argument('--port', type=int, default=8765, help='Local HTTP port (default: 8765).')
    ui.add_argument('--profile', action='append', required=True, metavar='NAME_OR_PATH',
                    help='Profile name or TOML path. Repeat to add tabs. The first profile opens by default.')
    download = add_command('download', 'Download documents for a notice or run queued jobs.')
    download.add_argument('--source', help='Notice source, such as BZP or TED.')
    download.add_argument('--number', help='Notice number from filter.')
    download.add_argument('--key', action='append', dest='keys',
                          help='Document key from list-documents. Repeat to select multiple documents.')
    download.add_argument('--enqueue-only', action='store_true', help='Queue the download for later.')
    download.add_argument('--run-queued', action='store_true', help='Process jobs already in the queue.')
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
    mcp = add_command('mcp', 'Serve tenderkit tools over MCP stdio.')
    mcp.add_argument('--profile-file', type=Path, action='append', default=[],
                     help='trusted profile to expose alongside the built-ins')
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.db is None:
        args.db = Path('.notices.sqlite3' if args.command == 'ui' else '.tenderkit/state.sqlite3')
    try:
        if args.command == 'profiles':
            print(dumps({name: asdict(p) for name, p in builtin_profiles().items()}))
            return 0
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
            create_server(args.db, profiles=profiles, document_root=args.documents_dir).run(transport='stdio')
            return 0
        # Validate inputs before creating or opening the state file.
        if args.command in ('fetch', 'filter', 'export'):
            profile = resolve_profile(args.profile)
            if args.start or args.end or args.command == 'fetch':
                start, end = window(args.start, args.end)
            else:
                start = end = None
        state = State(args.db)
        if args.command in ('download', 'list-documents', 'read-document'):
            from tenderkit.documents import Documents
            service = Documents(state, args.documents_dir or state.path.parent / 'documents')
            if args.command == 'list-documents':
                print(dumps(service.list(args.source, args.number, limit=args.limit, offset=args.offset, refresh=not args.cached)))
            elif args.command == 'read-document':
                print(dumps(service.read(args.document_id, page=args.page, pages=args.pages)))
            elif args.run_queued:
                if args.source or args.number or args.keys or args.enqueue_only:
                    raise ValueError('--run-queued cannot be combined with a notice selection')
                result = service.run(args.max_jobs)
                print(dumps(result))
                return int(not result['locked'] or any(j['status'] != 'DONE' for j in result['jobs']))
            else:
                if not args.source or not args.number:
                    raise ValueError('download requires --source and --number, or --run-queued')
                result = service.fetch(args.source, args.number, args.keys, enqueue_only=args.enqueue_only)
                print(dumps(result))
                return 0 if args.enqueue_only or result['status'] == 'DONE' else 1
            return 0
        if args.command == 'fetch':
            adapters = sources()
            if args.source_names:
                if set(args.source_names) - set(adapters):
                    raise ValueError('Unknown source')
                adapters = {name: adapters[name] for name in dict.fromkeys(args.source_names)}
            summaries = refresh(state, profile, start, end, adapters)
            print(dumps(dict(window=[str(start), str(end)], sources=summaries)))
            return int(any(r['status'] == 'FAILED' for r in summaries))
        if args.command == 'filter':
            print(dumps(search(state, profile, start=start, end=end, limit=args.limit,
                               offset=args.offset, source_names=args.source_names or ())))
        elif args.command == 'export':
            for notice in filtered(state, profile, start=start, end=end, source_names=args.source_names or ()):
                print(dumps(notice_record(notice)))
        elif args.command == 'status':
            print(dumps(state.coverage()))
        return 0
    except (ValueError, OSError, RuntimeError, FetchError) as exc:
        print(f'tenderkit: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

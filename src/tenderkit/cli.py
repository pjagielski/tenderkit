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
    parser = argparse.ArgumentParser(prog='tenderkit')
    parser.add_argument('--db', type=Path, default=os.environ.get('TENDERKIT_DB'))
    parser.add_argument('--documents-dir', type=Path, default=os.environ.get('TENDERKIT_DOCUMENT_DIR'))
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('fetch', 'filter', 'export'):
        command = commands.add_parser(name)
        command.add_argument('--profile', required=True, help='built-in name or TOML path')
        command.add_argument('--from', dest='start')
        command.add_argument('--to', dest='end')
        command.add_argument('--source', action='append', dest='source_names')
        if name == 'filter':
            command.add_argument('--limit', type=int, default=100)
            command.add_argument('--offset', type=int, default=0)
        if name == 'export':
            command.add_argument('--format', choices=('jsonl',), default='jsonl')
    commands.add_parser('profiles')
    commands.add_parser('status')
    ui = commands.add_parser('ui', help='serve a local browser view of saved notices')
    ui.add_argument('--port', type=int, default=8765)
    ui.add_argument('--profile', default='oprogramowanie', help='initial built-in profile or TOML path')
    download = commands.add_parser('download')
    download.add_argument('--source')
    download.add_argument('--number')
    download.add_argument('--key', action='append', dest='keys')
    download.add_argument('--enqueue-only', action='store_true')
    download.add_argument('--run-queued', action='store_true')
    download.add_argument('--max-jobs', type=int, default=1)
    listing = commands.add_parser('list-documents')
    listing.add_argument('--source', required=True)
    listing.add_argument('--number', required=True)
    listing.add_argument('--limit', type=int, default=100)
    listing.add_argument('--offset', type=int, default=0)
    listing.add_argument('--cached', action='store_true')
    reader = commands.add_parser('read-document')
    reader.add_argument('document_id')
    reader.add_argument('--page', type=int, default=1)
    reader.add_argument('--pages', type=int, default=1)
    mcp = commands.add_parser('mcp')
    mcp.add_argument('--profile-file', type=Path, action='append', default=[],
                     help='trusted profile to expose alongside the built-ins')
    args = parser.parse_args(argv)
    if args.db is None:
        args.db = Path('.notices.sqlite3' if args.command == 'ui' else '.tenderkit/state.sqlite3')
    try:
        if args.command == 'profiles':
            print(dumps({name: asdict(p) for name, p in builtin_profiles().items()}))
            return 0
        if args.command == 'ui':
            from tenderkit.ui import serve
            profiles = builtin_profiles()
            profile = resolve_profile(args.profile)
            profiles[profile.name] = profile
            serve(args.db, profiles, profile.name, args.port)
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

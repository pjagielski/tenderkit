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


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(prog='tenderkit')
    parser.add_argument('--db', type=Path, default=Path(os.environ.get('TENDERKIT_DB', '.tenderkit/state.sqlite3')))
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
    mcp = commands.add_parser('mcp')
    mcp.add_argument('--profile-file', type=Path, action='append', default=[],
                     help='trusted profile to expose alongside the built-ins')
    args = parser.parse_args(argv)
    try:
        if args.command == 'profiles':
            print(dumps({name: asdict(p) for name, p in builtin_profiles().items()}))
            return 0
        if args.command == 'mcp':
            from tenderkit.mcp import create_server
            profiles = builtin_profiles()
            for path in args.profile_file:
                profile = resolve_profile(str(path))
                profiles[profile.name] = profile
            create_server(args.db, profiles=profiles).run(transport='stdio')
            return 0
        # Validate inputs before creating or opening the state file.
        if args.command != 'status':
            profile = resolve_profile(args.profile)
            if args.start or args.end or args.command == 'fetch':
                start, end = window(args.start, args.end)
            else:
                start = end = None
        state = State(args.db)
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
    except (ValueError, OSError, RuntimeError) as exc:
        print(f'tenderkit: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

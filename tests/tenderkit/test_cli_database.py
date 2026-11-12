from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tenderkit.cli import default_database, main


class CliDatabaseTests(unittest.TestCase):
    def test_default_path_for_each_system(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cases = (
                ('win32', {'LOCALAPPDATA': str(home / 'local')}, home / 'local'),
                ('win32', {'LOCALAPPDATA': ''}, home / 'AppData' / 'Local'),
                ('darwin', {}, home / 'Library' / 'Application Support'),
                ('linux', {'XDG_DATA_HOME': str(home / 'data')}, home / 'data'),
                ('linux', {'XDG_DATA_HOME': 'relative'}, home / '.local' / 'share'),
            )
            for platform, env, root in cases:
                with self.subTest(platform=platform, env=env), patch('sys.platform', platform), \
                     patch.dict('os.environ', env, clear=True), patch.object(Path, 'home', return_value=home):
                    self.assertEqual(default_database(), root / 'tenderkit' / 'state.sqlite3')

    def test_default_database_is_shared_by_cli_ui_and_mcp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data' / 'state.sqlite3'
            with patch.dict('os.environ', {'TENDERKIT_DB': ''}), \
                 patch('tenderkit.cli.default_database', return_value=path), \
                 patch('tenderkit.ui.serve') as serve, patch('tenderkit.mcp.create_server') as mcp, \
                 redirect_stdout(StringIO()):
                self.assertEqual(main(['status']), 0)
                self.assertEqual(main(['ui', '--profile', 'oprogramowanie']), 0)
                self.assertEqual(main(['mcp']), 0)
                self.assertTrue(path.is_file())
                self.assertEqual(serve.call_args.args[0], path)
                self.assertEqual(mcp.call_args.args[0], path)

    def test_explicit_path_overrides_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / 'env.sqlite3'
            explicit_path = Path(directory) / 'explicit.sqlite3'
            with patch.dict('os.environ', {'TENDERKIT_DB': str(env_path)}), redirect_stdout(StringIO()):
                self.assertEqual(main(['--db', str(explicit_path), 'status']), 0)
                self.assertFalse(env_path.exists())
                self.assertTrue(explicit_path.is_file())
                self.assertEqual(main(['status']), 0)
                self.assertTrue(env_path.is_file())

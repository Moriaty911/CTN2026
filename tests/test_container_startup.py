"""Container mode agreement and explicit macro write authorization."""
import asyncio
from contextlib import closing, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from ctn.config import Settings
from ctn.macro.worker import execution_mode, run_pi, serve as serve_macro
from ctn.main import serve as serve_fleet
from ctn.models import Side
from ctn.storage import StateStore


class ContainerModeTests(unittest.TestCase):
    def test_worker_mode_shares_fleet_data_source_and_defaults_to_no_writes(self):
        with patch.dict('os.environ', {}, clear=True):
            for mode in ('mock', 'dry-run', 'live'):
                with self.subTest(mode=mode):
                    settings=Settings(mode=mode, dry_run=mode!='live')
                    self.assertEqual(execution_mode(settings), 'dry-run' if mode=='live' else mode)

    def test_macro_live_needs_both_project_live_mode_and_separate_opt_in(self):
        with patch.dict('os.environ', {}, clear=True):
            settings=Settings(mode='live', dry_run=False)
            self.assertEqual(execution_mode(settings, live=True), 'live')
            for mode in ('mock', 'dry-run'):
                with self.subTest(mode=mode):
                    with self.assertRaisesRegex(ValueError, 'CTN_MODE=live'):
                        execution_mode(Settings(mode=mode), live=True)
            with patch.dict('os.environ', {'CTN_MACRO_LIVE':'true'}):
                self.assertEqual(execution_mode(settings), 'live')
                with self.assertRaisesRegex(ValueError, 'CTN_MODE=live'):
                    execution_mode(Settings(mode='dry-run'))
                self.assertEqual(execution_mode(settings, force_mock=True), 'mock')

    def test_conflicting_mode_configuration_is_rejected(self):
        with patch.dict('os.environ', {'CTN_MODE':'live', 'CTN_DRY_RUN':'true'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                execution_mode(Settings(mode='live', dry_run=True))

    def test_default_mock_fleet_and_worker_share_database_without_pi(self):
        with tempfile.TemporaryDirectory() as td, patch.dict('os.environ', {}, clear=True):
            root=Path(td)
            settings=replace(Settings(), mode='mock', side=Side.YES,
                             state_dir=root/'state', log_dir=root/'logs')
            path=settings.database_path()
            args=SimpleNamespace(state_file=str(path), mock=False, live=False,
                                 once=True, harness=str(root/'state'/'harness.md'))
            with patch('ctn.config.Settings.load', return_value=settings), \
                 patch('ctn.macro.worker.run_pi', new_callable=AsyncMock) as pi, \
                 redirect_stdout(io.StringIO()):
                asyncio.run(serve_fleet(settings, once=True))
                asyncio.run(serve_macro(args))
                pi.assert_not_awaited()
            with closing(StateStore(path, migrate=False)) as store:
                store.bind_data_source('mock')
                self.assertIsNotNone(store.policy('macro_health'))
            self.assertTrue(Path(args.harness).is_file())


class PiConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_deepseek_high_reaches_pi_without_fleet_credentials(self):
        process=SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b'{"act":false}', b'')))
        selected={'CTN_PI_MODEL':'deepseek/deepseek-flash','CTN_PI_THINKING':'high',
                  'DEEPSEEK_API_KEY':'synthetic-pi-only','CTN_PLATFORM_API_KEY':'synthetic-platform',
                  'CTN_LLM_API_KEY':'synthetic-python'}
        with patch.dict('os.environ', selected, clear=True), \
             patch('ctn.macro.worker.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=process) as start:
            self.assertEqual(await run_pi({'sample':'offline'}), {'act':False})
        args=start.await_args.args
        self.assertEqual(args[args.index('--model')+1], 'deepseek/deepseek-flash')
        self.assertEqual(args[args.index('--thinking')+1], 'high')
        self.assertEqual(start.await_args.kwargs['env'], {'DEEPSEEK_API_KEY':'synthetic-pi-only'})

    async def test_invalid_thinking_is_rejected_before_starting_pi(self):
        with patch.dict('os.environ', {'CTN_PI_THINKING':'unsupported'}, clear=True), \
             patch('ctn.macro.worker.asyncio.create_subprocess_exec', new_callable=AsyncMock) as start:
            with self.assertRaisesRegex(ValueError, 'CTN_PI_THINKING'):
                await run_pi({})
            start.assert_not_awaited()


if __name__=='__main__':
    unittest.main()

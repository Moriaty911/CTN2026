"""Offline checks of the production service's JEV component wiring."""

import asyncio
from contextlib import closing
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from ctn.client.mock import MockLegitClient
from ctn.config import JevConfig, LLMConfig, PlatformConfig, RateConfig, Settings
from ctn.jev import JevClient
from ctn.llm import DeepSeekClient
from ctn.main import serve, worker_error_fatal
from ctn.models import Side
from ctn.storage import StateStore
from tests.test_jev_actions import set_score
from tests.test_jev_evaluator import FakeTransport, choose, content


class WorkerErrorPolicyTests(unittest.TestCase):
    """One busy database used to end the whole fleet: sqlite3.OperationalError is not an OSError,
    so it fell through to `raise` in the worker loop and killed every worker at once."""

    def test_lock_timeouts_are_retried_next_tick(self):
        import sqlite3

        from ctn.storage import is_lock_error

        exc = sqlite3.OperationalError("database is locked")
        self.assertTrue(is_lock_error(exc))
        self.assertFalse(worker_error_fatal(exc))

    def test_network_errors_are_retried_next_tick(self):
        self.assertFalse(worker_error_fatal(ConnectionError("reset")))
        self.assertFalse(worker_error_fatal(TimeoutError()))

    def test_programming_errors_still_stop_the_fleet(self):
        self.assertTrue(worker_error_fatal(KeyError("missing")))
        self.assertTrue(worker_error_fatal(ValueError("bad config")))


class ServiceJevWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_evaluates_opposing_reply_value_with_registered_background(self):
        def respond(body, data):
            choose(data, "policy_stance", "no")
            if "reply_value" in data["answers"]:
                set_score(data, "reply_value", 4)

        async def idle():
            pass

        def unexpected_llm_call(request):
            raise AssertionError("This wiring test must not generate or publish content")

        results = []
        reference = {
            "reference_id": "synthetic-background",
            "source_id": "synthetic-local-source",
            "text": "Synthetic background supplied only for this offline test.",
        }

        def make_loop(actuator, *, brain, **kwargs):
            async def evaluate_service_workflow():
                record = content()
                record["task_context"] = {"assigned_side": "yes"}
                record["trusted_background"] = {"items": [reference]}
                package = await asyncio.to_thread(brain.workflow.evaluate, record)
                results.append(package.to_dict(include_materials=True))

            return SimpleNamespace(
                reporter=SimpleNamespace(sweep_and_report=idle),
                run_frontline_step=idle,
                run_comment_step=idle,
                process_pending=evaluate_service_workflow,
            )

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "state" / "ctn.sqlite3"
            settings = Settings(
                side=Side.YES,
                platform=PlatformConfig(api_key="synthetic-platform-key"),
                jev=JevConfig(enabled=True, api_key="synthetic-service-jev-key", max_retries=0),
                llm=LLMConfig(api_key="synthetic-service-llm-key", max_retries=0),
                rate=RateConfig(read_rps=1000, write_rps=1000),
                state_dir=root / "state",
                log_dir=root / "logs",
            )
            with closing(StateStore(database)) as store:
                store.set_policy("trusted_background", [reference])
            transport = FakeTransport(respond)
            jev = JevClient(settings.jev, transport=transport)
            llm = DeepSeekClient(settings.llm, transport=httpx.MockTransport(unexpected_llm_call))
            self.addCleanup(llm.close)
            platform = MockLegitClient(failure_rate=0, latency_ms=(0, 0))
            platform.configure_pacing = Mock()
            previous_umask = os.umask(0o077)
            try:
                with (
                    patch("ctn.main.OfficialLegitClient", return_value=platform),
                    patch("ctn.main.JevClient", return_value=jev),
                    patch("ctn.main.DeepSeekClient", return_value=llm),
                    patch("ctn.main.BotLoop", side_effect=make_loop),
                    patch.object(asyncio.get_running_loop(), "add_signal_handler"),
                ):
                    await serve(settings, mode="dry-run", state_file=database, once=True)
            finally:
                os.umask(previous_umask)

            self.assertEqual(len(results), 1)
            result = results[0]
            deep = result["flow"]["deep"]["assessment"]["output"]
            self.assertEqual(deep["question_set_version"], "ctn-post.v3")
            self.assertEqual(deep["answers"]["reply_value"]["value"], 4)
            self.assertEqual(result["action_decision"]["status"], "decided")
            self.assertEqual(result["action_decision"]["action"], "reply")
            self.assertEqual(result["action_decision"]["signal_id"], "reply_value")
            self.assertEqual(result["action_decision"]["required_account_level"], "high")
            self.assertEqual(result["materials"]["verified_background"]["items"], [reference])
            self.assertEqual(len(transport.calls), 2)
            self.assertIn("reply_value", transport.calls[1]["questions"])
            self.assertEqual(platform.write_count, 0)

"""Tests for multi-account persona models, registry, and routing engine."""

import unittest
from unittest.mock import MagicMock

from ctn.jev.materials import MaterialBrief, TextSpan
from ctn.llm.models import DraftOptions, ReplyDraftResult
from ctn.models import ActionKind, Post, Side
from ctn.persona import (
    AccountState,
    Persona,
    PersonaRegistry,
    PersonaRouter,
    RoutingDecision,
    get_default_personas,
    get_default_registry,
)
from ctn.pipeline.brain import BrainVerdict, JevLlmBrain


class TestPersonaModels(unittest.TestCase):
    def test_persona_creation_and_dict(self):
        p = Persona(
            account_id="bot_debater_yes",
            archetype="sharp_debater",
            name="Elena Vance",
            bio="Civic debater.",
            tone_instruction="Sharp and witty.",
            cooldown_s=45.0,
            assigned_side="yes",
        )
        data = p.to_dict()
        self.assertEqual(data["account_id"], "bot_debater_yes")
        self.assertEqual(data["archetype"], "sharp_debater")
        self.assertEqual(data["cooldown_s"], 45.0)

    def test_account_state_cooldown_tracking(self):
        state = AccountState(account_id="bot_1")
        # Initially not in cooldown
        self.assertFalse(state.is_in_cooldown(now=100.0, cooldown_s=60.0))
        self.assertEqual(state.remaining_cooldown(now=100.0, cooldown_s=60.0), 0.0)

        # Mark used at t=100
        state.mark_used(now=100.0)
        self.assertEqual(state.post_count, 1)
        self.assertEqual(state.last_posted_at, 100.0)

        # Check at t=120 (remaining 40s)
        self.assertTrue(state.is_in_cooldown(now=120.0, cooldown_s=60.0))
        self.assertAlmostEqual(state.remaining_cooldown(now=120.0, cooldown_s=60.0), 40.0)

        # Check at t=161 (cooldown expired)
        self.assertFalse(state.is_in_cooldown(now=161.0, cooldown_s=60.0))
        self.assertEqual(state.remaining_cooldown(now=161.0, cooldown_s=60.0), 0.0)

    def test_routing_decision_dict(self):
        p = Persona(
            account_id="bot_test",
            archetype="medical_expert",
            name="Dr. Test",
            bio="Bio",
            tone_instruction="Tone",
        )
        decision = RoutingDecision(
            account_id="bot_test",
            persona=p,
            target_archetype="medical_expert",
            score=0.95,
            reason="matched_medical_expert",
            fallback_used=False,
            in_cooldown=False,
        )
        d = decision.to_dict()
        self.assertEqual(d["account_id"], "bot_test")
        self.assertEqual(d["score"], 0.95)
        self.assertFalse(d["fallback_used"])


class TestPersonaRegistry(unittest.TestCase):
    def test_default_registry_contents(self):
        registry = get_default_registry()
        yes_personas = registry.list_by_side("yes")
        no_personas = registry.list_by_side("no")

        self.assertEqual(len(yes_personas), 4)
        self.assertEqual(len(no_personas), 4)

        yes_archetypes = {p.archetype for p in yes_personas}
        expected = {"sharp_debater", "medical_expert", "grassroots_worker", "policy_analyst"}
        self.assertEqual(yes_archetypes, expected)

    def test_lookup_and_filter(self):
        registry = get_default_registry()
        debater = registry.get("bot_debater_yes")
        self.assertIsNotNone(debater)
        self.assertEqual(debater.name, "Elena Vance")

        medic_yes = registry.list_by_archetype("medical_expert", side="yes")
        self.assertEqual(len(medic_yes), 1)
        self.assertEqual(medic_yes[0].account_id, "bot_medic_yes")

    def test_custom_registration(self):
        registry = PersonaRegistry(personas=[])
        custom = Persona(
            account_id="bot_custom",
            archetype="sharp_debater",
            name="Custom Bot",
            bio="Custom bio",
            tone_instruction="Direct",
            assigned_side="yes",
        )
        registry.register(custom)
        self.assertEqual(registry.get("bot_custom"), custom)
        self.assertEqual(len(registry.list_all()), 1)


class TestPersonaRouter(unittest.TestCase):
    def setUp(self):
        self.router = PersonaRouter(default_side="yes")

    def test_route_troll_venting_to_sharp_debater(self):
        jev_data = {
            "flow": {
                "deep": {
                    "assessment": {
                        "output": {
                            "answers": {
                                "expression_function": {"value": "venting"},
                                "emotion_intensity": {"value": 3.0},
                            }
                        }
                    }
                }
            }
        }
        post_text = "This whole thing is a complete fake scam and total nonsense!"
        decision = self.router.route(jev_data, post_text, side="yes")
        self.assertEqual(decision.target_archetype, "sharp_debater")
        self.assertEqual(decision.account_id, "bot_debater_yes")
        self.assertFalse(decision.fallback_used)

    def test_route_medical_question_to_medical_expert(self):
        jev_data = {
            "flow": {
                "deep": {
                    "assessment": {
                        "output": {
                            "answers": {
                                "topic": {"value": "vaccine_evidence"},
                                "fact_check_needed": {"value": 0.8},
                            }
                        }
                    }
                }
            }
        }
        post_text = "What clinical trial data supports the safety of this vaccine against the flu virus?"
        decision = self.router.route(jev_data, post_text, side="yes")
        self.assertEqual(decision.target_archetype, "medical_expert")
        self.assertEqual(decision.account_id, "bot_medic_yes")
        self.assertFalse(decision.fallback_used)

    def test_route_policy_question_to_policy_analyst(self):
        jev_data = {
            "flow": {
                "deep": {
                    "assessment": {
                        "output": {
                            "answers": {
                                "topic": {"value": "public_access"},
                                "claim_kind": {"value": "policy_proposal"},
                            }
                        }
                    }
                }
            },
            "response_decision": {"prompt_type": "P04"},
        }
        post_text = "Which municipal council policy version governs transit regulation and exemption rules?"
        decision = self.router.route(jev_data, post_text, side="yes")
        self.assertEqual(decision.target_archetype, "policy_analyst")
        self.assertEqual(decision.account_id, "bot_policy_yes")
        self.assertFalse(decision.fallback_used)

    def test_route_daily_commute_to_grassroots_worker(self):
        jev_data = {
            "flow": {
                "deep": {
                    "assessment": {
                        "output": {
                            "answers": {
                                "expression_function": {"value": "experience"},
                                "topic": {"value": "personal_experience"},
                            }
                        }
                    }
                }
            }
        }
        post_text = "I take the crowded subway and bus to work every day, thinking about my kids and grocery bills."
        decision = self.router.route(jev_data, post_text, side="yes")
        self.assertEqual(decision.target_archetype, "grassroots_worker")
        self.assertEqual(decision.account_id, "bot_worker_yes")
        self.assertFalse(decision.fallback_used)

    def test_cooldown_fallback_behavior(self):
        # Scenario: A policy post routes to bot_policy_yes
        jev_data = {"response_decision": {"prompt_type": "P08"}}
        post_text = "Which policy version applies?"

        d1 = self.router.route(jev_data, post_text, side="yes", now=100.0)
        self.assertEqual(d1.account_id, "bot_policy_yes")
        self.assertFalse(d1.fallback_used)

        # bot_policy_yes posts at t=100.0
        self.router.mark_used("bot_policy_yes", now=100.0)

        # Next post arrives at t=110.0 (within 60s cooldown of bot_policy_yes)
        d2 = self.router.route(jev_data, post_text, side="yes", now=110.0)
        self.assertNotEqual(d2.account_id, "bot_policy_yes")
        self.assertTrue(d2.fallback_used)
        self.assertFalse(d2.in_cooldown)

        # Fast forward past cooldown (t=165.0)
        d3 = self.router.route(jev_data, post_text, side="yes", now=165.0)
        self.assertEqual(d3.account_id, "bot_policy_yes")
        self.assertFalse(d3.fallback_used)

    def test_all_accounts_in_cooldown(self):
        # Mark all 4 yes personas as used at t=100.0 with staggered timestamps
        t0 = 100.0
        for idx, p in enumerate(self.router.registry.list_by_side("yes")):
            self.router.mark_used(p.account_id, now=t0 + idx * 2)

        jev_data = {}
        post_text = "Any generic post"
        # Query at t=105.0 when all 4 accounts are still in cooldown
        decision = self.router.route(jev_data, post_text, side="yes", now=105.0)
        self.assertTrue(decision.in_cooldown)


class TestBrainPersonaIntegration(unittest.TestCase):
    def test_brain_selects_account_and_updates_cooldown(self):
        # Mock workflow
        workflow = MagicMock()
        mock_package = MagicMock()
        mock_package.to_dict.return_value = {
            "flow": {
                "status": "ready",
                "relative_stance": "neutral",
                "deep": {
                    "assessment": {
                        "output": {
                            "answers": {
                                "topic": {"value": "vaccine_evidence"},
                                "expression_function": {"value": "question"},
                            }
                        }
                    }
                },
            },
            "action_decision": {"status": "decided", "action": "reply"},
            "response_decision": {"prompt_type": "P08"},
        }
        workflow.evaluate.return_value = mock_package

        # Mock reply generator
        generator = MagicMock()
        generator.generate.return_value = ReplyDraftResult(
            status="draft",
            reason="generated",
            reply="Which clinical research data supports this?",
            prompt_type="P08",
        )

        # Mock postflight checker that approves
        postflight = MagicMock()
        pf_verdict = MagicMock()
        pf_verdict.can_send = True
        pf_verdict.reason = "authorized"
        pf_verdict.to_dict.return_value = {"can_send": True, "answers": {"send_authorization": "send"}}
        postflight.check.return_value = pf_verdict

        router = PersonaRouter(default_side="yes")
        brain = JevLlmBrain(
            workflow=workflow,
            generator=generator,
            side=Side.YES,
            router=router,
            postflight=postflight,
        )

        post = Post(post_id="p1", author="citizen1", text="What vaccine clinical trials exist?")
        verdict = brain.think(post)

        self.assertEqual(verdict.action, ActionKind.REPLY)
        self.assertEqual(verdict.detail.get("selected_account"), "bot_medic_yes")
        self.assertIn("routing", verdict.detail)

        # Verify router marked account as used
        state = router.get_state("bot_medic_yes")
        self.assertEqual(state.post_count, 1)

        # Verify generator received the selected persona
        call_kwargs = generator.generate.call_args[1]
        self.assertEqual(call_kwargs["persona"].account_id, "bot_medic_yes")

    def test_brain_skips_when_all_accounts_in_cooldown(self):
        workflow = MagicMock()
        mock_package = MagicMock()
        mock_package.to_dict.return_value = {
            "flow": {"status": "ready"},
            "action_decision": {"status": "decided", "action": "reply"},
            "response_decision": {"prompt_type": "P08"},
        }
        workflow.evaluate.return_value = mock_package

        generator = MagicMock()
        postflight = MagicMock()
        router = PersonaRouter(default_side="yes")

        # Put all accounts into cooldown using current monotonic time
        import time
        now = time.monotonic()
        for p in router.registry.list_by_side("yes"):
            router.mark_used(p.account_id, now=now)

        brain = JevLlmBrain(
            workflow=workflow,
            generator=generator,
            side=Side.YES,
            router=router,
            postflight=postflight,
        )

        post = Post(post_id="p2", author="citizen2", text="Any question")
        verdict = brain.think(post)

        # Must skip due to cooldown
        self.assertEqual(verdict.action, ActionKind.NONE)
        self.assertTrue(verdict.reason.startswith("account_cooldown:"))
        # Generator was never called
        generator.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

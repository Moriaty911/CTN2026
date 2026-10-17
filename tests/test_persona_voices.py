"""Tests for persona prompt construction, voice injection, and tone integrity."""

from __future__ import annotations

import json
import unittest

from ctn.llm.models import DraftOptions
from ctn.llm.prompts import build_messages
from ctn.persona import DEFAULT_PERSONAS, Persona, get_default_registry


def make_sample_material(prompt_type: str = "P08") -> dict:
    """Create minimal valid generation materials dictionary."""
    return {
        "schema_version": "jev-generation-material.v1",
        "prompt_type": prompt_type,
        "action_decision": {
            "status": "decided",
            "action": "reply",
            "reply_quality": "ordinary",
        },
        "target": {"post_id": "p_target_001", "text": "Is the policy finalized?"},
        "focus": None,
        "support": None,
        "secondary": None,
        "evaluated_materials": None,
        "comparison_materials": None,
        "information_gap": "Which policy version is intended?",
        "comments": None,
        "sample_scope": None,
        "human_verifications": None,
    }


class PersonaVoicePromptTests(unittest.TestCase):
    def setUp(self):
        self.options = DraftOptions(language="follow_post", max_reply_chars=280)
        self.material = make_sample_material("P08")
        self.registry = get_default_registry()

    def test_voice_injection_for_all_yes_personas(self):
        """Verify that all YES personas inject their specific name, bio, and tone into system prompt."""
        yes_personas = self.registry.list_by_side("yes")
        self.assertEqual(len(yes_personas), 80)

        for p in yes_personas:
            messages = build_messages(self.material, self.options, max_chars=280, persona=p)
            self.assertEqual(len(messages), 2)
            sys_msg = messages[0]["content"]

            # System prompt must contain persona profile markers
            self.assertIn(f"You are posting as '{p.name}'", sys_msg)
            self.assertIn(p.bio, sys_msg)
            self.assertIn(p.tone_instruction, sys_msg)

    def test_voice_injection_for_all_no_personas(self):
        """Verify that all NO personas inject their specific name, bio, and tone into system prompt."""
        no_personas = self.registry.list_by_side("no")
        self.assertEqual(len(no_personas), 0)

        for p in no_personas:
            messages = build_messages(self.material, self.options, max_chars=280, persona=p)
            sys_msg = messages[0]["content"]

            self.assertIn(f"You are posting as '{p.name}'", sys_msg)
            self.assertIn(p.bio, sys_msg)
            self.assertIn(p.tone_instruction, sys_msg)

    def test_prompt_without_persona(self):
        """When persona is None, system prompt does not contain persona profile section."""
        messages = build_messages(self.material, self.options, max_chars=280, persona=None)
        sys_msg = messages[0]["content"]

        self.assertNotIn("Persona Profile:", sys_msg)
        self.assertNotIn("You are posting as", sys_msg)
        # But core instructions remain intact
        self.assertIn("Fixed purpose: P08.", sys_msg)

    def test_prompt_with_retry_feedback(self):
        """When retry feedback is supplied, it is appended to the system instructions."""
        persona = self.registry.get("yes_073")
        messages = build_messages(
            self.material,
            self.options,
            max_chars=280,
            persona=persona,
            retry_feedback="conceded_to_opponents",
        )
        sys_msg = messages[0]["content"]

        self.assertIn("IMPORTANT CORRECTION", sys_msg)
        self.assertIn("conceded_to_opponents", sys_msg)
        self.assertIn(persona.name, sys_msg)

    def test_persona_integrity(self):
        """Verify all default personas have valid fields and positive cooldowns."""
        for p in DEFAULT_PERSONAS:
            self.assertEqual(p.account_id, "")
            self.assertTrue(p.persona_id.startswith("yes_"))
            self.assertTrue(len(p.name) > 0)
            self.assertTrue(len(p.bio) > 10)
            self.assertTrue(len(p.tone_instruction) > 10)
            self.assertGreater(p.cooldown_s, 0.0)
            self.assertIn(p.assigned_side, ("yes", "no"))


if __name__ == "__main__":
    unittest.main()

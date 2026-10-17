"""Tests for harness.md parsing, Pi skill integrity, and CLI edge cases."""

from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest

from ctn.macro.cli import main


class HarnessFileIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent
        self.harness_file = self.root / "harness.md"
        self.skill_file = self.root / ".pi" / "skills" / "campaign-publisher" / "SKILL.md"

    def test_harness_file_exists_and_contains_core_sections(self):
        self.assertTrue(self.harness_file.is_file(), "harness.md must exist at repository root")
        content = self.harness_file.read_text(encoding="utf-8")

        required_sections = [
            "1. Active Strategic Focus",
            "2. Account States & Cadence Locks",
            "3. Media & VIP NPC Watchlist",
            "4. Human Directives & Standing Rules",
            "5. Action History",
        ]
        for section in required_sections:
            self.assertIn(section, content, f"harness.md missing required section: {section}")

        # Verify harness.md contains strictly English text (no CJK characters)
        self.assertIsNone(
            re.search(r"[\u4e00-\u9fff]", content),
            "harness.md must be written strictly in English without Chinese characters",
        )

    def test_extract_active_human_directives(self):
        """Verify that standing directives marked with [ACTIVE] can be extracted."""
        content = self.harness_file.read_text(encoding="utf-8")
        active_directives = re.findall(r"\[ACTIVE\]\s*(.*)", content)
        self.assertGreater(len(active_directives), 0, "Should have at least one active human directive")
        for directive in active_directives:
            self.assertTrue(len(directive.strip()) > 5)

    def test_account_table_has_all_personas(self):
        """Verify that all default accounts are tracked in the harness.md table."""
        content = self.harness_file.read_text(encoding="utf-8")
        self.assertIn('config/personas/yes.v1.json',content)
        self.assertIn('80 YES profiles',content)
        self.assertIn('persona list',content)
        for account in ('bot_worker_yes','bot_medic_yes','bot_policy_yes','bot_debater_yes'):
            self.assertNotIn(account,content)

    def test_pi_skill_file_integrity(self):
        """Verify that .pi/skills/campaign-publisher/SKILL.md is valid and strictly in English."""
        self.assertTrue(self.skill_file.is_file(), "SKILL.md must exist in .pi/skills/campaign-publisher/")
        content = self.skill_file.read_text(encoding="utf-8")

        # Check YAML frontmatter
        self.assertTrue(content.startswith("---"))
        frontmatter_end = content.find("---", 3)
        self.assertGreater(frontmatter_end, 3)
        frontmatter = content[3:frontmatter_end]
        self.assertIn("name: campaign-publisher", frontmatter)
        self.assertIn("description:", frontmatter)

        # Check required workflow keywords in instructions
        self.assertIn("harness.md", content)
        self.assertIn("python -m ctn.macro status", content)
        self.assertIn('python -m ctn.macro --side "$CTN_SIDE" publish', content)

        # Verify SKILL.md contains strictly English text (no CJK characters)
        self.assertIsNone(
            re.search(r"[\u4e00-\u9fff]", content),
            "SKILL.md must be written strictly in English without Chinese characters",
        )


class MacroCliEdgeCaseTests(unittest.TestCase):
    @staticmethod
    def _bind(path):
        from contextlib import closing
        from ctn.storage import StateStore
        from ctn.persona import PersonaRegistry
        with closing(StateStore(path)) as store:
            PersonaRegistry().attach(store)
            store.bind_persona('yes_001','fresh_account')
            store.set_persona_modes(['yes_001'],'active')

    def test_publish_unknown_account_fails_gracefully(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            code = main([
                "--mock","--side","yes",
                "--state-file", tmp.name,
                "publish",
                "--account", "non_existent_account",
                "--text", "Valid post text",
                "--dry-run",
            ])
            self.assertEqual(code, 1)

    def test_publish_dry_run_flag(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            self._bind(tmp.name)
            code = main([
                "--mock","--side","yes",
                "--state-file", tmp.name,
                "publish",
                "--account", "fresh_account",
                "--text", "Debate inquiry about Kingstonia policy.",
                "--dry-run",
                "--bypass-jev",
            ])
            self.assertEqual(code, 0)

    def test_warmup_cycling_index(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            self._bind(tmp.name)
            # Seed index 0 on bot_worker_yes
            code0 = main([
                "--mock","--side","yes",
                "--state-file", tmp.name,
                "warmup",
                "--account", "fresh_account",
                "--index", "0",
                "--dry-run",
            ])
            self.assertEqual(code0, 0)

            # Seed index 1 on bot_debater_yes (different account, so not blocked by worker cooldown)
            code1 = main([
                "--mock","--side","yes",
                "--state-file", tmp.name,
                "warmup",
                "--account", "fresh_account",
                "--index", "1",
                "--dry-run",
            ])
            self.assertEqual(code1, 0)


if __name__ == "__main__":
    unittest.main()

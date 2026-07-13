from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
UI = ROOT / "src" / "rekit_factory" / "ui"


class MissionControlAttentionTests(unittest.TestCase):
    def test_operator_attention_surface_is_live_redacted_and_motion_safe(self):
        page = (UI / "index.html").read_text(encoding="utf-8")
        script = (UI / "mission-control.js").read_text(encoding="utf-8")
        style = (UI / "mission-control.css").read_text(encoding="utf-8")
        api = (UI.parent / "api.py").read_text(encoding="utf-8")

        for marker in (
            'id="operatorAttention"', 'id="operatorAttentionAnnouncer"',
            'id="inboxHeading" tabindex="-1"', "Open Inbox", "data-attention-later",
            "data-attention-dismiss", 'aria-live="assertive"', 'aria-atomic="true"',
        ):
            self.assertIn(marker, page)
        self.assertLess(page.index("/ui/mission-attention.js"), page.index("/ui/mission-control.js"))
        self.assertIn('"mission-attention.js": "text/javascript; charset=utf-8"', api)
        for behavior in (
            "state.attention.transitions(runs)", "MissionAttention.messageFor(runCount, questionCount)",
            "MissionAttention.claimQuestionState", "openAttentionInbox", "focusInbox",
            "MissionAttention.restoreFocus(", "MissionAttention.shouldFocusInbox(",
            "state.viewGeneration", "dismissAttention", "cancelAnimationFrame",
        ):
            self.assertIn(behavior, script)
        announce = script[script.index("async function announceAttention"):script.index("const delay")]
        attention_helper = (UI / "mission-attention.js").read_text(encoding="utf-8")
        for helper_behavior in ("question?.id", "tracker.claim(runId, questionIds)", "tracker.rearm(runId)"):
            self.assertIn(helper_behavior, attention_helper)
        for sensitive_detail in (
            "question.prompt", "question.message", "question.toolId", "run.target", "run.goal",
        ):
            self.assertNotIn(sensitive_detail, announce)
        self.assertIn("attention-arrive", style)
        self.assertIn("attention-ring", style)
        self.assertIn(".attention-announcer", style)
        self.assertIn("@media(prefers-reduced-motion:reduce)", style)

    def test_attention_tracker_transitions_and_deduplication(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        result = subprocess.run(
            [node, str(ROOT / "tests" / "mission_attention.test.js")],
            cwd=ROOT, check=False, capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("mission attention tracker: ok", result.stdout)

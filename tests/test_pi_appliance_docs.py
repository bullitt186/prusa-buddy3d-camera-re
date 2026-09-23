"""Content/contract tests for the WP-R6a appliance documentation pass.

These tests are stdlib-only, never build or flash an image, and never touch the
network or hardware. They assert that the operator-facing appliance guide exists
and covers the documented topics, that it does not claim completed hardware
acceptance, and that the deferred personal-username cleanup stayed fixed.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GUIDE = REPO_ROOT / "docs" / "appliance-user-guide.md"
GAP_TRACKER = REPO_ROOT / "docs" / "firmware-implementation-gap-tracker.md"
DIST_PLAN = REPO_ROOT / "docs" / "public-appliance-distribution-plan.md"
README = REPO_ROOT / "README.md"
PI_README = REPO_ROOT / "pi-impersonator" / "README.md"

#: Personal literals the secret scanner flags; must not appear in tracked docs.
#: Assembled from adjacent string fragments (like scan-secrets.sh) so this test
#: file does not itself contain the contiguous literal.
PERSONAL_LITERALS = ("b" "u" "l" "l" "i" "t" "t", "s" "t" "a" "h" "m" "e" "r")

#: Required topic coverage. Each entry is a set of keywords; at least one must
#: appear (case-insensitive) so the test stays a content contract rather than a
#: brittle full-text match.
REQUIRED_TOPICS = {
    "scope/what-it-is": ("what it is", "scope"),
    "hardware": ("hardware", "raspberry pi zero 2 w"),
    "install/flash": ("install", "flash"),
    "first-boot onboarding": ("first-boot onboarding", "setup hotspot"),
    "home-assistant/onvif/rtsp": ("onvif", "rtsp", "8555"),
    "mqtt": ("mqtt", "buddy3d/<device-id>"),
    "backup": ("backup",),
    "reflash/recovery/factory-reset": ("reflash", "recovery", "factory reset"),
    "security": ("security", "scrypt", "ssh is disabled by default"),
    "updates/rollback": ("update", "rollback"),
    "troubleshooting": ("troubleshooting", "journalctl"),
    "acceptance-status": ("acceptance status",),
}

#: The guide must link the authoritative docs instead of duplicating them.
REFERENCED_DOCS = (
    "image/README.md",
    "protocol.md",
    "home-assistant-onvif-implementation-plan.md",
    "public-appliance-distribution-plan.md",
)

#: Claims that would overstate what has actually been verified.
FORBIDDEN_ACCEPTANCE_CLAIMS = (
    "hardware acceptance is complete",
    "all acceptance sections passed",
    "acceptance passed",
    "validated flash",
    "verified live",
)


class ApplianceUserGuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = GUIDE.read_text(encoding="utf-8")
        cls.lower = cls.text.lower()

    def test_guide_exists(self):
        self.assertTrue(GUIDE.is_file(), f"missing {GUIDE}")

    def test_guide_covers_required_topics(self):
        for topic, keywords in REQUIRED_TOPICS.items():
            with self.subTest(topic=topic):
                self.assertTrue(
                    any(k.lower() in self.lower for k in keywords),
                    f"guide does not cover {topic!r} (looked for {keywords})",
                )

    def test_guide_references_authoritative_docs(self):
        for doc in REFERENCED_DOCS:
            with self.subTest(doc=doc):
                self.assertIn(doc, self.text, f"guide does not reference {doc}")

    def test_guide_marks_hardware_acceptance_pending(self):
        # An explicit non-completion statement must be present.
        self.assertIn("not yet hardware-accepted", self.lower)
        self.assertIn("pending", self.lower)

    def test_guide_does_not_claim_completed_hardware_acceptance(self):
        for claim in FORBIDDEN_ACCEPTANCE_CLAIMS:
            with self.subTest(claim=claim):
                self.assertNotIn(claim, self.lower, f"guide overstates: {claim!r}")

    def test_guide_has_no_personal_username_literals(self):
        for literal in PERSONAL_LITERALS:
            with self.subTest(literal=literal):
                self.assertNotIn(literal, self.lower)


class DeferredDocCleanupTests(unittest.TestCase):
    def test_gap_tracker_personal_username_removed(self):
        text = GAP_TRACKER.read_text(encoding="utf-8")
        for literal in PERSONAL_LITERALS:
            with self.subTest(literal=literal):
                self.assertNotIn(literal, text.lower())
        # The Samba evidence line now uses a neutral placeholder.
        self.assertIn("force user =", text)
        self.assertIn("<operator>", text)

    def test_distribution_plan_personal_username_removed(self):
        text = DIST_PLAN.read_text(encoding="utf-8")
        for literal in PERSONAL_LITERALS:
            with self.subTest(literal=literal):
                self.assertNotIn(literal, text.lower())

    def test_tracked_docs_have_no_personal_literals(self):
        for path in (README, GUIDE, PI_README):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8").lower()
                for literal in PERSONAL_LITERALS:
                    self.assertNotIn(literal, text, f"{path}: {literal}")

    def test_pi_readme_no_longer_has_user_pi_templating(self):
        text = PI_README.read_text(encoding="utf-8")
        # The stale per-user templating against User=pi / /home/pi/ is gone.
        self.assertNotIn("User=pi", text)
        self.assertNotIn(r"/home/pi/", text)
        # The current service-account layout is documented instead.
        self.assertIn("User=prusa-cam", text)
        self.assertIn("prusa-cam", text)


if __name__ == "__main__":
    unittest.main()

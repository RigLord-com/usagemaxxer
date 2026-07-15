import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_release  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTests(unittest.TestCase):
    def test_current_tag_matches_embedded_versions(self):
        # Derived from the actual VERSION, not hardcoded, so this never goes
        # stale when the version is bumped for a new release.
        (version,) = verify_release.embedded_versions()
        result = subprocess.run(
            [sys.executable, "verify_release.py", "--tag", f"v{version}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mismatched_tag_fails(self):
        result = subprocess.run(
            [sys.executable, "verify_release.py", "--tag", "v2.0.0"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)

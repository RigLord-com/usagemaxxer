import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTests(unittest.TestCase):
    def test_current_tag_matches_embedded_versions(self):
        result = subprocess.run(
            [sys.executable, "verify_release.py", "--tag", "v1.0.0"],
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

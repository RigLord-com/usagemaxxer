import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
import urllib.error

import usagemaxxer


class CredentialPersistenceTests(unittest.TestCase):
    def setUp(self):
        usagemaxxer._claude_pending_refresh = None
        usagemaxxer._claude_refresh_backoff_until = 0.0
        usagemaxxer._claude_last_refresh_error = ""

    def test_persist_merges_latest_non_oauth_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".credentials.json"
            path.write_text(json.dumps({
                "claudeAiOauth": {"refreshToken": "old", "other": "kept"},
                "cliAdded": {"value": True},
            }), encoding="utf-8")

            self.assertTrue(usagemaxxer._persist_claude_oauth(
                path, "old", {"accessToken": "new-access", "refreshToken": "new", "expiresAt": 1},
            ))

            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["cliAdded"], {"value": True})
            self.assertEqual(written["claudeAiOauth"], {
                "refreshToken": "new", "accessToken": "new-access", "expiresAt": 1, "other": "kept",
            })

    def test_persist_refuses_newer_cli_refresh_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".credentials.json"
            original = {"claudeAiOauth": {"refreshToken": "cli-new"}, "cliAdded": True}
            path.write_text(json.dumps(original), encoding="utf-8")

            self.assertFalse(usagemaxxer._persist_claude_oauth(
                path, "old", {"accessToken": "new-access", "refreshToken": "new"},
            ))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_refresh_conflict_uses_newer_disk_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".credentials.json"
            newer = {"claudeAiOauth": {"refreshToken": "cli-new", "accessToken": "cli-access"}}
            path.write_text(json.dumps(newer), encoding="utf-8")
            with mock.patch.object(usagemaxxer, "_get_json", return_value={
                "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 60,
            }), mock.patch.object(usagemaxxer, "_persist_claude_oauth", return_value=False):
                result = usagemaxxer._refresh_claude_oauth(
                    path, {"claudeAiOauth": {"refreshToken": "old"}},
                )
            self.assertEqual(result["accessToken"], "cli-access")

    def test_forced_refresh_respects_failed_grant_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".credentials.json"
            creds = {"claudeAiOauth": {"refreshToken": "old"}}
            path.write_text(json.dumps(creds), encoding="utf-8")
            usagemaxxer._claude_refresh_backoff_until = datetime.now(timezone.utc).timestamp() + 60
            with mock.patch.object(usagemaxxer, "_get_json") as get_json:
                self.assertIsNone(usagemaxxer._refresh_claude_oauth(path, creds))
            get_json.assert_not_called()

    def test_successful_rotation_is_usable_when_persistence_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".credentials.json"
            creds = {"claudeAiOauth": {"refreshToken": "old"}}
            path.write_text(json.dumps(creds), encoding="utf-8")
            with mock.patch.object(usagemaxxer, "_get_json", return_value={
                "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 60,
            }), mock.patch.object(usagemaxxer, "_persist_claude_oauth", side_effect=OSError("locked")):
                oauth = usagemaxxer._refresh_claude_oauth(path, creds)

            self.assertEqual(oauth["accessToken"], "new-access")
            self.assertIsNotNone(usagemaxxer._claude_pending_refresh)


class NetworkTests(unittest.TestCase):
    def test_claude_retries_once_after_401(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            creds_dir = home / ".claude"
            creds_dir.mkdir()
            (creds_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "expired", "refreshToken": "refresh", "expiresAt": 0,
            }}), encoding="utf-8")
            response = {"limits": [{"kind": "session", "percent": 10}]}
            error = urllib.error.HTTPError("https://api.anthropic.com", 401, "Unauthorized", {}, None)
            with mock.patch.object(usagemaxxer, "HOME", home), \
                    mock.patch.object(usagemaxxer, "_refresh_claude_oauth", return_value={"accessToken": "fresh"}) as refresh, \
                    mock.patch.object(usagemaxxer, "_get_json", side_effect=[error, response]) as get_json:
                snapshot = usagemaxxer.fetch_claude()

            self.assertTrue(snapshot.ok)
            self.assertEqual(refresh.call_count, 2)  # Expiry refresh, then one 401 retry.
            self.assertEqual(get_json.call_args_list[1].args[1]["Authorization"], "Bearer fresh")

    def test_response_larger_than_limit_is_rejected(self):
        class Response:
            def geturl(self):
                return "https://example.test/usage"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return b"x" * size

        with mock.patch.object(usagemaxxer._NO_REDIRECT_OPENER, "open", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "size limit"):
                usagemaxxer._get_json("https://example.test/usage", {})


class RefreshDeduplicationTests(unittest.TestCase):
    def test_widget_skips_overlapping_refresh(self):
        app = usagemaxxer.WidgetApp()
        entered = threading.Event()
        release = threading.Event()
        app.active_keys = mock.Mock(return_value=["claude"])

        def fetch():
            entered.set()
            release.wait(timeout=2)
            return usagemaxxer.Snapshot(provider="Claude Code")

        with mock.patch.dict(usagemaxxer.PROVIDER_BY_KEY, {"claude": {"fetch": fetch, "name": "Claude"}}, clear=False):
            first = threading.Thread(target=app.refresh)
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            app.refresh()
            release.set()
            first.join(timeout=2)

        self.assertEqual(app.active_keys.call_count, 1)

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from build import load_whitelisted_users, rollout_files
from enforce_codex_limit import main as enforce_main, recent_totals, rules_yaml, scan_known_users


class EnforceCodexLimitTests(unittest.TestCase):
    def test_recent_totals_uses_a_rolling_24_hour_window(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "test_account.txt").write_text(
                "2025-12-31T23:59:59Z\t10\n2026-01-01T00:00:00Z\t20\n2026-01-02T00:00:01Z\t40\n",
                encoding="utf-8",
            )
            start, end, totals = recent_totals(data_dir, datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(start.isoformat(), "2026-01-01T00:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-01-02T00:00:00+00:00")
        self.assertEqual(totals, {"test_account": 20})

    def test_whitelisted_users_are_excluded_from_collection_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            whitelist_path = base_dir / "whitelist.json"
            whitelist_path.write_text('{"whitelisted_users": ["excluded_account_one", "excluded_account_two"]}\n', encoding="utf-8")
            self.assertEqual(load_whitelisted_users(whitelist_path), frozenset({"excluded_account_one", "excluded_account_two"}))
            for username in ("test_account", "excluded_account_one", "excluded_account_two"):
                session_dir = base_dir / username / ".codex" / "sessions"
                session_dir.mkdir(parents=True)
                (session_dir / "rollout.jsonl").write_text("{}\n", encoding="utf-8")
                (base_dir / f"{username}.txt").write_text("2026-01-01T00:00:00Z\t999\n", encoding="utf-8")
            with patch("build.load_whitelisted_users", return_value=frozenset({"excluded_account_one", "excluded_account_two"})):
                found = {username for username, _ in rollout_files((base_dir,))}
            with patch("enforce_codex_limit.load_whitelisted_users", return_value=frozenset({"excluded_account_one", "excluded_account_two"})):
                _, _, totals = recent_totals(base_dir, datetime(2026, 1, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(found, {"test_account"})
        self.assertEqual(totals, {"test_account": 999})

    def test_scan_uses_uid_and_never_keeps_ip(self):
        connections = [{"metadata": {"uid": 1001, "host": "api.openai.com", "process": "codex", "sourceIP": "192.0.2.1"}}]
        with patch("enforce_codex_limit.username_for_uid", return_value="test_account"):
            users, ignored, details = scan_known_users(connections, "2026-01-01T00:00:00Z")
        self.assertEqual(users["test_account"]["uid"], 1001)
        self.assertEqual(users["test_account"]["domains"], ["api.openai.com"])
        self.assertNotIn("sourceIP", str(users))
        self.assertEqual(details[0]["host"], "api.openai.com")
        self.assertEqual(ignored["missing_uid"], 0)

    def test_rules_require_uid_and_domain(self):
        rendered = rules_yaml([{"username": "test_account", "uid": 1001, "total_tokens": 200_000_001}], ("api.openai.com",))
        self.assertIn("AND,((UID,1001),(DOMAIN,api.openai.com))", rendered)
        self.assertNotIn("SRC-IP-CIDR", rendered)

    def test_hourly_run_blocks_then_unblocks_with_rule_provider_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            rules_file = data_dir / "codex_over_limit.yaml"
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            (data_dir / "test_account.txt").write_text(f"{timestamp}\t200000001\n", encoding="utf-8")
            arguments = [
                "enforce_codex_limit.py", "--mihomo-controller-url", "http://127.0.0.1:9090",
                "--data-dir", str(data_dir), "--rules-file", str(rules_file), "--skip-update",
            ]
            connections = [{"metadata": {"uid": 1001, "host": "api.openai.com", "process": "codex"}}]
            with patch("sys.argv", arguments), patch("enforce_codex_limit.get_connections", return_value=connections), patch("enforce_codex_limit.username_for_uid", return_value="test_account"), patch("enforce_codex_limit.refresh_rule_provider") as refresh:
                enforce_main()
            state = json.loads((data_dir / "codex_block_index.json").read_text(encoding="utf-8"))
            self.assertEqual(state["blocked_users"], [{"username": "test_account", "uid": 1001, "total_tokens": 200000001}])
            self.assertTrue(state["active"])
            self.assertTrue(refresh.called)

            (data_dir / "test_account.txt").write_text(f"{timestamp}\t200000000\n", encoding="utf-8")
            with patch("sys.argv", arguments), patch("enforce_codex_limit.get_connections", return_value=connections), patch("enforce_codex_limit.username_for_uid", return_value="test_account"), patch("enforce_codex_limit.refresh_rule_provider") as refresh:
                enforce_main()
            state = json.loads((data_dir / "codex_block_index.json").read_text(encoding="utf-8"))
            self.assertEqual(state["blocked_users"], [])
            self.assertTrue(refresh.called)


if __name__ == "__main__":
    unittest.main()

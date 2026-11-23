import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build import OFFSET_INDEX_NAME, TIME_INDEX_NAME, build, index_key, write_index
from update import update


def token_event(timestamp, tokens):
    record = {
        "type": "event_msg",
        "timestamp": timestamp,
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"total_tokens": tokens}},
            "label": "测试",
        },
    }
    return json.dumps(record, ensure_ascii=False).encode("utf-8")


class UsageIndexTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.base_dirs = (self.directory / "homes",)
        self.output_dir = self.directory / "data"
        self.username = "test_account"
        self.rollout = self.base_dirs[0] / self.username / ".codex/sessions/rollout.jsonl"
        self.rollout.parent.mkdir(parents=True)
        self.offset_path = self.output_dir / OFFSET_INDEX_NAME
        self.key = index_key(self.username, self.rollout)
        self.user_file = self.output_dir / f"{self.username}.txt"

    def read_offset(self):
        return json.loads(self.offset_path.read_bytes())[self.key]

    def test_retries_partial_utf8_tail_when_size_changes_without_mtime(self):
        first = token_event("2026-01-01T00:00:00Z", 10) + b"\n"
        second = token_event("2026-01-01T00:01:00Z", 20) + b"\n"
        split = second.index("测".encode()) + 1
        self.rollout.write_bytes(first + second[:split])
        build(self.base_dirs, self.output_dir)
        self.assertEqual(self.read_offset(), len(first))
        self.assertEqual(update(self.base_dirs, self.output_dir)["events_appended"], 0)

        previous = self.rollout.stat()
        with self.rollout.open("ab") as file:
            file.write(second[split:])
        os.utime(self.rollout, ns=(previous.st_atime_ns, previous.st_mtime_ns))

        self.assertEqual(update(self.base_dirs, self.output_dir)["events_appended"], 1)
        self.assertEqual(update(self.base_dirs, self.output_dir)["events_appended"], 0)
        self.assertEqual(self.read_offset(), len(first) + len(second))
        self.assertEqual(self.user_file.read_text(), "2026-01-01T00:00:00Z\t10\n2026-01-01T00:01:00Z\t20\n")

    def test_complete_record_without_final_newline_is_counted_once(self):
        first = token_event("2026-01-01T00:00:00Z", 10)
        self.rollout.write_bytes(first)
        self.assertEqual(build(self.base_dirs, self.output_dir)[1], 1)
        self.assertEqual(self.read_offset(), len(first))
        with self.rollout.open("ab") as file:
            file.write(b"\r\n" + token_event("2026-01-01T00:01:00Z", 20) + b"\n")
        self.assertEqual(update(self.base_dirs, self.output_dir)["events_appended"], 1)
        self.assertEqual(len(self.user_file.read_text().splitlines()), 2)

    def test_legacy_offset_inside_line_is_reported_even_with_matching_mtime(self):
        self.rollout.write_bytes(token_event("2026-01-01T00:00:00Z", 10) + b"\n")
        build(self.base_dirs, self.output_dir)
        write_index(self.offset_path, {self.key: 10})

        result = update(self.base_dirs, self.output_dir)
        self.assertIn("落在 JSONL 行中间", result["skipped_files"][0])
        self.assertEqual(result["events_appended"], 0)
        self.assertEqual(self.read_offset(), 10)

    def test_failed_index_write_keeps_previous_json(self):
        self.output_dir.mkdir()
        write_index(self.offset_path, {self.key: 10})
        original = self.offset_path.read_bytes()

        def fail_after_partial_write(data, file, **kwargs):
            file.write('{"partial":')
            raise OSError("模拟写入中断")

        with patch("build.json.dump", side_effect=fail_after_partial_write):
            with self.assertRaises(OSError):
                write_index(self.offset_path, {self.key: 20})

        self.assertEqual(self.offset_path.read_bytes(), original)
        self.assertEqual(list(self.output_dir.iterdir()), [self.offset_path])

    def test_update_resumes_after_offset_index_is_committed_first(self):
        build(self.base_dirs, self.output_dir)
        self.rollout.write_bytes(token_event("2026-01-01T00:00:00Z", 10) + b"\n")

        def fail_time_index_write(path, data):
            if path.name == TIME_INDEX_NAME:
                raise OSError("模拟时间索引写入中断")
            write_index(path, data)

        with patch("update.write_index", side_effect=fail_time_index_write):
            with self.assertRaises(OSError):
                update(self.base_dirs, self.output_dir)

        self.assertEqual(update(self.base_dirs, self.output_dir)["events_appended"], 0)
        self.assertEqual(self.user_file.read_text(), "2026-01-01T00:00:00Z\t10\n")


if __name__ == "__main__":
    unittest.main()

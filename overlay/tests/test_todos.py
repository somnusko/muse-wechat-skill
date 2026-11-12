#!/usr/bin/env python3
"""Tests for the todos module (todos.py) and its ilink.py CLI.

Reconstructed coverage (mirrors the cases from the original
porting worker's report; this file was rewritten after the /tmp
scratch copies were wiped, so it is not byte-identical to the
original, but covers the same behaviours):

- parse_due: relative times (minutes/hours/half-hour/days),
  tonight/tomorrow morning/tomorrow afternoon, ISO absolute,
  ambiguous bare "明天 9 点", bare "21:00" (unambiguous),
  past time, nonexistent date, garbage input, >7 day relative.
- Bookkeeping: add -> list -> done flow (due sort, repeat done
  False), unknown id, cron_id backfill, empty-text rejection,
  atomic writes (valid JSON, no leftover tmp files), cleanup
  only purges old done/cancelled entries.
- CLI via ilink.main: todo-add/list/done (exit codes and
  TODO_OK/TODO_DONE/TODO_NOT_FOUND prefixes), ambiguous time
  TODO_TIME_ERROR, missing args exit 2, todo-show/todo-set-cron.

Run from the repo root: python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import todos  # noqa: E402
import ilink  # noqa: E402
from todos import (AmbiguousTimeError, PastTimeError,  # noqa: E402
                   parse_due, local_tz)

FIXED_NOW = datetime(2026, 11, 12, 12, 0, tzinfo=local_tz())


def _dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=local_tz())


class _DirCase(unittest.TestCase):
    """Base: point ILINK_DATA_DIR at a fresh temp dir per test."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ilink-todos-case-")
        self._old_env = os.environ.get("ILINK_DATA_DIR")
        os.environ["ILINK_DATA_DIR"] = self.dir

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("ILINK_DATA_DIR", None)
        else:
            os.environ["ILINK_DATA_DIR"] = self._old_env


class ParseDueTest(unittest.TestCase):
    def test_relative_minutes(self):
        self.assertEqual(parse_due("10 分钟后", now=FIXED_NOW),
                         FIXED_NOW + timedelta(minutes=10))

    def test_relative_hours(self):
        self.assertEqual(parse_due("2 小时后", now=FIXED_NOW),
                         FIXED_NOW + timedelta(hours=2))

    def test_relative_half_hour(self):
        self.assertEqual(parse_due("半小时后", now=FIXED_NOW),
                         FIXED_NOW + timedelta(minutes=30))

    def test_relative_days(self):
        self.assertEqual(parse_due("3 天后", now=FIXED_NOW),
                         FIXED_NOW + timedelta(days=3))

    def test_tonight(self):
        self.assertEqual(parse_due("今晚 9 点", now=FIXED_NOW),
                         _dt(2026, 11, 12, 21, 0))

    def test_tomorrow_morning(self):
        self.assertEqual(parse_due("明天早上 8 点", now=FIXED_NOW),
                         _dt(2026, 11, 13, 8, 0))

    def test_tomorrow_afternoon_half(self):
        self.assertEqual(parse_due("明天下午 3 点半", now=FIXED_NOW),
                         _dt(2026, 11, 13, 15, 30))

    def test_iso_absolute(self):
        self.assertEqual(parse_due("2026-11-13 08:00", now=FIXED_NOW),
                         _dt(2026, 11, 13, 8, 0))

    def test_ambiguous_bare_hour(self):
        with self.assertRaises(AmbiguousTimeError):
            parse_due("明天 9 点", now=FIXED_NOW)

    def test_bare_clock_unambiguous(self):
        self.assertEqual(parse_due("21:00", now=FIXED_NOW),
                         _dt(2026, 11, 12, 21, 0))

    def test_past_time(self):
        with self.assertRaises(PastTimeError):
            parse_due("今天 08:00", now=FIXED_NOW)

    def test_nonexistent_date(self):
        with self.assertRaises(ValueError):
            parse_due("2026-02-30 08:00", now=FIXED_NOW)

    def test_garbage_input(self):
        with self.assertRaises(ValueError):
            parse_due("随便说点啥", now=FIXED_NOW)

    def test_relative_too_far(self):
        with self.assertRaises(ValueError):
            parse_due("10 天后", now=FIXED_NOW)

    def test_none_and_dash(self):
        self.assertIsNone(parse_due(None, now=FIXED_NOW))
        self.assertIsNone(parse_due("-", now=FIXED_NOW))


class BookkeepingTest(_DirCase):
    def test_add_list_done_flow(self):
        # A due safely inside the 365-day window, computed from now.
        future = (datetime.now(local_tz()) + timedelta(days=30))
        due_str = future.strftime("%Y-%m-%dT%H:%M")
        a = todos.add("交报告", due=due_str)
        b = todos.add("买牛奶", due=None)
        items = todos.list_todos()
        # Sorted by due ascending; no-due entries go last.
        self.assertEqual([a["id"], b["id"]], [i["id"] for i in items])
        self.assertTrue(todos.done(a["id"]))
        # Repeat done returns False (already done).
        self.assertFalse(todos.done(a["id"]))
        self.assertEqual([], [i for i in todos.list_todos()
                              if i["id"] == a["id"]])
        self.assertEqual([b["id"]],
                         [i["id"] for i in todos.list_todos()])

    def test_unknown_id(self):
        self.assertIsNone(todos.get(99))
        self.assertFalse(todos.done(99))
        self.assertFalse(todos.set_cron_id(99, "x"))

    def test_cron_id_backfill(self):
        item = todos.add("交报告", due=None)
        self.assertTrue(todos.set_cron_id(item["id"], "remind-1-test"))
        got = todos.get(item["id"])
        self.assertEqual("remind-1-test", got["cron_id"])

    def test_empty_text_rejected(self):
        with self.assertRaises(ValueError):
            todos.add("   ")

    def test_atomic_write_valid_json_no_tmp_leftovers(self):
        todos.add("one", due=None)
        todos.add("two", due=None)
        with open(os.path.join(self.dir, "todos.json"),
                  encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(2, len(data))
        leftovers = [n for n in os.listdir(self.dir)
                     if n.startswith(".todos.")]
        self.assertEqual([], leftovers)

    def test_cleanup_only_old_done_cancelled(self):
        old = todos.add("old done", due=None)
        todos.done(old["id"])
        # Backdate its completed_at beyond the retention window.
        path = os.path.join(self.dir, "todos.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data[0]["completed_at"] = "2020-01-01T00:00:00"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        keep = todos.add("still pending", due=None)
        self.assertEqual(1, todos.cleanup(days=30))
        remaining = todos.list_todos(include_done=True)
        self.assertEqual([keep["id"]], [i["id"] for i in remaining])


class CliTest(_DirCase):
    def _run_cli(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ilink.main(["ilink.py"] + argv)
        return rc, buf.getvalue()

    def test_cli_add_list_done(self):
        rc, out = self._run_cli(["todo-add", "明天早上 8 点", "交报告"])
        self.assertEqual(0, rc)
        self.assertIn("TODO_OK:1", out)
        rc, out = self._run_cli(["todo-add", "-", "买牛奶"])
        self.assertEqual(0, rc)
        self.assertIn("TODO_OK:2", out)
        rc, out = self._run_cli(["todo-list"])
        self.assertEqual(0, rc)
        self.assertIn("交报告", out)
        self.assertIn("买牛奶", out)
        rc, out = self._run_cli(["todo-done", "1"])
        self.assertEqual(0, rc)
        self.assertIn("TODO_DONE:1", out)
        rc, out = self._run_cli(["todo-done", "99"])
        self.assertEqual(1, rc)
        self.assertIn("TODO_NOT_FOUND:99", out)

    def test_cli_ambiguous_time_error(self):
        rc, out = self._run_cli(["todo-add", "明天 9 点", "含糊测试"])
        self.assertEqual(1, rc)
        self.assertIn("TODO_TIME_ERROR:", out)
        # Nothing was stored.
        rc, out = self._run_cli(["todo-list"])
        self.assertIn("TODO_EMPTY", out)

    def test_cli_missing_args_exit_2(self):
        rc, out = self._run_cli(["todo-add", "明天早上 8 点"])
        self.assertEqual(2, rc)
        rc, out = self._run_cli(["todo-done"])
        self.assertEqual(2, rc)

    def test_cli_show_and_set_cron(self):
        rc, out = self._run_cli(["todo-add", "-", "买牛奶"])
        self.assertEqual(0, rc)
        rc, out = self._run_cli(["todo-set-cron", "1", "remind-1-test"])
        self.assertEqual(0, rc)
        self.assertIn("TODO_CRON:1:remind-1-test", out)
        rc, out = self._run_cli(["todo-show", "1"])
        self.assertEqual(0, rc)
        shown = json.loads(out)
        self.assertEqual("remind-1-test", shown["cron_id"])
        rc, out = self._run_cli(["todo-show", "99"])
        self.assertEqual(1, rc)
        self.assertIn("TODO_NOT_FOUND:99", out)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".claude" / "hooks" / "daily-chat-review.py"
LOCAL_TZ = timezone(timedelta(hours=7))
NOW = "2026-01-15T20:30:00+07:00"


def iso_z(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def local_dt(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL_TZ)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class DailyChatReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        write_json(self.data / "learner-profile.json", {"level": "B1", "target": "B2", "goals": ["work"]})
        write_json(self.data / "mistakes-db.json", {"metadata": {}, "error_patterns": {}})
        write_json(self.data / "spaced-repetition.json", {"metadata": {}, "items": {}, "review_queue": {}})
        self.base = self.data / "chat-review"
        self.day = datetime(2026, 1, 15).date()

    def run_cli(self, *args):
        env = os.environ.copy()
        env["FLUENT_DATA_DIR"] = str(self.data)
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + [str(a) for a in args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )

    def prepare(self, now=NOW):
        return self.run_cli("prepare", "--home", self.home, "--no-orca", "--now", now)

    def touch(self, path, dt):
        ts = int(dt.timestamp())
        os.utime(path, (ts, ts))

    def write_codex_session(self, session_id, texts, dt=None, cwd="/repo/fluent"):
        dt = dt or local_dt(self.day, 10)
        path = self.home / ".codex" / "sessions" / "test" / ("rollout-%s.jsonl" % session_id)
        rows = [{"type": "session_meta", "payload": {"id": session_id, "source": "vscode", "cwd": cwd}}]
        latest_dt = dt
        for index, item in enumerate(texts):
            if isinstance(item, tuple):
                text, item_dt = item
            else:
                text, item_dt = item, dt
            latest_dt = max(latest_dt, item_dt)
            rows.append({"type": "response_item", "timestamp": iso_z(item_dt), "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.touch(path, latest_dt)
        return path

    def write_state(self, last_end=None, runs=None):
        write_json(self.base / "state.json", {"last_end": last_end, "runs": runs or []})

    def write_intents(self, intents):
        write_json(self.base / "intents.json", intents)

    def load_json(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def state(self):
        return self.load_json(self.base / "state.json")

    def intents(self):
        return self.load_json(self.base / "intents.json")

    def latest_run(self):
        return self.state()["runs"][-1]

    def package(self, run_id=None):
        run_id = run_id or self.latest_run()["run_id"]
        return self.load_json(self.base / "runs" / run_id / "package.json")

    def write_analysis(self, run_id, analysis):
        path = self.base / "runs" / run_id / "analysis.json"
        write_json(path, analysis)
        return path

    def valid_expression(self, target):
        return {
            "intent_id": target,
            "scenario": "A service fails in staging and you want the agent to inspect it before changing anything. What would you tell the agent?",
            "model_answer": "Please investigate the issue and wait for my approval before editing.",
            "note_vi": "Câu này lịch sự và nêu rõ ranh giới trước khi sửa.",
        }

    def simple_prepare(self):
        self.write_codex_session("s-simple", [
            "I goes to work everyday. Tôi muốn bạn kiểm tra vấn đề này trước khi sửa."
        ])
        proc = self.prepare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status: needs_analysis", proc.stdout)
        return self.package()

    def simple_analysis(self, package):
        en = package["english_items"][0]["item_id"]
        vi = package["vietnamese_items"][0]["item_id"]
        return {
            "run_id": package["run_id"],
            "english_errors": [{
                "item_id": en,
                "quote": "I goes",
                "correction": "I go",
                "pattern_id": "subject_verb_agreement",
                "pattern_summary": "Subject and verb do not agree",
                "category": "grammar",
                "subcategory": "subject_verb_agreement",
                "severity": "high",
                "explanation_vi": "Chủ ngữ I cần động từ nguyên mẫu go.",
            }],
            "intents": [{
                "item_id": vi,
                "intent_id": None,
                "new_intent": {
                    "summary": "investigate a problem before editing",
                    "audience": "coding agent",
                    "constraints": ["investigate only", "wait for approval"],
                },
                "excerpt": "kiểm tra vấn đề này",
            }],
            "expressions": [self.valid_expression("new:%s" % vi)],
            "skipped": [],
        }

    def ingest_analysis(self, analysis):
        path = self.write_analysis(analysis["run_id"], analysis)
        return self.run_cli("ingest", path)

    def test_first_run_window_and_language_split(self):
        package = self.simple_prepare()
        self.assertEqual(package["window"]["start"], "2026-01-14T20:30:00+07:00")
        self.assertEqual(package["window"]["end"], "2026-01-15T20:30:00+07:00")
        self.assertEqual(len(package["english_items"]), 1)
        self.assertEqual(len(package["vietnamese_items"]), 1)
        self.assertEqual(package["english_items"][0]["item_id"], "en-1")
        self.assertEqual(package["vietnamese_items"][0]["item_id"], "vi-1")
        self.assertEqual(package["english_items"][0]["repo"], "fluent")
        self.assertIn("en-1", package["item_refs"])
        self.assertEqual(package["learner"]["level"], "B1")

    def test_mixed_turn_merges_consecutive_languages_and_drops_short(self):
        self.write_codex_session("s-mixed", [
            "First English sentence here. Second English sentence here. Tôi muốn hỏi điều này. Tôi cần thêm chi tiết. Third English sentence here."
        ])
        self.write_codex_session("s-short", ["Hi there."])
        proc = self.prepare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        package = self.package()
        self.assertEqual(len(package["english_items"]), 2)
        self.assertEqual(len(package["vietnamese_items"]), 1)
        self.assertIn("First English", package["english_items"][0]["text"])
        self.assertIn("Second English", package["english_items"][0]["text"])
        self.assertNotIn("Hi there", json.dumps(package))

    def test_window_from_last_end_and_seven_day_cap(self):
        self.write_state("2026-01-14T10:00:00+07:00")
        proc = self.prepare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        package = self.package()
        self.assertEqual(package["window"]["start"], "2026-01-14T10:00:00+07:00")

        other = self.root / "other-data"
        other.mkdir()
        self.data, old_data = other, self.data
        self.base = other / "chat-review"
        self.write_state("2026-01-01T00:00:00+07:00")
        proc = self.prepare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        package = self.package()
        self.assertEqual(package["window"]["start"], "2026-01-08T20:30:00+07:00")
        self.assertTrue(package["window"]["capped_to_7_days"])
        self.data = old_data
        self.base = old_data / "chat-review"

    def test_dry_run_writes_nothing(self):
        self.write_codex_session("s-dry", ["This is a simple English sentence."])
        before = set(self.data.rglob("*"))
        proc = self.run_cli("prepare", "--home", self.home, "--no-orca", "--now", NOW, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status: dry_run", proc.stdout)
        self.assertIn("english_items: 1", proc.stdout)
        after = set(self.data.rglob("*"))
        self.assertEqual(before, after)

    def test_zero_item_auto_finalize_and_digest_show(self):
        proc = self.prepare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status: done", proc.stdout)
        self.assertIn("Không có lỗi mới.", proc.stdout)
        self.assertIn("Chưa có ý định nào lặp lại đủ 2 lần.", proc.stdout)
        run = self.latest_run()
        self.assertEqual(run["status"], "done")
        self.assertTrue((self.base / "digests" / (run["digest_id"] + ".json")).exists())
        show = self.run_cli("digest", "show", run["digest_id"])
        self.assertEqual(show.returncode, 0)
        self.assertIn("Trả lời", show.stdout)

    def test_ingest_success_digest_rendering_and_state(self):
        package = self.simple_prepare()
        analysis = self.simple_analysis(package)
        proc = self.ingest_analysis(analysis)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("E1.", proc.stdout)
        self.assertIn("**Cách diễn đạt đề xuất**", proc.stdout)
        self.assertIn("Nguồn: codex 1", proc.stdout)
        self.assertIn("Đang chờ:", proc.stdout)
        self.assertEqual(self.state()["last_end"], package["window"]["end"])
        digest = self.load_json(self.base / "digests" / (self.latest_run()["digest_id"] + ".json"))
        self.assertEqual(digest["status"], "awaiting_approval")
        self.assertEqual(digest["english"][0]["key"], "E1")
        self.assertEqual(digest["english"][0]["count"], 1)
        intent = list(self.intents().values())[0]
        self.assertEqual(intent["status"], "pending")
        self.assertEqual(len(intent["occurrences"]), 1)
        self.assertEqual(len(intent["evidence"]), 1)

    def test_validation_failures_and_state_not_advanced(self):
        cases = []
        package = self.simple_prepare()
        valid = self.simple_analysis(package)

        def clone():
            return json.loads(json.dumps(valid))

        bad = clone(); bad["english_errors"][0]["quote"] = "not in text"; cases.append(("bad quote", bad, "exact substring"))
        bad = clone(); bad["skipped"] = []; bad["intents"] = []; bad["english_errors"] = []; cases.append(("missing", bad, "missing from analysis"))
        bad = clone(); bad["skipped"] = [{"item_id": bad["intents"][0]["item_id"], "reason": "x"}]; cases.append(("duplicate", bad, "appears in both"))
        bad = clone(); bad["intents"][0]["item_id"] = bad["english_errors"][0]["item_id"]; cases.append(("en vi cross", bad, "non-Vietnamese"))
        bad = clone(); bad["intents"][0]["intent_id"] = "missing_intent"; bad["intents"][0].pop("new_intent"); cases.append(("unknown intent", bad, "unknown intent_id"))
        bad = clone(); bad["expressions"] = []; cases.append(("missing expression", bad, "missing expression"))
        bad = clone(); bad["expressions"][0]["scenario"] = "Please investigate the issue and wait for my approval before editing in this situation."; cases.append(("scenario leak", bad, "4-word"))

        for name, analysis, expected in cases:
            with self.subTest(name=name):
                proc = self.ingest_analysis(analysis)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn(expected, proc.stderr)
                self.assertIsNone(self.state().get("last_end"))

    def test_expression_for_approved_intent_is_invalid(self):
        intent_id = "int_existing_abc123"
        self.write_intents({intent_id: {
            "summary": "investigate before editing", "audience": "coding agent", "constraints": [],
            "status": "approved", "occurrences": [], "evidence": [], "counting_since": None,
            "expression": None, "card_id": "card", "created_run": "old", "updated_run": "old",
        }})
        package = self.simple_prepare()
        analysis = self.simple_analysis(package)
        vi = analysis["intents"][0]
        vi["intent_id"] = intent_id
        vi.pop("new_intent")
        analysis["expressions"] = [self.valid_expression(intent_id)]
        proc = self.ingest_analysis(analysis)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("forbidden for approved", proc.stderr)

    def write_two_vi_sessions(self, same_session=False, dates=None):
        dates = dates or (self.day, self.day)
        first = local_dt(dates[0], 10)
        second = local_dt(dates[1], 11)
        if same_session:
            self.write_codex_session("s-vi", [
                ("Tôi muốn bạn kiểm tra vấn đề này trước khi sửa.", first),
                ("Tôi cần bạn xem xét vấn đề này trước khi chỉnh sửa.", second),
            ])
        else:
            self.write_codex_session("s-vi-a", [("Tôi muốn bạn kiểm tra vấn đề này trước khi sửa.", first)])
            self.write_codex_session("s-vi-b", [("Tôi cần bạn xem xét vấn đề này trước khi chỉnh sửa.", second)])

    def two_vi_analysis(self, package):
        vi1, vi2 = [row["item_id"] for row in package["vietnamese_items"]]
        return {
            "run_id": package["run_id"],
            "english_errors": [],
            "intents": [
                {"item_id": vi1, "intent_id": None, "new_intent": {
                    "summary": "investigate a problem before editing", "audience": "coding agent",
                    "constraints": ["wait for approval"]}, "excerpt": "kiểm tra vấn đề"},
                {"item_id": vi2, "intent_id": "new:%s" % vi1, "excerpt": "xem xét vấn đề"},
            ],
            "expressions": [self.valid_expression("new:%s" % vi1)],
            "skipped": [{"item_id": row["item_id"], "reason": "no errors"} for row in package.get("english_items", [])],
        }

    def test_two_sessions_same_new_intent_proposed_and_rendered(self):
        self.write_two_vi_sessions()
        proc = self.prepare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        package = self.package()
        proc = self.ingest_analysis(self.two_vi_analysis(package))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        intent = list(self.intents().values())[0]
        self.assertEqual(intent["status"], "proposed")
        self.assertIn("V1. Tình huống:", proc.stdout)
        digest = self.load_json(self.base / "digests" / (self.latest_run()["digest_id"] + ".json"))
        self.assertEqual(len(digest["expressions"]), 1)
        self.assertEqual(digest["expressions"][0]["occurrences"], 2)

    def test_same_session_same_day_twice_stays_pending(self):
        self.write_two_vi_sessions(same_session=True)
        self.prepare()
        proc = self.ingest_analysis(self.two_vi_analysis(self.package()))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        intent = list(self.intents().values())[0]
        self.assertEqual(intent["status"], "pending")
        self.assertNotIn("V1. Tình huống:", proc.stdout)

    def test_distinct_dates_same_session_proposed(self):
        self.write_state("2026-01-13T20:30:00+07:00")
        dates = (datetime(2026, 1, 14).date(), self.day)
        self.write_two_vi_sessions(same_session=True, dates=dates)
        self.prepare()
        proc = self.ingest_analysis(self.two_vi_analysis(self.package()))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        intent = list(self.intents().values())[0]
        self.assertEqual(intent["status"], "proposed")

    def test_same_run_duplicate_hash_becomes_cached_reference(self):
        text = "Tôi muốn bạn kiểm tra vấn đề này trước khi sửa."
        self.write_codex_session("s-dup-1", [text])
        self.write_codex_session("s-dup-2", [text])
        proc = self.prepare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        package = self.package()
        self.assertEqual(len(package["vietnamese_items"]), 1)
        self.assertEqual(len(package["cached_items"]), 1)
        vi = package["vietnamese_items"][0]["item_id"]
        analysis = {
            "run_id": package["run_id"],
            "english_errors": [],
            "intents": [{"item_id": vi, "intent_id": None,
                         "new_intent": {"summary": "inspect before changing anything", "audience": "agents", "constraints": ["check first"]},
                         "excerpt": "kiểm tra vấn đề này"}],
            "expressions": [self.valid_expression("new:%s" % vi)],
            "skipped": [],
        }
        proc = self.ingest_analysis(analysis)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        intents = self.intents()
        self.assertEqual(len(intents), 1)
        intent = list(intents.values())[0]
        self.assertEqual(len(intent["occurrences"]), 2)
        self.assertEqual(len({row["session_key"] for row in intent["occurrences"]}), 2)
        self.assertEqual(intent["status"], "proposed")

    def test_cached_items_excluded_and_reapplied(self):
        package = self.simple_prepare()
        analysis = self.simple_analysis(package)
        proc = self.ingest_analysis(analysis)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        intent_id = list(self.intents())[0]

        self.write_codex_session("s-cache", [
            ("I goes to work everyday. Tôi muốn bạn kiểm tra vấn đề này trước khi sửa.", local_dt(self.day, 20, 45))
        ])
        proc = self.prepare("2026-01-15T21:30:00+07:00")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status: done", proc.stdout)
        run = self.latest_run()
        package2 = self.package(run["run_id"])
        self.assertEqual(package2["english_items"], [])
        self.assertEqual(package2["vietnamese_items"], [])
        self.assertEqual(len(package2["cached_items"]), 2)
        self.assertEqual(len(self.intents()[intent_id]["occurrences"]), 2)
        digest2 = self.load_json(self.base / "digests" / (run["digest_id"] + ".json"))
        self.assertEqual(len(digest2["english"]), 1)

    def test_fourteen_day_expiry(self):
        intent_id = "int_old_abc123"
        old_ref = {"source": "codex", "session_id": "old", "turn_id": "old:1", "timestamp": "2025-12-20T10:00:00+07:00"}
        self.write_intents({intent_id: {
            "summary": "old intent", "audience": "coding agent", "constraints": [], "status": "pending",
            "occurrences": [{"session_key": "codex:old", "date": "2025-12-20", "turn_ref": old_ref}],
            "evidence": [], "counting_since": None, "expression": self.valid_expression("x"),
            "card_id": None, "created_run": "old", "updated_run": "old"}})
        proc = self.prepare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.intents()[intent_id]["status"], "expired")

    def test_approved_intent_evidence_only_no_v_item(self):
        intent_id = "int_existing_abc123"
        self.write_intents({intent_id: {
            "summary": "investigate before editing", "audience": "coding agent", "constraints": [],
            "status": "approved", "occurrences": [], "evidence": [], "counting_since": None,
            "expression": self.valid_expression("x"), "card_id": "card", "created_run": "old", "updated_run": "old"}})
        self.write_codex_session("s-approved", ["Tôi muốn bạn kiểm tra vấn đề này trước khi sửa."])
        self.prepare()
        package = self.package()
        vi = package["vietnamese_items"][0]["item_id"]
        analysis = {"run_id": package["run_id"], "english_errors": [], "intents": [
            {"item_id": vi, "intent_id": intent_id, "excerpt": "kiểm tra vấn đề"}], "expressions": [], "skipped": []}
        proc = self.ingest_analysis(analysis)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.intents()[intent_id]["occurrences"]), 1)
        digest = self.load_json(self.base / "digests" / (self.latest_run()["digest_id"] + ".json"))
        self.assertEqual(digest["expressions"], [])

    def make_decided_digest(self):
        self.write_two_vi_sessions()
        self.write_codex_session("s-en-for-approve", ["I goes home now."])
        self.prepare()
        package = self.package()
        analysis = self.two_vi_analysis(package)
        en_id = package["english_items"][0]["item_id"]
        analysis["skipped"] = [row for row in analysis["skipped"] if row["item_id"] != en_id]
        analysis["english_errors"] = [{
            "item_id": en_id, "quote": "I goes", "correction": "I go",
            "pattern_id": "subject_verb_agreement", "pattern_summary": "Subject and verb do not agree",
            "category": "grammar", "subcategory": "subject_verb_agreement", "severity": "high",
            "explanation_vi": "Chủ ngữ I cần động từ nguyên mẫu go."}]
        proc = self.ingest_analysis(analysis)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.latest_run()["digest_id"]

    def test_approve_all_idempotent_and_conflict(self):
        digest_id = self.make_decided_digest()
        proc = self.run_cli("approve", "--digest", digest_id, "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        intent = list(self.intents().values())[0]
        self.assertEqual(intent["status"], "approved")
        self.assertTrue(intent["card_id"].startswith("chat_expr_"))
        again = self.run_cli("approve", "--digest", digest_id, "--all")
        self.assertEqual(again.returncode, 0)
        self.assertIn("already approved", again.stdout)
        conflict = self.run_cli("approve", "--digest", digest_id, "--select", "V1")
        self.assertEqual(conflict.returncode, 2)

    def test_approve_select_rejects_unselected(self):
        digest_id = self.make_decided_digest()
        bad = self.run_cli("approve", "--digest", digest_id, "--select", "NONEXISTENT")
        self.assertEqual(bad.returncode, 2)
        proc = self.run_cli("approve", "--digest", digest_id, "--select", "V1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        digest = self.load_json(self.base / "digests" / (digest_id + ".json"))
        self.assertEqual(digest["decision"]["selected"], ["V1"])
        self.assertEqual(digest["decision"]["rejected"], ["E1"])
        intent = list(self.intents().values())[0]
        self.assertEqual(intent["status"], "approved")

    def test_approve_reject_resets_intent_counting(self):
        digest_id = self.make_decided_digest()
        proc = self.run_cli("approve", "--digest", digest_id, "--all", "--reject", "V1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        digest = self.load_json(self.base / "digests" / (digest_id + ".json"))
        self.assertEqual(digest["decision"]["selected"], ["E1"])
        self.assertEqual(digest["decision"]["rejected"], ["V1"])
        intent = list(self.intents().values())[0]
        self.assertEqual(intent["status"], "pending")
        self.assertIsNotNone(intent["counting_since"])

    def test_approve_reject_only_leaves_other_items_undecided(self):
        digest_id = self.make_decided_digest()
        proc = self.run_cli("approve", "--digest", digest_id, "--reject", "V1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        digest = self.load_json(self.base / "digests" / (digest_id + ".json"))
        self.assertEqual(digest["decision"]["selected"], [])
        self.assertEqual(digest["decision"]["rejected"], ["V1"])
        intent = list(self.intents().values())[0]
        self.assertEqual(intent["status"], "pending")

    def test_reingest_same_run_noop(self):
        package = self.simple_prepare()
        analysis = self.simple_analysis(package)
        proc = self.ingest_analysis(analysis)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        before_intents = (self.base / "intents.json").read_text(encoding="utf-8")
        proc = self.ingest_analysis(analysis)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("already ingested", proc.stdout)
        self.assertEqual(before_intents, (self.base / "intents.json").read_text(encoding="utf-8"))

    def test_lock_contention_clear_error(self):
        self.base.mkdir(parents=True, exist_ok=True)
        handle = open(str(self.base / ".lock"), "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            proc = self.prepare()
            self.assertEqual(proc.returncode, 2)
            self.assertIn("another chat-review operation is running", proc.stderr)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def test_status(self):
        proc = self.run_cli("status")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("last_end:", proc.stdout)
        self.assertIn("intents:", proc.stdout)
        self.assertIn("latest_digest_status:", proc.stdout)


if __name__ == "__main__":
    unittest.main()

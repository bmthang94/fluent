#!/usr/bin/env python3
import copy
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SCRIPT = REPO_ROOT / ".claude" / "hooks" / "update-db.py"
READ_SCRIPT = REPO_ROOT / ".claude" / "hooks" / "read-db.py"
DAILY_SCRIPT = REPO_ROOT / ".claude" / "hooks" / "daily-chat-review.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DAILY_TESTS = load_module("daily_review_test_helpers", REPO_ROOT / "tests" / "test_daily_chat_review.py")
UPDATE_TESTS = load_module("update_db_test_helpers", REPO_ROOT / "tests" / "test_update_db.py")


class ChatImportTest(DAILY_TESTS.DailyChatReviewTest):
    def setUp(self):
        super().setUp()
        UPDATE_TESTS.make_fixtures(self.data)

    def read_db_file(self, name):
        return json.loads((self.data / name).read_text(encoding="utf-8"))

    def write_db_file(self, name, value):
        (self.data / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def snapshot_db(self):
        return {p.name: p.read_bytes() for p in sorted(self.data.glob("*.json"))}

    def approve(self, digest_id, *args):
        return self.run_cli("approve", "--digest", digest_id, *args)

    def import_digest(self, digest_id):
        return self.run_cli("import", "--digest", digest_id)

    def decided_digest(self):
        return self.make_decided_digest()

    def test_approve_all_imports_mistakes_cards_backup_and_metadata(self):
        digest_id = self.decided_digest()
        before_untouched = {name: (self.data / name).read_bytes() for name in (
            "learner-profile.json", "progress-db.json", "mastery-db.json", "session-log.json")}
        intent_id = list(self.intents())[0]
        card_id = "chat_expr_" + intent_id
        digest = self.load_json(self.base / "digests" / (digest_id + ".json"))
        scenario = digest["expressions"][0]["scenario"]
        answer = digest["expressions"][0]["model_answer"]

        proc = self.approve(digest_id, "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Đã lưu", proc.stdout)

        mistakes = self.read_db_file("mistakes-db.json")
        pattern = mistakes["error_patterns"]["subject_verb_agreement"]
        self.assertEqual(pattern["frequency"], 1)
        self.assertEqual(len(pattern["examples"]), 1)
        self.assertEqual(pattern["examples"][0]["incorrect"], "I goes")
        self.assertIn(digest_id, mistakes["metadata"]["chat_imports"])

        sr = self.read_db_file("spaced-repetition.json")
        self.assertIn("subject_verb_agreement", sr["items"])
        self.assertEqual(sr["items"]["subject_verb_agreement"]["type"], "error_pattern")
        card = sr["items"][card_id]
        self.assertEqual(card["content"], scenario)
        self.assertEqual(card["answer"], answer)
        self.assertEqual(card["category"], "chat_expression")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertEqual(card["due_date"], tomorrow)
        self.assertIn(digest_id, sr["metadata"]["chat_imports"])

        backup = self.data / ".backups" / ("pre-chat-import-" + digest_id)
        self.assertTrue((backup / "mistakes-db.json").exists())
        self.assertTrue((backup / "spaced-repetition.json").exists())
        for name, content in before_untouched.items():
            self.assertEqual((self.data / name).read_bytes(), content, name)
        digest_after = self.load_json(self.base / "digests" / (digest_id + ".json"))
        self.assertEqual(digest_after["status"], "imported")
        self.assertEqual(digest_after["import_summary"]["errors"], 1)
        self.assertEqual(digest_after["import_summary"]["new_cards"], 1)

    def test_repeated_approve_and_import_are_idempotent(self):
        digest_id = self.decided_digest()
        self.assertEqual(self.approve(digest_id, "--all").returncode, 0)
        first = (self.read_db_file("mistakes-db.json"), self.read_db_file("spaced-repetition.json"))
        proc = self.approve(digest_id, "--all")
        self.assertEqual(proc.returncode, 0)
        proc = self.import_digest(digest_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        mistakes, sr = self.read_db_file("mistakes-db.json"), self.read_db_file("spaced-repetition.json")
        self.assertEqual(mistakes["error_patterns"]["subject_verb_agreement"]["frequency"], 1)
        self.assertEqual(len([k for k in sr["items"] if k.startswith("chat_expr_")]), 1)
        self.assertEqual((mistakes, sr), first)

    def test_existing_card_schedule_is_preserved(self):
        digest_id = self.decided_digest()
        intent_id = list(self.intents())[0]
        card_id = "chat_expr_" + intent_id
        sr = self.read_db_file("spaced-repetition.json")
        sr["items"][card_id] = {
            "id": card_id, "type": "vocabulary", "content": "old", "answer": "old",
            "category": "chat_expression", "difficulty": "A1", "created_date": "2026-01-01",
            "due_date": "2026-03-03", "interval_days": 17, "repetitions": 4,
            "easiness_factor": 2.2, "consecutive_correct": 3, "consecutive_incorrect": 0,
            "last_reviewed": "2026-02-01", "last_quality": 5, "mastery_level": 3,
            "total_reviews": 4, "priority": "low",
        }
        self.write_db_file("spaced-repetition.json", sr)
        proc = self.approve(digest_id, "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        card = self.read_db_file("spaced-repetition.json")["items"][card_id]
        self.assertEqual(card["due_date"], "2026-03-03")
        self.assertEqual(card["interval_days"], 17)
        digest = self.load_json(self.base / "digests" / (digest_id + ".json"))
        self.assertEqual(digest["import_summary"]["existing_cards"], 1)
        self.assertEqual(digest["import_summary"]["new_cards"], 0)

    def test_existing_pattern_frequency_increments_once_per_digest_group(self):
        package = self.simple_prepare()
        en = package["english_items"][0]["item_id"]
        vi = package["vietnamese_items"][0]["item_id"]
        error = {
            "item_id": en, "quote": "I goes", "correction": "I go",
            "pattern_id": "subject_verb_agreement", "pattern_summary": "Subject verb agreement",
            "category": "grammar", "subcategory": "subject_verb_agreement", "severity": "high",
            "explanation_vi": "Giải thích."}
        analysis = {"run_id": package["run_id"], "english_errors": [dict(error), dict(error)],
                    "intents": [], "expressions": [], "skipped": [{"item_id": vi, "reason": "no reusable communicative intent"}]}
        proc = self.ingest_analysis(analysis)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        digest_id = self.latest_run()["digest_id"]
        mistakes = self.read_db_file("mistakes-db.json")
        mistakes["error_patterns"]["subject_verb_agreement"] = {
            "category": "grammar", "subcategory": "subject_verb_agreement", "description": "old",
            "severity": "high", "frequency": 5, "mastery_level": 1, "examples": [],
        }
        self.write_db_file("mistakes-db.json", mistakes)
        proc = self.approve(digest_id, "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pattern = self.read_db_file("mistakes-db.json")["error_patterns"]["subject_verb_agreement"]
        self.assertEqual(pattern["frequency"], 6)

    def test_select_subset_imports_only_selected_and_rejects_v(self):
        digest_id = self.decided_digest()
        proc = self.approve(digest_id, "--select", "E1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        mistakes = self.read_db_file("mistakes-db.json")
        sr = self.read_db_file("spaced-repetition.json")
        self.assertIn("subject_verb_agreement", mistakes["error_patterns"])
        self.assertFalse(any(k.startswith("chat_expr_") for k in sr["items"]))

    def test_chat_imports_present_in_only_one_file_errors_without_writes(self):
        digest_id = self.decided_digest()
        mistakes = self.read_db_file("mistakes-db.json")
        mistakes.setdefault("metadata", {})["chat_imports"] = [digest_id]
        self.write_db_file("mistakes-db.json", mistakes)
        before_sr = (self.data / "spaced-repetition.json").read_bytes()
        before_mistakes = (self.data / "mistakes-db.json").read_bytes()
        proc = self.approve(digest_id, "--all")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("manual check", proc.stderr)
        self.assertEqual((self.data / "spaced-repetition.json").read_bytes(), before_sr)
        self.assertEqual((self.data / "mistakes-db.json").read_bytes(), before_mistakes)

    def test_db_lock_blocks_update_and_import_then_release_succeeds(self):
        digest_id = self.decided_digest()
        lock_path = self.data / ".fluent-db.lock"
        lock_path.touch()
        handle = open(str(lock_path), "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        env = os.environ.copy()
        env["FLUENT_DATA_DIR"] = str(self.data)
        env["FLUENT_DB_LOCK_TIMEOUT"] = "1"
        try:
            proc = subprocess.run([sys.executable, str(UPDATE_SCRIPT), json.dumps(UPDATE_TESTS.SESSION_PAYLOAD)],
                                  capture_output=True, env=env)
            self.assertEqual(proc.returncode, 2)
            self.assertIn(b"database lock", proc.stderr)
            before = (self.data / "mistakes-db.json").read_bytes()
            proc = self.approve(digest_id, "--all")
            self.assertEqual(proc.returncode, 2)
            self.assertEqual((self.data / "mistakes-db.json").read_bytes(), before)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        proc = subprocess.run([sys.executable, str(UPDATE_SCRIPT), json.dumps(UPDATE_TESTS.SESSION_PAYLOAD)],
                              capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.approve(digest_id, "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("subject_verb_agreement", self.read_db_file("mistakes-db.json")["error_patterns"])

    def test_replacement_guard_blocks_resend_after_chat_import(self):
        payload = copy.deepcopy(UPDATE_TESTS.SESSION_PAYLOAD)
        proc = subprocess.run([sys.executable, str(UPDATE_SCRIPT), json.dumps(payload)],
                              cwd=self.root, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run([sys.executable, str(UPDATE_SCRIPT), json.dumps(payload)],
                              cwd=self.root, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        digest_id = self.decided_digest()
        self.assertEqual(self.approve(digest_id, "--all").returncode, 0)
        before = self.snapshot_db()
        proc = subprocess.run([sys.executable, str(UPDATE_SCRIPT), json.dumps(payload)],
                              cwd=self.root, capture_output=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"chat review was imported", proc.stderr)
        self.assertEqual(self.snapshot_db(), before)

    def test_read_db_due_review_contains_chat_expression(self):
        digest_id = self.decided_digest()
        self.assertEqual(self.approve(digest_id, "--all").returncode, 0)
        sr = self.read_db_file("spaced-repetition.json")
        card_id = next(k for k in sr["items"] if k.startswith("chat_expr_"))
        today = datetime.now().strftime("%Y-%m-%d")
        sr["items"][card_id]["due_date"] = today
        self.write_db_file("spaced-repetition.json", sr)
        env = os.environ.copy(); env["FLUENT_DATA_DIR"] = str(self.data)
        proc = subprocess.run([sys.executable, str(READ_SCRIPT)], capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn(card_id, payload["computed"]["due_review_items"])

    def digest_json(self, digest_id):
        return self.load_json(self.base / "digests" / (digest_id + ".json"))

    def seed_proposed_intent(self):
        intents = self.intents()
        intents["int_seed"] = {
            "summary": "inspect before editing", "audience": "coding agent",
            "constraints": ["investigate first"], "status": "proposed",
            "occurrences": [
                {"session_key": "codex:a", "date": "2026-01-15",
                 "turn_ref": {"source": "codex", "session_id": "a", "turn_id": "t1", "timestamp": "2026-01-15T03:00:00Z"}},
                {"session_key": "codex:b", "date": "2026-01-15",
                 "turn_ref": {"source": "codex", "session_id": "b", "turn_id": "t1", "timestamp": "2026-01-15T03:05:00Z"}},
            ],
            "evidence": ["kiểm tra trước"],
            "counting_since": None,
            "expression": {
                "scenario": "A production job failed and you want diagnosis before any edit. What do you say?",
                "model_answer": "Please investigate the failure first and wait for my approval before editing.",
                "note_vi": "Nêu rõ cần chẩn đoán trước khi sửa.",
            },
            "card_id": None, "created_run": "seed", "updated_run": "seed",
        }
        self.write_intents(intents)

    def make_auto_digest(self):
        day = datetime(2026, 1, 16).date()
        dt = DAILY_TESTS.local_dt(day, 10)
        self.write_codex_session("s-auto-e1", ["I goes home now."], dt=dt)
        self.write_codex_session("s-auto-e2", ["She don't know yet."], dt=dt)
        proc = self.prepare("2026-01-16T20:30:00+07:00")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        package = self.package()
        errors = []
        for index, item in enumerate(package["english_items"], 1):
            first = "I goes" in item.get("text", "")
            quote = "I goes" if first else "don't"
            correction = "I go" if first else "doesn't"
            pattern = "subject_verb_agreement" if first else "auxiliary_agreement"
            errors.append({
                "item_id": item["item_id"], "quote": quote, "correction": correction,
                "pattern_id": pattern, "pattern_summary": pattern.replace("_", " "),
                "category": "grammar", "subcategory": pattern, "severity": "high",
                "explanation_vi": "Giải thích ngắn."})
        analysis = {"run_id": package["run_id"], "english_errors": errors,
                    "intents": [], "expressions": [], "skipped": []}
        proc = self.ingest_analysis(analysis)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.latest_run()["digest_id"]

    def enable_auto_mode(self):
        digest_id = self.decided_digest()
        self.assertNotIn("auto_approve_at", self.digest_json(digest_id))
        proc = self.approve(digest_id, "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.state().get("auto_approve_enabled_at"))

    def test_auto_mode_footer_reject_accumulation_and_delayed_import(self):
        self.enable_auto_mode()
        self.seed_proposed_intent()
        digest_id = self.make_auto_digest()
        digest = self.digest_json(digest_id)
        self.assertEqual(digest.get("auto_approve_at"), "2026-01-17T20:30:00+07:00")
        rendered = self.run_cli("digest", "show", digest_id).stdout
        self.assertIn('Tự lưu lúc 20:30 17/01 trừ mục anh trả lời "bỏ …"', rendered)

        proc = self.approve(digest_id, "--reject", "E1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Đã bỏ E1", proc.stdout)
        proc = self.approve(digest_id, "--reject", "E2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        digest = self.digest_json(digest_id)
        self.assertEqual(digest["status"], "awaiting_approval")
        self.assertEqual(digest["decision"]["rejected"], ["E1", "E2"])

        proc = self.prepare("2026-01-17T19:29:00+07:00")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Đã tự lưu bản", proc.stdout)
        self.assertEqual(self.digest_json(digest_id)["status"], "awaiting_approval")

        proc = self.prepare("2026-01-17T19:30:00+07:00")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("\nĐã tự lưu bản %s:" % digest_id, proc.stdout)
        note_pos = proc.stdout.index("Đã tự lưu bản %s:" % digest_id)
        self.assertLess(note_pos, proc.stdout.index("Fluent · chat"))
        digest = self.digest_json(digest_id)
        self.assertEqual(digest["status"], "imported")
        self.assertTrue(digest["decision"].get("auto"))
        self.assertEqual(digest["decision"]["selected"], ["V1"])
        mistakes = self.read_db_file("mistakes-db.json")
        self.assertNotIn("auxiliary_agreement", mistakes.get("error_patterns", {}))
        sr = self.read_db_file("spaced-repetition.json")
        self.assertIn("chat_expr_int_seed", sr["items"])
        latest = self.digest_json(self.latest_run()["digest_id"])
        self.assertTrue(latest.get("auto_saved_notes"))
        self.assertTrue(latest["auto_saved_notes"][0].startswith("Đã tự lưu bản %s:" % digest_id))

    def test_explicit_all_on_auto_digest_imports_immediately_minus_rejections(self):
        self.enable_auto_mode()
        self.seed_proposed_intent()
        digest_id = self.make_auto_digest()
        self.assertEqual(self.approve(digest_id, "--reject", "E1").returncode, 0)
        proc = self.approve(digest_id, "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        digest = self.digest_json(digest_id)
        self.assertEqual(digest["status"], "imported")
        self.assertEqual(digest["decision"]["selected"], ["E2", "V1"])
        mistakes = self.read_db_file("mistakes-db.json")
        self.assertNotIn("auxiliary_agreement", mistakes["error_patterns"])
        self.assertEqual(mistakes["error_patterns"]["subject_verb_agreement"]["frequency"], 2)
        # E1 was auxiliary_agreement in this digest and was rejected; selected E2 is the
        # subject/verb pattern, adding one frequency to the enabling digest's first row.

    def test_auto_saved_note_is_rendered_by_later_ingest(self):
        self.enable_auto_mode()
        self.seed_proposed_intent()
        digest_id = self.make_auto_digest()
        day = datetime(2026, 1, 17).date()
        self.write_codex_session("s-next-ingest", ["He don't understand it."], dt=DAILY_TESTS.local_dt(day, 10))
        proc = self.prepare("2026-01-17T19:30:00+07:00")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status: needs_analysis", proc.stdout)
        package = self.package()
        item = package["english_items"][0]["item_id"]
        analysis = {"run_id": package["run_id"], "english_errors": [{
            "item_id": item, "quote": "don't", "correction": "doesn't",
            "pattern_id": "auxiliary_agreement", "pattern_summary": "Auxiliary agreement",
            "category": "grammar", "subcategory": "auxiliary_agreement", "severity": "high",
            "explanation_vi": "Giải thích ngắn."}], "intents": [], "expressions": [], "skipped": []}
        proc = self.ingest_analysis(analysis)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        note_pos = proc.stdout.index("Đã tự lưu bản %s:" % digest_id)
        self.assertLess(note_pos, proc.stdout.index("Fluent · chat"))
        self.assertIn("2 lỗi tiếng Anh, 1 thẻ cách diễn đạt", proc.stdout)
        latest = self.digest_json(self.latest_run()["digest_id"])
        self.assertTrue(latest.get("auto_saved_notes"))
        self.assertEqual(self.digest_json(digest_id)["status"], "imported")
        mistakes = self.read_db_file("mistakes-db.json")
        self.assertEqual(mistakes["error_patterns"]["subject_verb_agreement"]["frequency"], 2)
        self.assertEqual(mistakes["error_patterns"]["auxiliary_agreement"]["frequency"], 1)

    def test_auto_import_failure_leaves_approved_not_imported_and_prepare_succeeds(self):
        self.enable_auto_mode()
        self.seed_proposed_intent()
        digest_id = self.make_auto_digest()
        lock_path = self.data / ".fluent-db.lock"
        lock_path.touch()
        handle = open(str(lock_path), "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        env = os.environ.copy()
        env["FLUENT_DATA_DIR"] = str(self.data)
        env["FLUENT_DB_LOCK_TIMEOUT"] = "1"
        try:
            proc = subprocess.run([sys.executable, str(DAILY_SCRIPT), "prepare", "--home", str(self.home),
                                   "--no-orca", "--now", "2026-01-17T19:30:00+07:00"],
                                  capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Chưa lưu được bản %s" % digest_id, proc.stdout)
            digest = self.digest_json(digest_id)
            self.assertEqual(digest["status"], "approved")
            self.assertNotIn(digest_id, self.read_db_file("mistakes-db.json").get("metadata", {}).get("chat_imports", []))
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        proc = self.import_digest(digest_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.digest_json(digest_id)["status"], "imported")

    def test_crash_during_second_save_is_atomic_and_retry_succeeds(self):
        digest_id = self.decided_digest()
        lock_path = self.data / ".fluent-db.lock"; lock_path.touch()
        handle = open(str(lock_path), "a+"); fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            proc = self.approve(digest_id, "--all")
            self.assertEqual(proc.returncode, 2)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN); handle.close()
        before_mistakes = (self.data / "mistakes-db.json").read_bytes()
        before_sr = (self.data / "spaced-repetition.json").read_bytes()
        code = r'''
import importlib.util, json, sys
from pathlib import Path
orig = json.dump
calls = []
def flaky(*args, **kwargs):
    calls.append(1)
    if len(calls) == 2:
        raise RuntimeError("simulated staging crash")
    return orig(*args, **kwargs)
json.dump = flaky
path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("daily_review", str(path))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.exit(module.main(["import", "--digest", sys.argv[2]]))
'''
        env = os.environ.copy(); env["FLUENT_DATA_DIR"] = str(self.data)
        proc = subprocess.run([sys.executable, "-c", code, str(DAILY_SCRIPT), digest_id], capture_output=True, env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual((self.data / "mistakes-db.json").read_bytes(), before_mistakes)
        self.assertEqual((self.data / "spaced-repetition.json").read_bytes(), before_sr)
        digest = self.load_json(self.base / "digests" / (digest_id + ".json"))
        self.assertNotEqual(digest.get("status"), "imported")
        proc = self.import_digest(digest_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        mistakes = self.read_db_file("mistakes-db.json")
        self.assertEqual(mistakes["error_patterns"]["subject_verb_agreement"]["frequency"], 1)
        self.assertIn(digest_id, mistakes["metadata"]["chat_imports"])


# The base class is reused as a fixture/helper provider; its test cases run in
# tests/test_daily_chat_review.py. Hide the inherited test methods from unittest
# collection here so this file exercises only the import-specific cases.
for _test_name in list(vars(DAILY_TESTS.DailyChatReviewTest)):
    if _test_name.startswith("test_"):
        setattr(ChatImportTest, _test_name, None)


if __name__ == "__main__":
    unittest.main()

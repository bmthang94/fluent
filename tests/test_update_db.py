#!/usr/bin/env python3
"""
Smoke test for .claude/hooks/update-db.py.

Runs the script against a fresh fixture DB in a temp dir, feeds it a sample
session report, and asserts schema invariants on the output files.

Usage:
    python3 tests/test_update_db.py
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".claude" / "hooks" / "update-db.py"
READ_SCRIPT = REPO_ROOT / ".claude" / "hooks" / "read-db.py"


def make_fixtures(data_dir: Path):
    (data_dir / "learner-profile.json").write_text(json.dumps({
        "learner": {"name": "Test", "target_language": "Dutch",
                    "current_level": "A1", "target_level": "A2"},
        "profile_created": "2026-04-20",
        "last_updated": "2026-04-23",
        "current_streak_days": 2,
        "total_sessions": 1,
        "total_study_minutes": 10,
        "skills": {
            "vocabulary": {"current_level": 1, "confidence": 60,
                           "last_practiced": "2026-04-23",
                           "total_practice_time": 10}
        },
        "focus_areas": [],
        "achievements": [],
        "preferences": {}
    }))
    (data_dir / "progress-db.json").write_text(json.dumps({
        "metadata": {"last_updated": "2026-04-23", "language": "Dutch",
                     "tracking_started": "2026-04-20"},
        "overall_stats": {"total_sessions": 1, "total_exercises": 4,
                          "total_correct": 3, "total_incorrect": 1,
                          "accuracy_rate": 0.75,
                          "total_study_minutes": 10,
                          "average_session_duration": 10},
        "accuracy_trend": [{"date": "2026-04-23", "accuracy": 0.75,
                            "exercises": 4}],
        "skill_progress": {
            "vocabulary": {"sessions": 1, "accuracy": 0.75,
                           "last_practiced": "2026-04-23",
                           "exercises_completed": 4, "correct_count": 3,
                           "incorrect_count": 1}
        },
        "weekly_summary": []
    }))
    (data_dir / "mistakes-db.json").write_text(json.dumps({
        "metadata": {"last_updated": "2026-04-23",
                     "total_patterns_tracked": 0, "language": "Dutch"},
        "error_patterns": {}
    }))
    (data_dir / "mastery-db.json").write_text(json.dumps({
        "metadata": {"last_updated": "2026-04-23", "language": "Dutch"},
        "skills": {
            "vocabulary": {"mastery_level": 1, "confidence_score": 0.75,
                           "total_practice_time": 10,
                           "last_practiced": "2026-04-23",
                           "practice_count": 4, "avg_accuracy": 0.75}
        },
        "patterns": {}
    }))
    (data_dir / "spaced-repetition.json").write_text(json.dumps({
        "metadata": {"algorithm": "SM-2", "last_updated": "2026-04-23",
                     "total_items_tracked": 1, "language": "Dutch"},
        "review_queue": {"today": [], "tomorrow": ["vocab_dag"],
                         "this_week": [], "later": []},
        "items": {
            "vocab_dag": {
                "id": "vocab_dag", "type": "vocabulary", "content": "dag",
                "answer": "day / hi-bye", "category": "greetings",
                "difficulty": "A1", "created_date": "2026-04-23",
                "due_date": "2026-04-24", "interval_days": 1,
                "repetitions": 1, "easiness_factor": 2.5,
                "consecutive_correct": 1, "consecutive_incorrect": 0,
                "last_reviewed": "2026-04-23", "last_quality": 4,
                "mastery_level": 1, "total_reviews": 1, "priority": "medium"
            }
        }
    }))
    (data_dir / "session-log.json").write_text(json.dumps({
        "metadata": {"language": "Dutch", "learner_name": "Test",
                     "total_sessions": 1},
        "sessions": [{
            "session_id": "session-001", "date": "2026-04-23",
            "duration_minutes": 10,
            "skills_practiced": ["vocabulary"],
            "exercises_completed": 4, "accuracy": 0.75,
            "score_breakdown": {"vocabulary": 0.75},
            "topics_covered": [], "breakthroughs": [],
            "focus_next_session": [], "notes": "",
            "achievements_earned": []
        }],
        "milestones": []
    }))


SESSION_PAYLOAD = {
    "session_id": "session-002",
    "date": "2026-04-24",
    "duration_minutes": 15,
    "command_used": "/fluent-learn",
    "skills_practiced": ["vocabulary"],
    "skill_scores": {
        "vocabulary": {"exercises": 5, "correct": 4, "time_minutes": 15}
    },
    "errors": [{
        "pattern_id": "verb_spreek",
        "category": "grammar",
        "subcategory": "verb_conjugation",
        "your_answer": "Hij spreek",
        "correct_answer": "Hij spreekt",
        "context": "3rd person",
        "severity": "critical",
        "difficulty_score": 0.7
    }],
    "new_vocabulary": [{
        "item_id": "het_huis",
        "item_type": "vocabulary",
        "content": "het huis",
        "answer": "the house",
        "category": "nouns",
        "difficulty": "A1",
        "initial_quality": 4
    }],
    "review_results": [{"item_id": "vocab_dag", "quality": 5}],
    "topics_covered": ["house_vocab"],
    "breakthroughs": ["Got 'het huis' on first try"],
    "focus_next_session": ["de/het drill"],
    "session_notes": "Good session.",
    "milestones": []
}


class UpdateDbSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fluent-test-"))
        (self.tmp / "data").mkdir()
        make_fixtures(self.tmp / "data")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, payload: dict):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(payload).encode(),
            cwd=str(self.tmp),
            capture_output=True,
        )
        return proc

    def test_happy_path(self):
        proc = self._run(SESSION_PAYLOAD)
        self.assertEqual(proc.returncode, 0,
                         msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")

        with open(self.tmp / "data" / "session-log.json", encoding="utf-8") as f:
            log = json.load(f)
        latest = log["sessions"][-1]
        self.assertEqual(latest["session_id"], "session-002")
        self.assertIn("skills_practiced", latest)
        self.assertIsInstance(latest["skills_practiced"], list)
        self.assertIn("score_breakdown", latest)
        self.assertIn("topics_covered", latest)
        self.assertIn("breakthroughs", latest)
        self.assertIn("focus_next_session", latest)
        self.assertIn("achievements_earned", latest)
        self.assertEqual(latest["streak_day"], 3)  # was 2, yesterday -> +1

        with open(self.tmp / "data" / "learner-profile.json", encoding="utf-8") as f:
            profile = json.load(f)
        self.assertEqual(profile["current_streak_days"], 3)
        conf = profile["skills"]["vocabulary"]["confidence"]
        self.assertIsInstance(conf, int)
        self.assertGreaterEqual(conf, 0)
        self.assertLessEqual(conf, 100)

        with open(self.tmp / "data" / "spaced-repetition.json", encoding="utf-8") as f:
            sr = json.load(f)
        dag = sr["items"]["vocab_dag"]
        # Schema preserved
        for k in ("consecutive_correct", "consecutive_incorrect",
                  "mastery_level", "total_reviews", "priority",
                  "content", "answer", "category", "difficulty"):
            self.assertIn(k, dag, f"lost field {k} on vocab_dag")
        self.assertEqual(dag["total_reviews"], 2)  # was 1, +1 review
        self.assertEqual(dag["last_quality"], 5)

        # New vocabulary item fully populated
        huis = sr["items"]["het_huis"]
        for k in ("id", "type", "content", "answer", "category",
                  "difficulty", "due_date", "interval_days", "repetitions",
                  "easiness_factor", "consecutive_correct",
                  "consecutive_incorrect", "mastery_level",
                  "total_reviews", "priority"):
            self.assertIn(k, huis, f"new item missing {k}")

        with open(self.tmp / "data" / "mistakes-db.json", encoding="utf-8") as f:
            mistakes = json.load(f)
        self.assertIn("verb_spreek", mistakes["error_patterns"])
        pat = mistakes["error_patterns"]["verb_spreek"]
        self.assertEqual(pat["consecutive_incorrect"], 1)
        self.assertEqual(pat["examples"][-1]["incorrect"], "Hij spreek")
        self.assertEqual(pat["examples"][-1]["correct"], "Hij spreekt")

        # Backup directory exists (nested inside data/ to avoid collisions
        # with other plugins when the global fallback ~/.claude/fluent-data is used).
        backup = self.tmp / "data" / ".backups" / "pre-update-session-002"
        self.assertTrue(backup.exists(), "pre-update backup missing")

    def test_payload_from_argv(self):
        """The payload may be passed as argv[1] instead of stdin (stdin is not
        always connected, e.g. when run as a background task)."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), json.dumps(SESSION_PAYLOAD)],
            stdin=subprocess.DEVNULL,
            cwd=str(self.tmp),
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0,
                         msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        with open(self.tmp / "data" / "session-log.json", encoding="utf-8") as f:
            log = json.load(f)
        self.assertEqual(log["sessions"][-1]["session_id"],
                         SESSION_PAYLOAD["session_id"])

    def test_invalid_argv_payload_exits_1(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "{not json"],
            stdin=subprocess.DEVNULL,
            cwd=str(self.tmp),
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 1)

    def test_missing_required_field_exits_1(self):
        proc = self._run({"date": "2026-04-24"})  # no session_id
        self.assertEqual(proc.returncode, 1)

    def test_same_day_does_not_bump_streak(self):
        # Profile last_updated = 2026-04-23; send a session on 2026-04-23.
        payload = dict(SESSION_PAYLOAD)
        payload["session_id"] = "session-003"
        payload["date"] = "2026-04-23"
        proc = self._run(payload)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        with open(self.tmp / "data" / "learner-profile.json", encoding="utf-8") as f:
            profile = json.load(f)
        self.assertEqual(profile["current_streak_days"], 2)

    # --- Milestones (issue #8) ---

    def _payload_with(self, session_id, milestones, date="2026-04-24"):
        payload = dict(SESSION_PAYLOAD)
        payload["session_id"] = session_id
        payload["date"] = date
        payload["milestones"] = milestones
        return payload

    def _load(self, name):
        with open(self.tmp / "data" / name, encoding="utf-8") as f:
            return json.load(f)

    def test_milestone_string_form(self):
        text = "Reached A2 vocabulary milestone"
        proc = self._run(self._payload_with("session-100", [text]))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        log = self._load("session-log.json")
        m = log["milestones"][-1]
        self.assertEqual(m["milestone"], text)
        self.assertEqual(m["date"], "2026-04-24")
        self.assertEqual(m["session_id"], "session-100")

        profile = self._load("learner-profile.json")
        ach = profile["achievements"][-1]
        self.assertEqual(ach["name"], text)
        self.assertEqual(ach["description"], text)
        self.assertEqual(ach["earned_date"], "2026-04-24")
        self.assertTrue(ach["id"].startswith("session_session-100_"))

    def test_milestone_object_form(self):
        ms = {"milestone": "Wrote first paragraph", "date": "2026-04-24",
              "session_id": "session-101"}
        proc = self._run(self._payload_with("session-101", [ms]))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        log = self._load("session-log.json")
        m = log["milestones"][-1]
        self.assertEqual(m["milestone"], "Wrote first paragraph")  # flat string
        self.assertEqual(m["date"], "2026-04-24")
        self.assertEqual(m["session_id"], "session-101")

        profile = self._load("learner-profile.json")
        self.assertEqual(profile["achievements"][-1]["name"], "Wrote first paragraph")

    def test_milestone_object_preserves_own_date(self):
        ms = {"milestone": "Backdated win", "date": "2026-04-20"}
        proc = self._run(self._payload_with("session-102", [ms], date="2026-04-24"))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        log = self._load("session-log.json")
        self.assertEqual(log["milestones"][-1]["date"], "2026-04-20")
        profile = self._load("learner-profile.json")
        self.assertEqual(profile["achievements"][-1]["earned_date"], "2026-04-20")

    def test_milestone_bad_date_falls_back_to_session_date(self):
        ms = {"milestone": "Typo date", "date": "not-a-date"}
        proc = self._run(self._payload_with("session-103", [ms], date="2026-04-24"))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        log = self._load("session-log.json")
        self.assertEqual(log["milestones"][-1]["date"], "2026-04-24")

    def test_milestone_malformed_exits_1_no_mutation(self):
        bad_cases = [
            {"date": "2026-04-24"},          # missing milestone
            {"milestone": None},
            {"milestone": ""},
            {"milestone": "   "},
            {"milestone": 5},
            42,                              # neither str nor dict
            "",                              # empty string form
        ]
        for n, bad in enumerate(bad_cases):
            with self.subTest(case=bad):
                proc = self._run(self._payload_with(f"session-2{n:02d}", [bad]))
                self.assertEqual(proc.returncode, 1,
                                 msg=f"case={bad!r} stderr={proc.stderr!r}")
                self.assertTrue(proc.stderr, "expected an error message on stderr")
                # No DB mutation: session-log still has its single original session.
                log = self._load("session-log.json")
                self.assertEqual(len(log["sessions"]), 1)
                self.assertEqual(log["milestones"], [])

    def test_milestone_nested_session_id_overridden(self):
        ms = {"milestone": "X", "session_id": "WRONG-999"}
        proc = self._run(self._payload_with("session-104", [ms]))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        log = self._load("session-log.json")
        self.assertEqual(log["milestones"][-1]["session_id"], "session-104")

    def test_milestone_distinct_achievement_ids(self):
        # Two strings sharing the first 30 chars would slugify identically;
        # the index prefix must keep their IDs distinct.
        prefix = "Mastered the perfect tense fo"  # 29 chars
        ms = [prefix + "r regular verbs", prefix + "r irregular verbs"]
        proc = self._run(self._payload_with("session-105", ms))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        profile = self._load("learner-profile.json")
        ids = [a["id"] for a in profile["achievements"][-2:]]
        self.assertEqual(len(set(ids)), 2, msg=f"colliding ids: {ids}")

    def test_milestone_non_latin_distinct_nonempty_ids(self):
        # All-non-Latin text slugifies to empty; fallback + index keep IDs valid.
        ms = ["مرحلة أولى", "مرحلة ثانية"]
        proc = self._run(self._payload_with("session-106", ms))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        profile = self._load("learner-profile.json")
        ids = [a["id"] for a in profile["achievements"][-2:]]
        self.assertEqual(len(set(ids)), 2, msg=f"colliding ids: {ids}")
        for i in ids:
            self.assertFalse(i.endswith("_"), f"bare trailing underscore: {i}")

    def test_cjk_payload_roundtrips_utf8(self):
        # Regression: under an ASCII/C locale (Git Bash on Windows), stdin was
        # decoded with surrogateescape and CJK payloads crashed at save time,
        # after some databases were already written.
        payload = dict(SESSION_PAYLOAD)
        payload["session_id"] = "session-300"
        payload["errors"] = [dict(SESSION_PAYLOAD["errors"][0],
                                  your_answer="achieve = 実績",
                                  correct_answer="achieve = 達成する（動詞）")]
        proc = self._run(payload)
        self.assertEqual(proc.returncode, 0,
                         msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        with open(self.tmp / "data" / "mistakes-db.json", encoding="utf-8") as f:
            mistakes = json.load(f)
        example = mistakes["error_patterns"]["verb_spreek"]["examples"][-1]
        self.assertEqual(example["incorrect"], "achieve = 実績")
        self.assertEqual(example["correct"], "achieve = 達成する（動詞）")

    def test_cjk_payload_survives_ascii_locale(self):
        # Same as above but forcing the worst-case locale explicitly.
        payload = dict(SESSION_PAYLOAD)
        payload["session_id"] = "session-301"
        payload["session_notes"] = "過去形と冠詞のリベンジ成功"
        env = dict(os.environ, LC_ALL="C", LANG="C")
        env.pop("PYTHONUTF8", None)
        env.pop("PYTHONIOENCODING", None)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(payload).encode(),
            cwd=str(self.tmp),
            capture_output=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0,
                         msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        with open(self.tmp / "data" / "session-log.json", encoding="utf-8") as f:
            log = json.load(f)
        self.assertEqual(log["sessions"][-1]["notes"], "過去形と冠詞のリベンジ成功")

    def test_rerun_same_session_id_preserves_first_backup(self):
        proc = self._run(SESSION_PAYLOAD)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        first = self.tmp / "data" / ".backups" / "pre-update-session-002"
        marker = json.loads((first / "learner-profile.json").read_text())

        proc = self._run(SESSION_PAYLOAD)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        second = self.tmp / "data" / ".backups" / "pre-update-session-002-2"
        self.assertTrue(second.exists(), "retry should get a numbered backup dir")
        # First backup still holds the original pre-update state.
        preserved = json.loads((first / "learner-profile.json").read_text())
        self.assertEqual(preserved, marker)

    def _snapshot(self):
        return {f.name: f.read_bytes()
                for f in sorted((self.tmp / "data").glob("*.json"))}

    def test_same_session_id_replaces_instead_of_double_counting(self):
        """Re-sending an identical payload must leave all six databases exactly
        as they were after the first call (issue #19)."""
        proc = self._run(SESSION_PAYLOAD)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        after_first = self._snapshot()

        for _ in range(2):
            proc = self._run(SESSION_PAYLOAD)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn(b"Replaced", proc.stdout)
        self.assertEqual(self._snapshot(), after_first)

    def test_same_session_id_applies_corrected_payload(self):
        """A corrected resubmission replaces the first record's content."""
        proc = self._run(SESSION_PAYLOAD)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        with open(self.tmp / "data" / "learner-profile.json", encoding="utf-8") as f:
            sessions_after_first = json.load(f)["total_sessions"]

        corrected = copy.deepcopy(SESSION_PAYLOAD)
        corrected["duration_minutes"] = SESSION_PAYLOAD.get("duration_minutes", 0) + 7
        corrected["session_notes"] = "corrected"
        proc = self._run(corrected)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        with open(self.tmp / "data" / "learner-profile.json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["total_sessions"], sessions_after_first)
        with open(self.tmp / "data" / "session-log.json", encoding="utf-8") as f:
            log = json.load(f)
        matching = [x for x in log["sessions"]
                    if x["session_id"] == SESSION_PAYLOAD["session_id"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["duration_minutes"], corrected["duration_minutes"])
        self.assertEqual(matching[0]["notes"], "corrected")

    def test_replacing_an_older_session_exits_1_no_mutation(self):
        """Only the most recent session can be replaced; rolling back an older
        one would silently drop the sessions recorded after it."""
        proc = self._run(SESSION_PAYLOAD)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        later = copy.deepcopy(SESSION_PAYLOAD)
        later["session_id"] = "session-003"
        proc = self._run(later)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        before = self._snapshot()

        proc = self._run(SESSION_PAYLOAD)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"not the most recent", proc.stderr)
        self.assertEqual(self._snapshot(), before)

    def test_replace_without_backup_exits_1_no_mutation(self):
        proc = self._run(SESSION_PAYLOAD)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        shutil.rmtree(self.tmp / "data" / ".backups")
        before = self._snapshot()

        proc = self._run(SESSION_PAYLOAD)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"backup", proc.stderr)
        self.assertEqual(self._snapshot(), before)

    def test_save_failure_leaves_all_databases_unchanged(self):
        # Force a staging failure by making one destination's .tmp path a
        # directory; the two-phase commit must abort before replacing any DB.
        before = {name: (self.tmp / "data" / name).read_text()
                  for name in ("learner-profile.json", "progress-db.json",
                               "mistakes-db.json", "mastery-db.json",
                               "spaced-repetition.json", "session-log.json")}
        blocker = self.tmp / "data" / "session-log.json.tmp"
        blocker.mkdir()
        try:
            proc = self._run(SESSION_PAYLOAD)
            self.assertEqual(proc.returncode, 2, msg=proc.stderr)
            after = {name: (self.tmp / "data" / name).read_text()
                     for name in before}
            self.assertEqual(after, before,
                             "a failed save must not modify any database")
        finally:
            blocker.rmdir()

    def test_milestones_empty_and_omitted_are_noops(self):
        for n, payload in enumerate([
            self._payload_with("session-107", []),
            {k: v for k, v in self._payload_with("session-108", []).items()
             if k != "milestones"},
        ]):
            with self.subTest(n=n):
                before = len(self._load("learner-profile.json").get("achievements", []))
                proc = self._run(payload)
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                after = len(self._load("learner-profile.json").get("achievements", []))
                self.assertEqual(after, before)

    # --- /fluent-review bug fixes ---

    def _error_pattern_item(self, item_id="err_articles", **over):
        item = {
            "id": item_id, "type": "error_pattern",
            "content": "de hond", "answer": "de hond",
            "category": "grammar", "difficulty": "",
            "created_date": "2026-04-20", "due_date": "2026-05-10",
            "interval_days": 6, "repetitions": 2, "easiness_factor": 2.5,
            "consecutive_correct": 2, "consecutive_incorrect": 0,
            "last_reviewed": "2026-04-18", "last_quality": 4,
            "mastery_level": 3, "total_reviews": 4, "priority": "medium",
            "review_history": [],
        }
        item.update(over)
        return item

    def _add_sr_item(self, item):
        path = self.tmp / "data" / "spaced-repetition.json"
        sr = json.loads(path.read_text())
        sr["items"][item["id"]] = item
        path.write_text(json.dumps(sr))

    def _session(self, session_id="session-400", **over):
        payload = {"session_id": session_id, "date": "2026-04-24"}
        payload.update(over)
        return payload

    def _sr(self):
        return self._load("spaced-repetition.json")

    def test_recurring_error_reschedules_existing_sr_item(self):
        """An errors[] entry whose pattern_id already exists in sr.items must
        lapse the item (reschedule to tomorrow), not leave its old due_date."""
        self._add_sr_item(self._error_pattern_item())
        proc = self._run(self._session(errors=[{
            "pattern_id": "err_articles", "category": "grammar",
            "your_answer": "het hond", "correct_answer": "de hond",
        }]))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        item = self._sr()["items"]["err_articles"]
        self.assertEqual(item["due_date"], "2026-04-25")
        self.assertEqual(item["repetitions"], 0)
        self.assertEqual(item["interval_days"], 1)
        self.assertEqual(item["mastery_level"], 2)
        self.assertEqual(item["consecutive_incorrect"], 1)
        self.assertEqual(item["consecutive_correct"], 0)
        self.assertEqual(item["total_reviews"], 5)
        self.assertEqual(item["last_quality"], 2)
        self.assertIn("err_articles",
                      self._sr()["review_queue"]["tomorrow"])

    def test_error_plus_review_result_applies_sm2_once(self):
        """An item in both review_results and errors uses the explicit quality;
        SM-2 runs once, mistakes-db frequency still bumps."""
        self._add_sr_item(self._error_pattern_item())
        mistakes = self._load("mistakes-db.json")
        mistakes["error_patterns"]["err_articles"] = {
            "category": "grammar", "mastery_level": 3, "frequency": 1,
            "consecutive_incorrect": 0, "consecutive_correct": 2,
            "examples": [],
        }
        (self.tmp / "data" / "mistakes-db.json").write_text(json.dumps(mistakes))

        proc = self._run(self._session(
            review_results=[{"item_id": "err_articles", "quality": 4}],
            errors=[{"pattern_id": "err_articles", "category": "grammar",
                     "your_answer": "het hond", "correct_answer": "de hond"}],
        ))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        item = self._sr()["items"]["err_articles"]
        self.assertEqual(item["total_reviews"], 5)  # +1 only
        self.assertEqual(item["last_quality"], 4)
        self.assertGreaterEqual(item["mastery_level"], 3)  # not decremented
        self.assertGreater(item["interval_days"], 6)  # interval grew
        self.assertEqual(len(item["review_history"]), 1)
        pat = self._load("mistakes-db.json")["error_patterns"]["err_articles"]
        self.assertEqual(pat["frequency"], 2)

    def test_low_quality_review_decrements_mastery(self):
        self._add_sr_item(self._error_pattern_item())
        proc = self._run(self._session(
            review_results=[{"item_id": "err_articles", "quality": 1}]))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(self._sr()["items"]["err_articles"]["mastery_level"], 2)

    def test_duplicate_review_results_count_once(self):
        """Duplicate review_results entries for one item: last entry wins,
        SM-2 applied once."""
        proc = self._run(self._session(review_results=[
            {"item_id": "vocab_dag", "quality": 5},
            {"item_id": "vocab_dag", "quality": 1},
        ]))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        item = self._sr()["items"]["vocab_dag"]
        self.assertEqual(item["total_reviews"], 2)  # was 1, +1 not +2
        self.assertEqual(item["last_quality"], 1)  # last entry wins
        self.assertEqual(len(item["review_history"]), 1)
        self.assertEqual(item["review_history"][0]["quality"], 1)

    def test_recurring_error_decrements_mistakes_mastery(self):
        mistakes = self._load("mistakes-db.json")
        mistakes["error_patterns"]["verb_spreek"] = {
            "category": "grammar", "mastery_level": 3, "frequency": 1,
            "consecutive_incorrect": 0, "consecutive_correct": 2,
            "examples": [],
        }
        (self.tmp / "data" / "mistakes-db.json").write_text(json.dumps(mistakes))
        proc = self._run(self._session(errors=[{
            "pattern_id": "verb_spreek", "category": "grammar",
            "your_answer": "Hij spreek", "correct_answer": "Hij spreekt",
        }]))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        pat = self._load("mistakes-db.json")["error_patterns"]["verb_spreek"]
        self.assertEqual(pat["mastery_level"], 2)

    def test_same_day_zero_streak_becomes_one(self):
        """A profile created today (streak 0) gets streak 1 on its first
        session of the same day."""
        profile = self._load("learner-profile.json")
        profile["last_updated"] = "2026-04-24"
        profile["current_streak_days"] = 0
        (self.tmp / "data" / "learner-profile.json").write_text(json.dumps(profile))
        proc = self._run(self._session())
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(self._load("learner-profile.json")
                         ["current_streak_days"], 1)


class ReadDbDueItemsTest(unittest.TestCase):
    """read-db.py computes due_review_items from the real clock."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fluent-test-read-"))
        (self.tmp / "data").mkdir()
        make_fixtures(self.tmp / "data")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_due_items_fresh_and_sorted(self):
        """Items with due_date <= today must appear even when the stored
        review_queue.today is empty, ordered by priority then due_date."""
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        def item(item_id, priority, due):
            return {
                "id": item_id, "type": "vocabulary", "content": item_id,
                "answer": "", "category": "", "difficulty": "",
                "created_date": yesterday, "due_date": due,
                "interval_days": 1, "repetitions": 1, "easiness_factor": 2.5,
                "consecutive_correct": 1, "consecutive_incorrect": 0,
                "last_reviewed": yesterday, "last_quality": 4,
                "mastery_level": 1, "total_reviews": 1, "priority": priority,
            }

        sr = {
            "metadata": {"algorithm": "SM-2", "last_updated": yesterday,
                         "total_items_tracked": 3, "language": "Dutch"},
            "review_queue": {"today": [], "tomorrow": [],
                             "this_week": [], "later": []},
            "items": {
                "low_early": item("low_early", "low", yesterday),
                "crit_late": item("crit_late", "critical", today),
                "high_early": item("high_early", "high", yesterday),
            },
        }
        (self.tmp / "data" / "spaced-repetition.json").write_text(json.dumps(sr))

        env = dict(os.environ, FLUENT_DATA_DIR=str(self.tmp / "data"))
        proc = subprocess.run(
            [sys.executable, str(READ_SCRIPT)],
            cwd=str(self.tmp), capture_output=True, env=env,
        )
        self.assertEqual(proc.returncode, 0,
                         msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        out = json.loads(proc.stdout.decode())
        self.assertEqual(out["computed"]["due_review_items"],
                         ["crit_late", "high_early", "low_early"])
        self.assertEqual(out["computed"]["due_reviews_count"], 3)


if __name__ == "__main__":
    unittest.main()

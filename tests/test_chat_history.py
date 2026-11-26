#!/usr/bin/env python3
"""
Tests for .claude/hooks/read-chat-history.py using synthetic provider stores.

Usage:
    python3 tests/test_chat_history.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".claude" / "hooks" / "read-chat-history.py"
LOCAL_TZ = timezone(timedelta(hours=7))
DAY = date(2026, 1, 15)


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def iso_local(day, hour, minute=0, second=0):
    dt = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_local(day, hour, minute=0, second=0):
    dt = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=LOCAL_TZ)
    return int(dt.timestamp())


def epoch_ms_local(day, hour, minute=0, second=0):
    return epoch_local(day, hour, minute, second) * 1000


def touch_in_window(path, day=DAY):
    ts = epoch_local(day, 12)
    os.utime(path, (ts, ts))


class ChatHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.data = self.root / "fluent-data"
        self.data.mkdir()

    def run_cli(self, *args, env_extra=None):
        env = os.environ.copy()
        env["FLUENT_DATA_DIR"] = str(self.data)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + [str(a) for a in args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )

    def run_json(self, day=DAY, extra=()):
        automation = () if "--automation-prompts" in extra or "--orca-db" in extra else ("--no-orca",)
        proc = self.run_cli("--date", day.isoformat(), "--format", "json", "--home", self.home, *(automation + tuple(extra)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def build_codex(self, day=DAY):
        root = self.home / ".codex" / "sessions" / "2026" / "01"
        owner_text = "Codex owner text"
        user = {
            "type": "response_item",
            "timestamp": iso_local(day, 10),
            "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "<environment_context>secret wrapper</environment_context>"},
                {"type": "input_text", "text": "# AGENTS.md\ninstructions"},
                {"type": "input_text", "text": "<skill>skill text</skill>"},
                {"type": "input_image", "image_url": "data:ignored"},
                {"type": "input_text", "text": owner_text},
            ]},
        }
        meta = {"type": "session_meta", "payload": {
            "id": "codex-owner-session", "source": "vscode", "cwd": "/repo/one",
            "originator": "tester", "forked_from_id": "older",
        }}
        p1 = root / "rollout-owner.jsonl"
        jsonl(p1, [meta, user])
        # A forked/copied transcript repeats the same turn byte-for-byte.
        p2 = root / "rollout-fork-copy.jsonl"
        jsonl(p2, [meta, user])
        exec_meta = {"type": "session_meta", "payload": {
            "id": "codex-exec-session", "source": "exec", "cwd": "/repo/one",
        }}
        exec_user = dict(user)
        exec_user["timestamp"] = iso_local(day, 10, 30)
        p3 = root / "rollout-exec.jsonl"
        jsonl(p3, [exec_meta, exec_user])
        for path in (p1, p2, p3):
            touch_in_window(path, day)

    def build_antigravity(self, day=DAY):
        logs = self.home / ".gemini" / "antigravity-cli" / "brain" / "brain-1" / ".system_generated" / "logs"
        rec = {"step_index": 3, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "done",
               "created_at": iso_local(day, 11),
               "content": "<USER_REQUEST>Antigravity owner text</USER_REQUEST><ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"}
        full = logs / "transcript_full.jsonl"
        fallback = logs / "transcript.jsonl"
        jsonl(full, [rec])
        jsonl(fallback, [rec])
        touch_in_window(full, day)
        touch_in_window(fallback, day)

    def build_devin(self, day=DAY):
        path = self.home / ".local" / "share" / "devin" / "cli" / "sessions.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("create table sessions (session_id text, working_directory text)")
        conn.execute("create table prompt_history (id integer primary key, content text, timestamp integer, session_id text, is_shell integer)")
        conn.execute("insert into sessions values ('devin-session', '/repo/devin')")
        rows = [
            (1, "Devin owner text", epoch_local(day, 12), "devin-session", 0),
            (2, "/review investigate flaky test", epoch_local(day, 12, 5), "devin-session", 0),
            (3, "ls -la", epoch_local(day, 12, 10), "devin-session", 1),
            (4, "/loop", epoch_local(day, 12, 15), "devin-session", 0),
        ]
        conn.executemany("insert into prompt_history values (?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        touch_in_window(path, day)

    def claude_user(self, text, day=DAY, hour=13, uuid="uuid", **fields):
        rec = {"type": "user", "timestamp": iso_local(day, hour), "sessionId": "claude-session",
               "uuid": uuid, "cwd": "/repo/claude", "message": {"content": text}}
        rec.update(fields)
        return rec

    def build_claude(self, day=DAY):
        project = self.home / ".claude" / "projects" / "repo-claude"
        records = [
            self.claude_user("Claude owner text", day, 13, "u-owner", promptSource="typed"),
            {"type": "user", "timestamp": iso_local(day, 13, 1), "sessionId": "claude-session", "uuid": "u-tool",
             "message": {"content": [{"type": "tool_result", "content": "tool output"}]}},
            self.claude_user("meta text", day, 13, "u-meta", isMeta=True, promptSource="typed"),
            self.claude_user("sidechain text", day, 13, "u-side", isSidechain=True, promptSource="typed"),
            self.claude_user("<command-message>review</command-message>\n<command-name>/review</command-name>\n<command-args>review this diff</command-args>",
                             day, 13, "u-command", promptSource="typed"),
            self.claude_user("<command-name>/review</command-name>\n<command-message>review</command-message>",
                             day, 13, "u-command-empty", promptSource="typed"),
            self.claude_user("<local-command-stdout>output</local-command-stdout>", day, 13, "u-stdout", promptSource="typed"),
            self.claude_user("accepted model words", day, 13, "u-suggest", promptSource="suggestion_accepted"),
            self.claude_user("mystery text", day, 13, "u-unknown", promptSource="unexpected_source"),
        ]
        path = project / "session.jsonl"
        jsonl(path, records)
        sub = project / "claude-session" / "subagents" / "nested.jsonl"
        jsonl(sub, [self.claude_user("subagent text must never appear", day, 13, "u-nested", promptSource="typed")])
        touch_in_window(path, day)
        touch_in_window(sub, day)

    def build_grok(self, day=DAY):
        updates = self.home / ".grok" / "sessions" / "%2FUsers%2Fme%2Fproject" / "grok-session" / "updates.jsonl"
        records = [
            {"method": "session/update", "timestamp": epoch_local(day, 14), "params": {"update": {
                "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "Grok "},
                "_meta": {"promptIndex": 7, "modelId": "grok"}}}},
            {"method": "session/update", "timestamp": epoch_local(day, 14, 0, 1), "params": {"update": {
                "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "<system-reminder>drop"},
                "_meta": {"promptIndex": 7, "modelId": "grok"}}}},
            {"method": "session/update", "timestamp": epoch_local(day, 14, 0, 2), "params": {"update": {
                "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "owner text"},
                "_meta": {"promptIndex": 7, "modelId": "grok"}}}},
        ]
        jsonl(updates, records)
        touch_in_window(updates, day)
        (self.home / ".grok" / "sessions" / "%2FUsers%2Fme%2Fproject" / "old-session").mkdir(parents=True, exist_ok=True)

    def build_all(self, day=DAY):
        self.build_codex(day)
        self.build_antigravity(day)
        self.build_devin(day)
        self.build_claude(day)
        self.build_grok(day)

    def test_provider_owner_turns_and_injected_blocks(self):
        self.build_all()
        payload = self.run_json()
        texts = {(t["source"], t["text"]) for t in payload["turns"]}
        self.assertIn(("codex", "Codex owner text"), texts)
        self.assertIn(("antigravity", "Antigravity owner text"), texts)
        self.assertIn(("devin", "Devin owner text"), texts)
        self.assertIn(("devin", "investigate flaky test"), texts)
        self.assertIn(("claude", "Claude owner text"), texts)
        self.assertIn(("claude", "review this diff"), texts)
        self.assertIn(("grok", "Grok owner text"), texts)
        joined = "\n".join(t["text"] for t in payload["turns"])
        for forbidden in ("environment_context", "# AGENTS.md", "skill text", "ADDITIONAL_METADATA",
                          "subagent text", "tool output", "output</local-command-stdout>"):
            self.assertNotIn(forbidden, joined)

    def test_provider_exclusions_and_coverage_reasons(self):
        self.build_all()
        proc = self.run_cli("--date", DAY.isoformat(), "--home", self.home, "--no-orca")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        for reason in ("codex_exec", "shell_command", "sidechain", "meta", "accepted_suggestion",
                       "injected", "empty_after_cleaning", "duplicate", "no_timestamps"):
            self.assertIn(reason, out)
        self.assertIn("claude: status=ok files_scanned=1 turns_in_window=8 owner=2 automated=4 unknown=1 empty_after_cleaning=1", out)
        self.assertIn("devin: status=ok files_scanned=1 turns_in_window=4 owner=2 automated=1 unknown=0 empty_after_cleaning=1", out)

    def write_single_claude_text(self, text, uuid="single"):
        path = self.home / ".claude" / "projects" / "p" / "s.jsonl"
        jsonl(path, [self.claude_user(text, DAY, 13, uuid, promptSource="typed")])
        touch_in_window(path)
        return path

    def write_orca_journal(self, rows):
        path = self.root / "orca-test.db"
        conn = sqlite3.connect(str(path))
        conn.execute("create table journal_submission_attempts (session_id text, epoch text, message_id text, current integer, item_id text, submitted_at integer, payload_json text)")
        conn.execute("create table journal_items (session_id text, epoch text, item_id text, payload_json text)")
        conn.execute("create table agent_session_records (session_id text, record_json text, adopted_native_id text)")
        for index, row in enumerate(rows):
            orca_session = row.get("orca_session", "orca-session-%d" % index)
            native = row["native_id"]
            provider = row["provider"]
            item_id = "item-%d" % index
            epoch = "1"
            handle_key = row.get("handle_key", "nativeId")
            record = {"provider": provider, "providerHandleChain": [{"handle": {handle_key: native}}]}
            conn.execute("insert into agent_session_records values (?,?,?)", (orca_session, json.dumps(record), None))
            item_payload = {"body": {"blocks": [{"text": row["text"]}]}}
            conn.execute("insert into journal_items values (?,?,?,?)", (orca_session, epoch, item_id, json.dumps(item_payload)))
            source = None if row.get("kind") is None else {"kind": row["kind"]}
            attempt_payload = {"origin": row["origin"], "source": source}
            if row.get("queuedMessageId"):
                attempt_payload["queuedMessageId"] = row["queuedMessageId"]
            conn.execute("insert into journal_submission_attempts values (?,?,?,?,?,?,?)", (
                orca_session, epoch, "message-%d" % index, 1, item_id,
                row.get("submitted_at", epoch_ms_local(DAY, 10)), json.dumps(attempt_payload)))
        conn.commit()
        conn.close()
        return path

    def write_enveloped_codex_turn(self, session_id, tail, hour=10, minute=0):
        raw = (
            "[Orca rules update: your name and the Tools and Rules below replace the earlier ones.]\n"
            "rules\n"
            "[Your SOUL.md was updated; this replaces your earlier standing instructions.]\n"
            "soul\n"
            "\n"
            + tail
        )
        path = self.home / ".codex" / "sessions" / "journal" / ("rollout-%s.jsonl" % session_id)
        jsonl(path, [
            {"type": "session_meta", "payload": {"id": session_id, "source": "cli"}},
            {"type": "response_item", "timestamp": iso_local(DAY, hour, minute), "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": raw}]}},
        ])
        touch_in_window(path)

    def test_orca_journal_client_submission_replaces_enveloped_text(self):
        tail = "owner final answer with enough length to match the journal"
        self.write_enveloped_codex_turn("codex-journal-owner", tail)
        db = self.write_orca_journal([{
            "provider": "codex", "native_id": "codex-journal-owner", "text": tail,
            "origin": "client", "kind": "user", "submitted_at": epoch_ms_local(DAY, 10),
        }])
        payload = self.run_json(extra=("--sources", "codex", "--orca-db", db))
        self.assertEqual(len(payload["turns"]), 1)
        turn = payload["turns"][0]
        self.assertEqual(turn["text"], tail)
        self.assertEqual(turn["reason"], "orca_client_submission")
        self.assertEqual(payload["coverage"]["codex"]["orca_matched_owner"], 1)

    def test_orca_journal_host_agent_and_system_classifications(self):
        for session_id in ("host-agent", "host-system"):
            self.write_enveloped_codex_turn(session_id, "forwarded automated prompt with enough length")
        db = self.write_orca_journal([
            {"provider": "codex", "native_id": "host-agent", "text": "forwarded automated prompt with enough length",
             "origin": "host", "kind": "agent", "submitted_at": epoch_ms_local(DAY, 10)},
            {"provider": "codex", "native_id": "host-system", "text": "forwarded automated prompt with enough length",
             "origin": "host", "kind": None, "submitted_at": epoch_ms_local(DAY, 10)},
        ])
        proc = self.run_cli("--date", DAY.isoformat(), "--home", self.home, "--sources", "codex", "--orca-db", db)
        self.assertIn("agent_sent=1", proc.stdout)
        self.assertIn("orca_system=1", proc.stdout)
        self.assertIn("orca_matched_automated=2", proc.stdout)

    def test_orca_journal_time_and_native_mismatch_fall_back_to_heuristics(self):
        tail = "ordinary owner text with enough length to remain owner"
        self.write_enveloped_codex_turn("codex-unmatched", tail)
        db = self.write_orca_journal([
            {"provider": "codex", "native_id": "codex-unmatched", "text": tail, "origin": "host", "kind": "agent",
             "submitted_at": epoch_ms_local(DAY, 10, 30), "orca_session": "late-session"},
            {"provider": "codex", "native_id": "other-native", "text": tail, "origin": "host", "kind": "agent",
             "submitted_at": epoch_ms_local(DAY, 10), "orca_session": "other-session"},
        ])
        payload = self.run_json(extra=("--sources", "codex", "--orca-db", db))
        self.assertEqual(len(payload["turns"]), 1)
        self.assertEqual(payload["turns"][0]["provenance"], "owner")
        self.assertEqual(payload["coverage"]["codex"]["orca_matched_owner"], 0)

    def test_orca_journal_short_submission_exact_match(self):
        self.write_enveloped_codex_turn("codex-short", "ok")
        db = self.write_orca_journal([{
            "provider": "codex", "native_id": "codex-short", "text": "ok",
            "origin": "client", "kind": "user", "submitted_at": epoch_ms_local(DAY, 10),
        }])
        payload = self.run_json(extra=("--sources", "codex", "--orca-db", db))
        self.assertEqual(len(payload["turns"]), 1)
        self.assertEqual(payload["turns"][0]["text"], "ok")
        self.assertEqual(payload["turns"][0]["reason"], "orca_client_submission")

    def test_orca_journal_threadid_native_id_matches_codex(self):
        tail = "thread id owner text with enough length to match"
        self.write_enveloped_codex_turn("codex-thread-native", tail)
        db = self.write_orca_journal([{
            "provider": "codex", "native_id": "codex-thread-native", "text": tail,
            "origin": "client", "kind": "user", "submitted_at": epoch_ms_local(DAY, 10),
            "handle_key": "threadId",
        }])
        payload = self.run_json(extra=("--sources", "codex", "--orca-db", db))
        self.assertEqual(len(payload["turns"]), 1)
        self.assertEqual(payload["turns"][0]["reason"], "orca_client_submission")
        self.assertEqual(payload["coverage"]["codex"]["orca_matched_owner"], 1)

    def test_orca_journal_client_suggestions_still_runs_heuristics(self):
        prompt = "Look around: find what is waiting on your owner and suggest up to 5 next steps for their Suggestions list."
        self.write_enveloped_codex_turn("codex-client-suggestions", prompt)
        db = self.write_orca_journal([{
            "provider": "codex", "native_id": "codex-client-suggestions", "text": prompt,
            "origin": "client", "kind": "user", "submitted_at": epoch_ms_local(DAY, 10),
        }])
        payload = self.run_json(extra=("--sources", "codex", "--orca-db", db))
        self.assertEqual(payload["turns"], [])
        self.assertEqual(payload["coverage"]["codex"]["orca_matched_owner"], 1)
        self.assertEqual(payload["coverage"]["codex"]["excluded_by_reason"].get("orca_suggestions"), 1)

    def test_orca_journal_client_submission_can_match_sender_crosscheck(self):
        prompt = "Please continue this journal matched owner brief with enough unique detail"
        sender = self.home / ".codex" / "sessions" / "journal-sender" / "rollout-journal-sender.jsonl"
        jsonl(sender, [
            {"type": "session_meta", "payload": {"id": "journal-sender-session", "source": "cli"}},
            {"type": "response_item", "timestamp": iso_local(DAY, 10, 55), "payload": {
                "type": "function_call", "arguments": json.dumps({"command": "agy -p '%s'" % prompt})}},
        ])
        touch_in_window(sender)
        self.write_enveloped_codex_turn("codex-client-forwarded", prompt, hour=11)
        db = self.write_orca_journal([{
            "provider": "codex", "native_id": "codex-client-forwarded", "text": prompt,
            "origin": "client", "kind": "user", "submitted_at": epoch_ms_local(DAY, 11),
        }])
        payload = self.run_json(extra=("--sources", "codex", "--orca-db", db))
        self.assertNotIn(prompt, [t["text"] for t in payload["turns"] if t["source"] == "codex"])
        self.assertEqual(payload["coverage"]["codex"]["orca_matched_owner"], 1)
        self.assertEqual(payload["coverage"]["codex"]["excluded_by_reason"].get("agent_sent"), 1)

    def test_orca_journal_host_queued_is_owner_candidate(self):
        text = "queued host delivered text with enough length to match"
        self.write_enveloped_codex_turn("codex-host-queued", text)
        db = self.write_orca_journal([{
            "provider": "codex", "native_id": "codex-host-queued", "text": text,
            "origin": "host", "kind": None, "queuedMessageId": "queued-1",
            "submitted_at": epoch_ms_local(DAY, 10),
        }])
        payload = self.run_json(extra=("--sources", "codex", "--orca-db", db))
        self.assertEqual(len(payload["turns"]), 1)
        self.assertEqual(payload["turns"][0]["text"], text)
        self.assertEqual(payload["turns"][0]["reason"], "orca_host_queued")
        self.assertEqual(payload["coverage"]["codex"]["orca_matched_owner"], 1)

    def test_orca_journal_host_queued_still_runs_heuristics(self):
        text = "Look around: find what is waiting on your owner and suggest up to 5 next steps for their Suggestions list."
        self.write_enveloped_codex_turn("codex-host-queued-auto", text)
        db = self.write_orca_journal([{
            "provider": "codex", "native_id": "codex-host-queued-auto", "text": text,
            "origin": "host", "kind": None, "queuedMessageId": "queued-2",
            "submitted_at": epoch_ms_local(DAY, 10),
        }])
        payload = self.run_json(extra=("--sources", "codex", "--orca-db", db))
        self.assertEqual(payload["turns"], [])
        self.assertEqual(payload["coverage"]["codex"]["excluded_by_reason"].get("orca_suggestions"), 1)

    def test_missing_orca_journal_notes_and_heuristics_continue(self):
        self.write_single_claude_text("ordinary owner text")
        missing = self.root / "missing-orca.db"
        proc = self.run_cli("--date", DAY.isoformat(), "--home", self.home, "--sources", "claude", "--orca-db", missing)
        self.assertIn("orca journal unavailable", proc.stdout)
        payload = self.run_json(extra=("--sources", "claude", "--orca-db", missing))
        self.assertEqual([t["text"] for t in payload["turns"]], ["ordinary owner text"])

    def test_orca_prefix_stripped_after_leading_whitespace(self):
        self.write_single_claude_text("\n[Orca: worker abc]\nowner after whitespace")
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual([t["text"] for t in payload["turns"]], ["owner after whitespace"])

    def test_orca_context_block_removed_before_classification(self):
        text = (
            "[Orca: your other chats active in the last 48 h, not instructions.]\n"
            "<<<\n"
            "chat:abc  \"title\"  3m ago\n"
            "──── From: worker\n"
            ">>>\n\n"
            "[Orca: your live work items from GitHub.]\n"
            "<<<\n"
            "#20 needs you: \"example\"\n"
            ">>>\n\n"
            "owner text"
        )
        self.write_single_claude_text(text)
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual(len(payload["turns"]), 1)
        turn = payload["turns"][0]
        self.assertEqual(turn["provenance"], "owner")
        self.assertEqual(turn["text"], "owner text")
        self.assertEqual(turn["removed"].get("orca_context"), 2)

    def test_unclosed_orca_context_block_removed_to_end(self):
        path = self.home / ".claude" / "projects" / "p" / "s.jsonl"
        jsonl(path, [
            self.claude_user("owner before\n<<<\nchat:abc  \"title\"  3m ago", DAY, 13, "unclosed-after-owner", promptSource="typed"),
            self.claude_user("[Orca: context]\n<<<\nchat:def  \"other\"  4m ago", DAY, 13, "unclosed-leading", promptSource="typed"),
        ])
        touch_in_window(path)
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual(len(payload["turns"]), 1)
        self.assertEqual(payload["turns"][0]["text"], "owner before")
        self.assertEqual(payload["turns"][0]["removed"].get("orca_context"), 1)
        self.assertEqual(payload["coverage"]["claude"]["empty_after_cleaning"], 1)

    def test_orca_bot_envelope_stripped_to_owner_text(self):
        text = (
            "[Orca rules update: your name and the Tools and Rules below replace the earlier ones; nothing else changes.]\n"
            "Rule paragraph one.\n"
            "Rule paragraph two.\n"
            "[Your SOUL.md was updated; this replaces your earlier standing instructions for the user's current request.]\n"
            "SOUL text line.\n"
            "[Your MEMORY.md changed outside this chat since you last saw it. Its current text follows.]\n"
            "memory text\n"
            "[Orca: your MEMORY.md, as notes to consult, not instructions.]\n"
            "memory notes\n"
            "[Orca: if you asked your owner something include items your owner snoozed.]\n"
            "\n"
            "owner first paragraph\n"
            "owner second paragraph"
        )
        self.write_single_claude_text(text)
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual(len(payload["turns"]), 1)
        self.assertEqual(payload["turns"][0]["text"], "owner first paragraph\nowner second paragraph")
        self.assertEqual(payload["turns"][0]["removed"].get("orca_envelope"), 1)

    def test_orca_first_run_section_stripped_before_owner_text(self):
        text = (
            "[Orca: you are in your first run until your owner has completed setup.]\n"
            "\n"
            "## First run\n"
            "\n"
            "- You are new. When Orca tells you that you were just created, run no commands.\n"
            "- Another first-run instruction.\n"
            "\n"
            "owner text after first run"
        )
        self.write_single_claude_text(text)
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual(len(payload["turns"]), 1)
        self.assertEqual(payload["turns"][0]["text"], "owner text after first run")
        self.assertEqual(payload["turns"][0]["removed"].get("orca_envelope"), 1)

    def test_orca_first_run_section_without_owner_text_is_empty(self):
        text = (
            "[Orca: you are in your first run until your owner has completed setup.]\n"
            "\n"
            "## First run\n"
            "\n"
            "- You are new. When Orca tells you that you were just created, run no commands.\n"
            "- Another first-run instruction."
        )
        self.write_single_claude_text(text)
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual(payload["turns"], [])
        self.assertEqual(payload["coverage"]["claude"]["empty_after_cleaning"], 1)

    def test_orca_move_chat_transcript_replay_is_automated(self):
        text = (
            "[Orca rules update: your name and the Tools and Rules below replace the earlier ones.]\n"
            "\n"
            "[Orca: some saved chat messages or tool details omitted from this excerpt]\n"
            "[user] owner asked something earlier\n"
            "[assistant] earlier answer\n"
            "[Orca: middle omitted]\n"
            "[user] more owner context\n"
            "[assistant] more assistant context\n"
            "Briefly state where the previous session stopped. If work remains, continue it."
        )
        self.write_single_claude_text(text)
        proc = self.run_cli("--date", DAY.isoformat(), "--home", self.home, "--sources", "claude", "--no-orca")
        self.assertIn("orca_resume=1", proc.stdout)
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual(payload["turns"], [])

    def test_single_inline_assistant_mention_stays_owner(self):
        text = "I saw [assistant] mentioned once in the documentation and wondered why"
        self.write_single_claude_text(text)
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual([t["text"] for t in payload["turns"]], [text])

    def test_unclosed_orca_envelope_bracket_is_automated(self):
        self.write_single_claude_text("[Orca rules update: this header never closes\nrules text")
        proc = self.run_cli("--date", DAY.isoformat(), "--home", self.home, "--sources", "claude", "--no-orca")
        self.assertIn("orca_envelope=1", proc.stdout)
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual(payload["turns"], [])

    def test_orca_dispatch_markers_are_automated(self):
        path = self.home / ".claude" / "projects" / "p" / "s.jsonl"
        jsonl(path, [
            self.claude_user("intro\n=== TASK ===\nbody", DAY, 13, "dispatch-line", promptSource="typed"),
            self.claude_user("You are a dispatched worker with a task", DAY, 13, "dispatch-worker", promptSource="typed"),
            self.claude_user("# Task: Item #123\nbody", DAY, 13, "dispatch-task", promptSource="typed"),
        ])
        touch_in_window(path)
        proc = self.run_cli("--date", DAY.isoformat(), "--home", self.home, "--sources", "claude", "--no-orca")
        self.assertIn("orca_dispatch=3", proc.stdout)

    def test_agent_sent_crosscheck_matches_and_exempts_owner_and_short_texts(self):
        sent_prompt = "Write the JSON content {\"test2\": 2} using write_file. Then reply 'done'"
        other_owner = "Write the JSON content {\"test2\": 99} using write_file. Then reply 'done'"
        short_prompt = "reply done now"
        codex = self.home / ".codex" / "sessions" / "sender" / "rollout-sender.jsonl"
        arguments = json.dumps({"command": "orca terminal send --text '%s'" % sent_prompt.replace('"', '\\"')})
        jsonl(codex, [
            {"type": "session_meta", "payload": {"id": "sender-session", "source": "cli"}},
            {"type": "response_item", "timestamp": iso_local(DAY, 10, 59), "payload": {
                "type": "function_call", "arguments": arguments}},
        ])
        touch_in_window(codex)

        db = self.home / ".local" / "share" / "devin" / "cli" / "sessions.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.execute("create table sessions (session_id text, working_directory text)")
        conn.execute("create table prompt_history (id integer primary key, content text, timestamp integer, session_id text, is_shell integer)")
        rows = [
            (1, sent_prompt, epoch_local(DAY, 11), "receiver", 0),
            (2, other_owner, epoch_local(DAY, 11, 1), "receiver", 0),
            (3, short_prompt, epoch_local(DAY, 11, 2), "receiver", 0),
        ]
        conn.executemany("insert into prompt_history values (?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        touch_in_window(db)

        payload = self.run_json(extra=("--sources", "codex,devin"))
        devin_texts = [t["text"] for t in payload["turns"] if t["source"] == "devin"]
        self.assertIn(other_owner, devin_texts)
        self.assertIn(short_prompt, devin_texts)
        self.assertNotIn(sent_prompt, devin_texts)
        self.assertEqual(payload["coverage"]["devin"]["excluded_by_reason"].get("agent_sent"), 1)

    def write_agent_sent_pair(self, prompt, sender_hour, sender_minute, sender_session="sender-session", arguments=None):
        codex = self.home / ".codex" / "sessions" / "sender" / "rollout-sender.jsonl"
        if arguments is None:
            arguments = json.dumps({"command": "orca terminal send --text '%s'" % prompt})
        jsonl(codex, [
            {"type": "session_meta", "payload": {"id": sender_session, "source": "cli"}},
            {"type": "response_item", "timestamp": iso_local(DAY, sender_hour, sender_minute), "payload": {
                "type": "function_call", "arguments": arguments}},
        ])
        touch_in_window(codex)
        db = self.home / ".local" / "share" / "devin" / "cli" / "sessions.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.execute("create table sessions (session_id text, working_directory text)")
        conn.execute("create table prompt_history (id integer primary key, content text, timestamp integer, session_id text, is_shell integer)")
        conn.execute("insert into prompt_history values (?,?,?,?,?)", (1, prompt, epoch_local(DAY, 11), "receiver", 0))
        conn.commit()
        conn.close()
        touch_in_window(db)

    def test_agent_sent_uses_raw_path_before_placeholder_substitution(self):
        prompt = 'Write the JSON content {"test2": 2} to the file /tmp/x/data/test2.json using write_file. Then reply done'
        sender = self.home / ".codex" / "sessions" / "raw-sender" / "rollout-raw-sender.jsonl"
        jsonl(sender, [
            {"type": "session_meta", "payload": {"id": "raw-sender-session", "source": "cli"}},
            {"type": "response_item", "timestamp": iso_local(DAY, 10, 59), "payload": {
                "type": "custom_tool_call", "input": {"command": "send prompt", "prompt": prompt}}},
        ])
        touch_in_window(sender)
        receiver = self.home / ".codex" / "sessions" / "raw-receiver" / "rollout-raw-receiver.jsonl"
        jsonl(receiver, [
            {"type": "session_meta", "payload": {"id": "raw-receiver-session", "source": "cli"}},
            {"type": "response_item", "timestamp": iso_local(DAY, 11), "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}},
        ])
        touch_in_window(receiver)
        payload = self.run_json(extra=("--sources", "codex"))
        self.assertNotIn(prompt, [t["text"] for t in payload["turns"]])
        self.assertEqual(payload["coverage"]["codex"]["excluded_by_reason"].get("agent_sent"), 1)

    def test_agent_sent_same_sender_session_stays_owner(self):
        prompt = "Please forward this exact owner sentence to the worker"
        arguments = json.dumps({"command": "orca terminal send --text '%s'" % prompt})
        path = self.home / ".codex" / "sessions" / "same" / "rollout-same.jsonl"
        jsonl(path, [
            {"type": "session_meta", "payload": {"id": "same-session", "source": "cli"}},
            {"type": "response_item", "timestamp": iso_local(DAY, 12, 59), "payload": {
                "type": "function_call", "arguments": arguments}},
            {"type": "response_item", "timestamp": iso_local(DAY, 13), "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}},
        ])
        touch_in_window(path)
        payload = self.run_json(extra=("--sources", "codex"))
        self.assertIn(prompt, [t["text"] for t in payload["turns"]])
        self.assertNotIn("agent_sent", payload["coverage"]["codex"]["excluded_by_reason"])

    def test_agent_sent_stale_sender_ninety_minutes_earlier_stays_owner(self):
        prompt = "This exact sentence was forwarded long after the owner wrote it"
        self.write_agent_sent_pair(prompt, 9, 30)
        payload = self.run_json(extra=("--sources", "codex,devin"))
        self.assertIn(prompt, [t["text"] for t in payload["turns"] if t["source"] == "devin"])
        self.assertNotIn("agent_sent", payload["coverage"]["devin"]["excluded_by_reason"])

    def test_sender_tool_output_does_not_mark_agent_sent(self):
        prompt = "This owner sentence merely appeared inside a sender tool output"
        self.write_single_claude_text(prompt)
        anti = self.home / ".gemini" / "antigravity-cli" / "brain" / "output-only" / ".system_generated" / "logs" / "transcript_full.jsonl"
        output_record = {"step_index": 1, "source": "MODEL", "type": "GENERIC", "created_at": iso_local(DAY, 12, 59),
                         "content": "orca terminal send output: " + prompt}
        jsonl(anti, [output_record])
        touch_in_window(anti)
        payload = self.run_json(extra=("--sources", "antigravity,claude"))
        self.assertIn(prompt, [t["text"] for t in payload["turns"] if t["source"] == "claude"])
        self.assertNotIn("agent_sent", payload["coverage"]["claude"]["excluded_by_reason"])

    def test_antigravity_planner_tool_input_can_mark_agent_sent(self):
        prompt = "This owner sentence was sent through an antigravity planner input"
        self.write_single_claude_text(prompt)
        anti = self.home / ".gemini" / "antigravity-cli" / "brain" / "planner-input" / ".system_generated" / "logs" / "transcript_full.jsonl"
        record = {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": iso_local(DAY, 12, 59),
                  "tool_calls": [{"name": "run_command", "args": {"CommandLine": "orca terminal send --text '%s'" % prompt}}]}
        jsonl(anti, [record])
        touch_in_window(anti)
        payload = self.run_json(extra=("--sources", "antigravity,claude"))
        self.assertNotIn(prompt, [t["text"] for t in payload["turns"] if t["source"] == "claude"])
        self.assertEqual(payload["coverage"]["claude"]["excluded_by_reason"].get("agent_sent"), 1)

    def test_orca_suggestions_prompt_is_automated(self):
        self.write_single_claude_text("Look around: find what is waiting on your owner and suggest up to 5 next steps for their Suggestions list.\nDetails")
        proc = self.run_cli("--date", DAY.isoformat(), "--home", self.home, "--sources", "claude", "--no-orca")
        self.assertIn("orca_suggestions=1", proc.stdout)

    def write_repeated_codex_sessions(self, count, text):
        for index in range(count):
            path = self.home / ".codex" / "sessions" / "repeat" / ("rollout-repeat-%d.jsonl" % index)
            jsonl(path, [
                {"type": "session_meta", "payload": {"id": "repeat-%d" % index, "source": "cli"}},
                {"type": "response_item", "timestamp": iso_local(DAY, 10), "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": text}]}},
            ])
            touch_in_window(path)

    def test_repeated_template_three_sessions_automated(self):
        text = ("shared template text with enough letters to normalize into a long repeated prompt " * 12).strip()
        self.write_repeated_codex_sessions(3, text)
        payload = self.run_json(extra=("--sources", "codex"))
        self.assertEqual(payload["turns"], [])
        self.assertEqual(payload["coverage"]["codex"]["excluded_by_reason"].get("repeated_template"), 3)

    def test_repeated_template_two_sessions_stays_owner(self):
        text = ("shared template text with enough letters to normalize into a long repeated prompt " * 12).strip()
        self.write_repeated_codex_sessions(2, text)
        payload = self.run_json(extra=("--sources", "codex"))
        self.assertEqual(len(payload["turns"]), 2)
        self.assertNotIn("repeated_template", payload["coverage"]["codex"]["excluded_by_reason"])

    def test_agent_sent_non_orca_command_five_minutes_earlier(self):
        prompt = "Please investigate this exact issue and report back"
        arguments = json.dumps({"command": "agy -p '%s'" % prompt})
        self.write_agent_sent_pair(prompt, 10, 55, arguments=arguments)
        payload = self.run_json(extra=("--sources", "codex,devin"))
        self.assertNotIn(prompt, [t["text"] for t in payload["turns"] if t["source"] == "devin"])
        self.assertEqual(payload["coverage"]["devin"]["excluded_by_reason"].get("agent_sent"), 1)

    def test_agent_sent_write_file_input_forty_minutes_earlier(self):
        prompt = "A long brief written to a scratch file before launching the worker"
        arguments = json.dumps({"path": "/tmp/brief.md", "content": prompt})
        self.write_agent_sent_pair(prompt, 10, 20, arguments=arguments)
        payload = self.run_json(extra=("--sources", "codex,devin"))
        self.assertNotIn(prompt, [t["text"] for t in payload["turns"] if t["source"] == "devin"])
        self.assertEqual(payload["coverage"]["devin"]["excluded_by_reason"].get("agent_sent"), 1)

    def test_devin_sender_timestamp_from_message_nodes_milliseconds(self):
        prompt = "A devin brief long enough to match through the sender cross-check"
        db = self.home / ".local" / "share" / "devin" / "cli" / "sessions.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.execute("create table sessions (session_id text, working_directory text, last_activity_at integer)")
        conn.execute("create table prompt_history (id integer primary key, content text, timestamp integer, session_id text, is_shell integer)")
        conn.execute("create table tool_call_state (session_id text, tool_call_id text, tool_call_json text, tool_call_update_json text, source_node_ids text)")
        conn.execute("create table message_nodes (session_id text, node_id integer, created_at integer)")
        conn.execute("insert into sessions values ('sender-devin', '/tmp/sender', ?)", (epoch_local(DAY, 11),))
        conn.execute("insert into sessions values ('receiver', '/tmp/receiver', ?)", (epoch_local(DAY, 11),))
        conn.execute("insert into prompt_history values (?,?,?,?,?)", (1, prompt, epoch_local(DAY, 11), "receiver", 0))
        conn.execute("insert into tool_call_state values (?,?,?,?,?)", (
            "sender-devin", "call-1", json.dumps({"input": {"content": prompt}}), None, json.dumps([101])))
        conn.execute("insert into message_nodes values (?,?,?)", ("sender-devin", 101, epoch_local(DAY, 10, 50) * 1000))
        conn.commit()
        conn.close()
        touch_in_window(db)
        payload = self.run_json(extra=("--sources", "devin"))
        self.assertEqual(payload["turns"], [])
        self.assertEqual(payload["coverage"]["devin"]["excluded_by_reason"].get("agent_sent"), 1)

    def test_claude_interrupt_marker_is_automated(self):
        self.write_single_claude_text("[Request interrupted by user]")
        proc = self.run_cli("--date", DAY.isoformat(), "--home", self.home, "--sources", "claude", "--no-orca")
        self.assertIn("interrupt_marker=1", proc.stdout)
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual(payload["turns"], [])

    def test_leading_slash_command_stripped_for_all_providers(self):
        codex = self.home / ".codex" / "sessions" / "s" / "rollout-slash.jsonl"
        jsonl(codex, [
            {"type": "session_meta", "payload": {"id": "slash-codex", "source": "cli"}},
            {"type": "response_item", "timestamp": iso_local(DAY, 10), "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "/codex-task do codex"}]}},
            {"type": "response_item", "timestamp": iso_local(DAY, 10, 1), "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "$codex-task do dollar"}]}},
        ])
        touch_in_window(codex)
        anti = self.home / ".gemini" / "antigravity-cli" / "brain" / "slash-ag" / ".system_generated" / "logs" / "transcript_full.jsonl"
        jsonl(anti, [{"step_index": 1, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": iso_local(DAY, 11),
                      "content": "<USER_REQUEST>/anti-task do anti</USER_REQUEST>"}])
        touch_in_window(anti)
        claude = self.home / ".claude" / "projects" / "p" / "slash.jsonl"
        jsonl(claude, [self.claude_user("/claude-task do claude", DAY, 12, "slash-claude", promptSource="typed")])
        touch_in_window(claude)
        grok = self.home / ".grok" / "sessions" / "%2Fx" / "s" / "updates.jsonl"
        jsonl(grok, [{"method": "session/update", "timestamp": epoch_local(DAY, 13), "params": {"update": {
            "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "/grok-task do grok"},
            "_meta": {"promptIndex": 1}}}}])
        touch_in_window(grok)
        payload = self.run_json(extra=("--sources", "codex,antigravity,claude,grok"))
        texts = {(t["source"], t["text"]) for t in payload["turns"]}
        self.assertIn(("codex", "do codex"), texts)
        self.assertIn(("codex", "do dollar"), texts)
        self.assertIn(("antigravity", "do anti"), texts)
        self.assertIn(("claude", "do claude"), texts)
        self.assertIn(("grok", "do grok"), texts)

    def test_grok_repeated_chunk_text_is_not_duplicated(self):
        grok = self.home / ".grok" / "sessions" / "%2Fx" / "s" / "updates.jsonl"
        records = []
        for minute in range(2):
            records.append({"method": "session/update", "timestamp": epoch_local(DAY, 14, minute), "params": {"update": {
                "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "same replayed text"},
                "_meta": {"promptIndex": 9}}}})
        jsonl(grok, records)
        touch_in_window(grok)
        payload = self.run_json(extra=("--sources", "grok"))
        self.assertEqual([t["text"] for t in payload["turns"]], ["same replayed text"])

    def test_calibration_recheck_recomputes_predictions_and_reports_not_found(self):
        forwarded = "Forwarded calibration prompt with enough unique detail for matching"
        owner = "Plain calibration owner prompt"
        sender = self.home / ".codex" / "sessions" / "recheck-sender" / "rollout-recheck-sender.jsonl"
        jsonl(sender, [
            {"type": "session_meta", "payload": {"id": "recheck-sender-session", "source": "cli"}},
            {"type": "response_item", "timestamp": iso_local(DAY, 10, 59), "payload": {
                "type": "function_call", "arguments": json.dumps({"command": "send '%s'" % forwarded})}},
        ])
        touch_in_window(sender)
        receiver = self.home / ".codex" / "sessions" / "recheck-receiver" / "rollout-recheck-receiver.jsonl"
        jsonl(receiver, [
            {"type": "session_meta", "payload": {"id": "recheck-receiver-session", "source": "cli"}},
            {"type": "response_item", "timestamp": iso_local(DAY, 11), "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": forwarded}]}},
            {"type": "response_item", "timestamp": iso_local(DAY, 11, 1), "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": owner}]}},
        ])
        touch_in_window(receiver)
        calibration = self.root / "labeled.jsonl"
        rows = [
            {"id": "fixed-not", "source": "codex", "session_id": "recheck-receiver-session", "turn_id": "rollout-recheck-receiver.jsonl:2",
             "timestamp": iso_local(DAY, 11), "predicted": "owner", "label": "not"},
            {"id": "mine-ok", "source": "codex", "session_id": "recheck-receiver-session", "turn_id": "rollout-recheck-receiver.jsonl:3",
             "timestamp": iso_local(DAY, 11, 1), "predicted": "owner", "label": "mine"},
            {"id": "still-leak", "source": "codex", "session_id": "recheck-receiver-session", "turn_id": "rollout-recheck-receiver.jsonl:3",
             "timestamp": iso_local(DAY, 11, 1), "predicted": "owner", "label": "not"},
            {"id": "missing-row", "source": "codex", "session_id": "missing", "turn_id": "missing:1",
             "timestamp": iso_local(DAY, 11), "predicted": "owner", "label": "mine"},
        ]
        calibration.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        proc = self.run_cli("--calibration-recheck", calibration, "--home", self.home, "--sources", "codex", "--no-orca")
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertIn("labeled: 4", proc.stdout)
        self.assertIn("not_found: 1", proc.stdout)
        self.assertIn("not_found_ids: missing-row", proc.stdout)
        self.assertIn("leaks: 1", proc.stdout)
        self.assertIn("leak_ids: still-leak", proc.stdout)

    def test_placeholder_only_text_counts_empty_after_cleaning(self):
        self.write_single_claude_text("https://example.com/only")
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual(payload["turns"], [])
        self.assertEqual(payload["coverage"]["claude"]["empty_after_cleaning"], 1)

    def test_orca_automation_exclusion_and_prefix_strip(self):
        records = [
            self.claude_user("──── From: worker\nplease act", DAY, 13, "mail", promptSource="typed"),
            self.claude_user("You have 3 orchestration messages waiting", DAY, 13, "notice", promptSource="typed"),
            self.claude_user("You are working inside Orca now", DAY, 13, "launch", promptSource="typed"),
            self.claude_user('{"disableAllHooks":true}', DAY, 13, "namer", promptSource="typed"),
            self.claude_user("Automate the dependency review", DAY, 13, "auto", promptSource="typed"),
            self.claude_user("[Orca: worker abc]\nowner remainder", DAY, 13, "prefix", promptSource="typed"),
        ]
        path = self.home / ".claude" / "projects" / "p" / "s.jsonl"
        jsonl(path, records)
        touch_in_window(path)
        prompts = self.root / "prompts.json"
        prompts.write_text(json.dumps(["Automate the dependency review"]), encoding="utf-8")
        proc = self.run_cli("--date", DAY.isoformat(), "--home", self.home,
                            "--automation-prompts", prompts)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for reason in ("orca_mail", "orca_notice", "orca_launch", "chat_namer", "automation_prompt"):
            self.assertIn(reason, proc.stdout)
        payload = self.run_json(extra=("--automation-prompts", prompts))
        self.assertIn("owner remainder", [t["text"] for t in payload["turns"]])

    def test_midnight_boundaries_and_utc_conversion(self):
        root = self.home / ".codex" / "sessions" / "x"
        meta = {"type": "session_meta", "payload": {"id": "midnight", "source": "cli"}}
        before = {"type": "response_item", "timestamp": iso_local(DAY, 23, 59, 59),
                  "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "before midnight"}]}}
        after = {"type": "response_item", "timestamp": iso_local(DAY + timedelta(days=1), 0, 0, 0),
                 "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "after midnight"}]}}
        path = root / "rollout-midnight.jsonl"
        jsonl(path, [meta, before, after])
        ts = epoch_local(DAY + timedelta(days=1), 12)
        os.utime(path, (ts, ts))
        first = self.run_json(DAY, extra=("--sources", "codex"))
        second = self.run_json(DAY + timedelta(days=1), extra=("--sources", "codex"))
        self.assertEqual([t["text"] for t in first["turns"]], ["before midnight"])
        self.assertEqual(first["turns"][0]["timestamp"], "2026-01-15T23:59:59+07:00")
        self.assertEqual([t["text"] for t in second["turns"]], ["after midnight"])

    def test_large_session_read_fully_when_mtime_after_window_start(self):
        root = self.home / ".codex" / "sessions" / "bulk"
        meta = {"type": "session_meta", "payload": {"id": "bulk", "source": "cli"}}
        records = [meta]
        for index in range(120):
            records.append({"type": "response_item", "timestamp": iso_local(DAY, 10, index % 60),
                            "payload": {"type": "message", "role": "user", "content": [
                                {"type": "input_text", "text": "bulk turn %03d" % index}]}})
        path = root / "rollout-bulk.jsonl"
        jsonl(path, records)
        touch_in_window(path)
        payload = self.run_json(extra=("--sources", "codex"))
        self.assertEqual(len(payload["turns"]), 120)
        self.assertEqual(payload["coverage"]["codex"]["owner"], 120)

    def test_long_owner_prompt_cleaning(self):
        filler = "Please review this very long implementation carefully. " * 110
        text = filler + "\n```python\nsecret_code()\n```\nSee https://example.com/spec?x=1\nTail question?"
        rec = self.claude_user(text, DAY, 13, "long", promptSource="typed")
        path = self.home / ".claude" / "projects" / "p" / "s.jsonl"
        jsonl(path, [rec])
        touch_in_window(path)
        payload = self.run_json(extra=("--sources", "claude"))
        self.assertEqual(len(payload["turns"]), 1)
        turn = payload["turns"][0]
        self.assertGreater(len(turn["text"]), 5000)
        self.assertNotIn("secret_code", turn["text"])
        self.assertNotIn("https://example.com", turn["text"])
        self.assertIn("<url>", turn["text"])
        self.assertEqual(turn["removed"].get("code_block"), 1)
        self.assertEqual(turn["removed"].get("url"), 1)

    def test_dedup_antigravity_fallback_and_codex_fork(self):
        self.build_codex()
        self.build_antigravity()
        payload = self.run_json(extra=("--sources", "codex,antigravity"))
        self.assertEqual(sum(1 for t in payload["turns"] if t["source"] == "antigravity"), 1)
        self.assertEqual(sum(1 for t in payload["turns"] if t["text"] == "Codex owner text"), 1)
        self.assertEqual(payload["coverage"]["codex"]["owner"], 1)
        self.assertEqual(payload["coverage"]["antigravity"]["owner"], 1)
        self.assertEqual(payload["coverage"]["antigravity"]["files_scanned"], 1)

    def test_coverage_missing_corrupt_and_other_sources_continue(self):
        empty_home = self.root / "empty-home"
        empty_home.mkdir()
        proc = self.run_cli("--date", DAY.isoformat(), "--home", empty_home, "--no-orca")
        self.assertIn("codex: status=missing", proc.stdout)
        self.assertIn("grok: status=missing", proc.stdout)

        self.build_all()
        path = self.home / ".codex" / "sessions" / "2026" / "01" / "rollout-owner.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("not json\n")
        proc = self.run_cli("--date", DAY.isoformat(), "--home", self.home, "--no-orca")
        self.assertIn("codex: status=partial", proc.stdout)
        self.assertIn("corrupt json lines", proc.stdout)
        self.assertIn("claude: status=ok", proc.stdout)

    def test_summary_contains_no_turn_text(self):
        self.build_all()
        proc = self.run_cli("--date", DAY.isoformat(), "--home", self.home, "--no-orca")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for text in ("Codex owner text", "Antigravity owner text", "Devin owner text",
                     "Claude owner text", "Grok owner text"):
            self.assertNotIn(text, proc.stdout)

    def build_calibration_store(self, day):
        # More than --sample candidates exist; the sampler must still choose
        # evenly across sources with about five owner and three non-owner rows each.
        codex_dir = self.home / ".codex" / "sessions" / "cal"
        codex_records = [{"type": "session_meta", "payload": {"id": "cal-codex", "source": "cli"}}]
        for i in range(8):
            codex_records.append({"type": "response_item", "timestamp": iso_local(day, 9, i),
                                  "payload": {"type": "message", "role": "user", "content": [
                                      {"type": "input_text", "text": "codex owner %d" % i}]}})
        for i in range(5):
            codex_records.append({"type": "response_item", "timestamp": iso_local(day, 9, 10 + i),
                                  "payload": {"type": "message", "role": "user", "content": [
                                      {"type": "input_text", "text": "──── From: bot %d" % i}]}})
        p = codex_dir / "rollout-cal.jsonl"
        jsonl(p, codex_records)
        touch_in_window(p, day)

        logs = self.home / ".gemini" / "antigravity-cli" / "brain" / "cal-ag" / ".system_generated" / "logs"
        records = []
        for i in range(8):
            records.append({"step_index": i, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                            "created_at": iso_local(day, 10, i),
                            "content": "<USER_REQUEST>ag owner %d</USER_REQUEST>" % i})
        for i in range(5):
            records.append({"step_index": 10 + i, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                            "created_at": iso_local(day, 10, 10 + i),
                            "content": "<USER_REQUEST>You have 2 orchestration messages</USER_REQUEST>"})
        p = logs / "transcript_full.jsonl"
        jsonl(p, records)
        touch_in_window(p, day)

        db = self.home / ".local" / "share" / "devin" / "cli" / "sessions.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.execute("create table sessions (session_id text, working_directory text)")
        conn.execute("create table prompt_history (id integer primary key, content text, timestamp integer, session_id text, is_shell integer)")
        rows = []
        for i in range(8):
            rows.append((i + 1, "devin owner %d" % i, epoch_local(day, 11, i), "s", 0))
        for i in range(5):
            rows.append((10 + i, "shell", epoch_local(day, 11, 10 + i), "s", 1))
        conn.executemany("insert into prompt_history values (?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        touch_in_window(db, day)

        records = []
        for i in range(8):
            records.append(self.claude_user("claude owner %d" % i, day, 12, "co%d" % i, promptSource="typed"))
        for i in range(5):
            records.append(self.claude_user("meta %d" % i, day, 12, "cm%d" % i, isMeta=True, promptSource="typed"))
        p = self.home / ".claude" / "projects" / "cal" / "s.jsonl"
        jsonl(p, records)
        touch_in_window(p, day)

        records = []
        for i in range(8):
            records.append({"method": "session/update", "timestamp": epoch_local(day, 13, i), "params": {"update": {
                "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "grok owner %d" % i},
                "_meta": {"promptIndex": i}}}})
        for i in range(5):
            records.append({"method": "session/update", "timestamp": epoch_local(day, 13, 10 + i), "params": {"update": {
                "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "You are working inside Orca %d" % i},
                "_meta": {"promptIndex": 10 + i}}}})
        p = self.home / ".grok" / "sessions" / "%2Fcal" / "s" / "updates.jsonl"
        jsonl(p, records)
        touch_in_window(p, day)

    def test_calibration_export_writes_balanced_rows(self):
        yesterday = datetime.now(LOCAL_TZ).date() - timedelta(days=1)
        self.build_calibration_store(yesterday)
        proc = self.run_cli("--calibration-export", "--days", 1, "--sample", 40,
                            "--home", self.home, "--no-orca")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        files = list((self.data / "chat-review").glob("calibration-*.jsonl"))
        self.assertEqual(len(files), 1)
        rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 40)
        counts = {source: sum(1 for row in rows if row["source"] == source) for source in
                  ("codex", "antigravity", "devin", "claude", "grok")}
        self.assertEqual(counts, {"codex": 8, "antigravity": 8, "devin": 8, "claude": 8, "grok": 8})
        for source in counts:
            self.assertEqual(sum(1 for row in rows if row["source"] == source and row["predicted"] == "owner"), 5)
            self.assertEqual(sum(1 for row in rows if row["source"] == source and row["predicted"] != "owner"), 3)
        self.assertTrue(all(row["label"] is None for row in rows))
        self.assertIn("rows: 40", proc.stdout)

    def test_calibration_score_reports_leak_and_miss_rate(self):
        path = self.root / "labeled.jsonl"
        rows = [
            {"id": "a", "predicted": "owner", "label": "not"},
            {"id": "b", "predicted": "owner", "label": "mine"},
            {"id": "c", "predicted": "automated", "label": "mine"},
            {"id": "d", "predicted": "unknown", "label": "mine"},
            {"id": "e", "predicted": "automated", "label": "not"},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        proc = self.run_cli("--calibration-score", path)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("leaks: 1", proc.stdout)
        self.assertIn("leak_ids: a", proc.stdout)
        self.assertIn("misses: 2", proc.stdout)
        self.assertIn("miss_ids: c,d", proc.stdout)
        self.assertIn("miss_rate: 0.667", proc.stdout)


if __name__ == "__main__":
    unittest.main()

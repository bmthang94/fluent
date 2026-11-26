#!/usr/bin/env python3
"""
Read local coding-agent chat history for one local day.

Extracts only turns plausibly written personally by the machine owner, with
provenance and cleaning metadata. The tool is read-only for all provider stores.

Usage:
    read-chat-history.py --date YYYY-MM-DD [--sources codex,antigravity,devin,claude,grok]
                         [--format summary|json] [--home PATH]
                         [--automation-prompts FILE | --no-orca]
    read-chat-history.py --calibration-export [--days 7] [--sample 40] ...
    read-chat-history.py --calibration-score FILE
"""
import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fluent_paths import data_dir, ensure_data_dir, force_utf8_io  # noqa: E402

force_utf8_io()

LOCAL_TZ = timezone(timedelta(hours=7))
SOURCES = ("codex", "antigravity", "devin", "claude", "grok")
ORCA_JOURNAL_DISABLED = object()


def iso_to_dt(value):
    if not value or not isinstance(value, str):
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ)
    except (ValueError, OverflowError):
        return None


def epoch_to_dt(value):
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone(LOCAL_TZ)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def local_iso(dt):
    return dt.astimezone(LOCAL_TZ).isoformat()


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def day_window(day):
    start = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ)
    return start, start + timedelta(days=1)


def in_window(dt, start, end):
    return dt is not None and start <= dt < end


def new_coverage():
    return {
        "status": "ok",
        "files_scanned": 0,
        "turns_in_window": 0,
        "owner": 0,
        "automated": 0,
        "unknown": 0,
        "empty_after_cleaning": 0,
        "orca_matched_owner": 0,
        "orca_matched_automated": 0,
        "excluded_by_reason": {},
        "notes": [],
    }


def note(coverage, text):
    if text not in coverage["notes"]:
        coverage["notes"].append(text)
    if coverage["status"] == "ok" and text != "automation cross-check unavailable":
        coverage["status"] = "partial"


def add_reason(coverage, reason):
    if reason:
        coverage["excluded_by_reason"][reason] = coverage["excluded_by_reason"].get(reason, 0) + 1


def prefiltered(files, start, coverage):
    out = []
    start_ts = start.timestamp()
    for path in files:
        try:
            if path.stat().st_mtime >= start_ts:
                out.append(path)
        except OSError as exc:
            note(coverage, "unreadable file metadata: %s" % exc.__class__.__name__)
    return out


def read_text_lines(path, coverage):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.readlines()
    except OSError as exc:
        note(coverage, "unreadable file: %s" % exc.__class__.__name__)
        return None


def parse_json_lines(path, coverage):
    lines = read_text_lines(path, coverage)
    if lines is None:
        return []
    records = []
    bad = 0
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            records.append((lineno, json.loads(line)))
        except json.JSONDecodeError:
            bad += 1
    if bad:
        note(coverage, "corrupt json lines: %d" % bad)
    return records


def content_blocks_text(blocks):
    texts = []
    if not isinstance(blocks, list):
        return texts
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    return texts


def codex_session_id(meta_payload, path):
    for key in ("id", "session_id", "thread_id"):
        value = meta_payload.get(key)
        if value:
            return str(value)
    return path.stem


def is_xmlish_wrapper(text):
    stripped = text.strip()
    if not (stripped.startswith("<") and stripped.endswith(">")):
        return False
    if re.match(r"^</[A-Za-z_][\w:.-]*\s*>$", stripped):
        return True
    if re.match(r"^<[A-Za-z_][\w:.-]*(\s[^>]*)?/>$", stripped):
        return True
    if re.match(r"^<[A-Za-z_][\w:.-]*(\s[^>]*)?>$", stripped):
        return True
    return bool(re.match(r"^<([A-Za-z_][\w:.-]*)(\s[^>]*)?>[\s\S]*</\1>$", stripped))


def read_codex(home, start, end):
    coverage = new_coverage()
    turns = []
    root = home / ".codex" / "sessions"
    if not root.exists():
        coverage["status"] = "missing"
        return turns, coverage
    files = sorted(root.glob("**/rollout-*.jsonl"))
    for path in prefiltered(files, start, coverage):
        coverage["files_scanned"] += 1
        records = parse_json_lines(path, coverage)
        meta_payload = {}
        for _, rec in records:
            if rec.get("type") == "session_meta" and isinstance(rec.get("payload"), dict):
                meta_payload = rec["payload"]
                break
        source = meta_payload.get("source")
        session_reason = None
        if source == "exec":
            session_reason = "codex_exec"
        elif meta_payload.get("parent_thread_id") is not None:
            session_reason = "codex_subagent"
        elif isinstance(source, dict) and "subagent" in json.dumps(source).lower():
            session_reason = "codex_subagent"
        session_id = codex_session_id(meta_payload, path)
        cwd = meta_payload.get("cwd") or ""
        for lineno, rec in records:
            if rec.get("type") != "response_item" or not isinstance(rec.get("payload"), dict):
                continue
            payload = rec["payload"]
            payload_type = payload.get("type")
            dt = iso_to_dt(rec.get("timestamp"))
            if payload_type in ("function_call", "custom_tool_call", "local_shell_call"):
                if dt is not None and in_window(dt, start, end):
                    for field in ("arguments", "input", "action"):
                        if field in payload:
                            add_sent_text(coverage, payload.get(field), "codex", session_id, dt)
                continue
            if payload_type != "message" or payload.get("role") != "user":
                continue
            if dt is None:
                note(coverage, "turn missing timestamp")
                continue
            if not in_window(dt, start, end):
                continue
            kept = []
            content = payload.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "input_text":
                        continue
                    text = block.get("text")
                    if not isinstance(text, str):
                        continue
                    stripped = text.strip()
                    if is_xmlish_wrapper(stripped) or stripped.startswith("# AGENTS.md"):
                        continue
                    kept.append(text)
            raw = "\n".join(kept)
            provenance = "automated" if session_reason else "owner"
            turns.append({
                "source": "codex", "session_id": session_id,
                "turn_id": "%s:%d" % (path.name, lineno), "dt": dt,
                "cwd": cwd, "provenance": provenance,
                "reason": session_reason or "provider_user_message",
                "raw_text": raw,
            })
    return turns, coverage


def read_antigravity(home, start, end):
    coverage = new_coverage()
    turns = []
    root = home / ".gemini" / "antigravity-cli" / "brain"
    if not root.exists():
        coverage["status"] = "missing"
        return turns, coverage
    candidates = []
    for brain in sorted(root.iterdir()):
        if not brain.is_dir():
            continue
        logdir = brain / ".system_generated" / "logs"
        full = logdir / "transcript_full.jsonl"
        fallback = logdir / "transcript.jsonl"
        if full.exists():
            candidates.append((brain.name, full))
        elif fallback.exists():
            candidates.append((brain.name, fallback))
    selected = []
    files_by_name = {path: session_id for session_id, path in candidates}
    for path in prefiltered([p for _, p in candidates], start, coverage):
        selected.append((files_by_name[path], path))
    for session_id, path in selected:
        coverage["files_scanned"] += 1
        for lineno, rec in parse_json_lines(path, coverage):
            if not isinstance(rec, dict):
                continue
            if rec.get("truncated_fields"):
                if "truncated fields present" not in coverage["notes"]:
                    coverage["notes"].append("truncated fields present")
            dt = iso_to_dt(rec.get("created_at"))
            if rec.get("source") == "MODEL" and rec.get("type") == "PLANNER_RESPONSE" and dt is not None and in_window(dt, start, end):
                if "tool_calls" in rec:
                    add_sent_text(coverage, rec.get("tool_calls"), "antigravity", session_id, dt)
            if rec.get("source") != "USER_EXPLICIT" or rec.get("type") != "USER_INPUT":
                continue
            if dt is None:
                note(coverage, "turn missing timestamp")
                continue
            if not in_window(dt, start, end):
                continue
            content = rec.get("content") or ""
            match = re.search(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>", content)
            raw = match.group(1) if match else ""
            turns.append({
                "source": "antigravity", "session_id": session_id,
                "turn_id": rec.get("step_index", lineno), "dt": dt,
                "cwd": "", "provenance": "owner", "reason": "provider_user_message",
                "raw_text": raw,
            })
    return turns, coverage


def sqlite_ro_path(path):
    absolute = str(path.resolve())
    return "file:%s?mode=ro" % urllib.parse.quote(absolute)


def read_devin(home, start, end):
    coverage = new_coverage()
    turns = []
    path = home / ".local" / "share" / "devin" / "cli" / "sessions.db"
    if not path.exists():
        coverage["status"] = "missing"
        return turns, coverage
    try:
        mtimes = [path.stat().st_mtime]
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                mtimes.append(sidecar.stat().st_mtime)
        if max(mtimes) < start.timestamp():
            return turns, coverage
    except OSError:
        note(coverage, "unreadable file metadata: OSError")
        return turns, coverage
    coverage["files_scanned"] = 1
    try:
        conn = sqlite3.connect(sqlite_ro_path(path), uri=True)
        try:
            cwd_by_session = {}
            try:
                columns = {row[1] for row in conn.execute("pragma table_info(sessions)")}
                id_column = "session_id" if "session_id" in columns else "id"
                if id_column in columns and "working_directory" in columns:
                    for sid, cwd in conn.execute("select %s, working_directory from sessions" % id_column):
                        cwd_by_session[str(sid)] = cwd or ""
                else:
                    note(coverage, "sessions table unavailable")
            except sqlite3.Error:
                note(coverage, "sessions table unavailable")
            rows = conn.execute(
                "select id, content, timestamp, session_id, is_shell from prompt_history"
            ).fetchall()
            try:
                tool_columns = {row[1] for row in conn.execute("pragma table_info(tool_call_state)")}
                session_columns = {row[1] for row in conn.execute("pragma table_info(sessions)")}
                join_column = "session_id" if "session_id" in session_columns else "id"
                if {"session_id", "tool_call_json"} <= tool_columns and join_column in session_columns and "last_activity_at" in session_columns:
                    cutoff = (start - timedelta(days=1)).timestamp()
                    source_column = "t.source_node_ids" if "source_node_ids" in tool_columns else "null"
                    sent_query = (
                        "select t.tool_call_json, t.session_id, {source_column} from tool_call_state t "
                        "join sessions s on s.{join_column} = t.session_id "
                        "where coalesce(s.last_activity_at, 0) >= ?"
                    ).format(source_column=source_column, join_column=join_column)
                    sent_rows = conn.execute(sent_query, (cutoff,)).fetchall()
                    node_columns = {row[1] for row in conn.execute("pragma table_info(message_nodes)")}
                    nodes_available = {"session_id", "node_id", "created_at"} <= node_columns
                    session_ts_magnitude = 0
                    if "created_at" in session_columns:
                        magnitude_row = conn.execute("select max(created_at) from sessions").fetchone()
                        session_ts_magnitude = (magnitude_row[0] or 0) if magnitude_row else 0
                    for (tool_json, sent_session_id, source_node_ids) in sent_rows:
                        sender_dt = None
                        if nodes_available and source_node_ids:
                            try:
                                node_ids = json.loads(source_node_ids)
                                if isinstance(node_ids, list) and node_ids:
                                    placeholders = ",".join("?" for _ in node_ids)
                                    row = conn.execute(
                                        "select min(created_at) from message_nodes where session_id = ? and node_id in (%s)" % placeholders,
                                        [sent_session_id] + node_ids,
                                    ).fetchone()
                                    created = row[0] if row else None
                                    if created is not None:
                                        created = float(created)
                                        # session timestamps are normally seconds; accept ms node
                                        # timestamps when they are clearly in that unit.
                                        if created > 1e12 or (session_ts_magnitude > 1e12 and created > 1e11):
                                            created /= 1000.0
                                        sender_dt = epoch_to_dt(created)
                            except (json.JSONDecodeError, TypeError, ValueError, sqlite3.Error):
                                sender_dt = None
                        add_sent_text(coverage, tool_json, "devin", sent_session_id, sender_dt)
            except sqlite3.Error:
                note(coverage, "sender cross-check unavailable")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        coverage["status"] = "error"
        coverage["notes"].append("sqlite error: %s" % exc.__class__.__name__)
        return turns, coverage
    for row in rows:
        try:
            ident, content, ts, sid, is_shell = row
        except (TypeError, ValueError):
            note(coverage, "malformed prompt_history row")
            continue
        dt = epoch_to_dt(ts)
        if dt is None:
            note(coverage, "turn missing timestamp")
            continue
        if not in_window(dt, start, end):
            continue
        raw = content or ""
        provenance = "owner"
        reason = "provider_user_message"
        if is_shell:
            provenance = "automated"
            reason = "shell_command"
        else:
            raw = re.sub(r"^[/\$][\w:.-]+\s*", "", raw)
        turns.append({
            "source": "devin", "session_id": str(sid or ""), "turn_id": ident,
            "dt": dt, "cwd": cwd_by_session.get(str(sid), ""),
            "provenance": provenance, "reason": reason, "raw_text": raw,
        })
    return turns, coverage


def claude_string_content(content):
    """Return (kind, text) for Claude string user content."""
    if not isinstance(content, str):
        return None, ""
    if "<command-name>" in content:
        args = re.search(r"<command-args>([\s\S]*?)</command-args>", content)
        return "command", args.group(1) if args else ""
    for prefix, reason in (
        ("<local-command-stdout>", "injected"),
        ("<local-command-caveat>", "injected"),
        ("<task-notification>", "injected"),
        ("<system-reminder>", "injected"),
    ):
        if content.startswith(prefix):
            return reason, content
    return "plain", content


def read_claude(home, start, end):
    coverage = new_coverage()
    turns = []
    root = home / ".claude" / "projects"
    if not root.exists():
        coverage["status"] = "missing"
        return turns, coverage
    files = sorted(root.glob("*/*.jsonl"))
    # This depth deliberately excludes */<session>/subagents/*.jsonl.
    for path in prefiltered(files, start, coverage):
        coverage["files_scanned"] += 1
        for lineno, rec in parse_json_lines(path, coverage):
            if not isinstance(rec, dict):
                continue
            if rec.get("type") == "assistant":
                dt = iso_to_dt(rec.get("timestamp"))
                if dt is not None and in_window(dt, start, end):
                    message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
                    content = message.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use" and "input" in block:
                                add_sent_text(coverage, block.get("input"), "claude", rec.get("sessionId") or path.stem, dt)
                continue
            if rec.get("type") != "user":
                continue
            content = (rec.get("message") or {}).get("content") if isinstance(rec.get("message"), dict) else rec.get("content")
            has_tool_result = "toolUseResult" in rec
            if isinstance(content, list):
                has_tool_result = has_tool_result or any(
                    isinstance(block, dict) and block.get("type") == "tool_result" for block in content
                )
            if has_tool_result:
                continue  # Silent skip: these are not turns at all.
            dt = iso_to_dt(rec.get("timestamp"))
            if dt is None:
                note(coverage, "turn missing timestamp")
                continue
            if not in_window(dt, start, end):
                continue
            prompt_source = rec.get("promptSource")
            origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else {}
            provenance = None
            reason = None
            if rec.get("isSidechain"):
                provenance, reason = "automated", "sidechain"
            elif rec.get("isMeta"):
                provenance, reason = "automated", "meta"
            elif rec.get("isCompactSummary"):
                provenance, reason = "automated", "compaction"
            elif prompt_source == "system" or origin.get("kind") == "task-notification":
                provenance, reason = "automated", "system_notification"
            elif prompt_source == "suggestion_accepted":
                provenance, reason = "automated", "accepted_suggestion"
            elif origin.get("kind") == "human" or prompt_source in ("typed", "queued", "sdk") or prompt_source is None:
                provenance, reason = "owner", "provider_user_message"
            else:
                provenance, reason = "unknown", "unrecognized_prompt_source"

            if isinstance(content, str):
                kind, raw = claude_string_content(content)
                if kind == "injected":
                    provenance, reason = "automated", "injected"
                elif kind == "command":
                    raw = raw or ""
            elif isinstance(content, list):
                raw = "\n".join(content_blocks_text(content))
            else:
                raw = ""
            turns.append({
                "source": "claude", "session_id": str(rec.get("sessionId") or path.stem),
                "turn_id": str(rec.get("uuid") or "%s:%d" % (path.name, lineno)),
                "dt": dt, "cwd": rec.get("cwd") or "",
                "provenance": provenance, "reason": reason, "raw_text": raw,
            })
    return turns, coverage


def read_grok(home, start, end):
    coverage = new_coverage()
    turns = []
    root = home / ".grok" / "sessions"
    if not root.exists():
        coverage["status"] = "missing"
        return turns, coverage
    update_files = []
    missing_updates = 0
    session_meta = {}
    for cwd_dir in sorted(root.iterdir()):
        if not cwd_dir.is_dir():
            continue
        for session_dir in sorted(cwd_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            updates = session_dir / "updates.jsonl"
            if updates.exists():
                update_files.append(updates)
                session_meta[updates] = (session_dir.name, urllib.parse.unquote(cwd_dir.name))
            else:
                missing_updates += 1
    if missing_updates:
        note(coverage, "no_timestamps: %d session(s) without updates.jsonl" % missing_updates)
    for path in prefiltered(update_files, start, coverage):
        coverage["files_scanned"] += 1
        session_id, cwd = session_meta[path]
        chunks = defaultdict(list)
        order = []
        for _, rec in parse_json_lines(path, coverage):
            if not isinstance(rec, dict):
                continue
            params = rec.get("params") if isinstance(rec.get("params"), dict) else {}
            update = params.get("update") if isinstance(params.get("update"), dict) else {}
            content = update.get("content") if isinstance(update.get("content"), dict) else {}
            session_update = update.get("sessionUpdate")
            if session_update == "tool_call":
                dt = epoch_to_dt(rec.get("timestamp"))
                if dt is not None and in_window(dt, start, end):
                    input_fields = {key: update[key] for key in ("rawInput", "title", "kind") if key in update}
                    add_sent_text(coverage, input_fields, "grok", session_id, dt)
            if session_update != "user_message_chunk" or content.get("type") != "text":
                continue
            meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
            prompt_index = meta.get("promptIndex")
            if prompt_index is None:
                note(coverage, "user chunk missing promptIndex")
                continue
            dt = epoch_to_dt(rec.get("timestamp"))
            if dt is None:
                note(coverage, "turn missing timestamp")
                continue
            text = content.get("text") or ""
            if text.startswith("<system-reminder") or text.startswith("<monitor-event"):
                continue
            key = str(prompt_index)
            if key not in chunks:
                order.append(key)
            if any(existing == text for _, existing in chunks[key]):
                continue
            chunks[key].append((dt, text))
        for key in order:
            items = chunks[key]
            if not items:
                continue
            dt = items[0][0]
            if not in_window(dt, start, end):
                continue
            raw = "".join(item[1] for item in items)
            turns.append({
                "source": "grok", "session_id": session_id, "turn_id": key if not key.isdigit() else int(key),
                "dt": dt, "cwd": cwd, "provenance": "owner",
                "reason": "provider_user_message", "raw_text": raw,
            })
    return turns, coverage


READERS = {
    "codex": read_codex,
    "antigravity": read_antigravity,
    "devin": read_devin,
    "claude": read_claude,
    "grok": read_grok,
}


ORCA_PREFIX_RE = re.compile(r"^\[(?:Orca:|Your SOUL\.md)[\s\S]*?\][ \t]*(?:\n|$)")


def strip_leading_orca_context_block(text):
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "<<<":
        return text, 0
    for index, line in enumerate(lines[1:], 1):
        if line.rstrip("\r\n") == ">>>":
            return "".join(lines[index + 1:]), 1
    return "", 1


def strip_orca_prefix_and_context(text):
    current = text
    context_count = 0
    while True:
        stripped = current.lstrip()
        without_prefix = ORCA_PREFIX_RE.sub("", stripped, count=1)
        if without_prefix != stripped:
            current = without_prefix
            continue
        without_context, count = strip_leading_orca_context_block(stripped)
        if count:
            context_count += count
            current = without_context
            continue
        return stripped, context_count


def strip_orca_prefix(text):
    return strip_orca_prefix_and_context(text)[0]


def strip_orca_context_blocks(text):
    lines = text.splitlines(keepends=True)
    output = []
    count = 0
    in_block = False
    for line in lines:
        marker = line.rstrip("\r\n")
        if not in_block and marker == "<<<":
            in_block = True
            continue
        if in_block:
            if marker == ">>>":
                in_block = False
                count += 1
            continue
        output.append(line)
    if in_block:
        count += 1
    return "".join(output), count

def is_orca_header_line(line):
    stripped = line.strip()
    return (
        (stripped.startswith("[Orca") or stripped.startswith("[Your SOUL.md") or stripped.startswith("[Your MEMORY.md"))
        and stripped.endswith("]")
    )


def starts_orca_header(text):
    stripped = text.lstrip()
    return stripped.startswith("[Orca") or stripped.startswith("[Your SOUL.md") or stripped.startswith("[Your MEMORY.md")


def drop_header_and_attached_block(lines, header_index):
    for index in range(header_index + 1, len(lines)):
        if not lines[index].strip():
            return "\n".join(lines[index + 1:])
    return ""


def strip_first_run_section(text):
    lines = text.lstrip().splitlines()
    if not lines or lines[0].strip() != "## First run":
        return text
    index = 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    while index < len(lines) and lines[index].lstrip().startswith("- "):
        # Consume this bullet paragraph, including continuation lines, then skip
        # the blank separator. Another bullet paragraph may follow.
        while index < len(lines) and lines[index].strip():
            index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
    return "\n".join(lines[index:])


def strip_orca_envelope_and_prefixes(text):
    """Strip leading Orca headers while preserving the owner remainder.

    Multi-header bot envelopes have instructional paragraphs between headers; for
    those, the last header's attached paragraph block is instructions too. Bare
    one-line prefixes (and prefixes whose context blocks were already removed)
    are stripped line-by-line so a directly following owner line survives.
    """
    current = text
    envelope_count = 0
    while True:
        stripped = current.lstrip()
        if not starts_orca_header(stripped):
            return stripped, envelope_count, False
        lines = stripped.splitlines()
        if not lines or not lines[0].strip().endswith("]"):
            return "", envelope_count, True
        header_indices = [index for index, line in enumerate(lines) if is_orca_header_line(line)]
        if not header_indices or header_indices[0] != 0:
            return "", envelope_count, True
        if len(header_indices) == 1:
            header_index = header_indices[0]
            has_blank = any(not line.strip() for line in lines[header_index + 1:])
            if has_blank:
                current = strip_first_run_section(drop_header_and_attached_block(lines, header_index))
                envelope_count += 1
                continue
            current = "\n".join(lines[1:])
            continue
        last_header = header_indices[-1]
        has_instruction_content = any(
            line.strip() and index not in header_indices
            for index, line in enumerate(lines[:last_header])
        )
        if has_instruction_content:
            current = strip_first_run_section(drop_header_and_attached_block(lines, last_header))
            envelope_count += 1
            continue
        current = "\n".join(lines[1:])


def normalize_ws(text):
    return " ".join((text or "").split())


def stringify_tool_value(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def add_sent_text(coverage, value, source, session_id, timestamp):
    text = stringify_tool_value(value)
    if text:
        coverage.setdefault("_sent_texts", []).append({
            "text": text,
            "source": source,
            "session_id": str(session_id or ""),
            "dt": timestamp,
        })


def decode_escapes_for_matching(text):
    # Tool payloads reach us as JSON text, shell commands, or both. Decode complete
    # JSON values where possible (sometimes nested), then handle common shell escapes;
    # malformed input is simply left as-is.
    current = text or ""
    for _ in range(2):
        try:
            value = json.loads(current)
        except (json.JSONDecodeError, TypeError):
            break
        if isinstance(value, str):
            current = value
        else:
            current = json.dumps(value, ensure_ascii=False)
    return current.replace("\\n", "\n").replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


def normalize_machine_text(text):
    decoded = decode_escapes_for_matching(text).lower()
    return "".join(re.findall(r"[^\W_]", decoded, flags=re.UNICODE))


def normalized_sent_strings(values):
    normalized = []
    for value in values:
        machine = normalize_machine_text(value["text"])
        if len(machine) >= 20:
            normalized.append({
                "normalized": machine,
                "source": value["source"],
                "session_id": value["session_id"],
                "dt": value["dt"],
            })
    return normalized


def sent_matches_turn(sent, turn, machine_prefix):
    if machine_prefix not in sent["normalized"]:
        return False
    if sent["source"] == turn["source"] and sent["session_id"] == str(turn["session_id"]):
        return False
    sender_dt = sent.get("dt")
    if sender_dt is not None:
        turn_dt = turn["dt"]
        if not (turn_dt - timedelta(minutes=60) <= sender_dt <= turn_dt + timedelta(minutes=2)):
            return False
    return True


def load_automation_prompts(args):
    if args.automation_prompts:
        try:
            with open(args.automation_prompts, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            if isinstance(value, list):
                return [str(x) for x in value], None
            return [], "automation cross-check unavailable"
        except (OSError, json.JSONDecodeError) as exc:
            return [], "automation cross-check unavailable"
    if args.no_orca:
        return [], "automation cross-check unavailable"
    exe = shutil.which("orca")
    if not exe:
        return [], "automation cross-check unavailable"
    try:
        proc = subprocess.run(
            [exe, "automations", "list", "--json"], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=15, check=False, text=True,
        )
        if proc.returncode != 0:
            return [], "automation cross-check unavailable"
        payload = json.loads(proc.stdout)
        automations = payload.get("result", {}).get("automations", [])
        return [str(item.get("prompt", "")) for item in automations if item.get("prompt")], None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, AttributeError):
        return [], "automation cross-check unavailable"


def is_orca_transcript_replay(text):
    lines = text.splitlines()
    if any(line.startswith("[Orca: some saved chat messages or tool details omitted") for line in lines):
        return True
    if any(line.strip() == "[Orca: middle omitted]" for line in lines):
        return True
    if "Briefly state where the previous session stopped" in text:
        return True
    return sum(1 for line in lines if line.startswith("[assistant] ")) >= 2


def automation_reason(text, prompts):
    stripped = text.lstrip()
    if stripped.startswith("────") or "──── From: " in stripped:
        return "orca_mail"
    if re.match(r"^\s*You have \d+ orchestration messages?\b", text):
        return "orca_notice"
    if stripped.startswith("You are working inside Orca"):
        return "orca_launch"
    if stripped.startswith("Continue work from the prior Orca session") or stripped.startswith("Orca restarted, which stopped"):
        return "orca_resume"
    if stripped.startswith("This session is being continued from a previous conversation"):
        return "compaction"
    if stripped.startswith('{"disableAllHooks":true}') or stripped.startswith("Name this chat from the user's first request"):
        return "chat_namer"
    if any(line == "=== TASK ===" for line in text.splitlines()):
        return "orca_dispatch"
    if "You are a dispatched worker" in text:
        return "orca_dispatch"
    if re.match(r"^# Task: Item #\d+", stripped):
        return "orca_dispatch"
    if stripped.startswith("Look around: find what is waiting on your owner"):
        return "orca_suggestions"
    normalized = normalize_ws(text)
    for prompt in prompts:
        pnorm = normalize_ws(prompt)
        if pnorm and (normalized == pnorm or normalized.startswith(pnorm[:200])):
            return "automation_prompt"
    return None


def journal_payload_json(text):
    try:
        return json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def journal_message_text(payload):
    body = payload.get("body") if isinstance(payload.get("body"), dict) else None
    if body is None and isinstance(payload.get("payload"), dict):
        body = payload["payload"].get("body") if isinstance(payload["payload"].get("body"), dict) else None
    blocks = body.get("blocks") if isinstance(body, dict) else []
    texts = []
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
    return "\n".join(texts)


def journal_classification(attempt_payload):
    origin = attempt_payload.get("origin")
    source = attempt_payload.get("source")
    kind = source.get("kind") if isinstance(source, dict) else None
    if origin == "client" and kind in ("user", None):
        return "owner", "orca_client_submission"
    if origin == "host" and kind == "agent":
        return "automated", "agent_sent"
    if origin == "host" and kind is None:
        if attempt_payload.get("queuedMessageId"):
            return "owner", "orca_host_queued"
        return "automated", "orca_system"
    if origin == "host" and kind == "user":
        return "unknown", "orca_host_user"
    return None, None


def native_id_from_record(record_payload, fallback_column=None):
    if fallback_column:
        return str(fallback_column)
    chain = record_payload.get("providerHandleChain")
    if isinstance(chain, list):
        for link in reversed(chain):
            if isinstance(link, dict):
                handle = link.get("handle")
                if isinstance(handle, dict):
                    for key in ("nativeId", "sessionId", "threadId"):
                        if handle.get(key):
                            return str(handle[key])
    for key in ("adopted_native_id", "adoptedNativeId", "nativeId"):
        if record_payload.get(key):
            return str(record_payload[key])
    return ""


def load_orca_journal(db_path, start, end):
    """Load journal submissions for this window. Returns (rows, error_note)."""
    path = Path(db_path).expanduser() if db_path else None
    if not path or not path.exists():
        return [], "orca journal unavailable"
    try:
        conn = sqlite3.connect(sqlite_ro_path(path), uri=True)
        try:
            record_columns = {row[1] for row in conn.execute("pragma table_info(agent_session_records)")}
            adopted_column = "adopted_native_id" if "adopted_native_id" in record_columns else None
            select_adopted = ", adopted_native_id" if adopted_column else ", null"
            native_by_session = {}
            try:
                for orca_session_id, record_json, adopted in conn.execute(
                    "select session_id, record_json%s from agent_session_records" % select_adopted
                ):
                    record_payload = journal_payload_json(record_json)
                    provider = record_payload.get("provider")
                    if provider not in ("codex", "claude", "antigravity", "grok", "devin"):
                        continue
                    native_id = native_id_from_record(record_payload, adopted)
                    if native_id:
                        native_by_session[str(orca_session_id)] = (provider, native_id)
            except sqlite3.Error:
                return [], "orca journal unavailable"

            start_ms = int((start - timedelta(days=1)).timestamp() * 1000)
            end_ms = int((end + timedelta(days=1)).timestamp() * 1000)
            rows = []
            query = (
                "select a.session_id, a.epoch, a.item_id, a.submitted_at, a.payload_json, i.payload_json "
                "from journal_submission_attempts a "
                "join journal_items i on i.session_id = a.session_id and i.epoch = a.epoch and i.item_id = a.item_id "
                "where a.current = 1 and a.submitted_at >= ? and a.submitted_at <= ?"
            )
            for session_id, epoch, item_id, submitted_at, attempt_json, item_json in conn.execute(query, (start_ms, end_ms)):
                mapped = native_by_session.get(str(session_id))
                if not mapped:
                    continue
                provider, native_id = mapped
                attempt_payload = journal_payload_json(attempt_json)
                provenance, reason = journal_classification(attempt_payload)
                if not provenance:
                    continue
                text = journal_message_text(journal_payload_json(item_json))
                submitted_dt = epoch_to_dt((float(submitted_at) / 1000.0) if submitted_at is not None else None)
                if submitted_dt is None:
                    continue
                rows.append({
                    "provider": provider,
                    "native_id": native_id,
                    "dt": submitted_dt,
                    "text": text,
                    "provenance": provenance,
                    "reason": reason,
                    "key": (str(session_id), str(epoch), str(item_id), float(submitted_at)),
                })
            return rows, None
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return [], "orca journal unavailable"


def journal_normalized_candidate(raw):
    without_context, _ = strip_orca_context_blocks(raw)
    candidate, _, _ = strip_orca_envelope_and_prefixes(without_context)
    candidate = re.sub(r"^[/\$][\w:.-]+\s*", "", candidate.lstrip())
    return normalize_machine_text(candidate)


def match_journal_submissions(candidates, submissions):
    by_key = defaultdict(list)
    for submission in submissions:
        submission["normalized"] = normalize_machine_text(submission["text"])
        if submission["normalized"]:
            by_key[(submission["provider"], str(submission["native_id"]))].append(submission)

    candidate_meta = {}
    pairs = []
    for turn_index, turn in enumerate(candidates):
        key = (turn["source"], str(turn["session_id"]))
        possible = by_key.get(key, [])
        if not possible:
            continue
        raw = turn.get("raw_text", "")
        stripped_normalized = journal_normalized_candidate(raw)
        raw_normalized = stripped_normalized
        candidate_meta[turn_index] = (raw_normalized, stripped_normalized)
        for sub_index, submission in enumerate(possible):
            delta = abs((turn["dt"] - submission["dt"]).total_seconds())
            if delta > 600:
                continue
            normalized = submission["normalized"]
            if len(normalized) < 20:
                matched = bool(normalized) and normalized == stripped_normalized
            else:
                matched = normalized[:80] in raw_normalized
            if matched:
                pairs.append((delta, turn_index, id(submission), submission))
    pairs.sort()
    assigned_turns = set()
    assigned_submissions = set()
    matches = {}
    for _, turn_index, submission_id, submission in pairs:
        if turn_index in assigned_turns or submission_id in assigned_submissions:
            continue
        assigned_turns.add(turn_index)
        assigned_submissions.add(submission_id)
        matches[turn_index] = submission
    return matches



SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"xox[bp]-[A-Za-z0-9-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [^-]*KEY-----[\s\S]*?-----END [^-]*KEY-----"),
    re.compile(r"\b(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9+/]{32,}={0,2})\b"),
]
URL_RE = re.compile(r"\bhttps?://[^\s<>()\[\]{}\"']+|\bwww\.[^\s<>()\[\]{}\"']+")
PATH_RE = re.compile(r"(?<![\w<])(?:~/(?:[^\s<>()\[\]{}\"'`/]+/)*[^\s<>()\[\]{}\"'`]+|/(?:[^\s<>()\[\]{}\"'`/]+/)+[^\s<>()\[\]{}\"'`/]+)")


def remove_fenced_code(text, removed):
    def repl(_match):
        removed["code_block"] = removed.get("code_block", 0) + 1
        return ""
    return re.sub(r"```[\s\S]*?(?:```|\Z)", repl, text)


def clean_owner_text(text):
    removed = {}
    cleaned = remove_fenced_code(text, removed)

    def pasted_repl(_match):
        removed["pasted"] = removed.get("pasted", 0) + 1
        return ""
    cleaned = re.sub(r"<pasted_content\b[^>]*>[\s\S]*?</pasted_content>", pasted_repl, cleaned)

    output = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            removed["quote"] = removed.get("quote", 0) + 1
            continue
        if (
            stripped.startswith("Traceback")
            or re.match(r'^\s*File ".*", line \d+', line)
            or re.match(r"^\s+at .+\(.+:\d+:\d+\)", line)
            or re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", stripped)
            or line.startswith("$ ")
            or line.startswith("% ")
        ):
            removed["log"] = removed.get("log", 0) + 1
            continue
        output.append(line)
    cleaned = "\n".join(output)

    count = len(re.findall(r"\[Image #\d+\]", cleaned))
    if count:
        removed["image"] = count
        cleaned = re.sub(r"\[Image #\d+\]", "", cleaned)

    count = len(URL_RE.findall(cleaned))
    if count:
        removed["url"] = count
        cleaned = URL_RE.sub("<url>", cleaned)
    count = len(PATH_RE.findall(cleaned))
    if count:
        removed["path"] = count
        cleaned = PATH_RE.sub("<path>", cleaned)
    secret_count = 0
    for pattern in SECRET_PATTERNS:
        cleaned, n = pattern.subn("<secret>", cleaned)
        secret_count += n
    if secret_count:
        removed["secret"] = secret_count

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, removed


def classify_and_clean(candidates, coverage_by_source, automation_prompts, sent_normalized=None, orca_submissions=None):
    processed = []
    journal_matches = match_journal_submissions(candidates, orca_submissions or [])
    for turn_index, turn in enumerate(candidates):
        coverage = coverage_by_source[turn["source"]]
        coverage["turns_in_window"] += 1
        original_raw = turn.pop("raw_text", "")
        submission = journal_matches.get(turn_index)
        owner_submission = submission is not None and submission["provenance"] == "owner"
        authoritative = submission is not None and not owner_submission
        transcript_replay = False
        orca_context_count = 0
        orca_envelope_count = 0
        unclosed_orca_envelope = False

        if submission is not None:
            if owner_submission:
                original_raw = submission["text"]
                coverage["orca_matched_owner"] += 1
            elif submission["provenance"] == "automated":
                coverage["orca_matched_automated"] += 1

        if authoritative:
            provenance = submission["provenance"]
            reason = submission["reason"]
            raw = original_raw.lstrip()
            raw = re.sub(r"^[/\$][\w:.-]+\s*", "", raw)
        else:
            transcript_replay = is_orca_transcript_replay(original_raw)
            raw, orca_context_count = strip_orca_context_blocks(original_raw)
            raw, orca_envelope_count, unclosed_orca_envelope = strip_orca_envelope_and_prefixes(raw)
            raw = re.sub(r"^[/\$][\w:.-]+\s*", "", raw.lstrip())
            provenance = "owner" if owner_submission else turn["provenance"]
            reason = submission["reason"] if owner_submission else turn["reason"]
            if transcript_replay:
                provenance, reason = "automated", "orca_resume"
            if unclosed_orca_envelope:
                provenance, reason = "automated", "orca_envelope"
            if turn["source"] == "claude" and raw.strip().startswith("[Request interrupted by user"):
                provenance, reason = "automated", "interrupt_marker"

        if unclosed_orca_envelope:
            coverage["automated"] += 1
            add_reason(coverage, "orca_envelope")
            continue
        if transcript_replay and not raw.strip():
            coverage["automated"] += 1
            add_reason(coverage, "orca_resume")
            continue
        if not authoritative and provenance != "automated":
            auto = automation_reason(raw, automation_prompts)
            if auto:
                provenance, reason = "automated", auto
        cleaned, removed = clean_owner_text(raw)
        if orca_context_count:
            removed["orca_context"] = orca_context_count
        if orca_envelope_count:
            removed["orca_envelope"] = orca_envelope_count
        residue = re.sub(r"<url>|<path>|<secret>", "", cleaned)
        placeholder_only = bool(cleaned) and not re.search(r"[^\W_]", residue)
        if (not cleaned or placeholder_only) and not authoritative:
            coverage["empty_after_cleaning"] += 1
            add_reason(coverage, "empty_after_cleaning")
            continue
        if not authoritative and provenance != "automated" and sent_normalized:
            machine_prefix = normalize_machine_text(raw)[:80]
            if len(machine_prefix) >= 20 and any(sent_matches_turn(sent, turn, machine_prefix) for sent in sent_normalized):
                provenance, reason = "automated", "agent_sent"
        normalized = normalize_ws(cleaned)
        result = {
            "source": turn["source"],
            "session_id": turn["session_id"],
            "turn_id": turn["turn_id"],
            "timestamp": local_iso(turn["dt"]),
            "cwd": turn["cwd"],
            "provenance": provenance,
            "reason": reason,
            "text": cleaned,
            "raw_text": original_raw,
            "removed": removed,
            "hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        }
        coverage[provenance] += 1
        if provenance != "owner":
            add_reason(coverage, reason)
        processed.append(result)

    template_sessions = defaultdict(set)
    template_turns = defaultdict(list)
    for turn in processed:
        if turn["provenance"] == "automated":
            continue
        machine = normalize_machine_text(turn["text"])
        if len(machine) >= 500:
            prefix = machine[:200]
            template_sessions[prefix].add((turn["source"], turn["session_id"]))
            template_turns[prefix].append(turn)
    for prefix, sessions in template_sessions.items():
        if len(sessions) < 3:
            continue
        for turn in template_turns[prefix]:
            coverage = coverage_by_source[turn["source"]]
            coverage[turn["provenance"]] = max(0, coverage[turn["provenance"]] - 1)
            turn["provenance"] = "automated"
            turn["reason"] = "repeated_template"
            coverage["automated"] += 1
            add_reason(coverage, "repeated_template")
    return processed

def dedupe(turns, coverage_by_source):
    seen = set()
    output = []
    for turn in sorted(turns, key=lambda item: (item["timestamp"], item["source"], item["session_id"], str(item["turn_id"]))):
        second = turn["timestamp"][:19]
        key = (turn["source"], turn["session_id"], turn["hash"], second)
        if key in seen:
            coverage = coverage_by_source[turn["source"]]
            coverage[turn["provenance"]] = max(0, coverage[turn["provenance"]] - 1)
            add_reason(coverage, "duplicate")
            continue
        seen.add(key)
        output.append(turn)
    return output


def collect_for_window(home, source_names, start, end, automation_prompts, automation_note=None, orca_db_path=None):
    all_candidates = []
    coverage_by_source = {}
    for source in source_names:
        coverage = new_coverage()
        coverage_by_source[source] = coverage
        try:
            candidates, cov = READERS[source](home, start, end)
            coverage.update(cov)
            all_candidates.extend(candidates)
        except Exception as exc:  # one source must not abort the others
            coverage["status"] = "error"
            coverage["notes"].append("reader error: %s" % exc.__class__.__name__)
        if automation_note:
            if automation_note not in coverage["notes"]:
                coverage["notes"].append(automation_note)
    if orca_db_path is ORCA_JOURNAL_DISABLED:
        orca_submissions, journal_note = [], None
    else:
        orca_submissions, journal_note = load_orca_journal(orca_db_path, start, end)
    if journal_note:
        for coverage in coverage_by_source.values():
            if journal_note not in coverage["notes"]:
                coverage["notes"].append(journal_note)
    sent_values = []
    for coverage in coverage_by_source.values():
        sent_values.extend(coverage.get("_sent_texts", []))
    sent_normalized = normalized_sent_strings(sent_values)
    processed = classify_and_clean(all_candidates, coverage_by_source, automation_prompts, sent_normalized, orca_submissions)
    for coverage in coverage_by_source.values():
        coverage.pop("_sent_texts", None)
    return dedupe(processed, coverage_by_source), coverage_by_source


def public_turn(turn, include_raw=False):
    result = {
        "source": turn["source"],
        "session_id": turn["session_id"],
        "turn_id": turn["turn_id"],
        "timestamp": turn["timestamp"],
        "cwd": turn["cwd"],
        "provenance": turn["provenance"],
        "reason": turn["reason"],
        "text": turn["text"],
        "removed": turn["removed"],
        "hash": turn["hash"],
    }
    if include_raw:
        result["raw_text"] = turn["raw_text"]
    return result


def print_summary(date_text, coverage):
    print("date: %s" % date_text)
    print("timezone: +07:00")
    for source in SOURCES:
        if source not in coverage:
            continue
        cov = coverage[source]
        print(
            "%s: status=%s files_scanned=%d turns_in_window=%d owner=%d automated=%d unknown=%d empty_after_cleaning=%d orca_matched_owner=%d orca_matched_automated=%d"
            % (source, cov["status"], cov["files_scanned"], cov["turns_in_window"], cov["owner"],
               cov["automated"], cov["unknown"], cov["empty_after_cleaning"],
               cov.get("orca_matched_owner", 0), cov.get("orca_matched_automated", 0))
        )
        if cov["excluded_by_reason"]:
            reasons = ",".join("%s=%d" % (k, cov["excluded_by_reason"][k]) for k in sorted(cov["excluded_by_reason"]))
            print("  excluded_by_reason: %s" % reasons)
        if cov["notes"]:
            print("  notes: %s" % "; ".join(cov["notes"]))


def parse_sources(value):
    if not value:
        return list(SOURCES)
    requested = [part.strip() for part in value.split(",") if part.strip()]
    unknown = [part for part in requested if part not in SOURCES]
    if unknown:
        raise argparse.ArgumentTypeError("unknown source(s): %s" % ",".join(unknown))
    return requested


def variety_order(rows, rng):
    buckets = {"long": [], "removed": [], "short": [], "other": []}
    for row in rows:
        if len(row["text"]) > 1500:
            buckets["long"].append(row)
        elif row["removed"]:
            buckets["removed"].append(row)
        elif len(row["text"]) < 200:
            buckets["short"].append(row)
        else:
            buckets["other"].append(row)
    for bucket in buckets.values():
        rng.shuffle(bucket)
    ordered = []
    while any(buckets.values()):
        for name in ("short", "long", "removed", "other"):
            if buckets[name]:
                ordered.append(buckets[name].pop(0))
    return ordered


def select_calibration_rows(turns, sample, seed_text):
    rng = random.Random(seed_text)
    by_source = defaultdict(list)
    for turn in turns:
        by_source[turn["source"]].append(turn)
    for source in by_source:
        owners = variety_order([t for t in by_source[source] if t["provenance"] == "owner"], rng)
        others = variety_order([t for t in by_source[source] if t["provenance"] != "owner"], rng)
        by_source[source] = {"owner": owners, "other": others, "total": len(owners) + len(others)}

    quotas = {source: 0 for source in by_source}
    order = sorted(by_source)
    while sum(quotas.values()) < sample:
        progressed = False
        for source in order:
            if sum(quotas.values()) >= sample:
                break
            if quotas[source] < by_source[source]["total"]:
                quotas[source] += 1
                progressed = True
        if not progressed:
            break

    selected = []
    used = set()
    for source in sorted(by_source):
        quota = quotas[source]
        groups = by_source[source]
        owner_target = int(round(quota * 0.6))
        other_target = quota - owner_target
        chosen = []
        for row in groups["owner"][:owner_target]:
            chosen.append(row)
            used.add(id(row))
        for row in groups["other"][:other_target]:
            chosen.append(row)
            used.add(id(row))
        shortage = quota - len(chosen)
        if shortage:
            remainder = [r for r in groups["owner"] + groups["other"] if id(r) not in used]
            for row in remainder[:shortage]:
                chosen.append(row)
                used.add(id(row))
        selected.extend(chosen[:quota])

    # Classification supply can still leave holes (for example a source may have no
    # non-owner rows). Fill them round-robin from unselected rows of any class.
    if len(selected) < sample:
        remaining = []
        for source in sorted(by_source):
            rows = [r for r in by_source[source]["owner"] + by_source[source]["other"] if id(r) not in used]
            remaining.extend((source, row) for row in rows)
        for _, row in remaining:
            if len(selected) >= sample:
                break
            selected.append(row)
            used.add(id(row))
    return selected[:sample]

def calibration_export(args, automation_prompts, automation_note):
    home = Path(args.home).expanduser() if args.home else Path.home()
    today = datetime.now(LOCAL_TZ).date()
    all_turns = []
    all_coverage = {}
    for offset in range(args.days, 0, -1):
        day = today - timedelta(days=offset)
        start, end = day_window(day)
        turns, coverage = collect_for_window(home, args.source_list, start, end, automation_prompts, automation_note, resolve_orca_db_path(args, home))
        all_turns.extend(turns)
        all_coverage[day.isoformat()] = coverage
    rows = select_calibration_rows(all_turns, args.sample, today.isoformat())
    outdir = ensure_data_dir() / "chat-review"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / ("calibration-%s.jsonl" % today.strftime("%Y%m%d"))
    with open(path, "w", encoding="utf-8") as handle:
        for index, turn in enumerate(rows, 1):
            row = {
                "id": "cal-%03d" % index,
                "source": turn["source"],
                "session_id": turn["session_id"],
                "turn_id": turn["turn_id"],
                "timestamp": turn["timestamp"],
                "predicted": turn["provenance"],
                "reason": turn["reason"],
                "raw_text": turn["raw_text"][:2000],
                "cleaned_text": turn["text"],
                "label": None,
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    counts = defaultdict(int)
    for row in rows:
        counts[row["source"]] += 1
    print("wrote: %s" % path)
    print("rows: %d" % len(rows))
    for source in sorted(counts):
        print("%s: %d" % (source, counts[source]))
    return 0


def calibration_score(path_text):
    rows = []
    try:
        with open(path_text, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as exc:
        print("calibration-score: cannot read file: %s" % exc.__class__.__name__, file=sys.stderr)
        return 2
    labeled = [row for row in rows if row.get("label") in ("mine", "not")]
    leaks = [row for row in labeled if row.get("predicted") == "owner" and row.get("label") == "not"]
    misses = [row for row in labeled if row.get("label") == "mine" and row.get("predicted") != "owner"]
    mine_total = sum(1 for row in labeled if row.get("label") == "mine")
    print("labeled: %d" % len(labeled))
    print("leaks: %d" % len(leaks))
    if leaks:
        print("leak_ids: %s" % ",".join(str(row.get("id", "")) for row in leaks))
    print("misses: %d" % len(misses))
    if misses:
        print("miss_ids: %s" % ",".join(str(row.get("id", "")) for row in misses))
    miss_rate = (len(misses) / mine_total) if mine_total else 0.0
    print("miss_rate: %.3f" % miss_rate)
    return 1 if leaks else 0


def calibration_recheck(args, automation_prompts, automation_note):
    rows = []
    try:
        with open(args.calibration_recheck, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as exc:
        print("calibration-recheck: cannot read file: %s" % exc.__class__.__name__, file=sys.stderr)
        return 2

    labeled = [row for row in rows if row.get("label") in ("mine", "not")]
    home = Path(args.home).expanduser() if args.home else Path.home()
    day_cache = {}
    found = []
    not_found = []
    for row in labeled:
        timestamp = str(row.get("timestamp") or "")
        try:
            day = parse_date(timestamp[:10])
        except ValueError:
            not_found.append(row)
            continue
        day_key = day.isoformat()
        if day_key not in day_cache:
            start, end = day_window(day)
            turns, _coverage = collect_for_window(
                home, args.source_list, start, end, automation_prompts, automation_note,
                resolve_orca_db_path(args, home),
            )
            day_cache[day_key] = turns
        match = None
        for turn in day_cache[day_key]:
            if (
                turn["source"] == row.get("source")
                and str(turn["session_id"]) == str(row.get("session_id"))
                and str(turn["turn_id"]) == str(row.get("turn_id"))
            ):
                match = turn
                break
        if match is None:
            not_found.append(row)
        else:
            found.append((row, match["provenance"]))

    leaks = [(row, prediction) for row, prediction in found if prediction == "owner" and row.get("label") == "not"]
    misses = [(row, prediction) for row, prediction in found if row.get("label") == "mine" and prediction != "owner"]
    mine_total = sum(1 for row in labeled if row.get("label") == "mine")
    print("labeled: %d" % len(labeled))
    print("not_found: %d" % len(not_found))
    if not_found:
        print("not_found_ids: %s" % ",".join(str(row.get("id", "")) for row in not_found))
    print("leaks: %d" % len(leaks))
    if leaks:
        print("leak_ids: %s" % ",".join(str(row.get("id", "")) for row, _prediction in leaks))
    print("misses: %d" % len(misses))
    if misses:
        print("miss_ids: %s" % ",".join(str(row.get("id", "")) for row, _prediction in misses))
    miss_rate = (len(misses) / mine_total) if mine_total else 0.0
    print("miss_rate: %.3f" % miss_rate)
    return 1 if leaks else 0


def build_parser():
    parser = argparse.ArgumentParser(description="Read local owner-written coding-agent chat history.")
    parser.add_argument("--date", help="local date YYYY-MM-DD in +07:00")
    parser.add_argument("--sources", type=parse_sources, default=list(SOURCES),
                        help="comma-separated subset of codex,antigravity,devin,claude,grok")
    parser.add_argument("--format", choices=("summary", "json"), default="summary")
    parser.add_argument("--home", help="override home directory for provider roots")
    parser.add_argument("--orca-db", help="override Orca agent-session-domain.db path")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--automation-prompts", help="JSON file containing automation prompt strings")
    group.add_argument("--no-orca", action="store_true", help="do not query orca automations")
    parser.add_argument("--calibration-export", action="store_true")
    parser.add_argument("--calibration-score", metavar="FILE")
    parser.add_argument("--calibration-recheck", metavar="FILE")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--sample", type=int, default=40)
    return parser


def resolve_orca_db_path(args, home):
    if args.no_orca:
        return ORCA_JOURNAL_DISABLED
    if args.orca_db:
        return Path(args.orca_db).expanduser()
    return home / "Library" / "Application Support" / "orca" / "agent-session-domain.db"


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.source_list = args.sources

    if args.calibration_score:
        return calibration_score(args.calibration_score)

    automation_prompts, automation_note = load_automation_prompts(args)
    if args.calibration_recheck:
        return calibration_recheck(args, automation_prompts, automation_note)
    if args.calibration_export:
        if args.days < 1 or args.sample < 1:
            parser.error("--days and --sample must be positive")
        return calibration_export(args, automation_prompts, automation_note)

    if not args.date:
        parser.error("--date is required unless using --calibration-export, --calibration-score, or --calibration-recheck")
    try:
        day = parse_date(args.date)
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")
    home = Path(args.home).expanduser() if args.home else Path.home()
    start, end = day_window(day)
    turns, coverage = collect_for_window(home, args.source_list, start, end, automation_prompts, automation_note, resolve_orca_db_path(args, home))
    if args.format == "json":
        payload = {
            "date": args.date,
            "timezone": "+07:00",
            "coverage": coverage,
            "turns": [public_turn(turn) for turn in turns if turn["provenance"] == "owner"],
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print_summary(args.date, coverage)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Fluent hooks for the Antigravity CLI (agy).

agy speaks a different hook contract from Claude Code (camelCase payloads,
`injectSteps` output, no UserPromptSubmit/SessionEnd). This adapter translates
each agy event into the shared Fluent scripts so the logic lives in one place:

  SessionStart   -> session-start.py   (stats injected as a system message)
  PreInvocation  -> answer-leak-guard  (reminder once a practice command started the
                                        conversation) + any pending data warning
  PostToolUse    -> validate-data.py   (file-edit tools; a bad JSON file is reported
                                        on the next PreInvocation, PostToolUse output
                                        cannot reach the model)
  Stop           -> answer-leak-guard  (void a leaked exercise) + session-end.py

Usage: agy-hook.py <SessionStart|PreInvocation|PostToolUse|Stop>
"""
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
from fluent_paths import force_utf8_io  # noqa: E402

force_utf8_io()

EDIT_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content"}


def load_guard():
    spec = importlib.util.spec_from_file_location("answer_leak_guard", HOOKS / "answer-leak-guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(name, payload, *args):
    return subprocess.run(
        [sys.executable, str(HOOKS / name), *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )


def user_prompt(payload):
    return re.sub(r"</?USER_REQUEST>", "", str(payload.get("lastUserInput") or "")).strip()


def last_model_text(transcript_path):
    """Text of the most recent model reply in agy's transcript.jsonl ('' if unreadable)."""
    text = ""
    try:
        with open(Path(transcript_path).expanduser(), "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") == "PLANNER_RESPONSE" and str(entry.get("content") or "").strip():
                    text = entry["content"]
    except (OSError, TypeError):
        return ""
    return text


def side_file(guard, conversation_id, suffix):
    marker = guard._marker(conversation_id)
    return marker.with_name(marker.name + suffix) if marker else None


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    conversation = payload.get("conversationId")
    guard = load_guard()
    out = {}

    if event == "SessionStart":
        result = run_script("session-start.py", {}, "--json")
        try:
            message = json.loads(result.stdout).get("systemMessage")
        except ValueError:
            message = None
        if message:
            out = {"injectSteps": [{"systemMessage": {"systemMessage": message}}]}

    elif event == "PreInvocation":
        steps = []
        warning = side_file(guard, conversation, ".warn")
        if warning and warning.exists():
            steps.append({"ephemeralMessage": warning.read_text(encoding="utf-8")})
            warning.unlink()
        if guard.marked_session(conversation, user_prompt(payload)):
            steps.append({"ephemeralMessage": guard.REMINDER})
        if steps:
            out = {"injectSteps": steps}

    elif event == "PostToolUse":
        call = payload.get("toolCall") or {}
        target = (call.get("args") or {}).get("TargetFile")
        if call.get("name") in EDIT_TOOLS and target and not payload.get("error"):
            result = run_script("validate-data.py", {"tool_input": {"file_path": target}})
            warning = side_file(guard, conversation, ".warn")
            if result.returncode == 2 and warning:
                try:
                    warning.parent.mkdir(parents=True, exist_ok=True)
                    warning.write_text(result.stderr.strip(), encoding="utf-8")
                except OSError:
                    pass

    elif event == "Stop":
        out = {"decision": ""}
        prompt = user_prompt(payload)
        if guard.marked_session(conversation):
            leak = guard.find_leak(last_model_text(payload.get("transcriptPath")))
            blocked = side_file(guard, conversation, ".blocked")
            digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()
            # Void once per learner turn so a repeated leak can never loop the agent.
            if leak and blocked and not (blocked.exists() and blocked.read_text() == digest):
                try:
                    blocked.write_text(digest)
                except OSError:
                    pass
                out = {"decision": "continue", "reason": guard.BLOCK_REASON % leak}
        if payload.get("fullyIdle", True):
            run_script("session-end.py", {})

    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()

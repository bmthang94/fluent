#!/usr/bin/env python3
"""
Fluent Data Validation Hook
Validates JSON structure and creates backups after Write/Edit operations
"""
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fluent_paths import data_dir, emit, force_utf8_io  # noqa: E402

force_utf8_io()
MAX_BACKUPS_PER_FILE = 10


def rotate_backups(file_path: str, keep: int = MAX_BACKUPS_PER_FILE) -> None:
    """Keep only the most recent `keep` .backup-* files for a given data file."""
    parent = Path(file_path).parent
    stem = Path(file_path).name
    backups = sorted(parent.glob(f"{stem}.backup-*"), reverse=True)
    for old in backups[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Move to): (.+?)\s*$", re.M)


def edited_paths(hook_input):
    """Files a Write/Edit-style tool call touched.

    Claude Code and Devin pass tool_input.file_path. Codex edits through
    apply_patch, whose tool_input.command is the patch text; the touched files
    are its "*** Add/Update File:" and "*** Move to:" headers (relative to cwd).
    """
    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    paths = []
    if tool_input.get("file_path"):
        paths.append(tool_input["file_path"])
    command = tool_input.get("command")
    if isinstance(command, str) and "*** Begin Patch" in command:
        base = Path(hook_input.get("cwd") or ".")
        paths += [str(base / m) for m in PATCH_FILE.findall(command)]
    return paths


def check(file_path, data):
    """Validate and back up one file. Returns an exit code (0 ok, 1 error, 2 invalid JSON)."""
    if not file_path.endswith(".json"):
        return 0

    # Only validate files inside the resolved data dir
    try:
        Path(file_path).resolve().relative_to(data)
    except (ValueError, OSError):
        return 0

    if not os.path.exists(file_path):
        return 0

    try:
        with open(file_path, 'r') as f:
            json.load(f)

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = f"{file_path}.backup-{timestamp}"
        shutil.copy2(file_path, backup_path)
        rotate_backups(file_path)

        emit([
            f"[Fluent] ✓ Data saved and validated: {file_path}",
            f"[Fluent] 💾 Backup created: {backup_path}",
        ])
        return 0

    except json.JSONDecodeError as e:
        print(f"[Fluent] ⚠️  WARNING: Invalid JSON in {file_path}", file=sys.stderr)
        print(f"[Fluent] Error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[Fluent] Error processing {file_path}: {e}", file=sys.stderr)
        return 1


def main():
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid hook input: {e}", file=sys.stderr)
        sys.exit(1)

    data = data_dir().resolve()
    codes = [check(path, data) for path in edited_paths(hook_input)]
    sys.exit(max(codes, default=0))


if __name__ == "__main__":
    main()

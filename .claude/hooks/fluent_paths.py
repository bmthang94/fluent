"""
Fluent path resolution — supports dual-mode (clone vs plugin install).

Data directory resolution precedence:
  1. $FLUENT_DATA_DIR if set (absolutized)
  2. $CLAUDE_PROJECT_DIR/data (or $DEVIN_PROJECT_DIR/data) if that dir holds
     learner-profile.json (clone mode, non-repo cwd)
  3. ./data if ./data/learner-profile.json exists (clone mode, in-repo cwd)
  4. <repo root of this file>/data if it holds learner-profile.json (agents that
     set no project-dir variable, e.g. Codex started from a subdirectory)
  5. ~/.claude/fluent-data (plugin-mode fallback)

Plugin-root resolution precedence:
  1. $CLAUDE_PLUGIN_ROOT if set
  2. $CLAUDE_PROJECT_DIR if set
  3. parent of this file's .claude/ dir (dev-run fallback)

Pure resolvers (data_dir / plugin_root / backups_dir) do not create directories.
Call ensure_data_dir() before writing.
"""
from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path


PROJECT_DIR_VARS = ("CLAUDE_PROJECT_DIR", "DEVIN_PROJECT_DIR")


def force_utf8_io() -> None:
    """Make stdin/stdout/stderr UTF-8 so emoji/CJK doesn't crash on Windows.

    Windows consoles default to a legacy code page (cp1252/gbk); printing the
    emoji in the hook summaries raises UnicodeEncodeError there. Under an
    ASCII/C locale (e.g. Git Bash) stdin is decoded with surrogateescape, so
    CJK bytes in a piped payload survive as lone surrogates and blow up only
    later, at re-encode time. No-op on platforms whose streams are already
    UTF-8 or predate ``reconfigure``. Call once at the top of any hook that
    prints or reads a payload.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def emit(lines, context_event=None) -> None:
    """Print hook output for the human/model.

    Default is plain text, which Claude Code shows and feeds to the model. Codex
    parses stdout that begins with "[" as JSON, so its hooks pass ``--json``;
    Devin only reads JSON, and is detected through the AI_AGENT variable it sets
    (it also runs the hooks declared in .claude/settings.json). In JSON mode the
    text goes out as ``systemMessage`` (shown in the UI) and, when
    ``context_event`` is given, also as ``additionalContext`` (seen by the model).
    """
    text = "\n".join(lines)
    if not text:
        return
    if "--json" in sys.argv[1:] or os.environ.get("AI_AGENT", "").startswith("devin"):
        out = {"systemMessage": text}
        if context_event:
            out["hookSpecificOutput"] = {
                "hookEventName": context_event,
                "additionalContext": text,
            }
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(text)


@lru_cache(maxsize=1)
def data_dir() -> Path:
    """Resolve the runtime data directory (pure — does not create it)."""
    env = os.environ.get("FLUENT_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()

    for var in PROJECT_DIR_VARS:
        project = os.environ.get(var)
        if project:
            candidate = (Path(project) / "data").resolve()
            if (candidate / "learner-profile.json").exists():
                return candidate

    cwd_data = (Path.cwd() / "data").resolve()
    if (cwd_data / "learner-profile.json").exists():
        return cwd_data

    repo_data = (Path(__file__).resolve().parents[2] / "data").resolve()
    if (repo_data / "learner-profile.json").exists():
        return repo_data

    return (Path.home() / ".claude" / "fluent-data").resolve()


def ensure_data_dir() -> Path:
    """Resolve the data directory and create it if missing. Call before writing."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


@lru_cache(maxsize=1)
def plugin_root() -> Path:
    """Resolve the plugin/repo root directory."""
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return Path(env).resolve()
    for var in PROJECT_DIR_VARS:
        env = os.environ.get(var)
        if env:
            return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def backups_dir() -> Path:
    """Resolve the backups directory. Always nested inside data_dir to avoid collisions
    when the fallback ~/.claude/fluent-data is used (the parent ~/.claude/ is shared
    across plugins)."""
    return data_dir() / ".backups"


def ensure_backups_dir() -> Path:
    """Resolve the backups directory and create it if missing."""
    b = backups_dir()
    b.mkdir(parents=True, exist_ok=True)
    return b

#!/usr/bin/env python3
"""Prepare, ingest, and approve daily owner-chat language review digests."""
from __future__ import print_function

import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fluent_paths import data_dir, backups_dir, db_lock, force_utf8_io  # noqa: E402

force_utf8_io()

LOCAL_TZ = timezone(timedelta(hours=7))
HOOK_DIR = Path(__file__).resolve().parent
ANALYZE_PATH = HOOK_DIR.parent / "skills" / "fluent-chat-review" / "ANALYZE.md"


def load_reader():
    path = HOOK_DIR / "read-chat-history.py"
    spec = importlib.util.spec_from_file_location("fluent_chat_history_reader", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


READER = load_reader()


def load_update_db():
    path = HOOK_DIR / "update-db.py"
    spec = importlib.util.spec_from_file_location("fluent_update_db", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UPDATE_DB = load_update_db()

VN_RE = re.compile(
    "[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]",
    re.IGNORECASE,
)
ASCII_WORD_RE = re.compile(r"[A-Za-z]+")
WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
CATEGORIES = {"grammar", "vocabulary", "word_choice", "spelling", "punctuation", "register"}
SEVERITIES = set(SEVERITY_ORDER)


class ReviewError(Exception):
    pass


class LockContended(ReviewError):
    pass


def parse_iso(value):
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ)
    except (ValueError, OverflowError):
        return None


def minute_dt(dt):
    return dt.astimezone(LOCAL_TZ).replace(second=0, microsecond=0)


def iso(dt):
    return minute_dt(dt).isoformat()


def local_date(dt):
    return minute_dt(dt).date().isoformat()


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(tmp), str(path))
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def read_json(path, default):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def review_dir():
    return data_dir() / "chat-review"


@contextmanager
def review_lock(base):
    base.mkdir(parents=True, exist_ok=True)
    handle = open(str(base / ".lock"), "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LockContended("another chat-review operation is running")
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def load_state(base):
    state = read_json(base / "state.json", {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("last_end", None)
    runs = state.get("runs")
    if not isinstance(runs, list):
        runs = []
    state["runs"] = runs
    return state


def load_cache(base):
    value = read_json(base / "cache.json", {})
    return value if isinstance(value, dict) else {}


def load_intents(base):
    value = read_json(base / "intents.json", {})
    return value if isinstance(value, dict) else {}


def save_state(base, state):
    atomic_write_json(base / "state.json", state)


def save_cache(base, cache):
    atomic_write_json(base / "cache.json", cache)


def save_intents(base, intents):
    atomic_write_json(base / "intents.json", intents)


def normalize_text(text):
    return " ".join((text or "").lower().split())


def text_hash(text):
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def word_count(text):
    return len(WORD_RE.findall(text or ""))


def language_of(sentence):
    if VN_RE.search(sentence):
        return "vi"
    if ASCII_WORD_RE.search(sentence):
        return "en"
    return None


def split_sentences(text):
    pieces = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for part in re.split(r"(?<=[.!?…])\s+", line):
            part = part.strip()
            if part:
                pieces.append(part)
    return pieces


def split_language_items(text):
    groups = []
    current_lang = None
    current = []
    for sentence in split_sentences(text):
        lang = language_of(sentence)
        if lang is None:
            if current:
                groups.append((current_lang, " ".join(current)))
                current = []
                current_lang = None
            continue
        if lang != current_lang and current:
            groups.append((current_lang, " ".join(current)))
            current = []
        current_lang = lang
        current.append(sentence)
    if current:
        groups.append((current_lang, " ".join(current)))
    return [(lang, item) for lang, item in groups if word_count(item) >= 3]


def turn_ref(turn):
    return {
        "source": turn["source"],
        "session_id": turn["session_id"],
        "turn_id": turn["turn_id"],
        "timestamp": turn["timestamp"],
    }


def load_learner(root):
    value = read_json(root / "learner-profile.json", {})
    if not isinstance(value, dict):
        return {}
    profile = value.get("learner") if isinstance(value.get("learner"), dict) else value
    result = {}
    if profile.get("level") or profile.get("current_level"):
        result["level"] = profile.get("level") or profile.get("current_level")
    if profile.get("target") or profile.get("target_level"):
        result["target"] = profile.get("target") or profile.get("target_level")
    goals = profile.get("goals") or profile.get("learning_goal") or profile.get("primary_goal")
    if goals:
        result["goals"] = goals if isinstance(goals, list) else [goals]
    return result


def cache_pattern_ids(cache):
    ids = set()
    for entry in cache.values():
        if not isinstance(entry, dict) or entry.get("kind") != "en":
            continue
        result = entry.get("result")
        if isinstance(result, list):
            for error in result:
                if isinstance(error, dict) and error.get("pattern_id"):
                    ids.add(str(error["pattern_id"]))
    return ids


def load_known_patterns(root, cache):
    patterns = {}
    mistakes = read_json(root / "mistakes-db.json", {})
    raw = mistakes.get("error_patterns", {}) if isinstance(mistakes, dict) else {}
    if isinstance(raw, dict):
        for pid, value in raw.items():
            summary = str(pid)
            if isinstance(value, dict):
                summary = str(value.get("summary") or value.get("notes") or value.get("context") or pid)
            elif isinstance(value, str):
                summary = value
            patterns[str(pid)] = summary
    elif isinstance(raw, list):
        for value in raw:
            if isinstance(value, dict) and value.get("pattern_id"):
                pid = str(value["pattern_id"])
                patterns[pid] = str(value.get("summary") or value.get("notes") or value.get("context") or pid)
    for pid in sorted(cache_pattern_ids(cache)):
        patterns.setdefault(pid, pid)
    return [{"pattern_id": pid, "summary": patterns[pid]} for pid in sorted(patterns)]


def coverage_summary(coverage):
    result = {}
    for source in READER.SOURCES:
        cov = coverage.get(source)
        if not cov:
            continue
        result[source] = {
            "status": cov.get("status", "unknown"),
            "owner": cov.get("owner", 0),
            "notes": list(cov.get("notes", [])),
        }
    return result


def make_run_id(end, base):
    stamp = minute_dt(end).strftime("%Y%m%d-%H%M")
    candidate = stamp
    suffix = 2
    while (base / "runs" / candidate).exists():
        candidate = "%s-%d" % (stamp, suffix)
        suffix += 1
    return candidate


def digest_id_for(end, base):
    stamp = minute_dt(end).strftime("%Y-%m-%d")
    candidate = stamp
    suffix = 2
    while (base / "digests" / (candidate + ".json")).exists():
        candidate = "%s-%d" % (stamp, suffix)
        suffix += 1
    return candidate


def resolve_window(state, now):
    end = minute_dt(now)
    capped = False
    if state.get("last_end"):
        start = parse_iso(state.get("last_end"))
    else:
        start = None
    if start is None:
        start = end - timedelta(days=1)
    minimum = end - timedelta(days=7)
    if start < minimum:
        start = minimum
        capped = True
    start = minute_dt(start)
    if start > end:
        start = end
    return start, end, capped


def automation_for_args(args):
    class AutomationArgs(object):
        automation_prompts = None
        no_orca = args.no_orca
    return READER.load_automation_prompts(AutomationArgs())


def orca_for_args(args, home):
    class OrcaArgs(object):
        no_orca = args.no_orca
        orca_db = args.orca_db
    return READER.resolve_orca_db_path(OrcaArgs(), home)


def collect_owner_turns(args, start, end, home):
    automation_prompts, automation_note = automation_for_args(args)
    turns, coverage = READER.collect_for_window(
        home, list(READER.SOURCES), start, end, automation_prompts, automation_note,
        orca_for_args(args, home),
    )
    return [turn for turn in turns if turn.get("provenance") == "owner"], coverage


def build_items(turns, cache):
    english = []
    vietnamese = []
    refs = {}
    cached = []
    counts = {"en": 0, "vi": 0}
    seen_pending = {}
    for turn in sorted(turns, key=lambda t: (t.get("timestamp", ""), t.get("source", ""), str(t.get("session_id", "")), str(t.get("turn_id", "")))):
        for lang, text in split_language_items(turn.get("text", "")):
            counts[lang] += 1
            item_id = "%s-%d" % (lang, counts[lang])
            digest = text_hash(text)
            refs[item_id] = turn_ref(turn)
            item = {
                "item_id": item_id,
                "text": text,
                "agent": turn["source"],
                "repo": Path(turn.get("cwd") or "").name,
                "hash": digest,
            }
            if digest in cache:
                cached.append({
                    "item_id": item_id,
                    "hash": digest,
                    "kind": lang,
                    "result": copy.deepcopy(cache[digest].get("result")),
                })
            elif digest in seen_pending:
                cached.append({
                    "item_id": item_id,
                    "hash": digest,
                    "kind": lang,
                    "result": {"duplicate_of": seen_pending[digest]},
                })
            elif lang == "en":
                seen_pending[digest] = item_id
                english.append(item)
            else:
                seen_pending[digest] = item_id
                vietnamese.append(item)
    return english, vietnamese, refs, cached


def prepare(args):
    base = review_dir()
    lock_context = nullcontext() if args.dry_run else review_lock(base)
    with lock_context:
        state = load_state(base)
        now = parse_iso(args.now) if args.now else datetime.now(LOCAL_TZ)
        if now is None:
            raise ReviewError("invalid --now ISO timestamp")
        start, end, capped = resolve_window(state, now)
        home = Path(args.home).expanduser() if args.home else Path.home()
        turns, coverage = collect_owner_turns(args, start, end, home)
        cache = load_cache(base)
        english, vietnamese, refs, cached = build_items(turns, cache)
        if args.dry_run:
            print("status: dry_run")
            print("window: %s -> %s" % (iso(start), iso(end)))
            if capped:
                print("note: window capped to 7 days")
            print("owner_turns: %d" % len(turns))
            print("english_items: %d" % len(english))
            print("vietnamese_items: %d" % len(vietnamese))
            print("cached_items: %d" % len(cached))
            print("coverage: " + " ".join("%s=%d" % (source, coverage.get(source, {}).get("owner", 0)) for source in READER.SOURCES))
            return 0

        intents = load_intents(base)
        data_root = data_dir()
        run_id = make_run_id(end, base)
        run_dir = base / "runs" / run_id
        package = {
            "run_id": run_id,
            "window": {"start": iso(start), "end": iso(end), "capped_to_7_days": capped},
            "learner": load_learner(data_root),
            "english_items": [{k: v for k, v in item.items() if k != "hash"} for item in english],
            "vietnamese_items": [{k: v for k, v in item.items() if k != "hash"} for item in vietnamese],
            "known_error_patterns": load_known_patterns(data_root, cache),
            "intent_catalog": [
                {"intent_id": intent_id, "summary": value.get("summary", ""), "status": value.get("status", "pending")}
                for intent_id, value in sorted(intents.items())
                if value.get("status") in ("pending", "proposed", "approved")
            ],
            "coverage": coverage,
            "cached_items": cached,
            "item_refs": refs,
        }
        atomic_write_json(run_dir / "package.json", package)
        state["runs"].append({
            "run_id": run_id,
            "status": "prepared",
            "window": package["window"],
            "digest_id": None,
        })
        save_state(base, state)

        if not english and not vietnamese:
            analysis = {"run_id": run_id, "english_errors": [], "intents": [], "expressions": [], "skipped": []}
            atomic_write_json(run_dir / "analysis.json", analysis)
            digest = finalize_run(base, run_dir, package, analysis)
            print("status: done")
            if capped:
                print("note: window capped to 7 days")
            print(render_digest(digest, load_intents(base)))
            return 0

        print("status: needs_analysis")
        if capped:
            print("note: window capped to 7 days")
        print("run_dir: %s" % run_dir)
        print("english_items: %d" % len(english))
        print("vietnamese_items: %d" % len(vietnamese))
        print("cached_items: %d" % len(cached))
        print("instructions: %s" % ANALYZE_PATH)
        return 0


def item_maps(package):
    items = {}
    for item in package.get("english_items", []):
        items[item["item_id"]] = ("en", item)
    for item in package.get("vietnamese_items", []):
        items[item["item_id"]] = ("vi", item)
    return items


def fourgrams(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return set(tuple(words[index:index + 4]) for index in range(0, max(0, len(words) - 3)))


def validate_analysis(package, analysis):
    problems = []
    if not isinstance(analysis, dict):
        return ["analysis must be a JSON object"]
    if analysis.get("run_id") != package.get("run_id"):
        problems.append("run_id does not match package run_id")
    for key in ("english_errors", "intents", "expressions", "skipped"):
        if not isinstance(analysis.get(key), list):
            problems.append("%s must be a list" % key)
    if problems:
        return problems

    items = item_maps(package)
    model_ids = set(items)
    claims = {}
    errors_by_item = {}
    intents_by_item = {}
    skipped_by_item = {}

    def claim(item_id, group, allow_error_repeat=False):
        if item_id not in items:
            problems.append("%s references unknown item %s" % (group, item_id))
            return
        previous = claims.get(item_id)
        if previous is None:
            claims[item_id] = group
        elif previous != group or not (group == "english_errors" and allow_error_repeat):
            problems.append("item %s appears in both %s and %s" % (item_id, previous, group))

    catalog = {row.get("intent_id"): row for row in package.get("intent_catalog", []) if isinstance(row, dict)}
    known_patterns = {row.get("pattern_id") for row in package.get("known_error_patterns", []) if isinstance(row, dict)}
    for index, error in enumerate(analysis["english_errors"]):
        where = "english_errors[%d]" % index
        if not isinstance(error, dict):
            problems.append("%s must be an object" % where)
            continue
        item_id = error.get("item_id")
        if item_id in items and items[item_id][0] != "en":
            problems.append("%s references non-English item %s" % (where, item_id))
        claim(item_id, "english_errors", allow_error_repeat=True)
        errors_by_item.setdefault(item_id, []).append(error)
        for field in ("quote", "correction", "pattern_id", "category", "subcategory", "severity", "explanation_vi"):
            if not isinstance(error.get(field), str) or not error.get(field):
                problems.append("%s.%s is required" % (where, field))
        summary_value = error.get("pattern_summary")
        if error.get("pattern_id") in known_patterns:
            if summary_value is not None and not isinstance(summary_value, str):
                problems.append("%s.pattern_summary must be a string" % where)
        elif not isinstance(summary_value, str) or not summary_value:
            problems.append("%s.pattern_summary is required for a new pattern" % where)
        if item_id in items and isinstance(error.get("quote"), str) and error.get("quote") not in items[item_id][1].get("text", ""):
            problems.append("%s.quote is not an exact substring of %s" % (where, item_id))
        if isinstance(error.get("pattern_id"), str) and not re.match(r"^[a-z][a-z0-9_]*$", error.get("pattern_id", "")):
            problems.append("%s.pattern_id must be snake_case" % where)
        if isinstance(error.get("subcategory"), str) and not re.match(r"^[a-z][a-z0-9_]*$", error.get("subcategory", "")):
            problems.append("%s.subcategory must be snake_case" % where)
        if error.get("category") not in CATEGORIES:
            problems.append("%s.category is invalid" % where)
        if error.get("severity") not in SEVERITIES:
            problems.append("%s.severity is invalid" % where)

    new_definitions = {}
    new_references = {}
    for index, intent in enumerate(analysis["intents"]):
        where = "intents[%d]" % index
        if not isinstance(intent, dict):
            problems.append("%s must be an object" % where)
            continue
        item_id = intent.get("item_id")
        if item_id in items and items[item_id][0] != "vi":
            problems.append("%s references non-Vietnamese item %s" % (where, item_id))
        claim(item_id, "intents")
        intents_by_item[item_id] = intent
        intent_id = intent.get("intent_id")
        if intent_id is None:
            new_intent = intent.get("new_intent")
            if not isinstance(new_intent, dict):
                problems.append("%s.new_intent is required when intent_id is null" % where)
            else:
                if not new_intent.get("summary"):
                    problems.append("%s.new_intent.summary is required" % where)
                if not new_intent.get("audience"):
                    problems.append("%s.new_intent.audience is required" % where)
                if not isinstance(new_intent.get("constraints"), list):
                    problems.append("%s.new_intent.constraints must be a list" % where)
                new_definitions[item_id] = new_intent
        elif isinstance(intent_id, str) and intent_id.startswith("new:"):
            new_references[item_id] = intent_id[4:]
        elif intent_id not in catalog:
            problems.append("%s references unknown intent_id %s" % (where, intent_id))
        if not isinstance(intent.get("excerpt"), str) or not intent.get("excerpt"):
            problems.append("%s.excerpt is required" % where)
        elif item_id in items and intent["excerpt"] not in items[item_id][1].get("text", ""):
            problems.append("%s.excerpt is not an exact substring of %s" % (where, item_id))

    for item_id, target in new_references.items():
        if target not in new_definitions:
            problems.append("intents item %s references undefined new intent new:%s" % (item_id, target))

    for index, skipped in enumerate(analysis["skipped"]):
        where = "skipped[%d]" % index
        if not isinstance(skipped, dict):
            problems.append("%s must be an object" % where)
            continue
        item_id = skipped.get("item_id")
        claim(item_id, "skipped")
        skipped_by_item[item_id] = skipped
        if not isinstance(skipped.get("reason"), str) or not skipped.get("reason"):
            problems.append("%s.reason is required" % where)

    for item_id in sorted(model_ids - set(claims)):
        problems.append("item %s is missing from analysis" % item_id)

    expressions = analysis["expressions"]
    expression_targets = []
    for index, expression in enumerate(expressions):
        where = "expressions[%d]" % index
        if not isinstance(expression, dict):
            problems.append("%s must be an object" % where)
            continue
        for field in ("intent_id", "scenario", "model_answer", "note_vi"):
            if not isinstance(expression.get(field), str) or not expression.get(field):
                problems.append("%s.%s is required" % (where, field))
        target = expression.get("intent_id")
        if target in expression_targets:
            problems.append("%s duplicates expression target %s" % (where, target))
        expression_targets.append(target)
        if isinstance(target, str) and target.startswith("new:"):
            if target[4:] not in new_definitions:
                problems.append("%s references undefined new intent %s" % (where, target))
        elif target not in catalog:
            problems.append("%s references unknown intent_id %s" % (where, target))
        elif catalog.get(target, {}).get("status") == "approved":
            problems.append("%s is forbidden for approved intent %s" % (where, target))
        if isinstance(expression.get("scenario"), str) and isinstance(expression.get("model_answer"), str):
            overlap = fourgrams(expression["scenario"]) & fourgrams(expression["model_answer"])
            if overlap:
                problems.append("%s.scenario shares a 4-word sequence with model_answer" % where)

    required_expression_targets = set("new:%s" % item_id for item_id in new_definitions)
    for item_id, intent in intents_by_item.items():
        intent_id = intent.get("intent_id")
        if intent_id in catalog and catalog[intent_id].get("status") in ("pending", "proposed"):
            required_expression_targets.add(intent_id)
    for target in sorted(required_expression_targets - set(expression_targets)):
        problems.append("missing expression for %s" % target)
    return problems


def slugify_summary(summary):
    words = re.findall(r"[a-z0-9]+", (summary or "").lower())
    slug = "_".join(words)[:40].strip("_")
    return slug or "intent"


def stable_intent_id(summary):
    digest = hashlib.sha1((summary or "").encode("utf-8")).hexdigest()[:6]
    return "int_%s_%s" % (slugify_summary(summary), digest)


def occurrence_for(ref):
    dt = parse_iso(ref.get("timestamp"))
    return {
        "session_key": "%s:%s" % (ref.get("source", ""), ref.get("session_id", "")),
        "date": local_date(dt) if dt else "",
        "turn_ref": dict(ref),
    }


def occurrence_dt(occurrence):
    ref = occurrence.get("turn_ref", {}) if isinstance(occurrence, dict) else {}
    return parse_iso(ref.get("timestamp"))


def counting_occurrences(intent):
    since = parse_iso(intent.get("counting_since")) if intent.get("counting_since") else None
    rows = []
    for occurrence in intent.get("occurrences", []):
        dt = occurrence_dt(occurrence)
        if since is not None and dt is not None and dt < since:
            continue
        rows.append(occurrence)
    return rows


def threshold_met(intent):
    rows = counting_occurrences(intent)
    sessions = set(row.get("session_key") for row in rows)
    dates = set(row.get("date") for row in rows)
    return len(rows) >= 2 and (len(sessions) >= 2 or len(dates) >= 2)


def add_occurrence(intent, ref, excerpt):
    occurrence = occurrence_for(ref)
    if occurrence not in intent.setdefault("occurrences", []):
        intent["occurrences"].append(occurrence)
    evidence = intent.setdefault("evidence", [])
    if excerpt and excerpt not in evidence and len(evidence) < 2:
        evidence.append(excerpt)


def resolve_analysis_intents(analysis):
    new_defs = {}
    for entry in analysis.get("intents", []):
        if entry.get("intent_id") is None:
            new_defs[entry["item_id"]] = entry.get("new_intent", {})
    resolution = {}
    for entry in analysis.get("intents", []):
        intent_id = entry.get("intent_id")
        if intent_id is None:
            resolution[entry["item_id"]] = ("new", entry["item_id"])
        elif isinstance(intent_id, str) and intent_id.startswith("new:"):
            resolution[entry["item_id"]] = ("new", intent_id[4:])
        else:
            resolution[entry["item_id"]] = ("existing", intent_id)
    expression_map = {}
    for expression in analysis.get("expressions", []):
        expression_map[expression.get("intent_id")] = {
            "scenario": expression.get("scenario", ""),
            "model_answer": expression.get("model_answer", ""),
            "note_vi": expression.get("note_vi", ""),
        }
    return new_defs, resolution, expression_map


def resolve_duplicate_cached_results(package, analysis):
    """Resolve same-run duplicate references against the first item's analysis."""
    new_defs, resolution, _expression_map = resolve_analysis_intents(analysis)
    new_ids = {item_id: stable_intent_id(defn.get("summary", "")) for item_id, defn in new_defs.items()}
    errors_by_item = {}
    for error in analysis.get("english_errors", []):
        errors_by_item.setdefault(error.get("item_id"), []).append(error)
    intents_by_item = {row.get("item_id"): row for row in analysis.get("intents", [])}
    skipped_by_item = {row.get("item_id"): row for row in analysis.get("skipped", [])}
    for cached in package.get("cached_items", []):
        result = cached.get("result")
        if not isinstance(result, dict) or not result.get("duplicate_of"):
            continue
        target_id = result.get("duplicate_of")
        if target_id in intents_by_item:
            entry = intents_by_item[target_id]
            kind, target = resolution.get(target_id, (None, None))
            intent_id = new_ids.get(target) if kind == "new" else target
            cached["result"] = {"intent_id": intent_id, "excerpt": entry.get("excerpt", "")}
        elif target_id in errors_by_item:
            cached["result"] = copy.deepcopy(errors_by_item[target_id])
        else:
            cached["result"] = {"reason": skipped_by_item.get(target_id, {}).get("reason", "skipped")}


def update_intent_statuses(intents, end):
    for intent in intents.values():
        if intent.get("status") != "pending":
            continue
        rows = counting_occurrences(intent)
        if rows and threshold_met(intent) and intent.get("expression"):
            intent["status"] = "proposed"
            continue
        dates = [occurrence_dt(row) for row in rows]
        dates = [dt for dt in dates if dt is not None]
        if dates and min(dates) < end - timedelta(days=14):
            intent["status"] = "expired"


def apply_cached_items(package, intents, end):
    refs = package.get("item_refs", {})
    for cached in package.get("cached_items", []):
        if cached.get("kind") != "vi" or not isinstance(cached.get("result"), dict):
            continue
        intent_id = cached["result"].get("intent_id")
        intent = intents.get(intent_id)
        if not intent:
            continue
        ref = refs.get(cached.get("item_id"))
        if ref:
            add_occurrence(intent, ref, cached["result"].get("excerpt", ""))
        intent["updated_run"] = package["run_id"]


def apply_analysis(package, analysis, intents, end):
    resolve_duplicate_cached_results(package, analysis)
    refs = package.get("item_refs", {})
    new_defs, resolution, expression_map = resolve_analysis_intents(analysis)
    new_ids = {}
    for item_id, definition in new_defs.items():
        intent_id = stable_intent_id(definition.get("summary", ""))
        new_ids[item_id] = intent_id
        if intent_id not in intents:
            intents[intent_id] = {
                "summary": definition.get("summary", ""),
                "audience": definition.get("audience", ""),
                "constraints": definition.get("constraints", []),
                "status": "pending",
                "occurrences": [],
                "evidence": [],
                "counting_since": None,
                "expression": None,
                "card_id": None,
                "created_run": package["run_id"],
                "updated_run": package["run_id"],
            }
    for key, expression in expression_map.items():
        if key.startswith("new:"):
            intent_id = new_ids.get(key[4:])
        else:
            intent_id = key
        intent = intents.get(intent_id)
        if intent and intent.get("status") in ("pending", "proposed"):
            intent["expression"] = expression
            intent["updated_run"] = package["run_id"]
    for entry in analysis.get("intents", []):
        kind, target = resolution.get(entry.get("item_id"), (None, None))
        intent_id = new_ids.get(target) if kind == "new" else target
        intent = intents.get(intent_id)
        if not intent:
            continue
        ref = refs.get(entry.get("item_id"))
        if ref:
            add_occurrence(intent, ref, entry.get("excerpt", ""))
        intent["updated_run"] = package["run_id"]
    apply_cached_items(package, intents, end)
    update_intent_statuses(intents, end)


def write_analysis_cache(base, cache, package, analysis, intents):
    new_defs, resolution, expression_map = resolve_analysis_intents(analysis)
    new_ids = {item_id: stable_intent_id(defn.get("summary", "")) for item_id, defn in new_defs.items()}
    item_hashes = {}
    for row in package.get("english_items", []) + package.get("vietnamese_items", []):
        item_hashes[row["item_id"]] = text_hash(row.get("text", ""))
    errors_by_item = {}
    for error in analysis.get("english_errors", []):
        errors_by_item.setdefault(error.get("item_id"), []).append(error)
    intents_by_item = {row.get("item_id"): row for row in analysis.get("intents", [])}
    skipped_by_item = {row.get("item_id"): row for row in analysis.get("skipped", [])}
    kinds = item_maps(package)
    for item_id, digest in item_hashes.items():
        lang, _item = kinds[item_id]
        if item_id in errors_by_item:
            result = errors_by_item[item_id]
        elif item_id in intents_by_item:
            entry = intents_by_item[item_id]
            kind, target = resolution.get(item_id, (None, None))
            intent_id = new_ids.get(target) if kind == "new" else target
            result = {"intent_id": intent_id, "excerpt": entry.get("excerpt", "")}
        else:
            result = {"reason": skipped_by_item.get(item_id, {}).get("reason", "skipped")}
        cache[digest] = {"kind": lang, "result": result, "run_id": package["run_id"]}
    save_cache(base, cache)


def digest_error_groups(package, analysis):
    refs = package.get("item_refs", {})
    rows = []
    for error in analysis.get("english_errors", []):
        row = dict(error)
        row["item_id"] = error.get("item_id")
        rows.append(row)
    for cached in package.get("cached_items", []):
        if cached.get("kind") == "en" and isinstance(cached.get("result"), list):
            for error in cached["result"]:
                row = dict(error)
                row["item_id"] = cached.get("item_id")
                rows.append(row)
    groups = {}
    for error in rows:
        pid = error.get("pattern_id", "unknown")
        group = groups.setdefault(pid, {
            "pattern_id": pid,
            "pattern_summary": error.get("pattern_summary") or pid,
            "category": error.get("category", ""),
            "subcategory": error.get("subcategory", ""),
            "severity": error.get("severity", "low"),
            "examples": [],
            "count": 0,
        })
        group["count"] += 1
        if len(group["examples"]) < 2:
            group["examples"].append({
                "quote": error.get("quote", ""),
                "correction": error.get("correction", ""),
                "explanation_vi": error.get("explanation_vi", ""),
                "turn_ref": refs.get(error.get("item_id"), {}),
            })
    ordered = sorted(groups.values(), key=lambda row: (SEVERITY_ORDER.get(row["severity"], 9), -row["count"], row["pattern_id"]))
    for index, group in enumerate(ordered, 1):
        group["key"] = "E%d" % index
    return ordered


def awaiting_digest_intents(base, exclude_path=None):
    used = set()
    digest_dir = base / "digests"
    if not digest_dir.exists():
        return used
    for path in sorted(digest_dir.glob("*.json")):
        if exclude_path is not None and path == exclude_path:
            continue
        digest = read_json(path, {})
        if digest.get("status") != "awaiting_approval":
            continue
        for row in digest.get("expressions", []):
            if row.get("intent_id"):
                used.add(row["intent_id"])
    return used


def digest_expressions(base, intents):
    used = awaiting_digest_intents(base)
    rows = []
    for intent_id, intent in sorted(intents.items()):
        if intent.get("status") != "proposed" or intent_id in used or not intent.get("expression"):
            continue
        rows.append({
            "intent_id": intent_id,
            "scenario": intent["expression"].get("scenario", ""),
            "model_answer": intent["expression"].get("model_answer", ""),
            "note_vi": intent["expression"].get("note_vi", ""),
            "evidence": list(intent.get("evidence", []))[:2],
            "occurrences": len(counting_occurrences(intent)),
        })
    for index, row in enumerate(rows, 1):
        row["key"] = "V%d" % index
    return rows


def finalize_run(base, run_dir, package, analysis):
    intents = load_intents(base)
    end = parse_iso(package["window"]["end"])
    apply_analysis(package, analysis, intents, end)
    cache = load_cache(base)
    write_analysis_cache(base, cache, package, analysis, intents)
    save_intents(base, intents)

    digest_id = digest_id_for(end, base)
    digest = {
        "digest_id": digest_id,
        "run_id": package["run_id"],
        "window": package["window"],
        "coverage": coverage_summary(package.get("coverage", {})),
        "english": digest_error_groups(package, analysis),
        "expressions": digest_expressions(base, intents),
        "pending_count": sum(1 for intent in intents.values() if intent.get("status") == "pending"),
        "status": "awaiting_approval",
        "decision": None,
    }
    atomic_write_json(base / "digests" / (digest_id + ".json"), digest)

    state = load_state(base)
    state["last_end"] = package["window"]["end"]
    for run in state.get("runs", []):
        if run.get("run_id") == package["run_id"]:
            run["status"] = "done"
            run["digest_id"] = digest_id
    save_state(base, state)
    return digest


def render_digest(digest, intents=None):
    start = parse_iso(digest["window"]["start"])
    end = parse_iso(digest["window"]["end"])
    lines = [
        "Fluent · chat %s → %s · Mã %s" % (
            start.strftime("%d/%m %H:%M"), end.strftime("%d/%m %H:%M"), digest.get("digest_id", "")
        )
    ]
    coverage = digest.get("coverage", {})
    parts = []
    missing = []
    for source in READER.SOURCES:
        cov = coverage.get(source, {})
        parts.append("%s %d" % (source, cov.get("owner", 0)))
        if cov.get("status") != "ok":
            note_text = "; ".join(cov.get("notes", [])) or cov.get("status", "missing")
            missing.append("%s: %s" % (source, note_text))
    lines.append("Nguồn: " + " · ".join(parts))
    if missing:
        lines.append("Thiếu: " + "; ".join(missing))
    lines.append("")
    lines.append("**Lỗi tiếng Anh**")
    if digest.get("english"):
        for group in digest["english"]:
            example = group.get("examples", [{}])[0]
            lines.append('%s. "%s" → "%s" — %s (%d lần)' % (
                group.get("key", ""), example.get("quote", ""), example.get("correction", ""),
                example.get("explanation_vi", ""), group.get("count", 0),
            ))
    else:
        lines.append("Không có lỗi mới.")
    lines.append("")
    lines.append("**Cách diễn đạt đề xuất**")
    if digest.get("expressions"):
        for row in digest["expressions"]:
            lines.append("%s. Tình huống: %s" % (row.get("key", ""), row.get("scenario", "")))
            lines.append("    Câu mẫu: %s" % row.get("model_answer", ""))
            lines.append("    Ghi chú: %s" % row.get("note_vi", ""))
            evidence = " · ".join('"%s"' % item for item in row.get("evidence", []))
            lines.append("    Từ chat: %s" % evidence)
    else:
        lines.append("Chưa có ý định nào lặp lại đủ 2 lần.")
    lines.append("")
    lines.append("Đang chờ: %d ý định mới gặp 1 lần." % digest.get("pending_count", 0))
    lines.append('Trả lời "duyệt hết", "duyệt E1 V2" hoặc "bỏ E3" để lưu vào thẻ ôn.')
    return "\n".join(lines)


def strict_load_json(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError("cannot read %s: %s" % (path, exc.__class__.__name__))


def chat_import_ids(value):
    metadata = value.get("metadata", {}) if isinstance(value, dict) else {}
    ids = metadata.get("chat_imports", [])
    return set(ids) if isinstance(ids, list) else set()


def append_chat_import_id(value, digest_id):
    metadata = value.setdefault("metadata", {})
    ids = metadata.setdefault("chat_imports", [])
    if digest_id not in ids:
        ids.append(digest_id)


def backup_chat_import_files(digest_id, paths):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", digest_id)
    root = backups_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / ("pre-chat-import-%s" % safe)
    suffix = 2
    while target.exists():
        target = root / ("pre-chat-import-%s-%d" % (safe, suffix))
        suffix += 1
    target.mkdir(parents=True)
    for path in paths:
        shutil.copy2(str(path), str(target / path.name))
    return target


def learner_difficulty():
    profile = read_json(data_dir() / "learner-profile.json", {})
    learner = profile.get("learner", {}) if isinstance(profile, dict) else {}
    if isinstance(learner, dict):
        return learner.get("current_level") or profile.get("current_level") or ""
    return ""


def selected_digest_rows(digest):
    selected = set((digest.get("decision") or {}).get("selected", []))
    errors = [row for row in digest.get("english", []) if row.get("key") in selected]
    expressions = [row for row in digest.get("expressions", []) if row.get("key") in selected]
    return errors, expressions


def approval_date(digest):
    decided = parse_iso((digest.get("decision") or {}).get("decided_at"))
    if decided is None:
        decided = parse_iso(digest.get("window", {}).get("end"))
    return decided.astimezone(LOCAL_TZ).date().isoformat()


def build_import_session(digest, today):
    error_rows, expression_rows = selected_digest_rows(digest)
    errors = []
    for row in error_rows:
        example = (row.get("examples") or [{}])[0]
        ref = example.get("turn_ref", {}) if isinstance(example, dict) else {}
        turn_dt = parse_iso(ref.get("timestamp"))
        context = "chat · %s · %s" % (ref.get("source", ""), turn_dt.astimezone(LOCAL_TZ).date().isoformat() if turn_dt else "")
        errors.append({
            "pattern_id": row.get("pattern_id", ""),
            "category": row.get("category", ""),
            "subcategory": row.get("subcategory", ""),
            "severity": row.get("severity", ""),
            "description": row.get("pattern_summary", ""),
            "your_answer": example.get("quote", ""),
            "correct_answer": example.get("correction", ""),
            "context": context,
            "notes": example.get("explanation_vi", ""),
            "review_quality": 2,
        })
    difficulty = learner_difficulty()
    vocabulary = []
    for row in expression_rows:
        vocabulary.append({
            "item_id": "chat_expr_" + row.get("intent_id", ""),
            "item_type": "vocabulary",
            "content": row.get("scenario", ""),
            "answer": row.get("model_answer", ""),
            "category": "chat_expression",
            "difficulty": difficulty,
            "initial_quality": 3,
            "priority": "medium",
        })
    return {"date": today, "errors": errors, "new_vocabulary": vocabulary}, error_rows, expression_rows


def vietnamese_import_summary(error_count, new_cards, existing_cards, due_date):
    total_selected = error_count + new_cards + existing_cards
    if not total_selected:
        return "Không lưu mục nào."
    try:
        due_text = datetime.strptime(due_date, "%Y-%m-%d").strftime("%d/%m")
    except ValueError:
        due_text = due_date
    existing_text = ""
    if existing_cards:
        existing_text = " (%d thẻ đã có, giữ lịch cũ)" % existing_cards
    return "Đã lưu %d lỗi tiếng Anh và %d thẻ cách diễn đạt mới%s. Thẻ mới đến hạn %s trong /fluent-review." % (
        error_count, new_cards, existing_text, due_text)


def perform_import(digest_id):
    """Import a decided digest. Lock order: chat-review lock first, db_lock second."""
    base = review_dir()
    digest_path = base / "digests" / (digest_id + ".json")
    digest = read_json(digest_path, None)
    if digest is None:
        raise ReviewError("digest not found")
    if not digest.get("decision"):
        raise ReviewError("digest has no recorded decision")

    root = data_dir()
    mistakes_path = root / "mistakes-db.json"
    sr_path = root / "spaced-repetition.json"
    if not mistakes_path.exists() or not sr_path.exists():
        raise ReviewError("mistakes-db.json or spaced-repetition.json is missing")

    with db_lock():
        mistakes = strict_load_json(mistakes_path)
        sr = strict_load_json(sr_path)
        in_mistakes = digest_id in chat_import_ids(mistakes)
        in_sr = digest_id in chat_import_ids(sr)
        if in_mistakes and in_sr:
            if digest.get("status") != "imported":
                error_rows, expression_rows = selected_digest_rows(digest)
                today = approval_date(digest)
                digest["status"] = "imported"
                digest["imported_at"] = minute_dt(datetime.now(LOCAL_TZ)).isoformat()
                digest["import_summary"] = digest.get("import_summary") or {
                    "errors": len(error_rows),
                    "new_cards": 0,
                    "existing_cards": len(expression_rows),
                    "due_date": UPDATE_DB.tomorrow(today),
                }
                atomic_write_json(digest_path, digest)
            summary = digest.get("import_summary", {})
            return vietnamese_import_summary(
                summary.get("errors", 0), summary.get("new_cards", 0),
                summary.get("existing_cards", 0), summary.get("due_date", ""))
        if in_mistakes != in_sr:
            raise ReviewError("chat_imports is present in only one database; manual check required")

        today = approval_date(digest)
        session, error_rows, expression_rows = build_import_session(digest, today)
        existing_cards = sum(1 for row in expression_rows if "chat_expr_" + row.get("intent_id", "") in sr.get("items", {}))
        new_cards = len(expression_rows) - existing_cards

        backup_chat_import_files(digest_id, [mistakes_path, sr_path])
        new_mistakes = copy.deepcopy(mistakes)
        new_sr = copy.deepcopy(sr)
        UPDATE_DB.update_mistakes_db(new_mistakes, session)
        UPDATE_DB.update_spaced_repetition(new_sr, session)
        append_chat_import_id(new_mistakes, digest_id)
        append_chat_import_id(new_sr, digest_id)
        files = {"mistakes": mistakes_path, "sr": sr_path}
        UPDATE_DB.save_all(files, {"mistakes": new_mistakes, "sr": new_sr})

        due_date = UPDATE_DB.tomorrow(today)
        digest["status"] = "imported"
        digest["imported_at"] = minute_dt(datetime.now(LOCAL_TZ)).isoformat()
        digest["import_summary"] = {
            "errors": len(error_rows),
            "new_cards": new_cards,
            "existing_cards": existing_cards,
            "due_date": due_date,
        }
        atomic_write_json(digest_path, digest)
        return vietnamese_import_summary(len(error_rows), new_cards, existing_cards, due_date)


def ingest(args):
    base = review_dir()
    with review_lock(base):
        analysis_path = Path(args.analysis).expanduser()
        analysis = read_json(analysis_path, None)
        if analysis is None:
            print("problem: cannot read analysis JSON", file=sys.stderr)
            return 2
        state = load_state(base)
        run_id = analysis.get("run_id") if isinstance(analysis, dict) else None
        run_row = None
        for row in reversed(state.get("runs", [])):
            if row.get("run_id") == run_id:
                run_row = row
                break
        if run_row and run_row.get("status") == "done":
            print("already ingested")
            return 0
        problems = []
        latest_prepared = None
        for row in reversed(state.get("runs", [])):
            if row.get("status") == "prepared":
                latest_prepared = row
                break
        if latest_prepared is None or run_id != latest_prepared.get("run_id"):
            problems.append("run_id does not match the latest prepared run")
        run_dir = base / "runs" / str(run_id)
        package = read_json(run_dir / "package.json", None)
        if package is None:
            problems.append("package.json for run is missing")
        else:
            problems.extend(validate_analysis(package, analysis))
        if problems:
            for problem in problems:
                print("problem: %s" % problem, file=sys.stderr)
            return 2
        atomic_write_json(run_dir / "analysis.json", analysis)
        digest = finalize_run(base, run_dir, package, analysis)
        print("status: done")
        print(render_digest(digest, load_intents(base)))
        return 0


def available_keys(digest):
    return set(row.get("key") for row in digest.get("english", [])) | set(row.get("key") for row in digest.get("expressions", []))


def approve(args):
    base = review_dir()
    with review_lock(base):
        path = base / "digests" / (args.digest + ".json")
        digest = read_json(path, None)
        if digest is None:
            print("error: digest not found", file=sys.stderr)
            return 2
        if not args.all and not args.select and not args.reject:
            print("error: approve requires --all, --select, or --reject", file=sys.stderr)
            return 2
        keys = available_keys(digest)
        selected = set()
        rejected = set()
        if args.all:
            selected = set(keys)
        if args.select:
            selected = set(part.strip() for part in args.select.split(",") if part.strip())
            rejected = set(keys) - selected
        if args.reject:
            explicit = set(part.strip() for part in args.reject.split(",") if part.strip())
            rejected |= explicit
            selected -= explicit
        unknown = (selected | rejected) - keys
        if unknown:
            print("error: unknown digest keys: %s" % ",".join(sorted(unknown)), file=sys.stderr)
            return 2
        decided_at = minute_dt(datetime.now(LOCAL_TZ)).isoformat()
        new_decision = {"selected": sorted(selected), "rejected": sorted(rejected), "decided_at": decided_at}
        if digest.get("decision"):
            old = digest["decision"]
            if set(old.get("selected", [])) == selected and set(old.get("rejected", [])) == rejected:
                if digest.get("status") == "imported":
                    print("already approved")
                    return 0
                try:
                    print(perform_import(args.digest))
                except (ReviewError, TimeoutError) as exc:
                    print("error: import failed: %s" % exc, file=sys.stderr)
                    return 2
                except Exception as exc:
                    print("error: import failed: %s" % exc, file=sys.stderr)
                    return 2
                print("already approved")
                return 0
            print("error: digest already has a different decision", file=sys.stderr)
            return 2
        intents = load_intents(base)
        for row in digest.get("expressions", []):
            intent = intents.get(row.get("intent_id"))
            if not intent:
                continue
            if row.get("key") in selected:
                intent["status"] = "approved"
                intent["card_id"] = "chat_expr_" + row["intent_id"]
            elif row.get("key") in rejected:
                intent["status"] = "pending"
                intent["counting_since"] = decided_at
            intent["updated_run"] = digest.get("run_id")
        digest["decision"] = new_decision
        digest["status"] = "approved"
        atomic_write_json(path, digest)
        save_intents(base, intents)
        print("status: approved")
        print("selected: %s" % ",".join(sorted(selected)))
        print("rejected: %s" % ",".join(sorted(rejected)))
        try:
            print(perform_import(args.digest))
        except (ReviewError, TimeoutError) as exc:
            print("error: import failed: %s" % exc, file=sys.stderr)
            return 2
        except Exception as exc:
            print("error: import failed: %s" % exc, file=sys.stderr)
            return 2
        return 0


def import_command(args):
    base = review_dir()
    with review_lock(base):
        try:
            print(perform_import(args.digest))
        except (ReviewError, TimeoutError) as exc:
            print("error: import failed: %s" % exc, file=sys.stderr)
            return 2
        except Exception as exc:
            print("error: import failed: %s" % exc, file=sys.stderr)
            return 2
        return 0


def digest_command(args):
    base = review_dir()
    if args.digest_command != "show":
        print("error: unknown digest command", file=sys.stderr)
        return 2
    digest_id = args.id
    if not digest_id:
        state = load_state(base)
        for row in reversed(state.get("runs", [])):
            if row.get("digest_id"):
                digest_id = row["digest_id"]
                break
    if not digest_id:
        print("error: no digest found", file=sys.stderr)
        return 2
    digest = read_json(base / "digests" / (digest_id + ".json"), None)
    if digest is None:
        print("error: digest not found", file=sys.stderr)
        return 2
    print(render_digest(digest, load_intents(base)))
    return 0


def status_command(args):
    base = review_dir()
    state = load_state(base)
    intents = load_intents(base)
    latest_run = state.get("runs", [])[-1] if state.get("runs") else None
    print("last_end: %s" % (state.get("last_end") or ""))
    print("latest_run: %s" % (latest_run.get("run_id") if latest_run else ""))
    print("latest_run_status: %s" % (latest_run.get("status") if latest_run else ""))
    latest_digest_id = ""
    for row in reversed(state.get("runs", [])):
        if row.get("digest_id"):
            latest_digest_id = row["digest_id"]
            break
    latest_digest_status = ""
    if latest_digest_id:
        latest_digest = read_json(base / "digests" / (latest_digest_id + ".json"), {})
        latest_digest_status = latest_digest.get("status", "")
    print("latest_digest: %s" % latest_digest_id)
    print("latest_digest_status: %s" % latest_digest_status)
    awaiting = []
    approved_not_imported = []
    digest_dir = base / "digests"
    if digest_dir.exists():
        for path in sorted(digest_dir.glob("*.json")):
            row = read_json(path, {})
            if row.get("status") == "awaiting_approval":
                awaiting.append(row.get("digest_id", path.stem))
            elif row.get("status") == "approved":
                approved_not_imported.append(row.get("digest_id", path.stem))
    print("digests_awaiting_approval: %s" % ",".join(awaiting))
    print("digests_approved_not_imported: %s" % ",".join(approved_not_imported))
    counts = {"pending": 0, "proposed": 0, "approved": 0, "expired": 0}
    for intent in intents.values():
        counts[intent.get("status", "pending")] = counts.get(intent.get("status", "pending"), 0) + 1
    print("intents: " + " ".join("%s=%d" % (key, counts.get(key, 0)) for key in ("pending", "proposed", "approved", "expired")))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Prepare and ingest daily Fluent chat reviews.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--now")
    p.add_argument("--home")
    p.add_argument("--orca-db")
    p.add_argument("--no-orca", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=prepare)

    p = sub.add_parser("ingest")
    p.add_argument("analysis")
    p.set_defaults(func=ingest)

    p = sub.add_parser("approve")
    p.add_argument("--digest", required=True)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true")
    group.add_argument("--select")
    p.add_argument("--reject")
    p.set_defaults(func=approve)

    p = sub.add_parser("import")
    p.add_argument("--digest", required=True)
    p.set_defaults(func=import_command)

    p = sub.add_parser("digest")
    p.add_argument("digest_command", choices=("show",))
    p.add_argument("id", nargs="?")
    p.set_defaults(func=digest_command)

    p = sub.add_parser("status")
    p.set_defaults(func=status_command)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LockContended as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except ReviewError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

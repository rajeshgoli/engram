"""Chunk assembly with drift-priority scheduling.

Reworked from v2's next_chunk() for v3: 4 living docs, stable IDs,
FULL/STUB forms, and all 4 drift threshold types.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from engram.config import resolve_doc_paths
from engram.epistemic_history import (
    extract_external_history_for_entry,
    extract_inline_history_lines,
    infer_history_path,
)
from engram.fold.ids import IDAllocator, estimate_new_entities
from engram.fold.prompt import render_agent_prompt, render_chunk_input, render_triage_input
from engram.parse import extract_id, is_stub, parse_sections

# Regex for ISO dates (YYYY-MM-DD) in text
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# Regex for month-name dates, e.g. "Dec 11", "Dec 11, 2025", "11 Dec 2025"
_MONTH_PATTERN = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|sept|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?"
)
_NATURAL_DATE_RE = re.compile(
    rf"\b(?:(?P<month>{_MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}})"
    rf"(?:(?:,\s*|\s+)(?P<year>\d{{4}}))?|(?P<day2>\d{{1,2}})\s+"
    rf"(?P<month2>{_MONTH_PATTERN})\.?(?:(?:,\s*|\s+)(?P<year2>\d{{4}}))?)\b",
    re.IGNORECASE,
)
_MONTH_TO_NUM = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
# Cache lowercased queue file text across repeated drift scans.
_QUEUE_TEXT_CACHE_MAX_ENTRIES = 2048
_QUEUE_TEXT_CACHE: OrderedDict[tuple[str, str, int, int], str] = OrderedDict()

# Evidence bullets in external epistemic history files.
_EVIDENCE_COMMIT_RE = re.compile(r"Evidence@([0-9a-fA-F]{7,40})")
_EVIDENCE_COMMIT_DATE_CACHE: dict[tuple[str, str], datetime | None] = {}


@dataclass
class DriftReport:
    """Results from scanning living docs for drift conditions."""

    orphaned_concepts: list[dict] = field(default_factory=list)
    epistemic_audit: list[dict] = field(default_factory=list)
    contested_claims: list[dict] = field(default_factory=list)
    stale_unverified: list[dict] = field(default_factory=list)
    workflow_repetitions: list[dict] = field(default_factory=list)
    ref_commit: str | None = None

    def triggered(self, thresholds: dict) -> str | None:
        """Return the highest-priority drift type that exceeds its threshold.

        Priority order:
        orphans > epistemic_audit > contested > stale_unverified > workflow.
        Returns None if no threshold is exceeded.
        """
        if len(self.orphaned_concepts) > thresholds.get("orphan_triage", 50):
            return "orphan_triage"
        if len(self.epistemic_audit) > thresholds.get("epistemic_audit", 0):
            return "epistemic_audit"
        if len(self.contested_claims) > thresholds.get("contested_review", 5):
            return "contested_review"
        if len(self.stale_unverified) > thresholds.get("stale_unverified", 10):
            return "stale_unverified"
        if len(self.workflow_repetitions) > thresholds.get("workflow_repetition", 3):
            return "workflow_synthesis"
        return None


@dataclass
class ChunkResult:
    """Output from next_chunk() for CLI reporting."""

    chunk_id: int
    input_path: Path
    prompt_path: Path
    chunk_type: str  # "fold" | "orphan_triage" | "epistemic_audit" | ...
    items_count: int
    chunk_chars: int
    budget: int
    living_docs_chars: int
    remaining_queue: int
    date_range: str | None = None
    drift_entry_count: int = 0
    pre_assigned_ids: dict[str, list[str]] = field(default_factory=dict)


# ------------------------------------------------------------------
# Queue drain predicate
# ------------------------------------------------------------------


def queue_is_empty(project_root: Path) -> bool:
    """Return True if the dispatch queue is drained.

    Checks ``.engram/queue.jsonl``: returns True if the file is missing,
    empty, or contains zero entries.  This is the drain predicate used
    to decide when L0 briefing should regenerate.
    """
    queue_file = project_root / ".engram" / "queue.jsonl"
    if not queue_file.exists():
        return True
    try:
        text = queue_file.read_text()
    except OSError:
        return True
    for line in text.splitlines():
        if line.strip():
            return False
    return True


# ------------------------------------------------------------------
# Drift detection
# ------------------------------------------------------------------


def _extract_code_paths(section_text: str) -> list[str]:
    """Extract file paths from a Code: field in a concept section."""
    paths: list[str] = []
    for line in section_text.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("- **Code:**") and not stripped.startswith("**Code:**"):
            continue
        # Everything after "Code:" is the value
        _, _, val = stripped.partition(":**")
        val = val.strip()
        for p in val.split(","):
            p = p.strip().strip("`").strip()
            if p:
                paths.append(p)
    return paths


def _resolve_ref_commit(project_root: Path, fold_from: str) -> str | None:
    """Resolve a fold_from date to the nearest git commit hash.

    Uses ``git log --before=<date+1day> -1 --format=%H`` to find the
    latest commit on or before the fold_from date.

    Returns the commit hash, or None if no commit found.
    """
    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--before={fold_from}T23:59:59",
                "-1", "--format=%H",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _file_exists_at_commit(
    project_root: Path, ref_commit: str, path: str,
) -> bool:
    """Check if a file exists at a specific git commit.

    Uses ``git ls-tree <commit> -- <path>`` — exit code 0 with output
    means the file exists.
    """
    try:
        result = subprocess.run(
            ["git", "ls-tree", ref_commit, "--", path],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=10,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _find_orphaned_concepts(
    concepts_path: Path,
    project_root: Path,
    ref_commit: str | None = None,
) -> list[dict]:
    """Find ACTIVE concepts whose referenced source files are all missing.

    When *ref_commit* is None (steady-state), uses ``os.path.exists()``
    against the current filesystem.  When set (fold-forward), uses
    ``_file_exists_at_commit()`` so only files missing at the reference
    commit are flagged.
    """
    if not concepts_path.exists():
        return []
    sections = parse_sections(concepts_path.read_text())
    orphans: list[dict] = []
    for sec in sections:
        # ACTIVE is not in parse.STATUS_RE, so sec["status"] is None for
        # ACTIVE entries. Check heading directly for "(ACTIVE".
        if "(ACTIVE" not in sec["heading"].upper():
            continue
        if is_stub(sec["heading"]):
            continue
        code_paths = _extract_code_paths(sec["text"])
        if not code_paths:
            continue

        if ref_commit:
            all_missing = all(
                not _file_exists_at_commit(project_root, ref_commit, p)
                for p in code_paths
            )
        else:
            all_missing = all(
                not (project_root / p).exists() for p in code_paths
            )

        if all_missing:
            orphans.append({
                "name": sec["heading"].lstrip("#").strip(),
                "id": extract_id(sec["heading"]),
                "paths": code_paths,
            })
    return orphans


def _parse_natural_date(
    match: re.Match[str],
    *,
    now: datetime,
) -> datetime | None:
    """Parse a month-name date regex match into a UTC datetime."""
    month_name = (match.group("month") or match.group("month2") or "").lower()
    day_raw = match.group("day") or match.group("day2")
    year_raw = match.group("year") or match.group("year2")
    if not month_name or not day_raw:
        return None

    month = _MONTH_TO_NUM.get(month_name)
    if not month:
        return None

    try:
        day = int(day_raw)
    except ValueError:
        return None

    if year_raw:
        try:
            year = int(year_raw)
        except ValueError:
            return None
    else:
        # Missing year: infer nearest past occurrence to avoid future skew.
        year = now.year

    try:
        parsed = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None

    if not year_raw and parsed > now:
        try:
            parsed = datetime(year - 1, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None

    return parsed


def _extract_latest_date(text: str) -> datetime | None:
    """Extract the most recent parseable date from text.

    Supports:
    - ISO dates: YYYY-MM-DD
    - Month-name dates: "Dec 11", "Dec 11, 2025", "11 Dec 2025"
    """
    parsed: list[datetime] = []

    for iso in _DATE_RE.findall(text):
        try:
            parsed.append(datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=timezone.utc))
        except ValueError:
            continue

    now = datetime.now(timezone.utc)
    for match in _NATURAL_DATE_RE.finditer(text):
        dt = _parse_natural_date(match, now=now)
        if dt is not None:
            parsed.append(dt)

    return max(parsed) if parsed else None


def _extract_latest_history_date(section_text: str) -> datetime | None:
    """Extract the most recent parseable date from an epistemic History block."""
    history_lines = extract_inline_history_lines(section_text)

    if not history_lines:
        return None

    return _extract_latest_date("\n".join(history_lines))


def _extract_latest_external_history_date(
    *,
    epistemic_path: Path,
    entry_id: str | None,
    project_root: Path | None = None,
) -> datetime | None:
    """Extract latest parseable date from inferred per-entry history file."""
    if not entry_id:
        return None
    history_path = infer_history_path(epistemic_path, entry_id)
    if not history_path.exists():
        return None
    try:
        history_text = history_path.read_text()
    except OSError:
        return None
    entry_history = extract_external_history_for_entry(history_text, entry_id)
    if not entry_history:
        return None
    latest_date = _extract_latest_date(entry_history)
    latest_evidence_date = _extract_latest_evidence_commit_date(
        entry_history=entry_history,
        project_root=project_root,
    )
    candidates = [dt for dt in (latest_date, latest_evidence_date) if dt is not None]
    return max(candidates) if candidates else None


def _resolve_git_commit_unix_ts(*, project_root: Path, commit: str) -> int | None:
    """Resolve a git commit hash to a unix timestamp (seconds)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "show", "-s", "--format=%ct", commit],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    try:
        return int(raw)
    except ValueError:
        return None


def _extract_latest_evidence_commit_date(
    *,
    entry_history: str,
    project_root: Path | None,
) -> datetime | None:
    """Extract latest commit date referenced by Evidence@<commit> bullets.

    Evidence lines in external history files are append-only and often omit
    explicit dates. Treating the referenced commit date as "activity time"
    prevents infinite epistemic drift triage loops.
    """
    if project_root is None:
        return None
    try:
        root_key = str(project_root.resolve())
    except OSError:
        root_key = str(project_root)

    commits = _EVIDENCE_COMMIT_RE.findall(entry_history)
    if not commits:
        return None

    latest: datetime | None = None
    for sha in commits:
        cache_key = (root_key, sha)
        if cache_key not in _EVIDENCE_COMMIT_DATE_CACHE:
            ts = _resolve_git_commit_unix_ts(project_root=project_root, commit=sha)
            _EVIDENCE_COMMIT_DATE_CACHE[cache_key] = (
                datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None else None
            )
        cached = _EVIDENCE_COMMIT_DATE_CACHE[cache_key]
        if cached is None:
            continue
        if latest is None or cached > latest:
            latest = cached
    return latest


def _latest_epistemic_activity_date(
    *,
    epistemic_path: Path,
    section_heading: str,
    section_text: str,
    project_root: Path | None = None,
) -> datetime | None:
    """Return latest activity date for an epistemic entry from inline/external history."""
    entry_id = extract_id(section_heading)
    inline_date = _extract_latest_history_date(section_text)
    external_date = _extract_latest_external_history_date(
        epistemic_path=epistemic_path,
        entry_id=entry_id,
        project_root=project_root,
    )
    dates = [dt for dt in (inline_date, external_date) if dt is not None]
    return max(dates) if dates else None


def _parse_queue_date(raw: Any) -> datetime | None:
    """Parse a queue entry date into timezone-aware UTC datetime."""
    if not isinstance(raw, str):
        return None
    value = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_epistemic_subject(heading: str) -> str | None:
    """Extract an epistemic entry subject from heading text."""
    match = re.match(r"^##\s+E\d{3,}:\s+(.+)$", heading.strip())
    if not match:
        return None
    subject = match.group(1)
    subject = re.sub(r"\s+\([^)]*\)\s*(?:→\s+\S+)?\s*$", "", subject).strip()
    return subject or None


def _read_queue_entry_text(project_root: Path, item: dict[str, Any]) -> str:
    """Read queue entry text for subject-reference matching.

    Returns lowercased text and caches it by path+mtime+size to avoid
    re-reading large queue files on every drift scan.
    """
    rel_path = item.get("path")
    if not isinstance(rel_path, str):
        return ""

    item_path = project_root / rel_path
    item_type = str(item.get("type", ""))

    try:
        stat = item_path.stat()
    except OSError:
        return ""

    cache_key = (
        str(item_path.resolve()),
        item_type,
        stat.st_mtime_ns,
        stat.st_size,
    )
    cached = _QUEUE_TEXT_CACHE.get(cache_key)
    if cached is not None:
        _QUEUE_TEXT_CACHE.move_to_end(cache_key)
        return cached

    try:
        if item_type == "issue":
            from engram.fold.sources import render_issue_markdown
            issue_data = json.loads(item_path.read_text())
            rendered = render_issue_markdown(issue_data)
            issue_title = issue_data.get("title") or item.get("issue_title")
            if isinstance(issue_title, str) and issue_title.strip():
                text = f"{issue_title.strip()}\n\n{rendered}"
            else:
                text = rendered
        else:
            text = item_path.read_text(errors="ignore")
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ""

    lowered = text.lower()
    _QUEUE_TEXT_CACHE[cache_key] = lowered
    _QUEUE_TEXT_CACHE.move_to_end(cache_key)
    while len(_QUEUE_TEXT_CACHE) > _QUEUE_TEXT_CACHE_MAX_ENTRIES:
        _QUEUE_TEXT_CACHE.popitem(last=False)
    return lowered


def _read_queue_entries(queue_file: Path) -> list[dict[str, Any]]:
    """Load queue.jsonl entries, skipping malformed lines."""
    if not queue_file.exists():
        return []
    entries: list[dict[str, Any]] = []
    with open(queue_file) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def _find_claims_by_status(
    epistemic_path: Path, status: str, days_threshold: int,
) -> list[dict]:
    """Find epistemic entries with given status older than days_threshold."""
    if not epistemic_path.exists():
        return []
    sections = parse_sections(epistemic_path.read_text())
    now = datetime.now(timezone.utc)
    results: list[dict] = []
    for sec in sections:
        if sec["status"] != status:
            continue
        if is_stub(sec["heading"]):
            continue
        latest = _latest_epistemic_activity_date(
            epistemic_path=epistemic_path,
            section_heading=sec["heading"],
            section_text=sec["text"],
            project_root=None,
        )
        if latest is None:
            continue
        age_days = (now - latest).days
        if age_days > days_threshold:
            results.append({
                "name": sec["heading"].lstrip("#").strip(),
                "id": extract_id(sec["heading"]),
                "days_old": age_days,
                "last_date": latest.strftime("%Y-%m-%d"),
            })
    return results


def _find_stale_epistemic_entries(
    epistemic_path: Path,
    *,
    days_threshold: int,
    project_root: Path | None = None,
    queue_entries: list[dict[str, Any]] | None = None,
) -> list[dict]:
    """Find stale believed/unverified epistemic entries for audit.

    An entry is stale when:
    - status is believed or unverified
    - age since latest History date exceeds days_threshold
    - no queue item references the entry subject after that history date
    """
    if not epistemic_path.exists():
        return []

    searchable_queue: list[tuple[datetime, str]] = []
    if project_root and queue_entries:
        for item in queue_entries:
            queued_at = _parse_queue_date(item.get("date"))
            if queued_at is None:
                continue
            text = _read_queue_entry_text(project_root, item)
            if text:
                searchable_queue.append((queued_at, text))

    sections = parse_sections(epistemic_path.read_text())
    now = datetime.now(timezone.utc)
    results: list[dict] = []

    for sec in sections:
        if sec["status"] not in {"believed", "unverified"}:
            continue
        if is_stub(sec["heading"]):
            continue

        latest_history = _latest_epistemic_activity_date(
            epistemic_path=epistemic_path,
            section_heading=sec["heading"],
            section_text=sec["text"],
            project_root=project_root,
        )
        if latest_history is None:
            continue

        age_days = (now - latest_history).days
        if age_days <= days_threshold:
            continue

        subject = _extract_epistemic_subject(sec["heading"])
        subject_lower = subject.lower() if subject else None
        was_referenced = bool(
            subject_lower and any(
                queued_at > latest_history and subject_lower in queue_text
                for queued_at, queue_text in searchable_queue
            )
        )
        if was_referenced:
            continue

        results.append({
            "name": sec["heading"].lstrip("#").strip(),
            "id": extract_id(sec["heading"]),
            "subject": subject,
            "status": sec["status"],
            "days_old": age_days,
            "last_date": latest_history.strftime("%Y-%m-%d"),
        })

    return results


def _read_synthesized_workflow_ids(manifest_file: Path) -> set[str]:
    """Return workflow IDs already covered in a previous synthesis chunk.

    Reads ``chunks_manifest.yaml`` and collects every workflow ID recorded
    under a ``workflow_synthesis`` entry.  These are excluded from the next
    synthesis trigger so the fold doesn't loop infinitely when an agent
    decides to keep all workflows as CURRENT.
    """
    if not manifest_file.exists():
        return set()
    import yaml  # local import — yaml is a standard engram dependency

    try:
        with open(manifest_file) as fh:
            manifest = yaml.safe_load(fh) or []
    except yaml.YAMLError as exc:
        log.warning("Could not parse %s — skipping synthesis dedup: %s", manifest_file, exc)
        return set()
    synthesized: set[str] = set()
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "workflow_synthesis":
            wids = entry.get("workflow_ids", [])
            if isinstance(wids, list):
                for wid in wids:
                    synthesized.add(wid)
    return synthesized


def _find_workflow_repetitions(workflows_path: Path) -> list[dict]:
    """Find CURRENT workflow entries as candidates for synthesis.

    Returns all CURRENT (non-STUB) workflows. The caller compares
    the count against the workflow_repetition threshold.
    """
    if not workflows_path.exists():
        return []
    sections = parse_sections(workflows_path.read_text())
    results: list[dict] = []
    for sec in sections:
        if sec["status"] != "current":
            continue
        if is_stub(sec["heading"]):
            continue
        results.append({
            "name": sec["heading"].lstrip("#").strip(),
            "id": extract_id(sec["heading"]),
        })
    return results


def scan_drift(
    config: dict,
    project_root: Path,
    fold_from: str | None = None,
) -> DriftReport:
    """Scan all living docs for drift conditions.

    When *fold_from* is set, orphan detection checks file existence at
    the git commit nearest to that date instead of the current filesystem.
    """
    paths = resolve_doc_paths(config, project_root)
    thresholds = config.get("thresholds", {})
    queue_entries = _read_queue_entries(project_root / ".engram" / "queue.jsonl")

    ref_commit: str | None = None
    if fold_from:
        ref_commit = _resolve_ref_commit(project_root, fold_from)
        if ref_commit is None:
            log.warning(
                "Could not resolve fold_from=%s to a git commit; "
                "falling back to filesystem check",
                fold_from,
            )

    return DriftReport(
        orphaned_concepts=_find_orphaned_concepts(
            paths["concepts"], project_root, ref_commit=ref_commit,
        ),
        epistemic_audit=_find_stale_epistemic_entries(
            paths["epistemic"],
            days_threshold=thresholds.get("stale_epistemic_days", 90),
            project_root=project_root,
            queue_entries=queue_entries,
        ),
        contested_claims=_find_claims_by_status(
            paths["epistemic"], "contested",
            thresholds.get("contested_review_days", 14),
        ),
        stale_unverified=_find_claims_by_status(
            paths["epistemic"], "unverified",
            thresholds.get("stale_unverified_days", 30),
        ),
        workflow_repetitions=_find_workflow_repetitions(paths["workflows"]),
        ref_commit=ref_commit,
    )


# ------------------------------------------------------------------
# Budget computation
# ------------------------------------------------------------------


def compute_budget(config: dict, doc_paths: dict[str, Path]) -> tuple[int, int]:
    """Compute available char budget for chunk content.

    Returns (budget, living_docs_chars).
    """
    budget_cfg = config.get("budget", {})
    context_limit = budget_cfg.get("context_limit_chars", 600_000)
    overhead = budget_cfg.get("instructions_overhead", 100_000)
    max_chunk = budget_cfg.get("max_chunk_chars", 80_000)

    living_doc_keys = ["timeline", "concepts", "epistemic", "workflows"]
    living_docs_chars = 0
    for key in living_doc_keys:
        p = doc_paths.get(key)
        if p and p.exists():
            living_docs_chars += len(p.read_text())

    remaining = context_limit - living_docs_chars - overhead
    budget = min(max(0, remaining), max_chunk)
    return budget, living_docs_chars


# ------------------------------------------------------------------
# Item rendering
# ------------------------------------------------------------------


def _render_item_content(item: dict[str, Any], project_root: Path) -> str:
    """Render a single queue item as markdown for the chunk input."""
    tag = "REVISIT" if item["pass"] == "revisit" else "INITIAL"
    item_path = project_root / item["path"]

    if item["type"] == "prompts":
        n = item.get("prompt_count", "?")
        header = f"## [USER PROMPTS] Session ({n} prompts)\n"
        header += f"**Date:** {item['date'][:10]}\n\n"
    elif item["type"] == "issue":
        header = f"## [{tag}] Issue #{item['issue_number']}: {item['issue_title']}\n"
        header += f"**Created:** {item['date'][:10]}\n\n"
    else:
        header = f"## [{tag}] Doc: {item['path']}\n"
        header += f"**Created:** {item['date'][:10]}"
        if tag == "REVISIT":
            header += f" | **Modified:** {item['date'][:10]}"
            header += f" | **First seen:** {item.get('first_seen_date', '?')[:10]}"
            header += (
                "\nThis doc was updated since first processed. "
                "Check existing entries and update based on what changed."
            )
        header += "\n\n"

    try:
        if item["type"] == "issue":
            from engram.fold.sources import render_issue_markdown
            issue_data = json.loads(item_path.read_text())
            content = render_issue_markdown(issue_data)
        else:
            content = item_path.read_text(errors="ignore")
    except FileNotFoundError:
        content = f"[FILE NOT FOUND: {item_path}]\n"

    return header + content + "\n\n---\n\n"


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------


def next_chunk(
    config: dict,
    project_root: Path,
    fold_from: str | None = None,
) -> ChunkResult:
    """Build the next chunk's input.md and prompt.txt.

    Parameters
    ----------
    config:
        Engram config dict.
    project_root:
        Project root directory.
    fold_from:
        Optional ISO date string.  When set, orphan detection uses the
        git state at that date instead of the current filesystem, and the
        triage prompt includes temporal context.

    Returns a ChunkResult with paths and metadata for CLI reporting.

    Raises:
        FileNotFoundError: If queue.jsonl doesn't exist.
        ValueError: If queue is empty.
    """
    engram_dir = project_root / ".engram"
    chunks_dir = engram_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    queue_file = engram_dir / "queue.jsonl"
    manifest_file = engram_dir / "chunks_manifest.yaml"

    if not queue_file.exists():
        raise FileNotFoundError("No queue found. Run 'build-queue' first.")

    with open(queue_file) as fh:
        queue = [json.loads(line) for line in fh if line.strip()]

    if not queue:
        raise ValueError("Queue is empty. All chunks have been produced.")

    # Determine chunk ID from existing chunks
    existing = list(chunks_dir.glob("chunk_*_input.md"))
    chunk_id = len(existing) + 1

    doc_paths = resolve_doc_paths(config, project_root)
    budget, living_docs_chars = compute_budget(config, doc_paths)

    # Check drift priorities
    drift = scan_drift(config, project_root, fold_from=fold_from)

    # Suppress synthesis only when every CURRENT workflow was already covered
    # in a prior synthesis chunk.  If any new ID exists, fire with the full
    # set so the threshold retains its original semantics (total CURRENT count).
    synthesized_ids = _read_synthesized_workflow_ids(manifest_file)
    if synthesized_ids and drift.workflow_repetitions:
        # If any workflow lacks a stable ID, skip dedup — can't safely assert
        # "all synthesized" when some entries are unidentifiable.
        all_have_ids = all(w.get("id") for w in drift.workflow_repetitions)
        if all_have_ids:
            current_ids = {w["id"] for w in drift.workflow_repetitions}
            if current_ids.issubset(synthesized_ids):
                drift.workflow_repetitions = []

    thresholds = config.get("thresholds", {})
    drift_type = drift.triggered(thresholds)

    # Reuse ref_commit resolved by scan_drift (avoids duplicate git call)
    ref_commit = drift.ref_commit

    if drift_type:
        # Drift triage chunk — queue is NOT consumed
        with open(queue_file, "w") as fh:
            for entry in queue:
                fh.write(json.dumps(entry) + "\n")

        input_content = render_triage_input(
            drift_type=drift_type,
            drift_report=drift,
            chunk_id=chunk_id,
            doc_paths=doc_paths,
            ref_commit=ref_commit,
            ref_date=fold_from,
            project_root=project_root,
        )

        input_path = chunks_dir / f"chunk_{chunk_id:03d}_input.md"
        input_path.write_text(input_content)

        prompt_content = render_agent_prompt(
            chunk_id=chunk_id,
            date_range=drift_type,
            input_path=input_path,
            doc_paths=doc_paths,
            project_root=project_root,
        )
        prompt_path = chunks_dir / f"chunk_{chunk_id:03d}_prompt.txt"
        prompt_path.write_text(prompt_content)

        # Count drift entries for reporting
        drift_counts = {
            "orphan_triage": len(drift.orphaned_concepts),
            "epistemic_audit": len(drift.epistemic_audit),
            "contested_review": len(drift.contested_claims),
            "stale_unverified": len(drift.stale_unverified),
            "workflow_synthesis": len(drift.workflow_repetitions),
        }

        with open(manifest_file, "a") as fh:
            fh.write(
                f"- id: {chunk_id}\n"
                f"  type: {drift_type}\n"
                f"  entries: {drift_counts.get(drift_type, 0)}\n"
                f"  input_file: {input_path.name}\n"
            )
            if drift_type == "workflow_synthesis":
                wids = [w["id"] for w in drift.workflow_repetitions if w.get("id")]
                fh.write(f"  workflow_ids: {wids}\n")

        return ChunkResult(
            chunk_id=chunk_id,
            input_path=input_path,
            prompt_path=prompt_path,
            chunk_type=drift_type,
            items_count=0,
            chunk_chars=len(input_content),
            budget=budget,
            living_docs_chars=living_docs_chars,
            remaining_queue=len(queue),
            drift_entry_count=drift_counts.get(drift_type, 0),
        )

    # Normal chunk — fill from queue up to budget
    chunk_items: list[dict] = []
    chunk_chars = 0
    while queue and (chunk_chars + queue[0]["chars"]) <= budget:
        item = queue.pop(0)
        chunk_items.append(item)
        chunk_chars += item["chars"]

    if not chunk_items and queue:
        # Single oversized item — take it anyway
        item = queue.pop(0)
        chunk_items.append(item)
        chunk_chars = item["chars"]

    # Pre-assign IDs
    estimates = estimate_new_entities(chunk_items)
    db_path = engram_dir / "engram.db"
    with IDAllocator(db_path) as allocator:
        pre_assigned = allocator.pre_assign_for_chunk(
            new_concepts=estimates["C"],
            new_epistemic=estimates["E"],
            new_workflows=estimates["W"],
        )

    date_range = f"{chunk_items[0]['date'][:10]} to {chunk_items[-1]['date'][:10]}"

    # Render item contents
    items_content = ""
    for item in chunk_items:
        items_content += _render_item_content(item, project_root)

    # Include orphan advisory (below threshold, informational)
    orphan_advisory = ""
    if drift.orphaned_concepts:
        orphan_advisory = (
            "## [ORPHANED CONCEPTS] Active concepts with missing source files\n\n"
        )
        if fold_from and ref_commit:
            orphan_advisory += (
                f"**Note:** Living docs are current through {fold_from} "
                f"(commit `{ref_commit[:12]}`). "
                f"Only files missing at that commit are listed.\n\n"
            )
        for o in drift.orphaned_concepts:
            orphan_advisory += f"- **{o['name']}**: {', '.join(o['paths'])}\n"
        orphan_advisory += (
            f"\n({len(drift.orphaned_concepts)} orphaned concepts found)\n\n---\n\n"
        )

    input_content = render_chunk_input(
        chunk_id=chunk_id,
        date_range=date_range,
        items_content=items_content,
        orphan_advisory=orphan_advisory,
        pre_assigned_ids=pre_assigned,
        doc_paths=doc_paths,
    )

    input_path = chunks_dir / f"chunk_{chunk_id:03d}_input.md"
    input_path.write_text(input_content)

    prompt_content = render_agent_prompt(
        chunk_id=chunk_id,
        date_range=date_range,
        input_path=input_path,
        doc_paths=doc_paths,
        project_root=project_root,
    )
    prompt_path = chunks_dir / f"chunk_{chunk_id:03d}_prompt.txt"
    prompt_path.write_text(prompt_content)

    # Write remaining queue
    with open(queue_file, "w") as fh:
        for entry in queue:
            fh.write(json.dumps(entry) + "\n")

    # Append manifest
    with open(manifest_file, "a") as fh:
        fh.write(
            f"- id: {chunk_id}\n"
            f'  date_range: "{date_range}"\n'
            f"  items: {len(chunk_items)}\n"
            f"  chars: {chunk_chars}\n"
            f"  input_file: {input_path.name}\n"
        )

    return ChunkResult(
        chunk_id=chunk_id,
        input_path=input_path,
        prompt_path=prompt_path,
        chunk_type="fold",
        items_count=len(chunk_items),
        chunk_chars=chunk_chars,
        budget=budget,
        living_docs_chars=living_docs_chars,
        remaining_queue=len(queue),
        date_range=date_range,
        pre_assigned_ids=pre_assigned,
    )

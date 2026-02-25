"""Chunk assembly with drift-priority scheduling.

Reworked from v2's next_chunk() for v3: 4 living docs, stable IDs,
FULL/STUB forms, and all 4 drift threshold types.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import hashlib
import shutil
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from engram.config import resolve_doc_paths
from engram.epistemic_history import (
    extract_external_history_for_entry,
    extract_inline_history_lines,
    infer_current_path,
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
_HEADING_LINE_COMMIT_DATE_CACHE: dict[tuple[str, str, int, int, int, str], datetime | None] = {}
_CHUNK_WORKTREE_NAME_RE = re.compile(r"^engram-chunk-\d{3,}-[0-9a-f]{8}-[A-Za-z0-9._-]+$")
_WORKFLOW_EXPLICIT_SIGNAL_TERMS = (
    "new workflow",
    "create workflow",
    "add workflow",
    "workflow registry",
    "workflow_synthesis",
    "process pattern",
    "runbook",
    "playbook",
)
_STABLE_ID_RE = re.compile(r"\b([CEW])(\d{3,})\b", re.IGNORECASE)
_SESSION_PROMPT_LINE_RE = re.compile(r"^\*\*\[(\d{2}:\d{2})\]\*\*\s+(.*)$")
_SESSION_SM_PROMPT_RE = re.compile(r"^\[sm[^\]]*\]", re.IGNORECASE)
_SESSION_RELAY_PROMPT_RE = re.compile(r"^\[input from:[^\]]+\]", re.IGNORECASE)
_SESSION_RELAY_MAX_CHARS = 320


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
    living_docs_budget_chars: int | None = None
    planning_context_chars: int = 0
    planning_predicted_ids: dict[str, list[str]] = field(default_factory=dict)
    planning_context_ids: dict[str, list[str]] = field(default_factory=dict)
    date_range: str | None = None
    drift_entry_count: int = 0
    pre_assigned_ids: dict[str, list[str]] = field(default_factory=dict)
    context_worktree_path: Path | None = None
    context_commit: str | None = None


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
        for token in _split_code_field_values(val):
            expanded = _expand_braced_path(token)
            for p in expanded:
                cleaned = p.strip().strip("`").strip()
                if not cleaned or "..." in cleaned:
                    continue
                if cleaned.startswith("./"):
                    cleaned = cleaned[2:]
                paths.append(cleaned)
    return paths


def _split_code_field_values(raw: str) -> list[str]:
    """Split a Code field by top-level commas (ignoring commas inside braces)."""
    values: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in raw:
        if ch == "{":
            depth += 1
            current.append(ch)
            continue
        if ch == "}":
            depth = max(0, depth - 1)
            current.append(ch)
            continue
        if ch == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                values.append(part)
            current = []
            continue
        current.append(ch)
    part = "".join(current).strip()
    if part:
        values.append(part)
    return values


def _expand_braced_path(token: str) -> list[str]:
    """Expand a single-level brace expression in a code path token."""
    start = token.find("{")
    end = token.find("}", start + 1) if start != -1 else -1
    if start == -1 or end == -1 or end < start:
        return [token]
    prefix = token[:start]
    suffix = token[end + 1:]
    inner = token[start + 1:end]
    if not inner:
        return [token]
    expanded: list[str] = []
    for value in inner.split(","):
        value = value.strip()
        if value:
            expanded.append(f"{prefix}{value}{suffix}")
    return expanded or [token]


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


def _resolve_head_commit(project_root: Path) -> str | None:
    """Resolve current HEAD commit hash for *project_root*."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
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


def _resolve_chunk_context_commit(
    project_root: Path,
    *,
    date_hint: str | None = None,
    fallback_commit: str | None = None,
) -> str | None:
    """Resolve the commit used for per-chunk context checkout.

    Priority:
    1) explicit fallback commit (e.g., fold_from-resolved reference)
    2) date-based commit nearest *date_hint*
    3) current HEAD commit
    """
    if fallback_commit:
        return fallback_commit
    if date_hint:
        commit = _resolve_ref_commit(project_root, date_hint)
        if commit:
            return commit
    return _resolve_head_commit(project_root)


def _create_chunk_context_worktree(
    project_root: Path,
    *,
    chunk_id: int,
    commit: str,
) -> Path | None:
    """Create per-chunk temporary context worktree under system temp dir."""
    temp_dir = Path(tempfile.gettempdir())
    prefix = f"engram-chunk-{chunk_id:03d}-{commit[:8]}-"
    worktree_path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(temp_dir)))

    try:
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path), commit],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        shutil.rmtree(worktree_path, ignore_errors=True)
        return None

    if result.returncode != 0:
        log.warning(
            "Failed to create chunk context worktree at %s: %s",
            worktree_path,
            (result.stderr or "").strip() or "unknown error",
        )
        shutil.rmtree(worktree_path, ignore_errors=True)
        return None

    return worktree_path


def cleanup_chunk_context_worktree(project_root: Path, worktree_path: Path | None) -> None:
    """Remove a previously-created per-chunk context worktree."""
    if worktree_path is None:
        return

    try:
        resolved = Path(worktree_path).resolve()
    except OSError:
        resolved = None
    if resolved is None:
        return

    temp_root = Path(tempfile.gettempdir())
    try:
        temp_root_resolved = temp_root.resolve()
    except OSError:
        temp_root_resolved = temp_root
    try:
        resolved.relative_to(temp_root_resolved)
    except ValueError:
        log.warning("Refusing to remove non-temp context worktree path: %s", worktree_path)
        return
    if not _CHUNK_WORKTREE_NAME_RE.fullmatch(resolved.name):
        log.warning("Refusing to remove non-engram context worktree path: %s", worktree_path)
        return

    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(resolved)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    shutil.rmtree(resolved, ignore_errors=True)


def _file_exists_at_commit(
    project_root: Path, ref_commit: str, path: str,
) -> bool:
    """Check if a file exists at a specific git commit.

    Uses a cached ``git ls-tree -r --name-only`` lookup and case-insensitive
    matching to tolerate historical path casing drift (e.g., ``Docs/`` vs
    ``docs/`` in older commits).
    """
    raw = path.strip().replace("\\", "/")
    if not raw:
        return False
    if raw.startswith("./"):
        raw = raw[2:]
    raw = raw.rstrip("/")
    lookup = _tracked_paths_lookup_at_commit(str(project_root), ref_commit)
    return raw.lower() in lookup


@lru_cache(maxsize=16)
def _tracked_paths_lookup_at_commit(
    project_root: str,
    ref_commit: str,
) -> dict[str, str]:
    """Return case-insensitive path lookup for all tracked files at commit."""
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", ref_commit],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    if result.returncode != 0:
        return {}

    lookup: dict[str, str] = {}
    for line in result.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        lookup.setdefault(path.lower(), path)
        parts = [part for part in path.split("/") if part]
        for idx in range(1, len(parts)):
            parent = "/".join(parts[:idx])
            lookup.setdefault(parent.lower(), parent)
    return lookup


def _active_concept_ids_at_commit(
    project_root: Path,
    ref_commit: str,
    concepts_path: Path,
) -> set[str] | None:
    """Return ACTIVE concept IDs present in the concept registry at ref commit."""
    try:
        rel_path = concepts_path.relative_to(project_root).as_posix()
    except ValueError:
        return None

    lookup = _tracked_paths_lookup_at_commit(str(project_root), ref_commit)
    matched = lookup.get(rel_path.lower())
    if not matched:
        return None

    try:
        result = subprocess.run(
            ["git", "show", f"{ref_commit}:{matched}"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None

    ids: set[str] = set()
    for sec in parse_sections(result.stdout):
        heading = sec.get("heading", "")
        if "(ACTIVE" not in heading.upper():
            continue
        if is_stub(heading):
            continue
        entry_id = extract_id(heading)
        if entry_id:
            ids.add(entry_id)
    return ids


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
    ref_active_ids = (
        _active_concept_ids_at_commit(project_root, ref_commit, concepts_path)
        if ref_commit
        else None
    )
    orphans: list[dict] = []
    for sec in sections:
        # ACTIVE is not in parse.STATUS_RE, so sec["status"] is None for
        # ACTIVE entries. Check heading directly for "(ACTIVE".
        if "(ACTIVE" not in sec["heading"].upper():
            continue
        if is_stub(sec["heading"]):
            continue
        entry_id = extract_id(sec["heading"])
        if ref_active_ids is not None and entry_id and entry_id not in ref_active_ids:
            # Concept introduced after the fold reference snapshot.
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
                "id": entry_id,
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
    """Extract latest parseable date from inferred per-entry external files."""
    if not entry_id:
        return None

    external_sources: list[str] = []
    candidate_paths = [
        infer_current_path(epistemic_path, entry_id),
        infer_history_path(epistemic_path, entry_id),
    ]
    seen_paths: set[str] = set()
    for path in candidate_paths:
        key = str(path)
        if key in seen_paths:
            continue
        seen_paths.add(key)

        if not path.exists():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        scoped = extract_external_history_for_entry(text, entry_id)
        if scoped:
            external_sources.append(scoped)

    if not external_sources:
        return None

    candidates: list[datetime] = []
    for source in external_sources:
        latest_date = _extract_latest_date(source)
        if latest_date is not None:
            candidates.append(latest_date)

        latest_evidence_date = _extract_latest_evidence_commit_date(
            entry_history=source,
            project_root=project_root,
        )
        if latest_evidence_date is not None:
            candidates.append(latest_evidence_date)

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


def _resolve_git_line_commit_date(
    *,
    project_root: Path,
    file_path: Path,
    line_number_1based: int,
) -> datetime | None:
    """Resolve the commit date for a specific file line via ``git blame``."""
    if line_number_1based < 1:
        return None

    try:
        root_key = str(project_root.resolve())
    except OSError:
        root_key = str(project_root)

    try:
        relative_path = str(file_path.resolve().relative_to(project_root.resolve()))
    except OSError:
        relative_path = str(file_path)
    except ValueError:
        relative_path = str(file_path)

    try:
        file_stat = file_path.stat()
        file_mtime_ns = int(file_stat.st_mtime_ns)
        file_size = int(file_stat.st_size)
    except OSError:
        file_mtime_ns = -1
        file_size = -1

    head_commit = _resolve_head_commit(project_root) or "__no_head__"
    cache_key = (
        root_key,
        relative_path,
        line_number_1based,
        file_mtime_ns,
        file_size,
        head_commit,
    )
    if cache_key in _HEADING_LINE_COMMIT_DATE_CACHE:
        return _HEADING_LINE_COMMIT_DATE_CACHE[cache_key]

    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "blame",
                "--line-porcelain",
                f"-L{line_number_1based},{line_number_1based}",
                "--",
                relative_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _HEADING_LINE_COMMIT_DATE_CACHE[cache_key] = None
        return None

    if proc.returncode != 0:
        _HEADING_LINE_COMMIT_DATE_CACHE[cache_key] = None
        return None

    commit_time: datetime | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("committer-time "):
            _, _, raw_ts = line.partition(" ")
            try:
                commit_time = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc)
            except ValueError:
                commit_time = None
            break

    _HEADING_LINE_COMMIT_DATE_CACHE[cache_key] = commit_time
    return commit_time


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


def _chunk_has_explicit_workflow_signal(
    *,
    items: list[dict[str, Any]],
    project_root: Path,
) -> bool:
    """Return True when chunk items explicitly request new workflow handling."""
    for item in items:
        issue_title = str(item.get("issue_title") or "").lower()
        item_path = str(item.get("path") or "").lower()
        if "workflow" in issue_title:
            return True
        if "workflow" in item_path:
            return True
        queue_text = _read_queue_entry_text(project_root, item)
        if any(term in queue_text for term in _WORKFLOW_EXPLICIT_SIGNAL_TERMS):
            return True
    return False


def _read_manifest_entries(manifest_file: Path) -> list[dict[str, Any]]:
    """Load manifest entries from YAML. Returns empty list on parse/read errors."""
    if not manifest_file.exists():
        return []

    import yaml

    try:
        with open(manifest_file) as fh:
            manifest = yaml.safe_load(fh) or []
    except (OSError, yaml.YAMLError):
        return []

    if not isinstance(manifest, list):
        return []
    return [entry for entry in manifest if isinstance(entry, dict)]


def _recent_preassigned_workflow_ids(
    *,
    manifest_file: Path,
    current_chunk_id: int,
    cooldown_chunks: int,
) -> set[str]:
    """Return W-IDs pre-assigned in recent fold chunks.

    Only includes IDs from entries in ``[current_chunk_id - cooldown_chunks, current_chunk_id)``.
    """
    if cooldown_chunks <= 0:
        return set()

    start_id = max(1, current_chunk_id - cooldown_chunks)
    recent_ids: set[str] = set()
    for entry in _read_manifest_entries(manifest_file):
        entry_id = entry.get("id")
        if not isinstance(entry_id, int):
            continue
        if entry_id < start_id or entry_id >= current_chunk_id:
            continue
        workflow_ids = entry.get("pre_assigned_workflow_ids")
        if not isinstance(workflow_ids, list):
            continue
        for workflow_id in workflow_ids:
            if not isinstance(workflow_id, str):
                continue
            normalized = workflow_id.strip().upper()
            if re.fullmatch(r"W\d{3,}", normalized):
                recent_ids.add(normalized)
    return recent_ids


def _find_claims_by_status(
    epistemic_path: Path,
    status: str,
    days_threshold: int,
    *,
    project_root: Path | None = None,
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
            project_root=project_root,
        )
        heading_commit_date = (
            _resolve_git_line_commit_date(
                project_root=project_root,
                file_path=epistemic_path,
                line_number_1based=sec["start"] + 1,
            )
            if project_root is not None
            else None
        )
        if heading_commit_date is not None and (
            latest is None or heading_commit_date > latest
        ):
            latest = heading_commit_date
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


def _read_last_workflow_synthesis_attempt(manifest_file: Path) -> dict[str, Any] | None:
    """Return the most recent workflow_synthesis manifest entry, if any.

    This is used for a *cooldown* mechanism to avoid infinite drift loops when
    synthesis is repeatedly attempted but workflows remain unchanged.
    """
    entries = _read_manifest_entries(manifest_file)
    for entry in reversed(entries):
        if entry.get("type") == "workflow_synthesis":
            return entry
    return None


def _sha256_file_text(path: Path) -> str | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _workflow_ids_signature(workflow_entries: list[dict[str, Any]]) -> str | None:
    """Return stable signature for CURRENT workflow ID membership."""
    ids = sorted(
        str(entry_id).upper()
        for entry in workflow_entries
        if (entry_id := entry.get("id"))
    )
    if not ids:
        return None
    return ",".join(ids)


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
            project_root=project_root,
        ),
        stale_unverified=_find_claims_by_status(
            paths["epistemic"], "unverified",
            thresholds.get("stale_unverified_days", 30),
            project_root=project_root,
        ),
        workflow_repetitions=_find_workflow_repetitions(paths["workflows"]),
        ref_commit=ref_commit,
    )


# ------------------------------------------------------------------
# Budget computation
# ------------------------------------------------------------------


def _living_docs_char_counts(
    doc_paths: dict[str, Path],
    *,
    mode: str = "full",
) -> tuple[int, int]:
    """Return (full_chars, budget_basis_chars) for living docs."""
    living_doc_keys = ["timeline", "concepts", "epistemic", "workflows"]
    full_chars = 0
    budget_basis_chars = 0
    selected_mode = mode.strip().lower()

    for key in living_doc_keys:
        path = doc_paths.get(key)
        if not path or not path.exists():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue

        file_chars = len(text)
        full_chars += file_chars

        if selected_mode in {"index", "index_headings"}:
            lines = text.splitlines(keepends=True)
            basis = 0
            for idx, line in enumerate(lines):
                if idx < 12:
                    basis += len(line)
                    continue
                if line.startswith("#") or line.startswith("Updated by"):
                    basis += len(line)
            budget_basis_chars += basis
        else:
            budget_basis_chars += file_chars

    return full_chars, budget_basis_chars


def _predict_touched_ids(
    *,
    items: list[dict[str, Any]],
    project_root: Path,
    max_items: int = 24,
    max_ids_per_type: int = 12,
) -> dict[str, list[str]]:
    """Cheap planning pass: predict IDs likely touched by upcoming queue items."""
    predicted: dict[str, set[str]] = {"C": set(), "E": set(), "W": set()}
    preview_items = items[:max(1, max_items)]

    for item in preview_items:
        text = _read_queue_entry_text(project_root, item)
        if not text:
            continue
        for match in _STABLE_ID_RE.finditer(text):
            prefix = match.group(1).upper()
            value = int(match.group(2))
            stable_id = f"{prefix}{value:03d}"
            bucket = predicted.get(prefix)
            if bucket is not None:
                bucket.add(stable_id)

    result: dict[str, list[str]] = {}
    for prefix in ("C", "E", "W"):
        ids = sorted(predicted[prefix], key=lambda stable: int(stable[1:]))
        if max_ids_per_type > 0:
            ids = ids[:max_ids_per_type]
        if ids:
            result[prefix] = ids
    return result


def _collect_context_pack(
    *,
    doc_paths: dict[str, Path],
    predicted_ids: dict[str, list[str]],
    max_ids_per_type: int = 8,
    max_chars: int = 120_000,
) -> tuple[list[Path], int, dict[str, list[str]]]:
    """Collect existing per-ID current files for adaptive budgeting."""
    roots = {
        "C": doc_paths["concepts"].with_suffix("") / "current",
        "E": doc_paths["epistemic"].with_suffix("") / "current",
        "W": doc_paths["workflows"].with_suffix("") / "current",
    }
    files: list[Path] = []
    chars = 0
    included_ids: dict[str, list[str]] = {}

    for prefix in ("C", "E", "W"):
        ids = predicted_ids.get(prefix, [])
        if max_ids_per_type > 0:
            ids = ids[:max_ids_per_type]
        for stable_id in ids:
            candidate = roots[prefix] / f"{stable_id}.md"
            if not candidate.exists():
                continue
            try:
                text = candidate.read_text()
            except OSError:
                continue

            text_chars = len(text)
            if max_chars > 0 and chars + text_chars > max_chars:
                return files, chars, included_ids

            files.append(candidate)
            chars += text_chars
            included_ids.setdefault(prefix, []).append(stable_id)

    return files, chars, included_ids


def compute_budget(
    config: dict,
    doc_paths: dict[str, Path],
    *,
    context_pack_chars: int = 0,
) -> tuple[int, int]:
    """Compute available char budget for chunk content.

    Returns (budget, living_docs_chars).
    """
    budget_cfg = config.get("budget", {})
    context_limit = budget_cfg.get("context_limit_chars", 600_000)
    overhead = budget_cfg.get("instructions_overhead", 100_000)
    max_chunk = budget_cfg.get("max_chunk_chars", 80_000)
    living_docs_budget_mode = str(
        budget_cfg.get("living_docs_budget_mode", "full"),
    ).lower()

    living_docs_chars, budget_basis_chars = _living_docs_char_counts(
        doc_paths,
        mode=living_docs_budget_mode,
    )
    remaining = context_limit - budget_basis_chars - overhead - max(0, context_pack_chars)
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

    if item["type"] == "prompts":
        content = _compact_prompt_markdown(content)

    return header + content + "\n\n---\n\n"


def _compact_prompt_markdown(content: str) -> str:
    """Reduce prompt-session noise for chunk inputs.

    Drops ``[sm ...]`` telemetry lines, trims long relay lines, and removes
    consecutive duplicate prompt texts in legacy session markdown files.
    """
    lines: list[str] = []
    last_prompt_text: str | None = None

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        match = _SESSION_PROMPT_LINE_RE.match(line.strip())
        if not match:
            if line.strip():
                lines.append(line)
            continue

        ts = match.group(1)
        text = match.group(2).strip()
        if _SESSION_SM_PROMPT_RE.match(text):
            continue
        if _SESSION_RELAY_PROMPT_RE.match(text) and len(text) > _SESSION_RELAY_MAX_CHARS:
            clipped = text[: _SESSION_RELAY_MAX_CHARS - 3].rsplit(" ", 1)[0]
            text = clipped + "..."
        if text == last_prompt_text:
            continue

        lines.append(f"**[{ts}]** {text}")
        lines.append("")
        last_prompt_text = text

    return "\n".join(lines).strip() + "\n"


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
    budget_cfg = config.get("budget", {})
    planning_enabled = bool(budget_cfg.get("adaptive_context_budgeting", True))
    planning_preview_items = int(budget_cfg.get("planning_preview_items", 24) or 24)
    planning_max_ids_per_type = int(
        budget_cfg.get("adaptive_context_max_ids_per_type", 8) or 8,
    )
    planning_max_context_chars = int(
        budget_cfg.get("adaptive_context_max_chars", 120_000) or 120_000,
    )
    living_docs_budget_mode = str(
        budget_cfg.get("living_docs_budget_mode", "full"),
    ).lower()

    planning_predicted_ids: dict[str, list[str]] = {}
    planning_context_ids: dict[str, list[str]] = {}
    planning_context_chars = 0

    if planning_enabled:
        planning_predicted_ids = _predict_touched_ids(
            items=queue,
            project_root=project_root,
            max_items=max(1, planning_preview_items),
            max_ids_per_type=max(1, planning_max_ids_per_type),
        )
        _context_files, planning_context_chars, planning_context_ids = _collect_context_pack(
            doc_paths=doc_paths,
            predicted_ids=planning_predicted_ids,
            max_ids_per_type=max(1, planning_max_ids_per_type),
            max_chars=max(0, planning_max_context_chars),
        )

    budget, living_docs_chars = compute_budget(
        config,
        doc_paths,
        context_pack_chars=planning_context_chars,
    )
    _living_chars_full, living_docs_budget_chars = _living_docs_char_counts(
        doc_paths,
        mode=living_docs_budget_mode,
    )

    # Check drift priorities
    drift = scan_drift(config, project_root, fold_from=fold_from)

    thresholds = config.get("thresholds", {})

    # Workflow synthesis cooldown:
    # - Avoid infinite workflow_synthesis drift loops when an agent doesn't merge workflows.
    # - Do NOT assume synthesis "completed" just because a chunk was generated.
    cooldown_chunks = int(thresholds.get("workflow_synthesis_cooldown_chunks", 5))
    if drift.workflow_repetitions:
        workflows_path = doc_paths.get("workflows")
        if workflows_path and workflows_path.exists():
            current_hash = _sha256_file_text(workflows_path)
        else:
            current_hash = None
        current_ids_signature = _workflow_ids_signature(drift.workflow_repetitions)

        last_attempt = _read_last_workflow_synthesis_attempt(manifest_file)
        same_hash = (
            bool(current_hash)
            and bool(last_attempt)
            and last_attempt.get("workflow_registry_hash") == current_hash
        )
        same_ids = (
            bool(current_ids_signature)
            and bool(last_attempt)
            and last_attempt.get("workflow_ids_signature") == current_ids_signature
        )
        if (
            (same_hash or same_ids)
            and isinstance(last_attempt.get("id"), int)
        ):
            existing = list(chunks_dir.glob("chunk_*_input.md"))
            chunk_id_preview = len(existing) + 1
            if chunk_id_preview - last_attempt["id"] <= cooldown_chunks:
                drift.workflow_repetitions = []

    # New-workflow cooldown:
    # - Avoid back-to-back "create workflow in fold chunk" followed immediately by
    #   workflow_synthesis churn that merges it into W001.
    # - Suppress synthesis briefly when CURRENT workflow set only expanded by a
    #   newly pre-assigned workflow ID in recent chunks.
    new_workflow_cooldown_chunks = int(
        thresholds.get("workflow_new_id_synthesis_cooldown_chunks", 3),
    )
    if drift.workflow_repetitions and new_workflow_cooldown_chunks > 0:
        current_chunk_id = chunk_id
        recent_preassigned_workflow_ids = _recent_preassigned_workflow_ids(
            manifest_file=manifest_file,
            current_chunk_id=current_chunk_id,
            cooldown_chunks=new_workflow_cooldown_chunks,
        )
        current_workflow_ids = {
            str(entry.get("id", "")).upper()
            for entry in drift.workflow_repetitions
            if isinstance(entry.get("id"), str)
        }
        repetition_threshold = int(thresholds.get("workflow_repetition", 3))
        if (
            recent_preassigned_workflow_ids
            and (recent_preassigned_workflow_ids & current_workflow_ids)
            and len(current_workflow_ids) <= repetition_threshold + 1
        ):
            drift.workflow_repetitions = []

    drift_type = drift.triggered(thresholds)

    # Reuse ref_commit resolved by scan_drift (avoids duplicate git call)
    ref_commit = drift.ref_commit

    if drift_type:
        context_commit = _resolve_chunk_context_commit(
            project_root,
            date_hint=fold_from,
            fallback_commit=ref_commit,
        )
        context_worktree_path = (
            _create_chunk_context_worktree(
                project_root,
                chunk_id=chunk_id,
                commit=context_commit,
            )
            if context_commit
            else None
        )

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
            context_worktree_path=context_worktree_path,
            context_commit=context_commit,
        )

        input_path = chunks_dir / f"chunk_{chunk_id:03d}_input.md"
        input_path.write_text(input_content)

        prompt_content = render_agent_prompt(
            chunk_id=chunk_id,
            date_range=drift_type,
            chunk_type=drift_type,
            input_path=input_path,
            doc_paths=doc_paths,
            pre_assigned_ids=None,
            project_root=project_root,
            context_worktree_path=context_worktree_path,
            context_commit=context_commit,
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
                workflows_path = doc_paths.get("workflows")
                if workflows_path:
                    wf_hash = _sha256_file_text(workflows_path)
                    if wf_hash:
                        fh.write(f"  workflow_registry_hash: {wf_hash}\n")
                wf_ids_signature = _workflow_ids_signature(drift.workflow_repetitions)
                if wf_ids_signature:
                    fh.write(f"  workflow_ids_signature: \"{wf_ids_signature}\"\n")

        return ChunkResult(
            chunk_id=chunk_id,
            input_path=input_path,
            prompt_path=prompt_path,
            chunk_type=drift_type,
            items_count=0,
            chunk_chars=len(input_content),
            budget=budget,
            living_docs_chars=living_docs_chars,
            living_docs_budget_chars=living_docs_budget_chars,
            planning_context_chars=planning_context_chars,
            planning_predicted_ids=planning_predicted_ids,
            planning_context_ids=planning_context_ids,
            remaining_queue=len(queue),
            drift_entry_count=drift_counts.get(drift_type, 0),
            context_worktree_path=context_worktree_path,
            context_commit=context_commit,
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
    min_preassign_concepts = int(thresholds.get("min_preassign_concepts", 0) or 0)
    min_preassign_epistemic = int(thresholds.get("min_preassign_epistemic", 0) or 0)
    min_preassign_workflows = int(thresholds.get("min_preassign_workflows", 0) or 0)
    workflow_variant_only_mode = False
    has_explicit_workflow_signal = _chunk_has_explicit_workflow_signal(
        items=chunk_items,
        project_root=project_root,
    )
    current_workflow_entries = _find_workflow_repetitions(doc_paths["workflows"])
    current_workflow_ids = {
        str(entry.get("id", "")).upper()
        for entry in current_workflow_entries
        if isinstance(entry.get("id"), str)
    }
    if (
        estimates["W"] <= 0
        and min_preassign_workflows > 0
        and not has_explicit_workflow_signal
        and "W001" in current_workflow_ids
        and len(current_workflow_ids) >= int(thresholds.get("workflow_repetition", 3))
    ):
        min_preassign_workflows = 0
        workflow_variant_only_mode = True

    min_next_ids = _compute_min_next_ids_from_living_docs(doc_paths)
    db_path = engram_dir / "engram.db"
    with IDAllocator(db_path) as allocator:
        pre_assigned = allocator.pre_assign_for_chunk(
            new_concepts=max(estimates["C"], min_preassign_concepts),
            new_epistemic=max(estimates["E"], min_preassign_epistemic),
            new_workflows=max(estimates["W"], min_preassign_workflows),
            min_next_ids=min_next_ids,
        )

    date_range = f"{chunk_items[0]['date'][:10]} to {chunk_items[-1]['date'][:10]}"
    chunk_end_date = chunk_items[-1]["date"][:10]
    context_commit = _resolve_chunk_context_commit(
        project_root,
        date_hint=chunk_end_date,
    )
    context_worktree_path = (
        _create_chunk_context_worktree(
            project_root,
            chunk_id=chunk_id,
            commit=context_commit,
        )
        if context_commit
        else None
    )

    # Render item contents
    items_content = ""
    for item in chunk_items:
        items_content += _render_item_content(item, project_root)

    input_content = render_chunk_input(
        chunk_id=chunk_id,
        date_range=date_range,
        items_content=items_content,
        pre_assigned_ids=pre_assigned,
        workflow_variant_only_mode=workflow_variant_only_mode,
        doc_paths=doc_paths,
        context_worktree_path=context_worktree_path,
        context_commit=context_commit,
    )

    input_path = chunks_dir / f"chunk_{chunk_id:03d}_input.md"
    input_path.write_text(input_content)

    prompt_content = render_agent_prompt(
        chunk_id=chunk_id,
        date_range=date_range,
        chunk_type="fold",
        input_path=input_path,
        doc_paths=doc_paths,
        pre_assigned_ids=pre_assigned,
        project_root=project_root,
        workflow_variant_only_mode=workflow_variant_only_mode,
        context_worktree_path=context_worktree_path,
        context_commit=context_commit,
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
        preassigned_workflow_ids = pre_assigned.get("W", [])
        if preassigned_workflow_ids:
            fh.write("  pre_assigned_workflow_ids:\n")
            for workflow_id in preassigned_workflow_ids:
                fh.write(f"    - {workflow_id}\n")

    return ChunkResult(
        chunk_id=chunk_id,
        input_path=input_path,
        prompt_path=prompt_path,
        chunk_type="fold",
        items_count=len(chunk_items),
        chunk_chars=chunk_chars,
        budget=budget,
        living_docs_chars=living_docs_chars,
        living_docs_budget_chars=living_docs_budget_chars,
        planning_context_chars=planning_context_chars,
        planning_predicted_ids=planning_predicted_ids,
        planning_context_ids=planning_context_ids,
        remaining_queue=len(queue),
        date_range=date_range,
        pre_assigned_ids=pre_assigned,
        context_worktree_path=context_worktree_path,
        context_commit=context_commit,
    )


def _compute_min_next_ids_from_living_docs(doc_paths: dict[str, Path]) -> dict[str, int]:
    """Compute minimum next-id counters based on stable IDs already in living docs.

    Engram's allocator DB (``.engram/engram.db``) may be newly created or out of
    sync with the project docs. If the allocator is behind, it can pre-assign
    IDs that already exist (e.g., pre-assigning C107 when C107 is already in
    the concept registry). This function derives ``{category: max_seen + 1}``
    so the allocator can bump counters forward before reserving ranges.
    """
    registry_docs = {
        "concepts": "C",
        "epistemic": "E",
        "workflows": "W",
    }

    min_next: dict[str, int] = {}
    for key, prefix in registry_docs.items():
        path = doc_paths.get(key)
        if not path or not path.exists():
            continue

        try:
            content = path.read_text()
        except OSError:
            continue

        max_seen = 0
        for section in parse_sections(content):
            entry_id = extract_id(section["heading"])
            if not entry_id or not entry_id.startswith(prefix):
                continue
            try:
                num = int(entry_id[1:])
            except ValueError:
                continue
            if num > max_seen:
                max_seen = num

        if max_seen > 0:
            min_next[prefix] = max_seen + 1

    return min_next

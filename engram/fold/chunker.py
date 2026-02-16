"""Chunk assembly with drift-priority scheduling.

Reworked from v2's next_chunk() for v3: 4 living docs, stable IDs,
FULL/STUB forms, and all 4 drift threshold types.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from engram.config import resolve_doc_paths
from engram.fold.ids import IDAllocator, estimate_new_entities
from engram.fold.prompt import render_agent_prompt, render_chunk_input, render_triage_input
from engram.parse import extract_id, is_stub, parse_sections

# Regex for ISO dates (YYYY-MM-DD) in text
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass
class DriftReport:
    """Results from scanning living docs for drift conditions."""

    orphaned_concepts: list[dict] = field(default_factory=list)
    contested_claims: list[dict] = field(default_factory=list)
    stale_unverified: list[dict] = field(default_factory=list)
    workflow_repetitions: list[dict] = field(default_factory=list)

    def triggered(self, thresholds: dict) -> str | None:
        """Return the highest-priority drift type that exceeds its threshold.

        Priority order: orphans > contested > stale_unverified > workflow.
        Returns None if no threshold is exceeded.
        """
        if len(self.orphaned_concepts) > thresholds.get("orphan_triage", 50):
            return "orphan_triage"
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
    chunk_type: str  # "fold" | "orphan_triage" | "contested_review" | ...
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


def _extract_latest_date(text: str) -> datetime | None:
    """Extract the most recent YYYY-MM-DD date from text."""
    matches = _DATE_RE.findall(text)
    if not matches:
        return None
    parsed = []
    for d in matches:
        try:
            parsed.append(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    return max(parsed) if parsed else None


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
        latest = _extract_latest_date(sec["text"])
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
        contested_claims=_find_claims_by_status(
            paths["epistemic"], "contested",
            thresholds.get("contested_review_days", 14),
        ),
        stale_unverified=_find_claims_by_status(
            paths["epistemic"], "unverified",
            thresholds.get("stale_unverified_days", 30),
        ),
        workflow_repetitions=_find_workflow_repetitions(paths["workflows"]),
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
    overhead = budget_cfg.get("instructions_overhead", 10_000)
    max_chunk = budget_cfg.get("max_chunk_chars", 200_000)

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
    thresholds = config.get("thresholds", {})
    drift_type = drift.triggered(thresholds)

    # Resolve ref_commit for temporal context in prompts
    ref_commit: str | None = None
    if fold_from:
        ref_commit = _resolve_ref_commit(project_root, fold_from)

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
        )

        input_path = chunks_dir / f"chunk_{chunk_id:03d}_input.md"
        input_path.write_text(input_content)

        prompt_content = render_agent_prompt(
            chunk_id=chunk_id,
            date_range=drift_type,
            input_path=input_path,
            doc_paths=doc_paths,
        )
        prompt_path = chunks_dir / f"chunk_{chunk_id:03d}_prompt.txt"
        prompt_path.write_text(prompt_content)

        # Count drift entries for reporting
        drift_counts = {
            "orphan_triage": len(drift.orphaned_concepts),
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

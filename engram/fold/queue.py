"""Chronological queue building from project artifacts.

Ported from v2 build_queue() with all paths parameterized by config.
Outputs JSONL with entries for docs, issues, and session prompts.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from engram.fold.sessions import get_adapter
from engram.fold.sources import (
    extract_issue_number,
    get_doc_git_dates,
    infer_github_repo,
    list_tracked_markdown_docs,
    parse_date,
    parse_frontmatter_date,
    pull_issues,
    render_issue_markdown,
)

# Dual-pass threshold: if modified > created + this many days, create revisit entry
REVISIT_THRESHOLD_DAYS = 7


def refresh_issue_snapshots(config: dict[str, Any], project_root: Path) -> tuple[bool, str]:
    """Refresh ``sources.issues`` JSON snapshots from GitHub.

    Returns:
        (ok, message) where ``ok`` indicates refresh success.
    """
    sources = config.get("sources", {})
    if not sources.get("refresh_issues", True):
        return True, "disabled by config (sources.refresh_issues: false)"

    issues_dir = project_root / sources.get("issues", "local_data/issues/")
    repo = sources.get("github_repo") or infer_github_repo(project_root)
    if not repo:
        return True, (
            "unable to resolve GitHub repo (set sources.github_repo or configure "
            "git remote.origin.url); using local issue snapshots"
        )

    try:
        issues = pull_issues(repo, issues_dir)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        return False, f"gh issue list failed for {repo}{detail}"

    return True, f"refreshed {len(issues)} issues from {repo}"


def build_queue(
    config: dict[str, Any],
    project_root: Path,
    output_dir: Path | None = None,
    start_date: str | None = None,
) -> list[dict[str, Any]]:
    """Build the chronological queue of all artifacts.

    Args:
        config: Loaded engram config dict.
        project_root: Absolute path to the project root.
        output_dir: Directory for queue.jsonl and item_sizes.json.
            Defaults to project_root / ".engram".
        start_date: Optional YYYY-MM-DD string. When set, only entries
            with ``date[:10] >= start_date`` are included in the queue
            and output files. Validated via ``date.fromisoformat()``.

    Returns:
        List of queue entry dicts, sorted by date.
    """
    if output_dir is None:
        output_dir = project_root / ".engram"
    output_dir.mkdir(parents=True, exist_ok=True)

    sources = config.get("sources", {})
    issues_dir = project_root / sources.get("issues", "local_data/issues/")
    doc_dirs = [project_root / d for d in sources.get("docs", [])]
    session_cfg = sources.get("sessions", {})

    # Optional project_start for frontmatter date filtering
    project_start = config.get("project_start")

    # Load issue dates for cross-referencing doc dates
    issue_dates: dict[int, str] = {}
    if issues_dir.exists():
        for f in sorted(issues_dir.glob("*.json")):
            try:
                issue = json.loads(f.read_text())
                issue_dates[issue["number"]] = issue["createdAt"]
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Skipping issue date from %s: %s", f.name, exc)

    entries: list[dict[str, Any]] = []
    sizes: dict[str, int] = {}

    # --- Process docs ---
    tracked_doc_paths = list_tracked_markdown_docs(project_root, doc_dirs)
    if tracked_doc_paths:
        doc_paths = tracked_doc_paths
    else:
        doc_paths = []
        for doc_dir in doc_dirs:
            if not doc_dir.exists():
                continue
            doc_paths.extend(sorted(doc_dir.glob("*.md")))

    for doc_path in doc_paths:
        char_count = len(doc_path.read_text(errors="ignore"))
        rel_path = str(doc_path.relative_to(project_root))
        sizes[rel_path] = char_count

        # Resolve created date (priority: frontmatter > issue > git > mtime)
        created = parse_frontmatter_date(doc_path, project_start)

        if not created:
            issue_num = extract_issue_number(doc_path)
            if issue_num and issue_num in issue_dates:
                created = issue_dates[issue_num]

        if not created:
            git_created, _ = get_doc_git_dates(doc_path, project_root)
            created = git_created

        if not created:
            mtime = os.path.getmtime(doc_path)
            created = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        # Resolve modified date
        _, git_modified = get_doc_git_dates(doc_path, project_root)
        modified = git_modified or created

        created_dt = parse_date(created)
        modified_dt = parse_date(modified)

        # Always add initial entry
        entries.append({
            "date": created,
            "type": "doc",
            "path": rel_path,
            "chars": char_count,
            "pass": "initial",
        })

        # Add revisit entry if substantially modified later
        delta = (modified_dt - created_dt).days
        if delta >= REVISIT_THRESHOLD_DAYS:
            entries.append({
                "date": modified,
                "type": "doc",
                "path": rel_path,
                "chars": char_count,
                "pass": "revisit",
                "first_seen_date": created,
            })

    # --- Process issues ---
    if issues_dir.exists():
        for f in sorted(issues_dir.glob("*.json")):
            try:
                issue = json.loads(f.read_text())
                rendered = render_issue_markdown(issue)
                char_count = len(rendered)
                rel_path = str(f.relative_to(project_root))
                sizes[rel_path] = char_count

                entries.append({
                    "date": issue["createdAt"],
                    "type": "issue",
                    "path": rel_path,
                    "chars": char_count,
                    "pass": "initial",
                    "issue_number": issue["number"],
                    "issue_title": issue.get("title", ""),
                })
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Skipping issue %s: %s", f.name, exc)

    # --- Process session prompts ---
    fmt = session_cfg.get("format", "claude-code")
    session_path = Path(session_cfg.get("path", "~/.claude/history.jsonl")).expanduser()
    project_match = session_cfg.get("project_match", [])

    adapter = get_adapter(fmt)
    session_entries = adapter.parse(session_path, project_match)

    sessions_dir = output_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Collect sessions without writing files yet
    pending_sessions: list[tuple[dict[str, Any], str, str]] = []
    for se in session_entries:
        rel_path = str((sessions_dir / f"{se.session_id}.md").relative_to(project_root))
        sizes[rel_path] = se.chars

        entry: dict[str, Any] = {
            "date": se.date,
            "type": "prompts",
            "path": rel_path,
            "chars": se.chars,
            "pass": "initial",
            "session_id": se.session_id,
            "prompt_count": se.prompt_count,
        }
        pending_sessions.append((entry, se.session_id, se.rendered))
        entries.append(entry)

    # Sort by date
    entries.sort(key=lambda e: e["date"])

    # Filter by start_date if provided
    if start_date:
        date.fromisoformat(start_date)  # Validates YYYY-MM-DD; raises ValueError
        entries = [e for e in entries if e["date"][:10] >= start_date]

    # Write only surviving session files
    surviving_paths = {e["path"] for e in entries if e["type"] == "prompts"}
    for entry, session_id, rendered in pending_sessions:
        if entry["path"] in surviving_paths:
            (sessions_dir / f"{session_id}.md").write_text(rendered)

    # Write queue JSONL
    queue_file = output_dir / "queue.jsonl"
    with open(queue_file, "w") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")

    # Write sizes
    sizes_file = output_dir / "item_sizes.json"
    with open(sizes_file, "w") as fh:
        json.dump(sizes, fh, indent=2)

    return entries

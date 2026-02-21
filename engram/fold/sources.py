"""Source ingestion: issue pulling, git dates, frontmatter parsing.

Ported from v2 (fractal-market-simulator/scripts/knowledge_fold.py)
with all paths parameterized by config.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path


def pull_issues(repo: str, issues_dir: Path) -> list[dict]:
    """Pull all GitHub issues with comments into local JSON files.

    Args:
        repo: GitHub repo in "owner/repo" format.
        issues_dir: Directory to write issue JSON files.

    Returns:
        List of issue dicts.
    """
    issues_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--repo", repo,
            "--state", "all",
            "--json", "number,title,body,createdAt,updatedAt,state,labels,comments",
            "--limit", "5000",
        ],
        capture_output=True, text=True, check=True,
    )

    issues = json.loads(result.stdout)

    for issue in issues:
        num = issue["number"]
        path = issues_dir / f"{num}.json"
        path.write_text(json.dumps(issue, indent=2))

    return issues


def render_issue_markdown(issue: dict) -> str:
    """Render a GitHub issue JSON object as clean markdown."""
    parts = []

    # State and labels
    state = issue.get("state", "UNKNOWN")
    labels = ", ".join(label["name"] for label in issue.get("labels", []))
    meta = f"**State:** {state}"
    if labels:
        meta += f" | **Labels:** {labels}"
    parts.append(meta)
    parts.append("")

    # Body
    body = issue.get("body", "") or ""
    parts.append(body)

    # Comments
    comments = issue.get("comments", [])
    if comments:
        parts.append("")
        parts.append("### Comments")
        parts.append("")
        for comment in comments:
            author = comment.get("author", {}).get("login", "unknown")
            date = comment.get("createdAt", "")[:10]
            cbody = comment.get("body", "")
            parts.append(f"**{author}** ({date}):")
            parts.append("")
            parts.append(cbody)
            parts.append("")

    return "\n".join(parts)


def get_doc_git_dates(
    doc_path: Path, project_root: Path
) -> tuple[str | None, str | None]:
    """Get first-commit and last-commit dates for a doc from git.

    Tracks renames while staying path-specific via ``--follow``.

    Returns:
        (created_date, modified_date) as ISO strings, or None.
    """
    rel_path = doc_path.relative_to(project_root)

    # First commit on this logical path (with rename following)
    result = subprocess.run(
        [
            "git", "log", "--all", "--follow",
            "--diff-filter=A", "--reverse", "--format=%aI",
            "--", str(rel_path),
        ],
        capture_output=True, text=True, cwd=project_root,
    )
    dates = [
        line for line in result.stdout.strip().split("\n")
        if line and line[0].isdigit()
    ]
    created = dates[0] if dates else None

    # Last commit on current path
    result = subprocess.run(
        ["git", "log", "-1", "--format=%aI", "--", str(rel_path)],
        capture_output=True, text=True, cwd=project_root,
    )
    modified = result.stdout.strip() if result.stdout.strip() else None

    return created, modified


def parse_frontmatter_date(
    doc_path: Path, project_start: str | None = None
) -> str | None:
    """Extract a date from doc frontmatter like **Date:** 2026-02-08.

    Args:
        doc_path: Path to the document.
        project_start: ISO date string. Dates before this are treated as
            typos and discarded. None means no filtering.

    Returns:
        ISO datetime string with timezone offset, or None.
    """
    try:
        content = doc_path.read_text(errors="ignore")[:2000]
        match = re.search(r'\*\*Date:\*\*\s*(\d{4}-\d{2}-\d{2})', content)
        if match:
            date_str = match.group(1)
            if project_start and date_str < project_start:
                return None
            return date_str + "T00:00:00+00:00"
    except Exception:
        pass
    return None


def extract_issue_number(doc_path: Path) -> int | None:
    """Extract issue number from filename like 1343_backtest_analysis.md."""
    match = re.match(r'^(\d+)_', doc_path.name)
    return int(match.group(1)) if match else None


def parse_date(date_str: str) -> datetime:
    """Parse ISO date string to datetime."""
    date_str = date_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        return datetime.fromisoformat(date_str[:10])


def git_diff_summary(
    date_from: str,
    date_to: str,
    project_root: Path,
    source_dirs: list[str] | None = None,
) -> str:
    """Get a summary of file creates/deletes/renames in the date range.

    Args:
        date_from: ISO date string (start).
        date_to: ISO date string (end).
        project_root: Repository root.
        source_dirs: Git pathspec dirs to filter (e.g. ["src/", "docs/"]).
            Defaults to ["src/", "docs/", "tests/"].

    Returns:
        Markdown section showing codebase changes, or empty string.
    """
    if source_dirs is None:
        source_dirs = ["src/", "docs/", "tests/"]

    cmd = [
        "git", "log", "--name-status", "--diff-filter=ADRT",
        f"--since={date_from}", f"--until={date_to}T23:59:59",
        "--format=", "--",
    ] + source_dirs

    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=project_root,
    )

    if not result.stdout.strip():
        return ""

    added, deleted, renamed = [], [], []
    for line in result.stdout.strip().split("\n"):
        if not line or line[0] not in "ADRT":
            continue
        parts = line.split("\t")
        status = parts[0][0]
        if status == "A":
            added.append(parts[1])
        elif status == "D":
            deleted.append(parts[1])
        elif status == "R" and len(parts) >= 3:
            renamed.append(f"{parts[1]} → {parts[2]}")

    if not added and not deleted and not renamed:
        return ""

    lines = ["## [CODEBASE CHANGES] File operations in this period\n"]
    if added:
        lines.append(f"**Files created ({len(added)}):**")
        for f in sorted(set(added)):
            lines.append(f"- `{f}`")
        lines.append("")
    if deleted:
        lines.append(f"**Files deleted ({len(deleted)}):**")
        for f in sorted(set(deleted)):
            lines.append(f"- `{f}`")
        lines.append("")
    if renamed:
        lines.append(f"**Files renamed ({len(renamed)}):**")
        for f in sorted(set(renamed)):
            lines.append(f"- `{f}`")
        lines.append("")

    lines.append(
        "Use this to determine which concepts are still alive (files exist) "
        "vs. dead (files deleted). If a concept's source files were deleted, "
        "mark it DEAD.\n"
    )
    lines.append("---\n")
    return "\n".join(lines)

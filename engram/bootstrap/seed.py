"""Bootstrap seed: read repo at point-in-time snapshot, produce initial living docs.

Uses ``git worktree add`` for historical snapshots (ephemeral, never touches
the user's working tree). Dispatches a single fold agent to populate the 4
living docs + 2 graveyard files from the repo snapshot.

Two modes:
- **Path B** (``from_date=None``): seed from today's repo state.
- **Path A** (``from_date=YYYY-MM-DD``): checkout at that date, seed, then
  fold forward via :mod:`engram.bootstrap.fold`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from engram.cli import GRAVEYARD_HEADERS, LIVING_DOC_HEADERS
from engram.config import load_config, resolve_doc_paths
from engram.fold.ids import IDAllocator
from engram.fold.prompt import render_seed_prompt

log = logging.getLogger(__name__)

# How many IDs to pre-assign for the seed round
_SEED_ID_BUDGET = {"C": 30, "E": 20, "W": 10}


def _find_commit_at_date(project_root: Path, target_date: date) -> str:
    """Find the last commit on or before *target_date*.

    Returns the commit SHA, or raises ``ValueError`` if no commit found.
    """
    iso = target_date.isoformat()
    result = subprocess.run(
        ["git", "log", f"--until={iso}T23:59:59", "--format=%H", "-1"],
        capture_output=True,
        text=True,
        cwd=str(project_root),
    )
    sha = result.stdout.strip()
    if not sha:
        raise ValueError(f"No commit found on or before {iso}")
    return sha


def _create_worktree(project_root: Path, commit: str) -> Path:
    """Create an ephemeral git worktree at *commit*.

    Returns the worktree path. Caller must clean up via ``_remove_worktree``.
    """
    worktree_dir = Path(tempfile.mkdtemp(prefix="engram-seed-"))
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree_dir), commit],
        capture_output=True,
        text=True,
        cwd=str(project_root),
        check=True,
    )
    log.info("Created worktree at %s (commit %s)", worktree_dir, commit[:8])
    return worktree_dir


def _remove_worktree(project_root: Path, worktree_dir: Path) -> None:
    """Remove an ephemeral git worktree."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_dir)],
        capture_output=True,
        text=True,
        cwd=str(project_root),
    )
    # Belt-and-suspenders: remove dir if git worktree remove failed
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)
    log.info("Removed worktree %s", worktree_dir)


def _collect_repo_snapshot(source_root: Path, config: dict[str, Any]) -> str:
    """Collect a textual snapshot of the repo at *source_root*.

    Reads source files, docs, README, and directory structure to produce
    a single markdown document the seed agent can process.
    """
    parts: list[str] = []

    # Directory tree (depth-limited)
    result = subprocess.run(
        ["find", ".", "-maxdepth", "3", "-not", "-path", "./.git/*",
         "-not", "-path", "./node_modules/*", "-not", "-path", "./venv/*",
         "-not", "-path", "./__pycache__/*"],
        capture_output=True,
        text=True,
        cwd=str(source_root),
    )
    if result.stdout.strip():
        parts.append("## Repository Structure\n\n```\n" + result.stdout.strip() + "\n```\n")

    # README
    for name in ("README.md", "readme.md", "README.rst", "README"):
        readme = source_root / name
        if readme.exists():
            content = readme.read_text(errors="ignore")[:10_000]
            parts.append(f"## {name}\n\n{content}\n")
            break

    # Key config files
    for name in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod",
                 "CLAUDE.md", ".claude/CLAUDE.md"):
        cfg_file = source_root / name
        if cfg_file.exists():
            content = cfg_file.read_text(errors="ignore")[:5_000]
            parts.append(f"## {name}\n\n```\n{content}\n```\n")

    # Source docs from config
    sources = config.get("sources", {})
    doc_dirs = sources.get("docs", [])
    docs_collected = 0
    max_docs = 20

    for doc_dir_rel in doc_dirs:
        doc_dir = source_root / doc_dir_rel
        if not doc_dir.exists():
            continue
        for doc_path in sorted(doc_dir.glob("*.md")):
            if docs_collected >= max_docs:
                break
            content = doc_path.read_text(errors="ignore")[:8_000]
            rel = doc_path.relative_to(source_root)
            parts.append(f"## Doc: {rel}\n\n{content}\n")
            docs_collected += 1

    # Issues (if present at snapshot)
    issues_dir = source_root / sources.get("issues", "local_data/issues/")
    if issues_dir.exists():
        issue_files = sorted(issues_dir.glob("*.json"))[:30]
        if issue_files:
            from engram.fold.sources import render_issue_markdown
            import json

            issue_parts: list[str] = []
            for f in issue_files:
                try:
                    issue = json.loads(f.read_text())
                    rendered = render_issue_markdown(issue)[:3_000]
                    issue_parts.append(f"### Issue #{issue['number']}: {issue.get('title', '')}\n\n{rendered}\n")
                except (json.JSONDecodeError, KeyError):
                    continue
            if issue_parts:
                parts.append("## Issues\n\n" + "\n".join(issue_parts))

    return "\n\n---\n\n".join(parts)


def _ensure_living_docs(project_root: Path, config: dict[str, Any]) -> None:
    """Create living doc and graveyard files with schema headers if missing."""
    doc_paths = resolve_doc_paths(config, project_root)
    for key in ("timeline", "concepts", "epistemic", "workflows"):
        p = doc_paths[key]
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(LIVING_DOC_HEADERS[key])
    for key, gy_key in [("concepts", "concept_graveyard"), ("epistemic", "epistemic_graveyard")]:
        p = doc_paths[gy_key]
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(GRAVEYARD_HEADERS[key])


def _dispatch_seed_agent(
    project_root: Path,
    config: dict[str, Any],
    snapshot_content: str,
) -> bool:
    """Dispatch the seed agent to populate initial living docs.

    Writes a seed input file and invokes the fold agent with the seed prompt.
    Returns True on success.
    """
    engram_dir = project_root / ".engram"
    engram_dir.mkdir(parents=True, exist_ok=True)
    doc_paths = resolve_doc_paths(config, project_root)

    # Pre-assign IDs for seed
    db_path = engram_dir / "engram.db"
    with IDAllocator(db_path) as allocator:
        pre_assigned = allocator.pre_assign_for_chunk(
            new_concepts=_SEED_ID_BUDGET["C"],
            new_epistemic=_SEED_ID_BUDGET["E"],
            new_workflows=_SEED_ID_BUDGET["W"],
        )

    # Render seed prompt (system instructions)
    seed_instructions = render_seed_prompt(
        doc_paths=doc_paths,
        pre_assigned_ids=pre_assigned,
    )

    # Write seed input file
    input_path = engram_dir / "seed_input.md"
    input_content = seed_instructions + "\n\n---\n\n# Repository Snapshot\n\n" + snapshot_content
    input_path.write_text(input_content)

    # Build agent prompt
    living_doc_keys = ["timeline", "concepts", "epistemic", "workflows"]
    doc_list = "\n".join(
        f"{i + 1}. {doc_paths[k]}" for i, k in enumerate(living_doc_keys)
    )
    graveyard_list = "\n".join([
        f"- {doc_paths['concept_graveyard']}",
        f"- {doc_paths['epistemic_graveyard']}",
    ])

    prompt = (
        f"You are bootstrapping a project's knowledge base.\n"
        f"\n"
        f"IMPORTANT CONSTRAINTS:\n"
        f"- Do NOT use the Task tool or spawn sub-agents. Do all work directly.\n"
        f"- Do NOT use Write to overwrite entire files. Use Edit for surgical updates only.\n"
        f"- Be SUCCINCT. High information density, no filler.\n"
        f"\n"
        f"Read the input file at {input_path.resolve()} — it contains seed instructions\n"
        f"and a snapshot of the repository.\n"
        f"\n"
        f"Follow the instructions. Populate these 4 living documents:\n"
        f"\n"
        f"{doc_list}\n"
        f"\n"
        f"Graveyard files (append-only):\n"
        f"\n"
        f"{graveyard_list}\n"
        f"\n"
        f"Read each living doc first, then make surgical edits to populate entries.\n"
        f"\n"
        f"Rules:\n"
        f"- Use ONLY pre-assigned IDs for new entries (listed in the input file)\n"
        f"- Extract concepts, claims, timeline events, workflows from the snapshot\n"
        f"- Be succinct: 5 lines per entry ideal, 10 max\n"
    )

    prompt_path = engram_dir / "seed_prompt.txt"
    prompt_path.write_text(prompt)

    # Invoke fold agent
    model = config.get("model", "sonnet")
    agent_cmd = config.get("agent_command")
    if agent_cmd:
        cmd = agent_cmd.split()
    else:
        cmd = ["claude", "--print", "--model", model]
    cmd.append(prompt)

    log.info("Dispatching seed agent...")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=600,
        )
        if result.returncode != 0:
            log.error("Seed agent failed (rc=%d): %s", result.returncode, result.stderr[:500])
            return False
        log.info("Seed agent completed successfully")
        return True
    except subprocess.TimeoutExpired:
        log.error("Seed agent timed out (10 min)")
        return False
    except FileNotFoundError:
        log.error("Agent command not found: %s", cmd[0])
        return False


def seed(
    project_root: Path,
    from_date: date | None = None,
    config: dict[str, Any] | None = None,
) -> bool:
    """Run bootstrap seed.

    Parameters
    ----------
    project_root:
        Root of the target project (must have ``.engram/config.yaml``).
    from_date:
        If provided (Path A), checkout repo at that date, seed, then
        fold forward to today. If None (Path B), seed from current state.
    config:
        Pre-loaded config. If None, loads from project_root.

    Returns
    -------
    bool
        True if seed completed successfully.
    """
    if config is None:
        config = load_config(project_root)

    _ensure_living_docs(project_root, config)

    worktree_dir: Path | None = None

    try:
        if from_date is not None:
            # Path A: historical seed
            commit = _find_commit_at_date(project_root, from_date)
            worktree_dir = _create_worktree(project_root, commit)
            source_root = worktree_dir
            log.info("Seeding from snapshot at %s (commit %s)", from_date, commit[:8])
        else:
            # Path B: seed from today
            source_root = project_root
            log.info("Seeding from current repo state")

        snapshot = _collect_repo_snapshot(source_root, config)
        success = _dispatch_seed_agent(project_root, config, snapshot)

        if not success:
            return False

        # Path A: fold forward after seeding
        if from_date is not None:
            from engram.bootstrap.fold import forward_fold

            log.info("Seed complete. Folding forward from %s to today...", from_date)
            fold_ok = forward_fold(project_root, from_date, config=config)
            if not fold_ok:
                log.warning("Forward fold had failures (seed itself succeeded)")
                return False

        return True

    finally:
        # Always clean up worktree
        if worktree_dir is not None:
            _remove_worktree(project_root, worktree_dir)

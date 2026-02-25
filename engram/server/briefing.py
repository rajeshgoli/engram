"""Standalone L0 briefing regeneration.

Extracted from :class:`Dispatcher` so that bootstrap paths (seed, fold)
can regenerate the briefing without instantiating a Dispatcher.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def regenerate_l0_briefing(
    config: dict[str, Any],
    project_root: Path,
    doc_paths: dict[str, Path],
) -> bool:
    """Regenerate the L0 briefing section in the project's CLAUDE.md.

    Uses a lightweight model call to compress living docs into a
    concise briefing (~50-100 lines).

    Returns True on success, False on failure.
    """
    briefing_cfg = config.get("briefing", {})
    target_file = project_root / briefing_cfg.get("file", "CLAUDE.md")
    section_header = briefing_cfg.get("section", "## Project Knowledge Briefing")

    if not target_file.exists():
        log.warning("Briefing target file not found: %s", target_file)
        return False

    # Read current living docs for briefing generation
    living_contents: list[str] = []
    for key in ("timeline", "concepts", "epistemic", "workflows"):
        p = doc_paths.get(key)
        if p and p.exists():
            content = p.read_text()
            # Truncate very large docs for briefing generation
            if len(content) > 10_000:
                content = content[:10_000] + "\n\n[... truncated for briefing ...]\n"
            living_contents.append(f"### {key.title()}\n{content}")

    if not living_contents:
        return False

    lookup_patterns = _build_lookup_patterns(doc_paths, project_root)

    # Generate briefing via lightweight model call
    briefing_text = _generate_briefing(
        config,
        project_root,
        "\n\n".join(living_contents),
        lookup_patterns,
    )
    if not briefing_text:
        log.warning("L0 briefing generation returned empty result")
        return False

    # Inject into target file
    _inject_section(target_file, section_header, briefing_text)
    log.info("L0 briefing regenerated in %s", target_file)
    return True


def _generate_briefing(
    config: dict[str, Any],
    project_root: Path,
    living_docs_content: str,
    lookup_patterns: dict[str, str],
) -> str | None:
    """Generate L0 briefing by shelling out to a fast model.

    Returns the briefing text, or None on failure.
    """
    prompt = (
        "Compress the following project knowledge into a concise briefing "
        "(50-100 lines). Focus on: what's alive vs dead, contested claims, "
        "key workflows, and agent guidance. Use stable IDs (C###/E###/W###).\n\n"
        "Output requirements:\n"
        "1) Keep the briefing self-contained: when an ID is first introduced, add a "
        "short inline gloss so the line is understandable without opening other files.\n"
        "2) Include a section titled 'Lookup Hooks (Use When Needed)' that tells agents "
        "exactly which per-ID files to open for deeper context.\n"
        "3) In Lookup Hooks, include these file patterns exactly:\n"
        f"- Concept details: {lookup_patterns['concepts']}\n"
        f"- Epistemic current state: {lookup_patterns['epistemic_current']}\n"
        f"- Epistemic history/provenance: {lookup_patterns['epistemic_history']}\n"
        f"- Workflow details: {lookup_patterns['workflows']}\n"
        "4) Keep the briefing concise but actionable; avoid ID-only shorthand with no hook.\n\n"
        f"{living_docs_content}"
    )

    try:
        result = subprocess.run(
            ["claude", "--print", "--model", "haiku", prompt],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("L0 briefing generation failed")

    return None


def _build_lookup_patterns(doc_paths: dict[str, Path], project_root: Path) -> dict[str, str]:
    """Build per-ID file lookup patterns for L0 briefing instructions."""
    concepts = _to_repo_relative(doc_paths["concepts"], project_root).with_suffix("")
    epistemic = _to_repo_relative(doc_paths["epistemic"], project_root).with_suffix("")
    workflows = _to_repo_relative(doc_paths["workflows"], project_root).with_suffix("")
    return {
        "concepts": f"{concepts}/current/C###.md",
        "epistemic_current": f"{epistemic}/current/E###.md",
        "epistemic_history": f"{epistemic}/history/E###.md",
        "workflows": f"{workflows}/current/W###.md",
    }


def _to_repo_relative(path: Path, project_root: Path) -> Path:
    """Return path relative to project root when possible."""
    resolved_path = path.resolve()
    resolved_root = project_root.resolve()
    try:
        return Path(os.path.relpath(resolved_path, resolved_root))
    except ValueError:
        # Fallback for cross-drive/path-layout edge cases: strip root so
        # generated hooks remain portable and non-absolute.
        return Path(*resolved_path.parts[1:]) if resolved_path.is_absolute() else resolved_path


def _inject_section(file_path: Path, section_header: str, content: str) -> None:
    """Inject or replace a section in a file.

    Finds ``section_header`` and replaces everything until the next
    same-level heading (or EOF) with ``content``.
    """
    text = file_path.read_text()
    header_level = section_header.count("#")

    start = text.find(section_header)
    if start == -1:
        # Append section at end
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n{section_header}\n\n{content}\n"
    else:
        # Find the end of this section (next same-level or higher heading)
        section_start = start + len(section_header)
        rest = text[section_start:]
        end_offset = len(rest)

        for i, line in enumerate(rest.split("\n")):
            if i == 0:
                continue
            stripped = line.lstrip()
            if stripped.startswith("#"):
                level = len(stripped) - len(stripped.lstrip("#"))
                if level <= header_level:
                    end_offset = sum(
                        len(l) + 1 for l in rest.split("\n")[:i]
                    )
                    break

        text = text[:start] + f"{section_header}\n\n{content}\n" + text[section_start + end_offset:]

    file_path.write_text(text)

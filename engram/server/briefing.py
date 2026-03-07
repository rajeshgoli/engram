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

DEFAULT_PROMPT = """\
Write an executive briefing for an engineer who just woke up with zero context \
about this project. Every token matters — every agent in the system reads this \
file on every session start.

Guiding question: What does an agent need to know to operate in this codebase \
without breaking things?

INCLUDE:
- What is this project? (2-3 sentences max)
- Current runtime architecture in plain English with key file paths
- What works, what's broken, what's the active research direction
- Hard rules (things an agent must never do)
- Key file paths table
- How to debug issues

DO NOT INCLUDE:
- Dead concepts — if something is deleted, don't mention it. Exception: if an \
agent might search for a file that used to exist, one line saying it doesn't exist.
- Engram concept IDs (C###), epistemic IDs (E###), or workflow IDs (W###) — \
these are internal bookkeeping, not operator-useful.
- Workflow registry dumps — state rules plainly.
- Lookup hooks to engram per-ID files.

Tone: executive briefing. Dense. Every line earns its place. If a line doesn't \
help an agent make a decision or avoid a mistake, cut it.

Target: 60-80 lines.

Project knowledge follows:

"""


def regenerate_l0_briefing(
    config: dict[str, Any],
    project_root: Path,
    doc_paths: dict[str, Path],
) -> bool:
    """Regenerate the L0 briefing section in the project's CLAUDE.md.

    Uses a model call to compress living docs into a concise executive
    briefing (~60-80 lines).

    Returns True on success, False on failure.
    """
    briefing_cfg = config.get("briefing", {})
    target_file = project_root / briefing_cfg.get("file", "CLAUDE.md")
    section_header = briefing_cfg.get("section", "## Project Knowledge Briefing")

    if not target_file.exists():
        log.warning("Briefing target file not found: %s", target_file)
        return False

    living_contents = _read_living_docs(doc_paths)
    if not living_contents:
        return False

    briefing_text = _generate_briefing(
        config,
        project_root,
        "\n\n".join(living_contents),
    )
    if not briefing_text:
        log.warning("L0 briefing generation returned empty result")
        return False

    _inject_section(target_file, section_header, briefing_text)
    log.info("L0 briefing regenerated in %s", target_file)
    return True


def _read_living_docs(doc_paths: dict[str, Path]) -> list[str]:
    """Read living docs for briefing generation.

    Skips timeline (too large, mostly historical narrative).
    Reads concepts, epistemic, and workflows in full — these are the
    docs that contain actionable current-state information.
    """
    contents: list[str] = []
    for key in ("concepts", "epistemic", "workflows"):
        p = doc_paths.get(key)
        if p and p.exists():
            content = p.read_text()
            contents.append(f"### {key.title()}\n{content}")
    return contents


def _generate_briefing(
    config: dict[str, Any],
    project_root: Path,
    living_docs_content: str,
) -> str | None:
    """Generate L0 briefing by shelling out to the configured model.

    Uses ``briefing.prompt`` from config if set, otherwise falls back
    to the built-in executive briefing prompt.  Uses the project's
    ``agent_command`` or ``model`` for the model call.

    Returns the briefing text, or None on failure.
    """
    briefing_cfg = config.get("briefing", {})
    custom_prompt = briefing_cfg.get("prompt")

    if custom_prompt:
        prompt = custom_prompt + "\n\n" + living_docs_content
    else:
        prompt = DEFAULT_PROMPT + living_docs_content

    model = config.get("model", "sonnet")
    agent_cmd = config.get("agent_command")
    if agent_cmd:
        cmd = agent_cmd.split()
    else:
        cmd = ["claude", "--print", "--model", model]

    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=300,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.returncode != 0:
            log.warning(
                "L0 briefing agent failed (rc=%d): %s",
                result.returncode,
                result.stderr[:300],
            )
    except subprocess.TimeoutExpired:
        log.warning("L0 briefing generation timed out (300s)")
    except FileNotFoundError:
        log.warning("Agent command not found: %s", cmd[0])

    return None


def _to_repo_relative(path: Path, project_root: Path) -> Path:
    """Return path relative to project root when possible."""
    resolved_path = path.resolve()
    resolved_root = project_root.resolve()
    try:
        return Path(os.path.relpath(resolved_path, resolved_root))
    except ValueError:
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
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n{section_header}\n\n{content}\n"
    else:
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

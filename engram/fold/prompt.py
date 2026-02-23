"""Jinja2 template rendering for fold prompts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from engram.epistemic_history import detect_epistemic_layout

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _basename(value: str) -> str:
    """Jinja2 filter: extract basename from a path string."""
    return os.path.basename(value)


def _get_env() -> Environment:
    """Create a Jinja2 environment loading from engram/templates/."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["basename"] = _basename
    return env


def _stringify_paths(doc_paths: dict[str, Path]) -> dict[str, str]:
    """Convert Path values to strings for template rendering."""
    return {k: str(v) for k, v in doc_paths.items()}


def _epistemic_layout_template_vars(doc_paths: dict[str, Path]) -> dict[str, str]:
    """Resolve canonical split epistemic layout vars for templates/prompts."""
    layout = detect_epistemic_layout(doc_paths["epistemic"])
    return {
        "epistemic_layout_mode": layout.mode,
        "epistemic_current_dir": str(layout.current_dir),
        "epistemic_history_dir": str(layout.history_dir),
        "epistemic_history_glob": layout.file_glob,
        "epistemic_history_ext": layout.extension,
    }


def render_chunk_input(
    *,
    chunk_id: int,
    date_range: str,
    items_content: str,
    pre_assigned_ids: dict[str, list[str]],
    doc_paths: dict[str, Path],
) -> str:
    """Render a normal fold chunk's input.md file.

    Combines system instructions (from fold_prompt.md template) with
    pre-assigned IDs, orphan advisory, and item content.
    """
    env = _get_env()
    template = env.get_template("fold_prompt.md")
    layout_vars = _epistemic_layout_template_vars(doc_paths)

    instructions = template.render(
        doc_paths=_stringify_paths(doc_paths),
        **layout_vars,
        pre_assigned_ids=pre_assigned_ids,
    )

    content = instructions
    content += f"\n# New Content ({date_range})\n"
    content += f"# Chunk {chunk_id}\n\n"

    content += items_content
    return content


def render_triage_input(
    *,
    drift_type: str,
    drift_report: Any,
    chunk_id: int,
    doc_paths: dict[str, Path],
    ref_commit: str | None = None,
    ref_date: str | None = None,
    project_root: Path | None = None,
) -> str:
    """Render a drift-triage chunk's input.md file.

    When *ref_commit* and *ref_date* are provided, the template
    includes a temporal context block instructing the agent to check
    file existence at the reference commit, not today's filesystem.
    """
    env = _get_env()
    template = env.get_template("triage_prompt.md")
    layout_vars = _epistemic_layout_template_vars(doc_paths)

    if drift_type == "orphan_triage":
        entries = drift_report.orphaned_concepts
    elif drift_type == "epistemic_audit":
        entries = drift_report.epistemic_audit
    elif drift_type == "contested_review":
        entries = drift_report.contested_claims
    elif drift_type == "stale_unverified":
        entries = drift_report.stale_unverified
    elif drift_type == "workflow_synthesis":
        entries = drift_report.workflow_repetitions
    else:
        entries = []

    lint_cmd = (
        f'engram lint --project-root "{project_root.resolve()}"'
        if project_root
        else "engram lint --project-root <project_root>"
    )

    return template.render(
        drift_type=drift_type,
        entries=entries,
        chunk_id=chunk_id,
        doc_paths=_stringify_paths(doc_paths),
        **layout_vars,
        entry_count=len(entries),
        ref_commit=ref_commit,
        ref_date=ref_date,
        lint_cmd=lint_cmd,
    )


def render_agent_prompt(
    *,
    chunk_id: int,
    date_range: str,
    input_path: Path,
    doc_paths: dict[str, Path],
    project_root: Path | None = None,
) -> str:
    """Render the chunk_NNN_prompt.txt agent execution prompt.

    This is the self-contained instruction file sent to the fold agent.
    """
    living_doc_keys = ["timeline", "concepts", "epistemic", "workflows"]
    doc_list = "\n".join(
        f"{i + 1}. {doc_paths[k]}" for i, k in enumerate(living_doc_keys)
    )
    graveyard_list = "\n".join([
        f"- {doc_paths['concept_graveyard']}",
        f"- {doc_paths['epistemic_graveyard']}",
    ])

    lint_cmd = (
        f'engram lint --project-root "{project_root.resolve()}"'
        if project_root
        else "engram lint --project-root <project_root>"
    )
    layout_vars = _epistemic_layout_template_vars(doc_paths)
    epistemic_history_dir = layout_vars["epistemic_history_dir"]
    epistemic_current_dir = layout_vars["epistemic_current_dir"]
    epistemic_constraints = (
        f"- Epistemic current-state files live under {epistemic_current_dir}/E*.md and are editable.\n"
        f"- Do NOT read per-ID epistemic history files under {epistemic_history_dir}/E*.md.\n"
        f"  They are append-only logs; when needed, append via Bash without opening them.\n"
    )

    return (
        f"You are processing a knowledge fold chunk.\n"
        f"\n"
        f"IMPORTANT CONSTRAINTS:\n"
        f"- Do NOT use the Task tool or spawn sub-agents. Do all work directly.\n"
        f"- Do NOT use Write to overwrite entire files. Use Edit for surgical updates only.\n"
        f"- Be SUCCINCT. High information density, no filler, no narrative prose.\n"
        f"{epistemic_constraints}"
        f"\n"
        f"Read the input file at {input_path.resolve()} — it contains system instructions\n"
        f"and new content covering {date_range}.\n"
        f"\n"
        f"Follow the instructions in that file. Update these 4 living documents:\n"
        f"\n"
        f"{doc_list}\n"
        f"\n"
        f"Graveyard files (append-only — do NOT read these. Use Bash to append new entries):\n"
        f"\n"
        f"{graveyard_list}\n"
        f"\n"
        f"Read each living doc first (living docs only, NOT graveyards), then make surgical edits based on the chunk content.\n"
        f"\n"
        f"Rules:\n"
        f"- Extract concepts, claims, timeline events, workflows from the chunk\n"
        f"- USER PROMPTS encode the project owner's intent — they are authoritative\n"
        f"- DEAD/refuted entries: 1-2 sentences max. Key lesson + what replaced it.\n"
        f"- Process ALL items in the chunk\n"
        f"- Use ONLY IDs listed under 'Pre-assigned IDs for this chunk'. If none are listed, do NOT create new IDs in this chunk.\n"
        f"\n"
        f"After All Edits: Lint Check (Required)\n"
        f"\n"
        f"Run the linter after completing all edits:\n"
        f"  {lint_cmd}\n"
        f"Fix every violation reported. Re-run until lint passes with 0 violations.\n"
        f"Do not stop until lint is clean.\n"
    )


def render_seed_prompt(
    *,
    doc_paths: dict[str, Path],
    pre_assigned_ids: dict[str, list[str]] | None = None,
) -> str:
    """Render a bootstrap seed prompt."""
    env = _get_env()
    template = env.get_template("seed_prompt.md")
    return template.render(
        doc_paths=_stringify_paths(doc_paths),
        pre_assigned_ids=pre_assigned_ids or {},
    )

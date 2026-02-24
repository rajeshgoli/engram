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


def _concept_workflow_layout_template_vars(doc_paths: dict[str, Path]) -> dict[str, str]:
    """Resolve mutable per-ID file directories for concepts and workflows."""
    return {
        "concept_current_dir": str(doc_paths["concepts"].with_suffix("") / "current"),
        "workflow_current_dir": str(doc_paths["workflows"].with_suffix("") / "current"),
    }


def render_chunk_input(
    *,
    chunk_id: int,
    date_range: str,
    items_content: str,
    pre_assigned_ids: dict[str, list[str]],
    workflow_variant_only_mode: bool = False,
    doc_paths: dict[str, Path],
    context_worktree_path: Path | None = None,
    context_commit: str | None = None,
) -> str:
    """Render a normal fold chunk's input.md file.

    Combines system instructions (from fold_prompt.md template) with
    pre-assigned IDs, orphan advisory, and item content.
    """
    env = _get_env()
    template = env.get_template("fold_prompt.md")
    layout_vars = {
        **_epistemic_layout_template_vars(doc_paths),
        **_concept_workflow_layout_template_vars(doc_paths),
    }

    instructions = template.render(
        doc_paths=_stringify_paths(doc_paths),
        **layout_vars,
        pre_assigned_ids=pre_assigned_ids,
        workflow_variant_only_mode=workflow_variant_only_mode,
        context_worktree_path=str(context_worktree_path) if context_worktree_path else None,
        context_commit=context_commit,
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
    context_worktree_path: Path | None = None,
    context_commit: str | None = None,
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
        context_worktree_path=str(context_worktree_path) if context_worktree_path else None,
        context_commit=context_commit,
    )


def render_agent_prompt(
    *,
    chunk_id: int,
    date_range: str,
    chunk_type: str,
    input_path: Path,
    doc_paths: dict[str, Path],
    project_root: Path | None = None,
    pre_assigned_ids: dict[str, list[str]] | None = None,
    workflow_variant_only_mode: bool = False,
    context_worktree_path: Path | None = None,
    context_commit: str | None = None,
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
    layout_vars = {
        **_epistemic_layout_template_vars(doc_paths),
        **_concept_workflow_layout_template_vars(doc_paths),
    }
    epistemic_history_dir = layout_vars["epistemic_history_dir"]
    epistemic_current_dir = layout_vars["epistemic_current_dir"]
    epistemic_constraints = (
        f"- Epistemic current-state files live under {epistemic_current_dir}/E*.md and are editable.\n"
        f"- Do NOT read per-ID epistemic history files under {epistemic_history_dir}/E*.md.\n"
        f"  They are append-only logs; when needed, append via Bash without opening them.\n"
    )
    touched_cw_entries = (
        len(pre_assigned_ids.get("C", [])) + len(pre_assigned_ids.get("W", []))
        if pre_assigned_ids
        else 0
    )
    concept_workflow_constraints = (
        "- Concept/workflow current-state files are available for detailed updates: "
        f"{layout_vars['concept_current_dir']}/C*.md and "
        f"{layout_vars['workflow_current_dir']}/W*.md.\n"
    )
    if touched_cw_entries and touched_cw_entries <= 5:
        concept_workflow_constraints += (
            "  - For small chunks, keep living doc C*/W* entries concise\n"
            "    and keep detailed rationale in per-ID current files.\n"
        )
    context_path_text = str(context_worktree_path.resolve()) if context_worktree_path else None
    context_commit_short = context_commit[:12] if context_commit else None
    triage_repo_inspection_types = {"orphan_triage", "epistemic_audit"}
    if chunk_type in triage_repo_inspection_types and context_path_text:
        repo_scope_constraints = (
            "- Use this chunk's input + living docs first.\n"
            "- Repo inspection is allowed for this triage chunk only when needed.\n"
            f"- If inspecting repo files, use ONLY {context_path_text}"
            + (f" (commit `{context_commit_short}`)" if context_commit_short else "")
            + ".\n"
            "- Do NOT inspect source files from the project-root workspace.\n"
        )
    elif chunk_type in triage_repo_inspection_types:
        repo_scope_constraints = (
            "- Use this chunk's input + living docs first.\n"
            "- Repo inspection is allowed for this triage chunk only when needed.\n"
            "- Follow triage input instructions for the correct repo view (e.g., temporal worktree when provided).\n"
        )
    elif context_path_text:
        repo_scope_constraints = (
            "- For standard fold/workflow_synthesis chunks, use only the input file + living docs.\n"
            "- Do NOT inspect source code/git/filesystem for this chunk.\n"
            f"- A context checkout exists at {context_path_text}"
            + (f" (commit `{context_commit_short}`)" if context_commit_short else "")
            + "; ignore it unless a future triage chunk explicitly requires repo verification.\n"
        )
    else:
        repo_scope_constraints = (
            "- For standard fold/workflow_synthesis chunks, use only the input file + living docs.\n"
            "- Do NOT inspect source code/git/filesystem for this chunk.\n"
        )

    return (
        f"You are processing a knowledge fold chunk.\n"
        f"\n"
        f"IMPORTANT CONSTRAINTS:\n"
        f"- Do NOT use the Task tool or spawn sub-agents. Do all work directly.\n"
        f"- Do NOT use Write to overwrite entire files. Use Edit for surgical updates only.\n"
        f"- Be SUCCINCT. High information density, no filler, no narrative prose.\n"
        f"- Exception: per-ID epistemic current files ({epistemic_current_dir}/E*.md) should be detailed and coherent, not terse.\n"
        f"{repo_scope_constraints}"
        f"{epistemic_constraints}"
        f"{concept_workflow_constraints}"
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
        f"- Every timeline phase entry must include 'IDs:' with C###/E###/W### "
        f"or 'IDs: NONE(reason)' when no stable ID applies.\n"
        f"- USER PROMPTS encode the project owner's intent — they are authoritative\n"
        f"- DEAD/refuted entries: 1-2 sentences max. Key lesson + what replaced it.\n"
        f"- Process ALL items in the chunk\n"
        f"- Use ONLY IDs listed under 'Pre-assigned IDs for this chunk'. If none are listed, do NOT create new IDs in this chunk.\n"
        + (
            "- Workflow novelty gate: when no W IDs are pre-assigned for this chunk, "
            "prefer updating an existing CURRENT workflow (usually W001 variant) instead of creating a new workflow entry.\n"
            if workflow_variant_only_mode
            else ""
        )
        + f"\n"
        + f"After All Edits: Lint Check (Required)\n"
        + f"\n"
        + f"Run the linter after completing all edits:\n"
        + f"  {lint_cmd}\n"
        + f"Fix every violation reported. Re-run until lint passes with 0 violations.\n"
        + f"Do not stop until lint is clean.\n"
    )


def render_seed_prompt(
    *,
    doc_paths: dict[str, Path],
    pre_assigned_ids: dict[str, list[str]] | None = None,
) -> str:
    """Render a bootstrap seed prompt."""
    env = _get_env()
    template = env.get_template("seed_prompt.md")
    layout_vars = {
        **_concept_workflow_layout_template_vars(doc_paths),
    }
    return template.render(
        doc_paths=_stringify_paths(doc_paths),
        **layout_vars,
        pre_assigned_ids=pre_assigned_ids or {},
    )

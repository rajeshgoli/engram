"""One-time migration of v2 living docs to v3 format.

Upgrades existing v2 living docs (no stable IDs, no workflow registry,
no graveyard files) to v3 format with:

1. ID backfill — assign C###/E###/W### IDs in document order
2. 4th doc extraction — move workflow-like entries to workflow_registry.md
3. Graveyard bootstrapping — move DEAD/refuted entries to graveyard files
4. Cross-reference rewrite — replace name-based references with stable IDs
5. Counter initialization — set id_counters in SQLite from max assigned IDs
6. Fold continuation marker — set marker date in SQLite if --fold-from provided
7. Validation pass — run linter to confirm all entries and refs are valid

Idempotent: running twice produces the same result.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

from engram.cli import GRAVEYARD_HEADERS, LIVING_DOC_HEADERS
from engram.compact.graveyard import compact_living_doc
from engram.config import load_config, resolve_doc_paths
from engram.linter import LintResult, lint
from engram.parse import (
    extract_id,
    parse_sections,
)


# v2 headings: "## Name (STATUS)" — no stable ID prefix
_V2_HEADING_RE = re.compile(
    r'^##\s+(?![CEW]\d{3,}:)(.+?)\s*'
    r'\(([^)]+)\)\s*$'
)

# Workflow indicator fields — entries with these are workflow-like
_WORKFLOW_FIELDS = re.compile(
    r'^\s*-?\s*\*?\*?(?:Context|Current method|Trigger(?:\s+for\s+change)?)\*?\*?:',
    re.MULTILINE,
)

# Status normalization: v2 status text → v3 canonical status
_CONCEPT_STATUS_MAP = {
    "active": "ACTIVE",
    "dead": "DEAD",
    "evolved": "EVOLVED",
}

_EPISTEMIC_STATUS_MAP = {
    "believed": "believed",
    "refuted": "refuted",
    "contested": "contested",
    "unverified": "unverified",
}

_WORKFLOW_STATUS_MAP = {
    "current": "CURRENT",
    "superseded": "SUPERSEDED",
    "merged": "MERGED",
}


def _normalize_status(status_raw: str, doc_type: str) -> str:
    """Normalize a v2 status string to v3 canonical form."""
    key = status_raw.strip().lower().split()[0]  # First word, lowercased

    if doc_type == "concepts":
        return _CONCEPT_STATUS_MAP.get(key, status_raw.strip())
    elif doc_type == "epistemic":
        return _EPISTEMIC_STATUS_MAP.get(key, status_raw.strip())
    elif doc_type == "workflows":
        return _WORKFLOW_STATUS_MAP.get(key, status_raw.strip())
    return status_raw.strip()


def _id_prefix_for_type(doc_type: str) -> str:
    """Return the ID prefix for a doc type."""
    return {"concepts": "C", "epistemic": "E", "workflows": "W"}[doc_type]


# ------------------------------------------------------------------
# Phase 1: ID backfill
# ------------------------------------------------------------------

def backfill_ids(
    content: str,
    doc_type: str,
    counters: dict[str, int],
) -> tuple[str, dict[str, str], dict[str, int]]:
    """Assign stable IDs to v2 entries that lack them.

    Parameters
    ----------
    content:
        Full text of a living doc.
    doc_type:
        One of "concepts", "epistemic", "workflows".
    counters:
        Current counter state: {"C": next_c, "E": next_e, "W": next_w}.
        Modified in place.

    Returns
    -------
    tuple
        (new_content, name_to_id_map, updated_counters)
        name_to_id_map maps entry names to their assigned IDs.
    """
    sections = parse_sections(content)
    if not sections:
        return content, {}, counters

    prefix = _id_prefix_for_type(doc_type)
    lines = content.split("\n")

    # Get preamble
    first_start = sections[0]["start"]

    name_to_id: dict[str, str] = {}
    new_lines = list(lines[:first_start])

    for sec in sections:
        heading = sec["heading"]
        existing_id = extract_id(heading)

        if existing_id:
            # Already has ID — preserve it, track the mapping
            m = re.match(r'^##\s+[CEW]\d{3,}:\s+(.+?)\s*\(', heading)
            if m:
                name_to_id[m.group(1).strip()] = existing_id
            # Keep entire section as-is
            sec_lines = sec["text"].split("\n")
            new_lines.extend(sec_lines)
            continue

        v2_match = _V2_HEADING_RE.match(heading)
        if not v2_match:
            # Non-entry section (e.g., preamble text) — keep as-is
            sec_lines = sec["text"].split("\n")
            new_lines.extend(sec_lines)
            continue

        name = v2_match.group(1).strip()
        status_raw = v2_match.group(2).strip()
        status = _normalize_status(status_raw, doc_type)

        # Assign ID
        next_num = counters.get(prefix, 1)
        entry_id = f"{prefix}{next_num:03d}"
        counters[prefix] = next_num + 1
        name_to_id[name] = entry_id

        # Replace the heading line, keep body
        new_heading = f"## {entry_id}: {name} ({status})"
        sec_lines = sec["text"].split("\n")
        # First line is the heading — replace it
        sec_lines[0] = new_heading
        new_lines.extend(sec_lines)

    new_content = "\n".join(new_lines)
    return new_content, name_to_id, counters


# ------------------------------------------------------------------
# Phase 2: Workflow extraction
# ------------------------------------------------------------------

def extract_workflows(
    concept_content: str,
    epistemic_content: str,
    workflow_content: str,
    counters: dict[str, int],
) -> tuple[str, str, str, dict[str, str], dict[str, int]]:
    """Extract workflow-like entries from concept/epistemic docs.

    Scans concept_registry and epistemic_state for entries whose body
    contains workflow-indicator fields (Context:, Current method:, Trigger:).
    Moves them to workflow_registry with W### IDs.

    Returns
    -------
    tuple
        (new_concept, new_epistemic, new_workflow, name_to_id, counters)
    """
    extracted_sections: list[str] = []
    name_to_id: dict[str, str] = {}

    def _process_doc(content: str, source_doc: str) -> str:
        """Process a doc, removing workflow entries and collecting them."""
        sections = parse_sections(content)
        if not sections:
            return content

        lines = content.split("\n")
        first_start = sections[0]["start"]
        new_lines = list(lines[:first_start])

        for sec in sections:
            if not _WORKFLOW_FIELDS.search(sec["text"]):
                # Not a workflow — keep in place
                sec_lines = sec["text"].split("\n")
                new_lines.extend(sec_lines)
                continue

            # This is a workflow entry — extract it
            heading = sec["heading"]
            existing_id = extract_id(heading)

            if existing_id and existing_id.startswith("W"):
                # Already a workflow ID — just move the section
                sec_lines = sec["text"].split("\n")
                extracted_sections.append("\n".join(sec_lines))
                continue

            # Has a non-W ID or no ID — need to re-ID as W
            if existing_id:
                # Has C### or E### — re-assign as W###
                m = re.match(r'^##\s+[CEW]\d{3,}:\s+(.+?)\s*\(([^)]+)\)', heading)
            else:
                m = _V2_HEADING_RE.match(heading)

            if not m:
                # Can't parse — keep in place
                sec_lines = sec["text"].split("\n")
                new_lines.extend(sec_lines)
                continue

            name = m.group(1).strip()
            status_raw = m.group(2).strip()
            status = _normalize_status(status_raw, "workflows")

            next_num = counters.get("W", 1)
            entry_id = f"W{next_num:03d}"
            counters["W"] = next_num + 1
            name_to_id[name] = entry_id

            # Build the new heading
            new_heading = f"## {entry_id}: {name} ({status})"
            sec_lines = sec["text"].split("\n")
            sec_lines[0] = new_heading
            extracted_sections.append("\n".join(sec_lines))

        return "\n".join(new_lines)

    new_concept = _process_doc(concept_content, "concepts")
    new_epistemic = _process_doc(epistemic_content, "epistemic")

    # Append extracted sections to workflow doc
    if extracted_sections:
        if workflow_content.rstrip():
            new_workflow = workflow_content.rstrip() + "\n\n" + "\n\n".join(extracted_sections)
        else:
            new_workflow = workflow_content + "\n".join(extracted_sections)
    else:
        new_workflow = workflow_content

    return new_concept, new_epistemic, new_workflow, name_to_id, counters


# ------------------------------------------------------------------
# Phase 4: Cross-reference rewrite
# ------------------------------------------------------------------

def rewrite_cross_references(
    content: str,
    name_to_id: dict[str, str],
) -> str:
    """Replace name-based references with stable ID references.

    Looks for patterns like "see concept_name", "C### (name)", references
    in Related concepts:, Supersedes:, etc.

    Parameters
    ----------
    content:
        Document text.
    name_to_id:
        Mapping from entry names to their stable IDs.

    Returns
    -------
    str
        Content with name-based references replaced by ID references.
    """
    if not name_to_id:
        return content

    # Sort names longest-first to avoid partial matches
    sorted_names = sorted(name_to_id.keys(), key=len, reverse=True)

    for name in sorted_names:
        entry_id = name_to_id[name]
        # Skip if this name is already an ID pattern
        if re.match(r'^[CEW]\d{3,}$', name):
            continue

        # Replace "see <name>" with "see <ID>"
        # Replace standalone mentions in reference fields
        # Be conservative: only replace in reference-like contexts
        # to avoid false positives in prose

        # Pattern: the name in reference contexts
        # - "see <name>"
        # - "(<name>)"
        # - "→ <name>"
        # - "Supersedes: <name>"
        # - "Related concepts: <name>"
        escaped = re.escape(name)

        # Replace "see <name>" → "see <ID> (<name>)"
        content = re.sub(
            rf'\bsee\s+{escaped}\b',
            f'see {entry_id}',
            content,
        )

        # Replace "Supersedes:.*<name>" references
        content = re.sub(
            rf'(Supersedes:\s*.*?)\b{escaped}\b',
            rf'\g<1>{entry_id}',
            content,
        )

        # Replace "Related concepts:.*<name>" references
        content = re.sub(
            rf'(Related concepts:\s*.*?)\b{escaped}\b',
            rf'\g<1>{entry_id}',
            content,
        )

    return content


# ------------------------------------------------------------------
# Phase 5: Counter initialization
# ------------------------------------------------------------------

def initialize_counters(
    db_path: Path,
    contents: dict[str, str],
) -> dict[str, int]:
    """Set id_counters in SQLite from max assigned IDs in the docs.

    Scans all docs for the highest assigned ID per category and sets
    the counter to max + 1.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.
    contents:
        Mapping of doc_type → content.

    Returns
    -------
    dict
        {"C": next_c, "E": next_e, "W": next_w}
    """
    max_ids: dict[str, int] = {"C": 0, "E": 0, "W": 0}

    for content in contents.values():
        for section in parse_sections(content):
            entry_id = extract_id(section["heading"])
            if entry_id:
                prefix = entry_id[0]
                num = int(entry_id[1:])
                if prefix in max_ids and num > max_ids[prefix]:
                    max_ids[prefix] = num

    # Set counters to max + 1
    next_ids = {cat: max_val + 1 for cat, max_val in max_ids.items()}

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS id_counters (
                category TEXT PRIMARY KEY,
                next_id  INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        for cat, next_id in next_ids.items():
            conn.execute(
                "INSERT OR REPLACE INTO id_counters (category, next_id) VALUES (?, ?)",
                (cat, next_id),
            )
        conn.commit()
    finally:
        conn.close()

    return next_ids


# ------------------------------------------------------------------
# Phase 6: Fold continuation marker
# ------------------------------------------------------------------

def set_fold_marker(db_path: Path, fold_from: date) -> None:
    """Set the fold continuation marker in SQLite.

    Uses ServerDB to ensure the table always has the correct
    singleton-row schema, handling legacy migration if needed.
    """
    from engram.server.db import ServerDB

    db = ServerDB(db_path)
    db.set_fold_from(fold_from.isoformat())


# ------------------------------------------------------------------
# Main migration entry point
# ------------------------------------------------------------------

def migrate(
    project_root: Path,
    fold_from: date | None = None,
) -> tuple[LintResult, dict[str, int]]:
    """Run the full v2→v3 migration pipeline.

    Parameters
    ----------
    project_root:
        Root of the project containing .engram/ and living docs.
    fold_from:
        Optional date to set as fold continuation marker.

    Returns
    -------
    tuple
        (lint_result, counter_state)
    """
    config = load_config(project_root)
    paths = resolve_doc_paths(config, project_root)
    db_path = project_root / ".engram" / "engram.db"

    # Read existing docs (some may not exist yet)
    docs: dict[str, str] = {}
    for key in ("timeline", "concepts", "epistemic", "workflows"):
        path = paths[key]
        if path.exists():
            docs[key] = path.read_text()
        else:
            docs[key] = LIVING_DOC_HEADERS.get(key, "")

    # Ensure graveyard files exist with headers
    for key in ("concept_graveyard", "epistemic_graveyard"):
        path = paths[key]
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            gy_key = key.replace("_graveyard", "").replace("concept", "concepts")
            # Map back: concept_graveyard → concepts, epistemic_graveyard → epistemic
            if key == "concept_graveyard":
                path.write_text(GRAVEYARD_HEADERS["concepts"])
            else:
                path.write_text(GRAVEYARD_HEADERS["epistemic"])

    # Initialize counters for ID assignment
    counters: dict[str, int] = {"C": 1, "E": 1, "W": 1}

    # Check if docs already have IDs (for idempotency)
    # If they do, scan for max IDs and start from there
    for key in ("concepts", "epistemic", "workflows"):
        for sec in parse_sections(docs[key]):
            eid = extract_id(sec["heading"])
            if eid:
                prefix = eid[0]
                num = int(eid[1:])
                if num >= counters.get(prefix, 1):
                    counters[prefix] = num + 1

    # Collect all name→id mappings across phases
    all_name_to_id: dict[str, str] = {}

    # Phase 1: ID backfill on each doc
    for key in ("concepts", "epistemic"):
        doc_type = key
        docs[key], name_map, counters = backfill_ids(docs[key], doc_type, counters)
        all_name_to_id.update(name_map)

    # Phase 2: Extract workflows from concept/epistemic docs
    docs["concepts"], docs["epistemic"], docs["workflows"], wf_name_map, counters = (
        extract_workflows(
            docs["concepts"],
            docs["epistemic"],
            docs["workflows"],
            counters,
        )
    )
    all_name_to_id.update(wf_name_map)

    # Backfill IDs on workflow doc (for entries that were already there)
    docs["workflows"], wf_existing_map, counters = backfill_ids(
        docs["workflows"], "workflows", counters,
    )
    all_name_to_id.update(wf_existing_map)

    # Phase 3: Graveyard bootstrapping (reuses compact/graveyard.py)
    for doc_type, gy_key in [("concepts", "concept_graveyard"), ("epistemic", "epistemic_graveyard")]:
        docs[doc_type], _ = compact_living_doc(
            docs[doc_type], doc_type, paths[gy_key],
        )

    # Phase 4: Cross-reference rewrite
    for key in docs:
        docs[key] = rewrite_cross_references(docs[key], all_name_to_id)

    # Write updated docs
    for key in ("timeline", "concepts", "epistemic", "workflows"):
        paths[key].parent.mkdir(parents=True, exist_ok=True)
        paths[key].write_text(docs[key])

    # Phase 5: Counter initialization
    # Re-read all docs (including graveyard) for counter scan
    all_contents: dict[str, str] = dict(docs)
    for gy_key in ("concept_graveyard", "epistemic_graveyard"):
        if paths[gy_key].exists():
            all_contents[gy_key] = paths[gy_key].read_text()
    counter_state = initialize_counters(db_path, all_contents)

    # Phase 6: Fold continuation marker
    if fold_from:
        set_fold_marker(db_path, fold_from)

    # Phase 7: Validation pass
    living_docs = {k: docs[k] for k in ("timeline", "concepts", "epistemic", "workflows")}
    graveyard_docs: dict[str, str] = {}
    for gy_key in ("concept_graveyard", "epistemic_graveyard"):
        if paths[gy_key].exists():
            graveyard_docs[gy_key] = paths[gy_key].read_text()

    lint_result = lint(living_docs, graveyard_docs, config)

    return lint_result, counter_state

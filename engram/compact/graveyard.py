"""Graveyard compaction: move DEAD/refuted entries to append-only archives.

Ported from v2 ``_compact_doc()`` (knowledge_fold.py lines 666-718) and
``_find_orphaned_concepts()`` (lines 628-663), parameterized by config.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from engram.parse import (
    Section,
    extract_id,
    extract_referenced_ids,
    is_stub,
    parse_sections,
)

# Statuses that trigger a graveyard move per doc type
_GRAVEYARD_STATUSES: dict[str, set[str]] = {
    "concepts": {"dead", "evolved"},
    "epistemic": {"refuted"},
}

# File patterns to search for orphaned source references
_DEFAULT_SOURCE_PATTERNS = [
    r'(?:src|tests|lib|engram|frontend)/[\w/._-]+\.(?:py|ts|tsx|js|html)',
]


def generate_stub(section: Section, graveyard_filename: str) -> str:
    """Generate a one-liner STUB heading for a compacted entry.

    The STUB conforms to linter schema rules: heading + arrow pointer only.

    Parameters
    ----------
    section:
        The parsed section being moved to graveyard.
    graveyard_filename:
        Basename of the graveyard file (e.g., ``concept_graveyard.md``).

    Returns
    -------
    str
        A single STUB line like ``## C042: name (DEAD) -> graveyard.md#C042``.
    """
    entry_id = extract_id(section["heading"])
    if not entry_id:
        raise ValueError(f"Cannot generate stub: no stable ID in heading '{section['heading']}'")

    # Extract the name and status from the original heading
    # Heading format: "## C042: name (STATUS...)"
    m = re.match(r'^##\s+[CEW]\d{3,}:\s+(.+?)\s*\(([^)]+)\)', section["heading"])
    if not m:
        raise ValueError(f"Cannot parse heading: '{section['heading']}'")

    name = m.group(1).strip()
    status_raw = m.group(2).strip()

    return f"## {entry_id}: {name} ({status_raw}) \u2192 {graveyard_filename}#{entry_id}"


def move_to_graveyard(
    section: Section,
    doc_type: str,
    graveyard_path: Path,
) -> str:
    """Move a full entry to the graveyard file and return the STUB line.

    Appends the full section text to the graveyard file. The caller is
    responsible for replacing the section in the living doc with the
    returned STUB.

    Parameters
    ----------
    section:
        The parsed section to move.
    doc_type:
        ``concepts`` or ``epistemic``.
    graveyard_path:
        Absolute path to the graveyard file.

    Returns
    -------
    str
        The STUB line to replace the section in the living doc.
    """
    if doc_type not in _GRAVEYARD_STATUSES:
        raise ValueError(f"Unknown doc_type '{doc_type}'; expected one of {sorted(_GRAVEYARD_STATUSES)}")

    status = section.get("status")
    if status not in _GRAVEYARD_STATUSES[doc_type]:
        raise ValueError(
            f"Section status '{status}' is not a graveyard status for {doc_type}. "
            f"Expected one of {sorted(_GRAVEYARD_STATUSES[doc_type])}"
        )

    graveyard_filename = graveyard_path.name
    stub = generate_stub(section, graveyard_filename)

    # Append full entry to graveyard (append-only)
    entry_text = section["text"].rstrip("\n")
    separator = "\n\n" if graveyard_path.exists() and graveyard_path.stat().st_size > 0 else ""

    with open(graveyard_path, "a") as f:
        f.write(f"{separator}{entry_text}\n")

    return stub


def append_correction_block(
    graveyard_path: Path,
    entry_id: str,
    old_status: str,
    new_status: str,
    target: str | None = None,
    correction_date: date | None = None,
) -> None:
    """Append a correction block to the graveyard file.

    Used when a misclassification is discovered — e.g., an entry marked
    DEAD was actually EVOLVED. The original entry stays for audit trail;
    this block supersedes it.

    Parameters
    ----------
    graveyard_path:
        Path to the graveyard file.
    entry_id:
        The stable ID (e.g., ``C012``).
    old_status:
        Previous status (e.g., ``DEAD``).
    new_status:
        Corrected status (e.g., ``EVOLVED``).
    target:
        Optional target reference (e.g., ``C089``).
    correction_date:
        Date of correction. Defaults to today.
    """
    d = correction_date or date.today()
    date_str = d.isoformat()

    target_part = f" \u2192 {target}" if target else ""
    reclassified = f"{old_status} \u2192 {new_status}{target_part}"

    # Determine the living doc to reference
    prefix = entry_id[0]
    living_doc = {
        "C": "concept_registry.md",
        "E": "epistemic_state.md",
        "W": "workflow_registry.md",
    }.get(prefix, "unknown")

    block = (
        f"## {entry_id} CORRECTION ({date_str})\n"
        f"Reclassified: {reclassified}\n"
        f"Original entry above is superseded. See {target or entry_id} in {living_doc}."
    )

    separator = "\n\n" if graveyard_path.exists() and graveyard_path.stat().st_size > 0 else ""

    with open(graveyard_path, "a") as f:
        f.write(f"{separator}{block}\n")


def compact_living_doc(
    content: str,
    doc_type: str,
    graveyard_path: Path,
) -> tuple[str, int]:
    """Compact a living doc by moving DEAD/refuted entries to graveyard.

    Scans the living doc for entries with graveyard-eligible statuses,
    moves each to the graveyard file, and replaces it with a STUB.

    Parameters
    ----------
    content:
        Full text of the living doc.
    doc_type:
        ``concepts`` or ``epistemic``.
    graveyard_path:
        Path to the graveyard file (will be created if missing).

    Returns
    -------
    tuple[str, int]
        (new_living_doc_content, chars_saved).
    """
    if doc_type not in _GRAVEYARD_STATUSES:
        raise ValueError(f"Unknown doc_type '{doc_type}'; expected one of {sorted(_GRAVEYARD_STATUSES)}")

    sections = parse_sections(content)
    if not sections:
        return content, 0

    eligible_statuses = _GRAVEYARD_STATUSES[doc_type]
    lines = content.split("\n")

    # Get preamble (everything before first H2)
    first_start = sections[0]["start"]
    preamble = "\n".join(lines[:first_start])

    parts = [preamble]
    chars_saved = 0

    for sec in sections:
        if is_stub(sec["heading"]):
            # Already in graveyard — remove entirely from living doc
            chars_saved += len(sec["text"])
            continue

        status = sec.get("status")
        if status in eligible_statuses:
            # Move to graveyard — remove entirely from living doc (no stub)
            move_to_graveyard(sec, doc_type, graveyard_path)
            chars_saved += len(sec["text"])
        else:
            # Keep full text
            parts.append(sec["text"])

    new_content = "\n".join(parts)
    return new_content, chars_saved


def find_orphaned_concepts(
    registry_content: str,
    project_root: Path,
    source_patterns: list[str] | None = None,
) -> list[dict[str, str | list[str]]]:
    """Find ACTIVE concepts whose referenced source files no longer exist.

    Ported from v2 ``_find_orphaned_concepts()`` (lines 628-663),
    parameterized by project root and source file patterns.

    Parameters
    ----------
    registry_content:
        Full text of the concept registry.
    project_root:
        Root directory of the project being tracked.
    source_patterns:
        Regex patterns to match source file paths in Code: fields.
        Defaults to common patterns (src/, tests/, lib/, etc.).

    Returns
    -------
    list[dict]
        List of ``{"name": str, "id": str, "paths": list[str]}`` for
        concepts where ALL referenced files are missing.
    """
    sections = parse_sections(registry_content)
    if source_patterns is None:
        source_patterns = _DEFAULT_SOURCE_PATTERNS

    combined_pattern = "|".join(source_patterns)
    orphans: list[dict[str, str | list[str]]] = []

    for sec in sections:
        # Skip non-active entries
        if sec["status"] in ("dead", "refuted", "evolved", "superseded", "merged"):
            continue
        if is_stub(sec["heading"]):
            continue

        # Look for Code: field
        code_match = re.search(
            r'\*?\*?Code\*?\*?:\s*(.+?)(?:\n|$)', sec["text"]
        )
        if not code_match:
            continue

        paths = re.findall(combined_pattern, code_match.group(1))
        if not paths:
            continue

        missing = [p for p in paths if not (project_root / p).exists()]
        if missing and len(missing) == len(paths):
            entry_id = extract_id(sec["heading"]) or "unknown"
            name_match = re.match(r'^##\s+[CEW]\d{3,}:\s+(.+?)\s*\(', sec["heading"])
            name = name_match.group(1).strip() if name_match else sec["heading"]
            orphans.append({"name": name, "id": entry_id, "paths": missing})

    return orphans

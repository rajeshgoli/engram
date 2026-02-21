"""Helpers for externalized epistemic history files.

External history path is inferred from the epistemic state doc path and entry ID:
`<epistemic_state_stem>/<EID>.md`.
Example: docs/decisions/epistemic_state.md -> docs/decisions/epistemic_state/E005.md
"""

from __future__ import annotations

import re
from pathlib import Path


# Recognized epistemic field headers. Used to detect field boundaries without
# misclassifying free-form history lines like "Product Dec 11: ...".
EPISTEMIC_FIELD_NAMES = {
    "current position",
    "evidence",
    "history",
    "agent guidance",
    "corrected by",
    "superseded by",
}

_FIELD_PATTERNS = [
    # Colon inside bold markers: **History:**
    re.compile(r"^\*\*([A-Za-z][A-Za-z _/-]*):\*\*\s*(.*)$"),
    # Colon outside bold markers: **History**:
    re.compile(r"^\*\*([A-Za-z][A-Za-z _/-]*)\*\*:\s*(.*)$"),
    # Plain: History:
    re.compile(r"^([A-Za-z][A-Za-z _/-]*):\s*(.*)$"),
]

_ENTRY_HEADING_RE = re.compile(r"^##\s+(E\d{3,})\b", re.IGNORECASE)


def _parse_field_header(normalized_line: str) -> tuple[str, str] | None:
    """Parse a markdown field header and return (field_name_lower, remainder)."""
    for pat in _FIELD_PATTERNS:
        match = pat.match(normalized_line)
        if match:
            return match.group(1).strip().lower(), match.group(2).strip()
    return None


def _is_history_boundary(
    *,
    stripped_line: str,
    field: tuple[str, str] | None,
    field_name: str | None,
) -> bool:
    """Return True when a line marks the end of the inline History block.

    We stop on:
    - next section heading (`## ...`)
    - known epistemic fields
    - any unknown bold markdown field header (`**Field:**` or `- **Field:**`)

    Unknown plain `Label: ...` lines are kept as history content to avoid
    misclassifying free-form lines like "Product Dec 11: ...".
    """
    if stripped_line.startswith("## "):
        return True
    if not field or field_name == "history":
        return False
    if field_name in EPISTEMIC_FIELD_NAMES:
        return True
    normalized = stripped_line.removeprefix("- ").strip()
    return normalized.startswith("**")


def infer_history_dir(epistemic_doc_path: Path) -> Path:
    """Return inferred history directory for an epistemic state doc."""
    return epistemic_doc_path.with_suffix("")


def infer_history_path(epistemic_doc_path: Path, entry_id: str) -> Path:
    """Return inferred history file path for an epistemic entry ID."""
    return infer_history_dir(epistemic_doc_path) / f"{entry_id}.md"


def extract_external_history_for_entry(history_text: str, entry_id: str) -> str | None:
    """Return external history section text scoped to a single epistemic entry.

    External history files are expected to be per-ID files, but this helper is
    defensive and only returns the matching `## E###` section(s) when multiple
    headings are present.
    """
    lines = history_text.splitlines()
    section_starts: list[tuple[int, str]] = []

    for i, line in enumerate(lines):
        match = _ENTRY_HEADING_RE.match(line.strip())
        if not match:
            continue
        section_starts.append((i, match.group(1).upper()))

    if not section_starts:
        return None

    target_id = entry_id.upper()
    matching_sections: list[str] = []
    for i, (start, sec_id) in enumerate(section_starts):
        if sec_id != target_id:
            continue
        end = section_starts[i + 1][0] if i + 1 < len(section_starts) else len(lines)
        section_text = "\n".join(lines[start:end]).strip()
        if section_text:
            matching_sections.append(section_text)

    if not matching_sections:
        return None

    return "\n\n".join(matching_sections)


def extract_inline_history_lines(section_text: str) -> list[str]:
    """Extract lines in the History field from an epistemic section.

    Returns content lines only (without the `History:` header line).
    """
    history_lines: list[str] = []
    in_history = False

    for line in section_text.splitlines():
        stripped = line.strip()
        normalized = stripped.removeprefix("- ").strip()
        field = _parse_field_header(normalized)
        field_name = field[0] if field else None

        if field_name == "history":
            in_history = True
            remainder = field[1] if field else ""
            if remainder:
                history_lines.append(remainder)
            continue

        if not in_history:
            continue

        if _is_history_boundary(
            stripped_line=stripped,
            field=field,
            field_name=field_name,
        ):
            break

        if stripped:
            history_lines.append(stripped)

    return history_lines


def remove_inline_history(section_text: str) -> tuple[str, list[str]]:
    """Remove the History field block from a section.

    Returns `(updated_section_text, extracted_history_lines)`.
    """
    lines = section_text.splitlines()
    start_idx: int | None = None
    end_idx: int | None = None
    extracted: list[str] = []
    in_history = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        normalized = stripped.removeprefix("- ").strip()
        field = _parse_field_header(normalized)
        field_name = field[0] if field else None

        if field_name == "history" and start_idx is None:
            start_idx = i
            in_history = True
            remainder = field[1] if field else ""
            if remainder:
                extracted.append(remainder)
            continue

        if not in_history:
            continue

        if _is_history_boundary(
            stripped_line=stripped,
            field=field,
            field_name=field_name,
        ):
            end_idx = i
            break
        extracted.append(line)

    if start_idx is None:
        return section_text, []

    if end_idx is None:
        end_idx = len(lines)

    new_lines = lines[:start_idx] + lines[end_idx:]

    # Collapse repeated blank lines after removing block.
    compacted: list[str] = []
    prev_blank = False
    for line in new_lines:
        blank = line.strip() == ""
        if blank and prev_blank:
            continue
        compacted.append(line)
        prev_blank = blank

    # Trim extracted history lines to non-empty meaningful lines.
    cleaned = [ln.rstrip() for ln in extracted if ln.strip()]
    return "\n".join(compacted), cleaned

"""One-time migration: externalize inline epistemic history to per-ID files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from engram.epistemic_history import infer_history_path, remove_inline_history
from engram.parse import extract_id, is_stub, parse_sections


@dataclass
class EpistemicHistoryMigrationResult:
    """Summary of externalization changes."""

    migrated_entries: int
    created_files: int
    appended_blocks: int


def _extract_subject(heading: str) -> str:
    """Extract human-readable subject from an epistemic heading line."""
    text = re.sub(r"^##\s+E\d{3,}:\s+", "", heading.strip())
    text = re.sub(r"\s+\([^)]*\)\s*(?:→\s+\S+)?\s*$", "", text).strip()
    return text or "claim"


def _ensure_history_heading(path: Path, entry_id: str, subject: str) -> bool:
    """Ensure history file exists and contains heading for the entry ID.

    Returns True when a new file was created.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Epistemic History\n\n"
            f"## {entry_id}: {subject}\n\n",
        )
        return True

    text = path.read_text()
    if re.search(rf"^##\s+{re.escape(entry_id)}\b", text, re.MULTILINE):
        return False

    with open(path, "a") as fh:
        if not text.endswith("\n"):
            fh.write("\n")
        fh.write(f"\n## {entry_id}: {subject}\n\n")
    return False


def _append_history_lines(path: Path, lines: list[str]) -> None:
    """Append a migrated history block to a per-ID history file."""
    normalized: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            normalized.append(stripped)
        else:
            normalized.append(f"- {stripped}")

    if not normalized:
        return

    text = path.read_text()
    with open(path, "a") as fh:
        if not text.endswith("\n"):
            fh.write("\n")
        for line in normalized:
            fh.write(f"{line}\n")
        fh.write("\n")


def externalize_epistemic_history(epistemic_path: Path) -> EpistemicHistoryMigrationResult:
    """Move inline History blocks from epistemic_state.md into inferred E###.md files."""
    if not epistemic_path.exists():
        return EpistemicHistoryMigrationResult(0, 0, 0)

    original = epistemic_path.read_text()
    sections = parse_sections(original)
    lines = original.splitlines()

    migrated_entries = 0
    created_files = 0
    appended_blocks = 0

    # Iterate bottom-up to keep parse indices stable during line replacement.
    for sec in reversed(sections):
        entry_id = extract_id(sec["heading"])
        if not entry_id:
            continue
        if is_stub(sec["heading"]) or sec["status"] == "refuted":
            continue

        section_text = "\n".join(lines[sec["start"]:sec["end"]])
        updated_section, history_lines = remove_inline_history(section_text)
        if not history_lines:
            continue

        history_path = infer_history_path(epistemic_path, entry_id)
        subject = _extract_subject(sec["heading"])
        if _ensure_history_heading(history_path, entry_id, subject):
            created_files += 1
        _append_history_lines(history_path, history_lines)
        appended_blocks += 1

        lines[sec["start"]:sec["end"]] = updated_section.splitlines()
        migrated_entries += 1

    updated = "\n".join(lines)
    if original.endswith("\n"):
        updated += "\n"
    epistemic_path.write_text(updated)

    return EpistemicHistoryMigrationResult(
        migrated_entries=migrated_entries,
        created_files=created_files,
        appended_blocks=appended_blocks,
    )


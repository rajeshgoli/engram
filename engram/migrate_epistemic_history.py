"""One-time migration for split per-ID epistemic current/history files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from engram.epistemic_history import (
    infer_current_path,
    infer_epistemic_dir,
    infer_history_path,
    remove_inline_history,
)
from engram.parse import extract_id, is_stub, parse_sections


@dataclass
class EpistemicHistoryMigrationResult:
    """Summary of externalization changes."""

    migrated_entries: int
    created_history_files: int
    created_current_files: int
    appended_blocks: int
    migrated_legacy_files: int


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


def _write_current_state(path: Path, section_text: str) -> bool:
    """Write canonical mutable current-state file for an epistemic entry.

    Returns True when a new current-state file was created.
    Existing files are preserved to keep migration reruns safe.
    """
    if path.exists():
        return False

    created = True
    path.parent.mkdir(parents=True, exist_ok=True)
    content = section_text.strip()
    if content:
        path.write_text(f"{content}\n")
    else:
        path.write_text("")
    return created


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


def find_legacy_epistemic_files(epistemic_path: Path) -> list[Path]:
    """Return legacy per-ID files under ``epistemic_state/E*.md``."""
    legacy_dir = infer_epistemic_dir(epistemic_path)
    if not legacy_dir.exists():
        return []
    return [
        path
        for path in sorted(legacy_dir.glob("E*.md"))
        if path.is_file() and re.fullmatch(r"E\d{3,}\.md", path.name)
    ]


def migrate_legacy_epistemic_files(epistemic_path: Path) -> int:
    """Move legacy per-ID files from ``epistemic_state/E*.md`` into ``history/``.

    Returns the number of legacy files migrated.
    """
    legacy_files = find_legacy_epistemic_files(epistemic_path)
    if not legacy_files:
        return 0

    legacy_dir = infer_epistemic_dir(epistemic_path)
    migrated = 0
    for legacy_path in legacy_files:
        entry_id = legacy_path.stem.upper()
        target_path = infer_history_path(epistemic_path, entry_id)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if target_path.exists():
            legacy_text = legacy_path.read_text()
            target_text = target_path.read_text()
            if legacy_text.strip() == target_text.strip():
                legacy_path.unlink(missing_ok=True)
                migrated += 1
                continue
            raise ValueError(
                "Cannot auto-migrate legacy epistemic files due to conflicting targets.\n"
                f"Legacy file: {legacy_path}\n"
                f"Existing target: {target_path}\n"
                "Resolve conflict manually, then rerun migration.",
            )

        legacy_path.replace(target_path)
        migrated += 1

    return migrated


def externalize_epistemic_history(epistemic_path: Path) -> EpistemicHistoryMigrationResult:
    """Split inline epistemic content into per-ID current/history inferred files."""
    if not epistemic_path.exists():
        return EpistemicHistoryMigrationResult(0, 0, 0, 0, 0)

    migrated_legacy_files = migrate_legacy_epistemic_files(epistemic_path)

    original = epistemic_path.read_text()
    sections = parse_sections(original)
    lines = original.splitlines()

    migrated_entries = 0
    created_history_files = 0
    created_current_files = 0
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

        current_path = infer_current_path(epistemic_path, entry_id)
        if _write_current_state(current_path, updated_section):
            created_current_files += 1

        history_path = infer_history_path(epistemic_path, entry_id)
        subject = _extract_subject(sec["heading"])
        if _ensure_history_heading(history_path, entry_id, subject):
            created_history_files += 1
        _append_history_lines(history_path, history_lines)
        appended_blocks += 1

        lines[sec["start"]:sec["end"]] = updated_section.splitlines()
        migrated_entries += 1

    # Materialize current-state files for entries without inline history too.
    refreshed_text = "\n".join(lines)
    refreshed_sections = parse_sections(refreshed_text)
    for sec in refreshed_sections:
        entry_id = extract_id(sec["heading"])
        if not entry_id:
            continue
        if is_stub(sec["heading"]) or sec["status"] == "refuted":
            continue
        section_text = "\n".join(lines[sec["start"]:sec["end"]])
        current_path = infer_current_path(epistemic_path, entry_id)
        if _write_current_state(current_path, section_text):
            created_current_files += 1

    updated = "\n".join(lines)
    if original.endswith("\n"):
        updated += "\n"
    epistemic_path.write_text(updated)

    return EpistemicHistoryMigrationResult(
        migrated_entries=migrated_entries,
        created_history_files=created_history_files,
        created_current_files=created_current_files,
        appended_blocks=appended_blocks,
        migrated_legacy_files=migrated_legacy_files,
    )

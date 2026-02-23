"""One-time migration for split per-ID epistemic current/history files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from engram.epistemic_history import (
    extract_external_history_for_entry,
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


def _append_history_block(path: Path, block_text: str) -> bool:
    """Append a raw history block to a per-ID history file."""
    block = block_text.strip()
    if not block:
        return False

    text = path.read_text()
    with open(path, "a") as fh:
        if not text.endswith("\n"):
            fh.write("\n")
        fh.write(f"{block}\n\n")
    return True


def _migrate_legacy_history_files(
    *,
    epistemic_path: Path,
    subjects_by_id: dict[str, str],
) -> tuple[int, int, int]:
    """Move legacy per-ID ``E*.md`` files into split history files.

    Returns ``(migrated_legacy_files, created_history_files, appended_blocks)``.
    """
    legacy_dir = infer_epistemic_dir(epistemic_path)
    if not legacy_dir.exists():
        return 0, 0, 0

    migrated_legacy_files = 0
    created_history_files = 0
    appended_blocks = 0

    for legacy_path in sorted(legacy_dir.glob("E*.md")):
        if not legacy_path.is_file():
            continue
        entry_id = legacy_path.stem.upper()
        if not re.fullmatch(r"E\d{3,}", entry_id):
            continue

        try:
            legacy_text = legacy_path.read_text()
        except OSError:
            continue

        history_path = infer_history_path(epistemic_path, entry_id)
        subject = subjects_by_id.get(entry_id, "claim")
        if _ensure_history_heading(history_path, entry_id, subject):
            created_history_files += 1

        scoped = extract_external_history_for_entry(legacy_text, entry_id)
        payload_source = scoped.strip() if scoped else legacy_text.strip()
        payload_lines = payload_source.splitlines()
        if payload_lines and re.match(r"^##\s+E\d{3,}\b", payload_lines[0].strip(), re.IGNORECASE):
            payload_lines = payload_lines[1:]
        while payload_lines and not payload_lines[0].strip():
            payload_lines = payload_lines[1:]
        payload = "\n".join(payload_lines).strip()

        if _append_history_block(history_path, payload):
            appended_blocks += 1

        legacy_path.unlink(missing_ok=True)
        migrated_legacy_files += 1

    return migrated_legacy_files, created_history_files, appended_blocks


def externalize_epistemic_history(epistemic_path: Path) -> EpistemicHistoryMigrationResult:
    """Split inline epistemic content into per-ID current/history inferred files."""
    if not epistemic_path.exists():
        return EpistemicHistoryMigrationResult(0, 0, 0, 0, 0)

    original = epistemic_path.read_text()
    sections = parse_sections(original)
    lines = original.splitlines()

    migrated_entries = 0
    created_history_files = 0
    created_current_files = 0
    appended_blocks = 0
    migrated_legacy_files = 0

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

    subjects_by_id: dict[str, str] = {}
    for sec in refreshed_sections:
        entry_id = extract_id(sec["heading"])
        if not entry_id:
            continue
        subjects_by_id[entry_id.upper()] = _extract_subject(sec["heading"])

    legacy_migrated, legacy_created, legacy_appended = _migrate_legacy_history_files(
        epistemic_path=epistemic_path,
        subjects_by_id=subjects_by_id,
    )
    migrated_legacy_files += legacy_migrated
    created_history_files += legacy_created
    appended_blocks += legacy_appended

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

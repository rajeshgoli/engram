"""Tests for epistemic history externalization migration."""

from __future__ import annotations

from pathlib import Path

from engram.migrate_epistemic_history import externalize_epistemic_history


def test_externalizes_inline_history_into_inferred_file(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    epistemic.parent.mkdir(parents=True, exist_ok=True)
    epistemic.write_text(
        "# Epistemic State\n\n"
        "## E005: Ground truth annotation > voting (believed)\n"
        "**Current position:** Better signal quality.\n"
        "**History:**\n"
        "- 2026-02-21: Confirmed from interview notes\n"
        "**Agent guidance:** Use annotation-first.\n",
    )

    result = externalize_epistemic_history(epistemic)
    assert result.migrated_entries == 1
    assert result.created_history_files == 1
    assert result.created_current_files == 1
    assert result.appended_blocks == 1
    assert result.migrated_legacy_files == 0

    updated = epistemic.read_text()
    assert "**History:**" not in updated
    assert "Ground truth annotation > voting (believed)" in updated

    current_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "current" / "E005.em"
    assert current_file.exists()
    assert "## E005:" in current_file.read_text()

    history_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "history" / "E005.em"
    assert history_file.exists()
    content = history_file.read_text()
    assert "## E005:" in content
    assert "2026-02-21: Confirmed from interview notes" in content


def test_migration_is_noop_when_no_inline_history(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    epistemic.parent.mkdir(parents=True, exist_ok=True)
    epistemic.write_text(
        "# Epistemic State\n\n"
        "## E010: claim (believed)\n"
        "**Current position:** still believed.\n"
        "**Agent guidance:** monitor.\n",
    )
    before = epistemic.read_text()

    result = externalize_epistemic_history(epistemic)
    assert result.migrated_entries == 0
    assert result.created_history_files == 0
    assert result.created_current_files == 1
    assert result.appended_blocks == 0
    assert result.migrated_legacy_files == 0
    assert epistemic.read_text() == before
    current_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "current" / "E010.em"
    assert current_file.exists()


def test_bold_history_header_does_not_emit_stray_marker(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    epistemic.parent.mkdir(parents=True, exist_ok=True)
    epistemic.write_text(
        "# Epistemic State\n\n"
        "## E015: bold header parsing (believed)\n"
        "**Current position:** valid.\n"
        "- **History:**\n"
        "- Product Dec 12: validated\n"
        "**Agent guidance:** continue.\n",
    )

    externalize_epistemic_history(epistemic)
    history_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "history" / "E015.em"
    content = history_file.read_text()
    assert "- **" not in content
    assert "- Product Dec 12: validated" in content


def test_unknown_bold_field_after_history_is_preserved(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    epistemic.parent.mkdir(parents=True, exist_ok=True)
    epistemic.write_text(
        "# Epistemic State\n\n"
        "## E020: keep custom field (believed)\n"
        "**Current position:** valid.\n"
        "**History:**\n"
        "- 2026-02-21: validated from timeline\n"
        "**Custom note:** retain in main doc.\n"
        "**Agent guidance:** continue.\n",
    )

    externalize_epistemic_history(epistemic)

    updated = epistemic.read_text()
    assert "**Custom note:** retain in main doc." in updated
    assert "**Agent guidance:** continue." in updated
    assert "**History:**" not in updated

    history_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "history" / "E020.em"
    content = history_file.read_text()
    assert "2026-02-21: validated from timeline" in content
    assert "Custom note" not in content


def test_migration_rerun_preserves_existing_current_file(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    epistemic.parent.mkdir(parents=True, exist_ok=True)
    epistemic.write_text(
        "# Epistemic State\n\n"
        "## E030: preserve current edits (believed)\n"
        "**Current position:** from main doc.\n"
        "**History:**\n"
        "- 2026-02-21: initial\n"
        "**Agent guidance:** baseline.\n",
    )

    first = externalize_epistemic_history(epistemic)
    assert first.created_current_files == 1

    current_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "current" / "E030.em"
    current_file.write_text(
        "## E030: preserve current edits (believed)\n"
        "**Current position:** user-updated current file.\n"
        "**History:**\n"
        "- Evidence@a3e0b731 docs/decisions/timeline.md:10: validated -> believed\n"
        "**Agent guidance:** keep this edit.\n",
    )

    second = externalize_epistemic_history(epistemic)
    assert second.created_current_files == 0
    assert "user-updated current file" in current_file.read_text()


def test_migrates_legacy_epistemic_files_into_split_history(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    epistemic.parent.mkdir(parents=True, exist_ok=True)
    epistemic.write_text(
        "# Epistemic State\n\n"
        "## E040: legacy migration claim (believed)\n"
        "**Current position:** from main file.\n"
        "**Agent guidance:** monitor.\n",
    )

    legacy_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "E040.md"
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text(
        "# Epistemic History\n\n"
        "## E040: legacy migration claim\n\n"
        "- 2026-01-07: imported from legacy file\n",
    )

    result = externalize_epistemic_history(epistemic)
    assert result.migrated_legacy_files == 1
    assert result.created_history_files == 1
    assert result.created_current_files == 1
    assert result.appended_blocks == 1
    assert not legacy_file.exists()

    history_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "history" / "E040.em"
    assert history_file.exists()
    content = history_file.read_text()
    assert "## E040:" in content
    assert "2026-01-07: imported from legacy file" in content


def test_migrates_legacy_file_without_matching_epistemic_section(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    epistemic.parent.mkdir(parents=True, exist_ok=True)
    epistemic.write_text("# Epistemic State\n\n")

    legacy_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "E050.md"
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text(
        "# Epistemic History\n\n"
        "## E050: orphan legacy claim\n\n"
        "- 2026-01-08: carried from legacy-only file\n",
    )

    result = externalize_epistemic_history(epistemic)
    assert result.migrated_entries == 0
    assert result.created_current_files == 0
    assert result.migrated_legacy_files == 1
    assert result.created_history_files == 1
    assert result.appended_blocks == 1
    assert not legacy_file.exists()

    history_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "history" / "E050.em"
    assert history_file.exists()
    content = history_file.read_text()
    assert "## E050: claim" in content
    assert "2026-01-08: carried from legacy-only file" in content

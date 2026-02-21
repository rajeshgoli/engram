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
    assert result.created_files == 1
    assert result.appended_blocks == 1

    updated = epistemic.read_text()
    assert "**History:**" not in updated
    assert "Ground truth annotation > voting (believed)" in updated

    history_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "E005.md"
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
    assert result.created_files == 0
    assert result.appended_blocks == 0
    assert epistemic.read_text() == before


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
    history_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "E015.md"
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

    history_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "E020.md"
    content = history_file.read_text()
    assert "2026-02-21: validated from timeline" in content
    assert "Custom note" not in content

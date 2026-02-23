"""Path inference helpers for split per-ID epistemic files."""

from __future__ import annotations

from pathlib import Path

from engram.epistemic_history import (
    detect_epistemic_layout,
    infer_current_path,
    infer_history_candidates,
    infer_history_path,
)


def test_infers_split_current_and_history_paths(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    assert infer_current_path(epistemic, "E005") == (
        tmp_path / "docs" / "decisions" / "epistemic_state" / "current" / "E005.md"
    )
    assert infer_history_path(epistemic, "E005") == (
        tmp_path / "docs" / "decisions" / "epistemic_state" / "history" / "E005.md"
    )


def test_history_candidates_include_legacy_fallback(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    assert infer_history_candidates(epistemic, "E123") == [
        tmp_path / "docs" / "decisions" / "epistemic_state" / "history" / "E123.md",
    ]


def test_detect_layout_defaults_to_split(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    layout = detect_epistemic_layout(epistemic)
    assert layout.mode == "split"
    assert layout.file_glob == "E*.md"
    assert layout.current_dir == (
        tmp_path / "docs" / "decisions" / "epistemic_state" / "current"
    )


def test_detect_layout_stays_split_even_when_legacy_files_exist(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    legacy_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "E005.md"
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text("## E005: legacy\n")

    layout = detect_epistemic_layout(epistemic)
    assert layout.mode == "split"
    assert layout.current_dir == (
        tmp_path / "docs" / "decisions" / "epistemic_state" / "current"
    )
    assert layout.file_glob == "E*.md"
    assert layout.history_dir == (
        tmp_path / "docs" / "decisions" / "epistemic_state" / "history"
    )

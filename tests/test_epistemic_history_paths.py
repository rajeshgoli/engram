"""Path inference helpers for split per-ID epistemic files."""

from __future__ import annotations

from pathlib import Path

from engram.epistemic_history import (
    infer_current_path,
    infer_history_candidates,
    infer_history_path,
    infer_legacy_history_path,
)


def test_infers_split_current_and_history_paths(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    assert infer_current_path(epistemic, "E005") == (
        tmp_path / "docs" / "decisions" / "epistemic_state" / "current" / "E005.em"
    )
    assert infer_history_path(epistemic, "E005") == (
        tmp_path / "docs" / "decisions" / "epistemic_state" / "history" / "E005.em"
    )


def test_history_candidates_include_legacy_fallback(tmp_path: Path) -> None:
    epistemic = tmp_path / "docs" / "decisions" / "epistemic_state.md"
    assert infer_history_candidates(epistemic, "E123") == [
        tmp_path / "docs" / "decisions" / "epistemic_state" / "history" / "E123.em",
        infer_legacy_history_path(epistemic, "E123"),
    ]

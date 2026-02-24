"""Tests for engram.config."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from engram.config import (
    DEFAULTS,
    ConfigError,
    _deep_merge,
    _validate,
    load_config,
    resolve_doc_paths,
)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Create a minimal .engram/config.yaml in tmp_path."""
    engram_dir = tmp_path / ".engram"
    engram_dir.mkdir()
    config = {
        "living_docs": {
            "timeline": "docs/timeline.md",
            "concepts": "docs/concept_registry.md",
            "epistemic": "docs/epistemic_state.md",
            "workflows": "docs/workflow_registry.md",
        },
        "graveyard": {
            "concepts": "docs/concept_graveyard.md",
            "epistemic": "docs/epistemic_graveyard.md",
        },
    }
    (engram_dir / "config.yaml").write_text(yaml.dump(config))
    return tmp_path


class TestDeepMerge:
    def test_flat_merge(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        assert _deep_merge(base, override) == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self) -> None:
        base = {"x": {"a": 1, "b": 2}}
        override = {"x": {"b": 3}}
        assert _deep_merge(base, override) == {"x": {"a": 1, "b": 3}}

    def test_override_replaces_non_dict(self) -> None:
        base = {"x": {"a": 1}}
        override = {"x": "flat"}
        assert _deep_merge(base, override) == {"x": "flat"}

    def test_does_not_mutate_base(self) -> None:
        base = {"a": {"b": 1}}
        _deep_merge(base, {"a": {"b": 2}})
        assert base["a"]["b"] == 1


class TestValidate:
    def test_valid_config(self) -> None:
        config = _deep_merge(DEFAULTS, {})
        _validate(config)  # Should not raise

    def test_missing_living_docs(self) -> None:
        config = _deep_merge(DEFAULTS, {})
        config["living_docs"] = {"timeline": "t.md"}  # Replace, not merge
        with pytest.raises(ConfigError, match="living_docs.*missing"):
            _validate(config)

    def test_living_docs_not_dict(self) -> None:
        config = _deep_merge(DEFAULTS, {"living_docs": "bad"})
        with pytest.raises(ConfigError, match="living_docs.*mapping"):
            _validate(config)

    def test_missing_graveyard(self) -> None:
        config = _deep_merge(DEFAULTS, {})
        config["graveyard"] = {"concepts": "c.md"}  # Replace, not merge
        with pytest.raises(ConfigError, match="graveyard.*missing"):
            _validate(config)

    def test_unsupported_session_format(self) -> None:
        config = _deep_merge(DEFAULTS, {
            "sources": {"sessions": {"format": "vscode"}}
        })
        with pytest.raises(ConfigError, match="Unsupported session format"):
            _validate(config)

    def test_claude_code_format_accepted(self) -> None:
        config = _deep_merge(DEFAULTS, {
            "sources": {"sessions": {"format": "claude-code"}}
        })
        _validate(config)  # Should not raise

    def test_codex_format_accepted(self) -> None:
        config = _deep_merge(DEFAULTS, {
            "sources": {"sessions": {"format": "codex"}}
        })
        _validate(config)  # Should not raise


class TestLoadConfig:
    def test_loads_and_merges_defaults(self, project_dir: Path) -> None:
        config = load_config(project_dir)
        # User-specified values present
        assert config["living_docs"]["timeline"] == "docs/timeline.md"
        # Defaults filled in
        assert config["model"] == "sonnet"
        assert config["budget"]["context_limit_chars"] == 600_000
        assert config["budget"]["living_docs_budget_mode"] == "index_headings"
        assert config["thresholds"]["orphan_triage"] == 50
        assert config["thresholds"]["stale_epistemic_days"] == 90

    def test_codex_format_gets_codex_default_path(self, tmp_path: Path) -> None:
        engram_dir = tmp_path / ".engram"
        engram_dir.mkdir()
        config = {
            "living_docs": {
                "timeline": "docs/timeline.md",
                "concepts": "docs/concept_registry.md",
                "epistemic": "docs/epistemic_state.md",
                "workflows": "docs/workflow_registry.md",
            },
            "graveyard": {
                "concepts": "docs/concept_graveyard.md",
                "epistemic": "docs/epistemic_graveyard.md",
            },
            "sources": {
                "sessions": {"format": "codex"},
            },
        }
        (engram_dir / "config.yaml").write_text(yaml.dump(config))

        loaded = load_config(tmp_path)
        assert loaded["sources"]["sessions"]["format"] == "codex"
        assert loaded["sources"]["sessions"]["path"] == "~/.codex/history.jsonl"

    def test_missing_config_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="Config not found"):
            load_config(tmp_path)

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        engram_dir = tmp_path / ".engram"
        engram_dir.mkdir()
        (engram_dir / "config.yaml").write_text("just a string")
        with pytest.raises(ConfigError, match="YAML mapping"):
            load_config(tmp_path)


class TestResolveDocPaths:
    def test_resolves_all_paths(self, project_dir: Path) -> None:
        config = load_config(project_dir)
        paths = resolve_doc_paths(config, project_dir)
        assert paths["timeline"] == project_dir / "docs" / "timeline.md"
        assert paths["concepts"] == project_dir / "docs" / "concept_registry.md"
        assert paths["epistemic"] == project_dir / "docs" / "epistemic_state.md"
        assert paths["workflows"] == project_dir / "docs" / "workflow_registry.md"
        assert paths["concept_graveyard"] == project_dir / "docs" / "concept_graveyard.md"
        assert paths["epistemic_graveyard"] == project_dir / "docs" / "epistemic_graveyard.md"

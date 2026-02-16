"""Load and validate .engram/config.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


# Default config values
DEFAULTS: dict[str, Any] = {
    "living_docs": {
        "timeline": "docs/decisions/timeline.md",
        "concepts": "docs/decisions/concept_registry.md",
        "epistemic": "docs/decisions/epistemic_state.md",
        "workflows": "docs/decisions/workflow_registry.md",
    },
    "graveyard": {
        "concepts": "docs/decisions/concept_graveyard.md",
        "epistemic": "docs/decisions/epistemic_graveyard.md",
    },
    "briefing": {
        "file": "CLAUDE.md",
        "section": "## Project Knowledge Briefing",
    },
    "sources": {
        "issues": "local_data/issues/",
        "docs": ["docs/working/", "docs/archive/", "docs/specs/"],
        "sessions": {
            "format": "claude-code",
            "path": "~/.claude/history.jsonl",
            "project_match": [],
        },
    },
    "thresholds": {
        "orphan_triage": 50,
        "contested_review_days": 14,
        "stale_unverified_days": 30,
        "workflow_repetition": 3,
    },
    "budget": {
        "context_limit_chars": 600_000,
        "instructions_overhead": 10_000,
        "max_chunk_chars": 200_000,
    },
    "model": "sonnet",
}

REQUIRED_LIVING_DOC_KEYS = {"timeline", "concepts", "epistemic", "workflows"}
REQUIRED_GRAVEYARD_KEYS = {"concepts", "epistemic"}


class ConfigError(Exception):
    """Raised when config is invalid or missing."""


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base recursively. Override wins on conflicts."""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _validate(config: dict) -> None:
    """Validate required fields in config."""
    living_docs = config.get("living_docs")
    if not isinstance(living_docs, dict):
        raise ConfigError("'living_docs' must be a mapping")
    missing = REQUIRED_LIVING_DOC_KEYS - set(living_docs.keys())
    if missing:
        raise ConfigError(f"'living_docs' missing required keys: {sorted(missing)}")

    graveyard = config.get("graveyard")
    if not isinstance(graveyard, dict):
        raise ConfigError("'graveyard' must be a mapping")
    missing = REQUIRED_GRAVEYARD_KEYS - set(graveyard.keys())
    if missing:
        raise ConfigError(f"'graveyard' missing required keys: {sorted(missing)}")

    sessions = config.get("sources", {}).get("sessions", {})
    fmt = sessions.get("format", "claude-code")
    if fmt not in ("claude-code",):
        raise ConfigError(
            f"Unsupported session format '{fmt}'. Built-in: claude-code."
        )


def load_config(project_root: Path | None = None) -> dict:
    """Load config from .engram/config.yaml under project_root.

    Falls back to cwd if project_root is None. Merges with DEFAULTS
    so callers always get a full config dict.
    """
    root = Path(project_root) if project_root else Path.cwd()
    config_path = root / ".engram" / "config.yaml"

    if not config_path.exists():
        raise ConfigError(f"Config not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ConfigError(f"Config must be a YAML mapping, got {type(raw).__name__}")

    config = _deep_merge(DEFAULTS, raw)
    _validate(config)
    return config


def resolve_doc_paths(config: dict, project_root: Path) -> dict[str, Path]:
    """Resolve all living doc + graveyard paths relative to project_root.

    Returns a flat dict: {timeline: Path, concepts: Path, ...,
    concept_graveyard: Path, epistemic_graveyard: Path}.
    """
    paths = {}
    for key, rel in config["living_docs"].items():
        paths[key] = project_root / rel
    paths["concept_graveyard"] = project_root / config["graveyard"]["concepts"]
    paths["epistemic_graveyard"] = project_root / config["graveyard"]["epistemic"]
    return paths

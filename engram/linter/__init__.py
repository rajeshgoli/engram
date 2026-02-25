"""Schema linter + invariant checks for engram living docs.

Main entry point: ``lint()`` validates all living docs and graveyard files,
returning a ``LintResult`` with pass/fail and a list of violations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engram.linter.guards import (
    check_fold_chunk_delta_documentation,
    check_diff_size,
    check_id_compliance,
    check_missing_sections,
)
from engram.linter.refs import validate_cross_references, validate_no_duplicate_ids
from engram.linter.schema import (
    Violation,
    validate_concept_registry,
    validate_epistemic_state,
    validate_timeline,
    validate_workflow_registry,
)


@dataclass
class LintResult:
    """Result of linting living docs."""

    passed: bool
    violations: list[Violation] = field(default_factory=list)

    def __repr__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"LintResult({status}, {len(self.violations)} violations)"


def lint(
    living_docs: dict[str, str],
    graveyard_docs: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
    doc_paths: dict[str, Path] | None = None,
) -> LintResult:
    """Validate all living docs against schema rules.

    Parameters
    ----------
    living_docs:
        Mapping of doc_type → content string.
        Expected keys: ``concepts``, ``epistemic``, ``workflows``, ``timeline``.
    graveyard_docs:
        Optional mapping: ``concept_graveyard`` → content, ``epistemic_graveyard`` → content.
    config:
        Optional config dict (currently unused, reserved for future threshold overrides).

    Returns
    -------
    LintResult
        ``.passed`` is True if no violations, False otherwise.
        ``.violations`` contains all found violations.
    """
    violations: list[Violation] = []

    # Schema validation per doc type
    if "concepts" in living_docs:
        violations.extend(validate_concept_registry(living_docs["concepts"]))
    if "epistemic" in living_docs:
        epistemic_path = doc_paths.get("epistemic") if doc_paths else None
        violations.extend(validate_epistemic_state(living_docs["epistemic"], epistemic_path))
    if "workflows" in living_docs:
        violations.extend(validate_workflow_registry(living_docs["workflows"]))
    if "timeline" in living_docs:
        violations.extend(validate_timeline(living_docs["timeline"]))

    # Cross-reference validation (needs all docs combined)
    all_contents: dict[str, str] = dict(living_docs)
    if graveyard_docs:
        all_contents.update(graveyard_docs)

    violations.extend(validate_no_duplicate_ids(all_contents))
    violations.extend(validate_cross_references(all_contents))

    return LintResult(passed=len(violations) == 0, violations=violations)


def lint_post_dispatch(
    before_contents: dict[str, str],
    after_contents: dict[str, str],
    graveyard_docs: dict[str, str] | None = None,
    pre_assigned_ids: list[str] | None = None,
    expected_growth: int = 0,
    config: dict[str, Any] | None = None,
    project_root: Path | None = None,
    chunk_type: str | None = None,
) -> LintResult:
    """Full post-dispatch validation: schema + refs + guards.

    Runs all schema and cross-reference checks on the after state,
    plus guard checks comparing before/after.

    Parameters
    ----------
    before_contents:
        Living doc contents before dispatch.
    after_contents:
        Living doc contents after dispatch.
    graveyard_docs:
        Graveyard file contents (after dispatch).
    pre_assigned_ids:
        IDs pre-assigned in the chunk input.
    expected_growth:
        Expected char growth for this chunk.
    config:
        Optional config dict.
    """
    doc_paths = None
    if project_root is not None and config is not None:
        from engram.config import resolve_doc_paths
        doc_paths = resolve_doc_paths(config, project_root)

    # Run standard lint on after state
    result = lint(after_contents, graveyard_docs, config, doc_paths=doc_paths)
    violations = list(result.violations)

    # Guard checks
    if expected_growth > 0:
        before_total = sum(len(c) for c in before_contents.values())
        after_total = sum(len(c) for c in after_contents.values())
        violations.extend(check_diff_size(before_total, after_total, expected_growth))

    violations.extend(check_missing_sections(before_contents, after_contents))
    if chunk_type == "fold":
        violations.extend(check_fold_chunk_delta_documentation(before_contents, after_contents))

    if pre_assigned_ids:
        violations.extend(check_id_compliance(
            after_contents, pre_assigned_ids, before_contents,
        ))

    return LintResult(passed=len(violations) == 0, violations=violations)


def lint_from_paths(
    project_root: Path,
    config: dict[str, Any],
) -> LintResult:
    """Load docs from filesystem and lint them.

    Convenience wrapper for CLI use. Reads file contents from paths
    specified in config.
    """
    from engram.config import resolve_doc_paths

    paths = resolve_doc_paths(config, project_root)

    living_docs: dict[str, str] = {}
    for key in ("timeline", "concepts", "epistemic", "workflows"):
        path = paths[key]
        if path.exists():
            living_docs[key] = path.read_text()

    graveyard_docs: dict[str, str] = {}
    for key, path_key in [("concept_graveyard", "concept_graveyard"),
                          ("epistemic_graveyard", "epistemic_graveyard")]:
        path = paths[path_key]
        if path.exists():
            graveyard_docs[key] = path.read_text()

    return lint(living_docs, graveyard_docs, config, doc_paths=paths)

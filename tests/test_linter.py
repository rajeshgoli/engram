"""Tests for engram.linter — schema validation + invariant checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.linter import LintResult, lint, lint_post_dispatch
from engram.linter.guards import (
    check_diff_size,
    check_id_compliance,
    check_missing_sections,
)
from engram.linter.refs import validate_cross_references, validate_no_duplicate_ids
from engram.linter.schema import (
    Violation,
    validate_concept_registry,
    validate_epistemic_state,
    validate_workflow_registry,
)


# ======================================================================
# Fixtures: example doc contents
# ======================================================================

VALID_CONCEPTS = """\
# Concept Registry

## C001: LegDetector (ACTIVE)
- **Code:** `leg_detector.py`
- **Issues:** #100

## C002: SignalGate (ACTIVE — HAS DEBT)
- **Code:** `signal_gate.py`, `signal_resolver.py`
- **Issues:** #200
- **Debt:** Possible duplication

## C003: proximity_pruning (DEAD) → concept_graveyard.md#C003
"""

VALID_EPISTEMIC = """\
# Epistemic State

## E001: 74% win rate claim (believed)
**Evidence:**
- #1343: "74% WR" → CURRENT BASELINE

## E002: tick-level edge (contested)
**History:**
- Initially claimed at 69.7%
- Under review pending broker data

## E003: old debunked claim (refuted) → epistemic_graveyard.md#E003
"""

VALID_WORKFLOWS = """\
# Workflow Registry

## W001: Investigation Protocol (CURRENT)
- **Context:** How agents investigate bugs
- **Current method:** Deterministic debugging — trace against real data

## W002: Agent Handoff (CURRENT — HAS DEBT)
- **Context:** How work transfers between sessions
- **Trigger for change:** #1404 — ambiguous handoffs caused delays
- **Debt:** No schema for handoff messages

## W003: Manual Code Review (SUPERSEDED) → W001
"""

VALID_TIMELINE = """\
# Timeline

## Phase: Early Architecture (2024-Q4)

Built initial swing detection with LegDetector (C001). Single-timeframe.

## Phase: Signal Rework (2025-Q1)

Replaced proximity pruning (C003 DEAD) with structure-driven approach.
"""

VALID_CONCEPT_GRAVEYARD = """\
# Concept Graveyard

## C003: proximity_pruning (DEAD)
- **Died:** ~#1100s era
- **Replaced by:** C001 structure-driven pruning
"""

VALID_EPISTEMIC_GRAVEYARD = """\
# Epistemic Graveyard

## E003: old debunked claim (REFUTED)
- **Claimed in:** #1000
- **Refuted by:** #1100
"""


# ======================================================================
# Schema: concept_registry
# ======================================================================

class TestConceptRegistrySchema:
    def test_valid_doc_no_violations(self) -> None:
        assert validate_concept_registry(VALID_CONCEPTS) == []

    def test_active_missing_code_field(self) -> None:
        doc = """\
## C001: LegDetector (ACTIVE)
- **Issues:** #100
"""
        violations = validate_concept_registry(doc)
        assert len(violations) == 1
        assert violations[0].entry_id == "C001"
        assert "Code:" in violations[0].message

    def test_stub_is_valid_without_fields(self) -> None:
        doc = "## C012: old_thing (DEAD) → concept_graveyard.md#C012\n"
        assert validate_concept_registry(doc) == []

    def test_evolved_stub(self) -> None:
        doc = "## C005: gateway (EVOLVED → C010) → concept_graveyard.md#C005\n"
        assert validate_concept_registry(doc) == []

    def test_active_with_modifier(self) -> None:
        doc = """\
## C010: SignalGate (ACTIVE — RATIONALE QUESTIONED)
- **Code:** `signal_gate.py`
"""
        assert validate_concept_registry(doc) == []

    def test_invalid_heading_pattern(self) -> None:
        doc = """\
## C001: LegDetector (UNKNOWN_STATUS)
- **Code:** `leg_detector.py`
"""
        violations = validate_concept_registry(doc)
        assert len(violations) == 1
        assert "does not match" in violations[0].message

    def test_non_concept_id_in_concept_doc(self) -> None:
        doc = """\
## E001: wrong_type (ACTIVE)
- **Code:** `something.py`
"""
        violations = validate_concept_registry(doc)
        assert len(violations) == 1
        assert "Non-concept" in violations[0].message

    def test_empty_doc(self) -> None:
        assert validate_concept_registry("# Concept Registry\n") == []

    def test_code_field_various_formats(self) -> None:
        """Code: field can appear with or without bold markers."""
        for fmt in ["Code:", "**Code:**", "- **Code:**", "- Code:"]:
            doc = f"## C001: test (ACTIVE)\n{fmt} `file.py`\n"
            violations = validate_concept_registry(doc)
            assert violations == [], f"Failed for format: {fmt}"


# ======================================================================
# Schema: epistemic_state
# ======================================================================

class TestEpistemicStateSchema:
    def test_valid_doc_no_violations(self) -> None:
        assert validate_epistemic_state(VALID_EPISTEMIC) == []

    def test_believed_missing_evidence_and_history(self) -> None:
        doc = """\
## E001: some claim (believed)
Just a statement with no evidence chain.
"""
        violations = validate_epistemic_state(doc)
        assert len(violations) == 1
        assert violations[0].entry_id == "E001"
        assert "Evidence:" in violations[0].message or "History:" in violations[0].message

    def test_contested_with_history_is_valid(self) -> None:
        doc = """\
## E005: disputed thing (contested)
**History:**
- Claimed by #100, disputed by #200
"""
        assert validate_epistemic_state(doc) == []

    def test_unverified_with_evidence_is_valid(self) -> None:
        doc = """\
## E010: untested hypothesis (unverified)
**Evidence:**
- Preliminary data suggests...
"""
        assert validate_epistemic_state(doc) == []

    def test_refuted_stub_is_valid(self) -> None:
        doc = "## E003: debunked claim (refuted) → epistemic_graveyard.md#E003\n"
        assert validate_epistemic_state(doc) == []

    def test_invalid_status(self) -> None:
        doc = """\
## E001: claim (ACTIVE)
**Evidence:** something
"""
        violations = validate_epistemic_state(doc)
        assert len(violations) == 1
        assert "does not match" in violations[0].message

    def test_non_epistemic_id(self) -> None:
        doc = """\
## C001: wrong_prefix (believed)
**Evidence:** something
"""
        violations = validate_epistemic_state(doc)
        assert len(violations) == 1
        assert "Non-epistemic" in violations[0].message

    def test_case_insensitive_status(self) -> None:
        """Status matching is case-insensitive per EPISTEMIC_FULL_RE."""
        doc = """\
## E001: claim (Believed)
**Evidence:** data
"""
        assert validate_epistemic_state(doc) == []

    def test_valid_with_inferred_external_history_file(self, tmp_path: Path) -> None:
        doc = """\
## E005: externalized claim (believed)
**Current position:** still believed.
**Agent guidance:** keep monitoring.
"""
        epistemic_path = tmp_path / "docs" / "decisions" / "epistemic_state.md"
        history_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "E005.md"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history_file.write_text(
            "# Epistemic History\n\n"
            "## E005: externalized claim\n\n"
            "- 2026-02-21: reviewed\n",
        )
        assert validate_epistemic_state(doc, epistemic_path=epistemic_path) == []

    def test_missing_inline_and_inferred_history_file_is_violation(self, tmp_path: Path) -> None:
        doc = """\
## E006: externalized claim missing file (believed)
**Current position:** still believed.
"""
        epistemic_path = tmp_path / "docs" / "decisions" / "epistemic_state.md"
        violations = validate_epistemic_state(doc, epistemic_path=epistemic_path)
        assert len(violations) == 1
        assert "inferred history file not found" in violations[0].message

    def test_inferred_history_file_must_match_entry_id(self, tmp_path: Path) -> None:
        doc = """\
## E007: mismatched history id (believed)
**Current position:** still believed.
"""
        epistemic_path = tmp_path / "docs" / "decisions" / "epistemic_state.md"
        history_file = tmp_path / "docs" / "decisions" / "epistemic_state" / "E007.md"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history_file.write_text(
            "# Epistemic History\n\n"
            "## E999: wrong id\n",
        )
        violations = validate_epistemic_state(doc, epistemic_path=epistemic_path)
        assert len(violations) == 1
        assert "matching heading for E007" in violations[0].message


# ======================================================================
# Schema: workflow_registry
# ======================================================================

class TestWorkflowRegistrySchema:
    def test_valid_doc_no_violations(self) -> None:
        assert validate_workflow_registry(VALID_WORKFLOWS) == []

    def test_current_missing_context(self) -> None:
        doc = """\
## W001: Investigation Protocol (CURRENT)
- **Current method:** Deterministic debugging
"""
        violations = validate_workflow_registry(doc)
        assert len(violations) == 1
        assert violations[0].entry_id == "W001"
        assert "Context:" in violations[0].message

    def test_current_missing_trigger_and_method(self) -> None:
        doc = """\
## W001: Investigation Protocol (CURRENT)
- **Context:** How agents investigate bugs
"""
        violations = validate_workflow_registry(doc)
        assert len(violations) == 1
        assert "Trigger:" in violations[0].message or "Current method:" in violations[0].message

    def test_current_with_trigger_only(self) -> None:
        doc = """\
## W001: Protocol (CURRENT)
- **Context:** How agents work
- **Trigger for change:** #1404
"""
        assert validate_workflow_registry(doc) == []

    def test_current_with_modifier(self) -> None:
        doc = """\
## W002: Handoff (CURRENT — HAS DEBT)
- **Context:** Work transfer
- **Current method:** sm send
"""
        assert validate_workflow_registry(doc) == []

    def test_superseded_stub(self) -> None:
        doc = "## W003: Manual Review (SUPERSEDED) → W001\n"
        assert validate_workflow_registry(doc) == []

    def test_merged_stub(self) -> None:
        doc = "## W004: Partial Review (MERGED) → W001\n"
        assert validate_workflow_registry(doc) == []

    def test_invalid_status(self) -> None:
        doc = """\
## W001: Protocol (ACTIVE)
- **Context:** something
- **Current method:** something
"""
        violations = validate_workflow_registry(doc)
        assert len(violations) == 1
        assert "does not match" in violations[0].message

    def test_non_workflow_id(self) -> None:
        doc = """\
## C001: wrong_prefix (CURRENT)
- **Context:** something
- **Current method:** something
"""
        violations = validate_workflow_registry(doc)
        assert len(violations) == 1
        assert "Non-workflow" in violations[0].message

    def test_missing_both_context_and_method(self) -> None:
        """Two violations: missing Context and missing Trigger/Current method."""
        doc = """\
## W001: Protocol (CURRENT)
Just a description, no required fields.
"""
        violations = validate_workflow_registry(doc)
        assert len(violations) == 2


# ======================================================================
# Cross-reference validation
# ======================================================================

class TestCrossReferences:
    def test_all_refs_resolve(self) -> None:
        contents = {
            "concepts": VALID_CONCEPTS,
            "epistemic": VALID_EPISTEMIC,
            "workflows": VALID_WORKFLOWS,
            "timeline": VALID_TIMELINE,
            "concept_graveyard": VALID_CONCEPT_GRAVEYARD,
            "epistemic_graveyard": VALID_EPISTEMIC_GRAVEYARD,
        }
        violations = validate_cross_references(contents)
        assert violations == []

    def test_unresolved_ref(self) -> None:
        contents = {
            "concepts": "## C001: test (ACTIVE)\n- **Code:** `f.py`\nSee C999\n",
            "epistemic": "",
            "workflows": "",
        }
        violations = validate_cross_references(contents)
        assert len(violations) == 1
        assert "C999" in violations[0].message

    def test_cross_doc_ref_resolves(self) -> None:
        """A workflow referencing a concept ID should resolve."""
        contents = {
            "concepts": "## C001: thing (ACTIVE)\n- **Code:** `f.py`\n",
            "workflows": "## W001: proto (CURRENT)\n- **Context:** Uses C001\n- **Current method:** test\n",
        }
        violations = validate_cross_references(contents)
        assert violations == []

    def test_graveyard_id_resolves_ref(self) -> None:
        """A reference to a graveyard entry should resolve."""
        contents = {
            "concepts": "## C001: alive (ACTIVE)\n- **Code:** `f.py`\nSee C003\n",
            "concept_graveyard": "## C003: dead (DEAD)\nReplaced by C001\n",
        }
        violations = validate_cross_references(contents)
        assert violations == []


# ======================================================================
# Duplicate ID detection
# ======================================================================

class TestDuplicateIds:
    def test_stub_graveyard_pair_not_duplicate(self) -> None:
        """A STUB in living doc + full entry in graveyard is expected, not a dup."""
        contents = {
            "concepts": VALID_CONCEPTS,
            "concept_graveyard": VALID_CONCEPT_GRAVEYARD,
        }
        violations = validate_no_duplicate_ids(contents)
        # C003 is a stub in living + full in graveyard — should NOT be flagged
        assert not any("C003" in v.message for v in violations)

    def test_epistemic_stub_graveyard_pair_not_duplicate(self) -> None:
        """Same for epistemic: refuted stub + graveyard entry is valid."""
        contents = {
            "epistemic": VALID_EPISTEMIC,
            "epistemic_graveyard": VALID_EPISTEMIC_GRAVEYARD,
        }
        violations = validate_no_duplicate_ids(contents)
        assert not any("E003" in v.message for v in violations)

    def test_non_stub_duplicate_with_graveyard_flagged(self) -> None:
        """An ACTIVE entry in living doc + entry in graveyard IS a duplicate."""
        contents = {
            "concepts": "## C001: thing (ACTIVE)\n- **Code:** `f.py`\n",
            "concept_graveyard": "## C001: thing (DEAD)\n- **Died:** yesterday\n",
        }
        violations = validate_no_duplicate_ids(contents)
        assert len(violations) == 1
        assert "C001" in violations[0].message

    def test_duplicate_within_doc(self) -> None:
        doc = """\
## C001: first (ACTIVE)
- **Code:** `a.py`

## C001: second (ACTIVE)
- **Code:** `b.py`
"""
        violations = validate_no_duplicate_ids({"concepts": doc})
        assert len(violations) == 1
        assert "C001" in violations[0].message

    def test_no_false_positives_across_categories(self) -> None:
        """C001 and E001 are different categories — not duplicates."""
        contents = {
            "concepts": "## C001: thing (ACTIVE)\n- **Code:** `f.py`\n",
            "epistemic": "## E001: claim (believed)\n**Evidence:** data\n",
        }
        violations = validate_no_duplicate_ids(contents)
        assert violations == []


# ======================================================================
# Guards: diff size
# ======================================================================

class TestDiffSizeGuard:
    def test_within_bounds(self) -> None:
        assert check_diff_size(1000, 1500, 500) == []

    def test_exactly_2x_passes(self) -> None:
        assert check_diff_size(1000, 2000, 500) == []

    def test_over_2x_flags(self) -> None:
        violations = check_diff_size(1000, 2001, 500)
        assert len(violations) == 1
        assert "Diff size guard" in violations[0].message

    def test_zero_expected_growth_skipped(self) -> None:
        assert check_diff_size(1000, 5000, 0) == []

    def test_shrinkage_passes(self) -> None:
        """Docs getting smaller is fine (compaction)."""
        assert check_diff_size(5000, 4000, 500) == []


# ======================================================================
# Guards: missing sections
# ======================================================================

class TestMissingSections:
    def test_no_missing_sections(self) -> None:
        before = {"concepts": "## C001: a (ACTIVE)\n- **Code:** `f.py`\n"}
        after = {"concepts": "## C001: a (ACTIVE)\n- **Code:** `f.py`\nUpdated.\n"}
        assert check_missing_sections(before, after) == []

    def test_section_removed(self) -> None:
        before = {
            "concepts": (
                "## C001: a (ACTIVE)\n- **Code:** `f.py`\n\n"
                "## C002: b (ACTIVE)\n- **Code:** `g.py`\n"
            ),
        }
        after = {
            "concepts": "## C001: a (ACTIVE)\n- **Code:** `f.py`\n",
        }
        violations = check_missing_sections(before, after)
        assert len(violations) == 1
        assert "C002" in violations[0].message

    def test_section_replaced_by_stub(self) -> None:
        """Converting to stub keeps the ID — not a violation."""
        before = {
            "concepts": "## C001: a (ACTIVE)\n- **Code:** `f.py`\n",
        }
        after = {
            "concepts": "## C001: a (DEAD) → concept_graveyard.md#C001\n",
        }
        assert check_missing_sections(before, after) == []

    def test_missing_doc_type_skipped(self) -> None:
        """If a doc type doesn't exist in before or after, skip it."""
        assert check_missing_sections({}, {"concepts": "## C001: a (ACTIVE)\n"}) == []


# ======================================================================
# Guards: ID compliance
# ======================================================================

class TestIdCompliance:
    def test_all_pre_assigned_present(self) -> None:
        before: dict[str, str] = {"concepts": "", "epistemic": ""}
        after = {
            "concepts": "## C010: new_thing (ACTIVE)\n- **Code:** `f.py`\n",
            "epistemic": "## E005: new_claim (believed)\n**Evidence:** data\n",
        }
        assert check_id_compliance(after, ["C010", "E005"], before) == []

    def test_missing_pre_assigned_id(self) -> None:
        before: dict[str, str] = {"concepts": ""}
        after = {
            "concepts": "## C010: new_thing (ACTIVE)\n- **Code:** `f.py`\n",
        }
        violations = check_id_compliance(after, ["C010", "E005"], before)
        assert len(violations) == 1
        assert "E005" in violations[0].message
        assert "not found in output" in violations[0].message

    def test_agent_invented_id_flagged(self) -> None:
        """IDs in output that weren't pre-assigned and didn't exist before."""
        before: dict[str, str] = {"concepts": ""}
        after = {
            "concepts": (
                "## C010: assigned (ACTIVE)\n- **Code:** `f.py`\n\n"
                "## C099: invented (ACTIVE)\n- **Code:** `g.py`\n"
            ),
        }
        violations = check_id_compliance(after, ["C010"], before)
        assert len(violations) == 1
        assert "C099" in violations[0].message
        assert "Agent-invented" in violations[0].message

    def test_pre_existing_id_not_flagged_as_invented(self) -> None:
        """IDs that existed before dispatch are not agent-invented."""
        before = {
            "concepts": "## C001: old (ACTIVE)\n- **Code:** `old.py`\n",
        }
        after = {
            "concepts": (
                "## C001: old (ACTIVE)\n- **Code:** `old.py`\n\n"
                "## C010: new (ACTIVE)\n- **Code:** `new.py`\n"
            ),
        }
        violations = check_id_compliance(after, ["C010"], before)
        assert violations == []

    def test_empty_pre_assigned_skipped(self) -> None:
        assert check_id_compliance({"concepts": ""}, []) == []

    def test_no_before_contents_still_checks_missing(self) -> None:
        """Without before_contents, invented IDs can't be detected but missing still works."""
        after = {"concepts": ""}
        violations = check_id_compliance(after, ["C010"])
        assert len(violations) == 1
        assert "C010" in violations[0].message


# ======================================================================
# Integration: lint()
# ======================================================================

class TestLintIntegration:
    def test_valid_docs_pass(self) -> None:
        living_docs = {
            "concepts": VALID_CONCEPTS,
            "epistemic": VALID_EPISTEMIC,
            "workflows": VALID_WORKFLOWS,
            "timeline": VALID_TIMELINE,
        }
        graveyard = {
            "concept_graveyard": VALID_CONCEPT_GRAVEYARD,
            "epistemic_graveyard": VALID_EPISTEMIC_GRAVEYARD,
        }
        result = lint(living_docs, graveyard)
        assert result.passed, f"Unexpected violations: {result.violations}"

    def test_missing_code_field_fails(self) -> None:
        living_docs = {
            "concepts": "## C001: test (ACTIVE)\nNo code field.\n",
            "epistemic": "",
            "workflows": "",
            "timeline": "",
        }
        result = lint(living_docs)
        assert not result.passed
        assert any("Code:" in v.message for v in result.violations)

    def test_unresolved_ref_fails(self) -> None:
        living_docs = {
            "concepts": "## C001: test (ACTIVE)\n- **Code:** `f.py`\nSee W999\n",
            "epistemic": "",
            "workflows": "",
            "timeline": "",
        }
        result = lint(living_docs)
        assert not result.passed
        assert any("W999" in v.message for v in result.violations)

    def test_empty_docs_pass(self) -> None:
        """Completely empty docs should pass (no entries to violate)."""
        result = lint({
            "concepts": "# Concept Registry\n",
            "epistemic": "# Epistemic State\n",
            "workflows": "# Workflow Registry\n",
            "timeline": "# Timeline\n",
        })
        assert result.passed

    def test_lint_result_repr(self) -> None:
        result = LintResult(passed=True, violations=[])
        assert "PASS" in repr(result)
        result2 = LintResult(passed=False, violations=[
            Violation("test", None, "bad"),
        ])
        assert "FAIL" in repr(result2)
        assert "1 violations" in repr(result2)


# ======================================================================
# Integration: lint_post_dispatch()
# ======================================================================

class TestLintPostDispatch:
    def test_clean_dispatch(self) -> None:
        before = {
            "concepts": "## C001: a (ACTIVE)\n- **Code:** `f.py`\n",
            "epistemic": "",
            "workflows": "",
            "timeline": "",
        }
        after = {
            "concepts": (
                "## C001: a (ACTIVE)\n- **Code:** `f.py`\n\n"
                "## C002: b (ACTIVE)\n- **Code:** `g.py`\n"
            ),
            "epistemic": "",
            "workflows": "",
            "timeline": "",
        }
        result = lint_post_dispatch(
            before, after,
            pre_assigned_ids=["C002"],
            expected_growth=200,
        )
        assert result.passed

    def test_oversized_diff_flagged(self) -> None:
        before = {
            "concepts": "x" * 1000,
            "epistemic": "",
            "workflows": "",
            "timeline": "",
        }
        after = {
            "concepts": "x" * 5000,
            "epistemic": "",
            "workflows": "",
            "timeline": "",
        }
        result = lint_post_dispatch(
            before, after,
            expected_growth=500,
        )
        assert not result.passed
        assert any("Diff size guard" in v.message for v in result.violations)

    def test_missing_section_flagged(self) -> None:
        before = {
            "concepts": "## C001: a (ACTIVE)\n- **Code:** `f.py`\n",
        }
        after = {
            "concepts": "# Concept Registry\n",
        }
        result = lint_post_dispatch(before, after)
        assert not result.passed
        assert any("C001" in v.message and "missing" in v.message
                    for v in result.violations)

    def test_missing_pre_assigned_id(self) -> None:
        before = {"concepts": "", "epistemic": "", "workflows": "", "timeline": ""}
        after = {
            "concepts": "## C010: new (ACTIVE)\n- **Code:** `f.py`\n",
            "epistemic": "",
            "workflows": "",
            "timeline": "",
        }
        result = lint_post_dispatch(
            before, after,
            pre_assigned_ids=["C010", "E005"],
        )
        assert not result.passed
        assert any("E005" in v.message for v in result.violations)

    def test_agent_invented_id_in_post_dispatch(self) -> None:
        """Agent invents C099 which wasn't pre-assigned or pre-existing."""
        before = {"concepts": "", "epistemic": "", "workflows": "", "timeline": ""}
        after = {
            "concepts": (
                "## C010: assigned (ACTIVE)\n- **Code:** `f.py`\n\n"
                "## C099: rogue (ACTIVE)\n- **Code:** `g.py`\n"
            ),
            "epistemic": "",
            "workflows": "",
            "timeline": "",
        }
        result = lint_post_dispatch(
            before, after,
            pre_assigned_ids=["C010"],
        )
        assert not result.passed
        assert any("C099" in v.message and "Agent-invented" in v.message
                    for v in result.violations)


# ======================================================================
# Violation object
# ======================================================================

class TestViolation:
    def test_repr(self) -> None:
        v = Violation("concepts", "C001", "missing Code: field")
        assert "concepts/C001" in repr(v)
        assert "missing Code:" in repr(v)

    def test_repr_no_id(self) -> None:
        v = Violation("guard", None, "diff too large")
        assert "guard" in repr(v)
        assert "C001" not in repr(v)

    def test_equality(self) -> None:
        v1 = Violation("concepts", "C001", "msg")
        v2 = Violation("concepts", "C001", "msg")
        assert v1 == v2

    def test_inequality(self) -> None:
        v1 = Violation("concepts", "C001", "msg1")
        v2 = Violation("concepts", "C001", "msg2")
        assert v1 != v2

    def test_hash(self) -> None:
        v1 = Violation("concepts", "C001", "msg")
        v2 = Violation("concepts", "C001", "msg")
        assert hash(v1) == hash(v2)
        assert len({v1, v2}) == 1

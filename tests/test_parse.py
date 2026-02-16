"""Tests for engram.parse."""

from __future__ import annotations

from engram.parse import (
    extract_id,
    extract_referenced_ids,
    extract_stub_target,
    is_stub,
    parse_sections,
)


SAMPLE_DOC = """\
# Concept Registry

Code concepts keyed by stable ID.

## C001: LegDetector (ACTIVE)
- **Code:** `leg_detector.py`
- **Issues:** #100

## C002: proximity_pruning (DEAD) → concept_graveyard.md#C002

## C003: SignalGate (EVOLVED → C010)
- **Code:** `signal_gate.py`
- **Issues:** #200
- **Rationale:** documented
"""

TIMELINE_DOC = """\
# Timeline

## Phase: Early Architecture (2024-Q4)

Built initial swing detection with LegDetector (C001). Single-timeframe.

## Phase: Signal Rework (2025-Q1)

Replaced proximity pruning (C002 DEAD) with structure-driven approach (C003).
Epistemic claim E001 refuted during this phase.
"""


class TestParseSections:
    def test_basic_parsing(self) -> None:
        sections = parse_sections(SAMPLE_DOC)
        assert len(sections) == 3

    def test_heading_preserved(self) -> None:
        sections = parse_sections(SAMPLE_DOC)
        assert sections[0]["heading"] == "## C001: LegDetector (ACTIVE)"
        assert sections[1]["heading"] == "## C002: proximity_pruning (DEAD) → concept_graveyard.md#C002"

    def test_status_extraction(self) -> None:
        sections = parse_sections(SAMPLE_DOC)
        assert sections[0]["status"] is None  # ACTIVE is not a lifecycle status
        assert sections[1]["status"] is None  # Stub: (DEAD) not at end of line
        assert sections[2]["status"] == "evolved"

    def test_status_on_non_stub(self) -> None:
        doc = "## C099: old_thing (DEAD)\nContent"
        sections = parse_sections(doc)
        assert sections[0]["status"] == "dead"

    def test_section_text(self) -> None:
        sections = parse_sections(SAMPLE_DOC)
        assert "leg_detector.py" in sections[0]["text"]
        assert "signal_gate.py" in sections[2]["text"]

    def test_line_numbers(self) -> None:
        sections = parse_sections(SAMPLE_DOC)
        # First section starts at the first ## line
        assert sections[0]["start"] < sections[0]["end"]
        # Second section starts after first
        assert sections[1]["start"] == sections[0]["end"]

    def test_empty_doc(self) -> None:
        assert parse_sections("") == []

    def test_no_sections(self) -> None:
        assert parse_sections("# Title\n\nJust preamble text.") == []

    def test_single_section(self) -> None:
        doc = "## C001: test (DEAD)\nContent here"
        sections = parse_sections(doc)
        assert len(sections) == 1
        assert sections[0]["status"] == "dead"

    def test_timeline_phases(self) -> None:
        sections = parse_sections(TIMELINE_DOC)
        assert len(sections) == 2
        # Phase headings have no status annotation
        assert sections[0]["status"] is None
        assert sections[1]["status"] is None


class TestExtractId:
    def test_concept_id(self) -> None:
        assert extract_id("## C042: check_level_hits (ACTIVE)") == "C042"

    def test_epistemic_id(self) -> None:
        assert extract_id("## E007: 2x_pullback edge (CONTESTED)") == "E007"

    def test_workflow_id(self) -> None:
        assert extract_id("## W003: Manual Code Review (SUPERSEDED)") == "W003"

    def test_no_id(self) -> None:
        assert extract_id("## Phase: Early Architecture (2024-Q4)") is None

    def test_stub_id(self) -> None:
        assert extract_id("## C012: proximity_pruning (DEAD) → concept_graveyard.md#C012") == "C012"


class TestIsStub:
    def test_stub(self) -> None:
        assert is_stub("## C012: proximity_pruning (DEAD) → concept_graveyard.md#C012")

    def test_not_stub(self) -> None:
        assert not is_stub("## C042: check_level_hits (ACTIVE)")

    def test_workflow_stub(self) -> None:
        assert is_stub("## W003: Manual Code Review (SUPERSEDED) → W007")


class TestExtractStubTarget:
    def test_concept_stub(self) -> None:
        result = extract_stub_target("## C012: proximity_pruning (DEAD) → concept_graveyard.md#C012")
        assert result == ("C012", "concept_graveyard.md#C012")

    def test_not_stub(self) -> None:
        assert extract_stub_target("## C042: check_level_hits (ACTIVE)") is None


class TestExtractReferencedIds:
    def test_finds_all_types(self) -> None:
        text = "See C042, contradicts E007, replaced W003"
        ids = extract_referenced_ids(text)
        assert ids == {"C042", "E007", "W003"}

    def test_no_ids(self) -> None:
        assert extract_referenced_ids("no ids here") == set()

    def test_deduplicates(self) -> None:
        text = "C042 appears twice: C042"
        assert extract_referenced_ids(text) == {"C042"}

    def test_in_timeline(self) -> None:
        ids = extract_referenced_ids(TIMELINE_DOC)
        assert "C001" in ids
        assert "C002" in ids
        assert "C003" in ids
        assert "E001" in ids

"""Shared markdown parsing utilities.

Foundation module used by linter and compaction. Ported from v2
(fractal-market-simulator/scripts/knowledge_fold.py).
"""

from __future__ import annotations

import re
from typing import TypedDict


class Section(TypedDict):
    """A parsed H2 section from a markdown document."""
    heading: str
    status: str | None
    start: int
    end: int
    text: str


# Matches status annotations in headings like "## Name (DEAD)" or "## Name (EVOLVED → C089)"
STATUS_RE = re.compile(r'\((DEAD|refuted|EVOLVED[^)]*|CONTESTED|believed|unverified'
                        r'|CURRENT|SUPERSEDED[^)]*|MERGED[^)]*)\)\s*$')

# Matches stable ID prefixes: C042, E007, W003
STABLE_ID_RE = re.compile(r'^##\s+([CEW]\d{3}):\s+')

# Matches graveyard pointer stubs: "## C012: name (DEAD) → concept_graveyard.md#C012"
STUB_RE = re.compile(r'^##\s+([CEW]\d{3}):.+→\s+(\S+)$')

# Matches phase headings in timeline: "## Phase: Name (Period)"
PHASE_RE = re.compile(r'^##\s+Phase:\s+(.+)$')


def parse_sections(content: str) -> list[Section]:
    """Parse a markdown doc into H2 sections with their status.

    Returns list of Section dicts: {heading, status, start, end, text}.
    """
    sections: list[Section] = []
    lines = content.split("\n")
    current: dict | None = None

    for i, line in enumerate(lines):
        if line.startswith("## "):
            if current:
                current["end"] = i
                current["text"] = "\n".join(lines[current["start"]:i])
                sections.append(current)  # type: ignore[arg-type]

            status = None
            m = STATUS_RE.search(line)
            if m:
                status = m.group(1).split()[0].lower()
            current = {"heading": line, "status": status, "start": i, "end": None}

        elif current is None and line.strip():
            # Preamble before first section — skip
            pass

    if current:
        current["end"] = len(lines)
        current["text"] = "\n".join(lines[current["start"]:])
        sections.append(current)  # type: ignore[arg-type]

    return sections


def extract_id(heading: str) -> str | None:
    """Extract stable ID (e.g., 'C042') from an H2 heading line."""
    m = STABLE_ID_RE.match(heading)
    return m.group(1) if m else None


def is_stub(heading: str) -> bool:
    """Check if a heading is a graveyard pointer stub."""
    return bool(STUB_RE.match(heading))


def extract_stub_target(heading: str) -> tuple[str, str] | None:
    """Extract (id, target_file) from a stub heading.

    Returns None if heading is not a stub.
    """
    m = STUB_RE.match(heading)
    if not m:
        return None
    return m.group(1), m.group(2)


def extract_referenced_ids(text: str) -> set[str]:
    """Find all stable ID references (C###, E###, W###) in text."""
    return set(re.findall(r'\b([CEW]\d{3})\b', text))

"""FULL vs STUB heading validation per doc type.

Schema rules (from engram_idea.md):

concept_registry:
  FULL (ACTIVE) requires Code:.
  STUB (DEAD|EVOLVED) → pointer only.

epistemic_state:
  FULL (believed|contested|unverified) requires Evidence: or History:.
  If a FULL believed/unverified entry includes an "Epistemic audit" history
  marker, it must include at least one claim-specific `Evidence@<commit>` line.
  Generic "reaffirmed -> believed" lines are invalid.
  STUB (refuted) → pointer only.

workflow_registry:
  FULL (CURRENT) requires Context: + (Trigger: or Current method:).
  STUB (SUPERSEDED|MERGED) → pointer only.
"""

from __future__ import annotations

import re
from pathlib import Path

from engram.epistemic_history import infer_history_path
from engram.parse import Section, extract_id, is_stub, parse_sections


# -- Heading patterns per doc type -----------------------------------------

# concept_registry: ## C{NNN}: {name} (ACTIVE[ — {MODIFIER}])
#                   ## C{NNN}: {name} (DEAD|EVOLVED...) → {target}
CONCEPT_FULL_RE = re.compile(
    r'^##\s+C\d{3,}:\s+.+\(ACTIVE(?:\s*—\s*.+)?\)\s*$'
)
CONCEPT_STUB_RE = re.compile(
    r'^##\s+C\d{3,}:\s+.+\((?:DEAD|EVOLVED[^)]*)\)\s*→\s*\S+'
)

# epistemic_state: ## E{NNN}: {name} (believed|contested|unverified)
#                  ## E{NNN}: {name} (refuted) → {target}
EPISTEMIC_FULL_RE = re.compile(
    r'^##\s+E\d{3,}:\s+.+\((?:believed|contested|unverified)\)\s*$',
    re.IGNORECASE,
)
EPISTEMIC_STUB_RE = re.compile(
    r'^##\s+E\d{3,}:\s+.+\(refuted\)\s*→\s*\S+',
    re.IGNORECASE,
)

# workflow_registry: ## W{NNN}: {name} (CURRENT[ — {MODIFIER}])
#                    ## W{NNN}: {name} (SUPERSEDED|MERGED...) → {target}
WORKFLOW_FULL_RE = re.compile(
    r'^##\s+W\d{3,}:\s+.+\(CURRENT(?:\s*—\s*.+)?\)\s*$'
)
WORKFLOW_STUB_RE = re.compile(
    r'^##\s+W\d{3,}:\s+.+\((?:SUPERSEDED|MERGED)[^)]*\)\s*→\s*\S+'
)

# Legacy compacted headings (no stable ID) should not remain in living docs.
LEGACY_COMPACTED_DEAD_RE = re.compile(
    r'^##\s+.+\(\s*DEAD\s*\)\s+—\s+\*compacted\*\s*$',
    re.IGNORECASE,
)
LEGACY_COMPACTED_REFUTED_RE = re.compile(
    r'^##\s+.+\(\s*REFUTED\s*\)\s+—\s+\*compacted\*\s*$',
    re.IGNORECASE,
)

# Required field patterns (bold markdown fields inside a section body)
_CODE_RE = re.compile(r'^\s*-?\s*\*?\*?Code\*?\*?:', re.MULTILINE)
_EVIDENCE_RE = re.compile(r'^\s*-?\s*\*?\*?Evidence\*?\*?:', re.MULTILINE)
_HISTORY_RE = re.compile(r'^\s*-?\s*\*?\*?History\*?\*?:', re.MULTILINE)
_EVIDENCE_AT_RE = re.compile(r'^\s*-\s*Evidence@[^\s]+', re.MULTILINE)
_AUDIT_MARKER_RE = re.compile(r'Epistemic\s+audit', re.IGNORECASE)
_REAFFIRMED_BELIEVED_RE = re.compile(r'reaffirmed.*believed', re.IGNORECASE)
_EPISTEMIC_STATUS_RE = re.compile(
    r'\((believed|contested|unverified)\)\s*$',
    re.IGNORECASE,
)
_CONTEXT_RE = re.compile(r'^\s*-?\s*\*?\*?Context\*?\*?:', re.MULTILINE)
_TRIGGER_RE = re.compile(r'^\s*-?\s*\*?\*?Trigger(?:\s+for\s+change)?\*?\*?:', re.MULTILINE)
_CURRENT_METHOD_RE = re.compile(r'^\s*-?\s*\*?\*?Current method\*?\*?:', re.MULTILINE)


class Violation:
    """A single schema violation."""

    __slots__ = ("doc_type", "entry_id", "message")

    def __init__(self, doc_type: str, entry_id: str | None, message: str) -> None:
        self.doc_type = doc_type
        self.entry_id = entry_id
        self.message = message

    def __repr__(self) -> str:
        loc = f"{self.doc_type}"
        if self.entry_id:
            loc += f"/{self.entry_id}"
        return f"Violation({loc}: {self.message})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Violation):
            return NotImplemented
        return (
            self.doc_type == other.doc_type
            and self.entry_id == other.entry_id
            and self.message == other.message
        )

    def __hash__(self) -> int:
        return hash((self.doc_type, self.entry_id, self.message))


def validate_concept_registry(content: str) -> list[Violation]:
    """Validate concept_registry.md schema rules."""
    violations: list[Violation] = []
    sections = parse_sections(content)

    for section in sections:
        heading = section["heading"]
        entry_id = extract_id(heading)

        if not entry_id and LEGACY_COMPACTED_DEAD_RE.match(heading):
            violations.append(Violation(
                "concepts", None,
                "Legacy compacted DEAD heading found in living concept doc; "
                "move it fully to concept_graveyard.md",
            ))
            continue

        if not entry_id:
            continue  # preamble or non-entry section

        if not entry_id.startswith("C"):
            violations.append(Violation(
                "concepts", entry_id,
                f"Non-concept ID '{entry_id}' in concept registry",
            ))
            continue

        if is_stub(heading):
            # STUB — verify heading matches pattern
            if not CONCEPT_STUB_RE.match(heading):
                violations.append(Violation(
                    "concepts", entry_id,
                    "Stub heading does not match expected pattern: "
                    "## C{NNN}: {name} (DEAD|EVOLVED) → {target}",
                ))
            # No field requirements for stubs
            continue

        # FULL — must match ACTIVE pattern
        if not CONCEPT_FULL_RE.match(heading):
            violations.append(Violation(
                "concepts", entry_id,
                "Heading does not match FULL or STUB pattern. "
                "Expected: ## C{NNN}: {name} (ACTIVE[ — MODIFIER]) "
                "or ## C{NNN}: {name} (DEAD|EVOLVED) → target",
            ))
            continue

        # FULL requires Code: field
        body = section["text"]
        if not _CODE_RE.search(body):
            violations.append(Violation(
                "concepts", entry_id,
                "ACTIVE concept missing required 'Code:' field",
            ))

    return violations


def validate_epistemic_state(content: str, epistemic_path: Path | None = None) -> list[Violation]:
    """Validate epistemic_state.md schema rules."""
    violations: list[Violation] = []
    sections = parse_sections(content)

    for section in sections:
        heading = section["heading"]
        entry_id = extract_id(heading)

        if not entry_id and LEGACY_COMPACTED_REFUTED_RE.match(heading):
            violations.append(Violation(
                "epistemic", None,
                "Legacy compacted REFUTED heading found in living epistemic doc; "
                "move it fully to epistemic_graveyard.md",
            ))
            continue

        if not entry_id:
            continue

        if not entry_id.startswith("E"):
            violations.append(Violation(
                "epistemic", entry_id,
                f"Non-epistemic ID '{entry_id}' in epistemic state",
            ))
            continue

        if is_stub(heading):
            if not EPISTEMIC_STUB_RE.match(heading):
                violations.append(Violation(
                    "epistemic", entry_id,
                    "Stub heading does not match expected pattern: "
                    "## E{NNN}: {name} (refuted) → {target}",
                ))
            continue

        # FULL — must match believed|contested|unverified
        if not EPISTEMIC_FULL_RE.match(heading):
            violations.append(Violation(
                "epistemic", entry_id,
                "Heading does not match FULL or STUB pattern. "
                "Expected: ## E{NNN}: {name} (believed|contested|unverified) "
                "or ## E{NNN}: {name} (refuted) → target",
            ))
            continue

        # FULL requires Evidence/History inline OR inferred external history file.
        body = section["text"]
        has_inline = bool(_EVIDENCE_RE.search(body) or _HISTORY_RE.search(body))
        history_sources = [body]
        history_path = infer_history_path(epistemic_path, entry_id) if epistemic_path else None

        if not has_inline and history_path is None:
            violations.append(Violation(
                "epistemic", entry_id,
                "Non-refuted epistemic entry missing required "
                "'Evidence:' or 'History:' field",
            ))
            continue

        if history_path and history_path.exists():
            try:
                external_history = history_path.read_text()
            except OSError:
                violations.append(Violation(
                    "epistemic", entry_id,
                    f"Could not read inferred history file: {history_path}",
                ))
                continue

            # Basic ID integrity guard: history file must contain a heading for this ID.
            if not re.search(rf"^##\s+{re.escape(entry_id)}\b", external_history, re.MULTILINE):
                violations.append(Violation(
                    "epistemic", entry_id,
                    f"Inferred history file does not contain matching heading for {entry_id}: "
                    f"{history_path}",
                ))
            history_sources.append(external_history)
        elif not has_inline:
            violations.append(Violation(
                "epistemic", entry_id,
                "Missing inline 'Evidence:'/'History:' and inferred history file "
                f"not found: {history_path}",
            ))
            continue

        history_source_text = "\n".join(history_sources)

        # Generic reaffirmation language is too weak for epistemic retention.
        if _REAFFIRMED_BELIEVED_RE.search(history_source_text):
            violations.append(Violation(
                "epistemic", entry_id,
                "Generic 'reaffirmed -> believed' history is not allowed; "
                "use claim-specific Evidence@<commit> bullets",
            ))

        # If this entry was touched by an epistemic audit and remains
        # believed/unverified, require claim-specific commit-pinned evidence.
        status_match = _EPISTEMIC_STATUS_RE.search(heading)
        status = status_match.group(1).lower() if status_match else ""
        if (
            status in {"believed", "unverified"}
            and _AUDIT_MARKER_RE.search(history_source_text)
            and not _EVIDENCE_AT_RE.search(history_source_text)
        ):
            violations.append(Violation(
                "epistemic", entry_id,
                "Epistemic-audited believed/unverified entry must include "
                "at least one 'Evidence@<commit>' history bullet",
            ))

    return violations


def validate_workflow_registry(content: str) -> list[Violation]:
    """Validate workflow_registry.md schema rules."""
    violations: list[Violation] = []
    sections = parse_sections(content)

    for section in sections:
        heading = section["heading"]
        entry_id = extract_id(heading)

        if not entry_id:
            continue

        if not entry_id.startswith("W"):
            violations.append(Violation(
                "workflows", entry_id,
                f"Non-workflow ID '{entry_id}' in workflow registry",
            ))
            continue

        if is_stub(heading):
            if not WORKFLOW_STUB_RE.match(heading):
                violations.append(Violation(
                    "workflows", entry_id,
                    "Stub heading does not match expected pattern: "
                    "## W{NNN}: {name} (SUPERSEDED|MERGED) → {target}",
                ))
            continue

        # FULL — must match CURRENT pattern
        if not WORKFLOW_FULL_RE.match(heading):
            violations.append(Violation(
                "workflows", entry_id,
                "Heading does not match FULL or STUB pattern. "
                "Expected: ## W{NNN}: {name} (CURRENT[ — MODIFIER]) "
                "or ## W{NNN}: {name} (SUPERSEDED|MERGED) → target",
            ))
            continue

        # FULL requires Context: + (Trigger: or Current method:)
        body = section["text"]
        if not _CONTEXT_RE.search(body):
            violations.append(Violation(
                "workflows", entry_id,
                "CURRENT workflow missing required 'Context:' field",
            ))
        if not _TRIGGER_RE.search(body) and not _CURRENT_METHOD_RE.search(body):
            violations.append(Violation(
                "workflows", entry_id,
                "CURRENT workflow missing required "
                "'Trigger:' or 'Current method:' field",
            ))

    return violations

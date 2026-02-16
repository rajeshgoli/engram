"""Cross-reference validation.

Ensures every C###/E###/W### reference resolves to an existing entry
and that no duplicate IDs exist within a doc type.
"""

from __future__ import annotations

from engram.parse import extract_id, extract_referenced_ids, parse_sections
from engram.linter.schema import Violation


def validate_no_duplicate_ids(
    contents: dict[str, str],
) -> list[Violation]:
    """Check that no ID appears more than once across its home doc + graveyard.

    Parameters
    ----------
    contents:
        Mapping of doc_type → content string. Expected keys include
        ``concepts``, ``epistemic``, ``workflows``, and optionally
        ``concept_graveyard``, ``epistemic_graveyard``.
    """
    violations: list[Violation] = []

    # Group docs by ID prefix to check within each registry
    registry_groups: dict[str, list[tuple[str, str]]] = {
        "C": [],  # (doc_type, content)
        "E": [],
        "W": [],
    }

    # Living docs
    if "concepts" in contents:
        registry_groups["C"].append(("concepts", contents["concepts"]))
    if "epistemic" in contents:
        registry_groups["E"].append(("epistemic", contents["epistemic"]))
    if "workflows" in contents:
        registry_groups["W"].append(("workflows", contents["workflows"]))

    # Graveyard docs
    if "concept_graveyard" in contents:
        registry_groups["C"].append(("concept_graveyard", contents["concept_graveyard"]))
    if "epistemic_graveyard" in contents:
        registry_groups["E"].append(("epistemic_graveyard", contents["epistemic_graveyard"]))

    for prefix, doc_pairs in registry_groups.items():
        seen: dict[str, str] = {}  # id → first doc_type
        for doc_type, content in doc_pairs:
            for section in parse_sections(content):
                entry_id = extract_id(section["heading"])
                if entry_id and entry_id.startswith(prefix):
                    if entry_id in seen:
                        violations.append(Violation(
                            doc_type, entry_id,
                            f"Duplicate ID '{entry_id}' — "
                            f"also in {seen[entry_id]}",
                        ))
                    else:
                        seen[entry_id] = doc_type

    return violations


def validate_cross_references(
    contents: dict[str, str],
) -> list[Violation]:
    """Check that every C###/E###/W### reference resolves to an existing entry.

    Scans all documents for ID references, then verifies each one exists
    as a heading in its home doc (living or graveyard).

    Parameters
    ----------
    contents:
        Same mapping as ``validate_no_duplicate_ids``.
    """
    violations: list[Violation] = []

    # Build registry of all defined IDs
    defined_ids: set[str] = set()
    for content in contents.values():
        for section in parse_sections(content):
            entry_id = extract_id(section["heading"])
            if entry_id:
                defined_ids.add(entry_id)

    # Home doc mapping for error messages
    home_doc = {"C": "concepts", "E": "epistemic", "W": "workflows"}

    # Scan all docs for references
    for doc_type, content in contents.items():
        referenced = extract_referenced_ids(content)
        for ref_id in sorted(referenced):
            if ref_id not in defined_ids:
                prefix = ref_id[0]
                expected_home = home_doc.get(prefix, "unknown")
                violations.append(Violation(
                    doc_type, None,
                    f"Unresolved reference '{ref_id}' — "
                    f"not found in {expected_home} or its graveyard",
                ))

    return violations

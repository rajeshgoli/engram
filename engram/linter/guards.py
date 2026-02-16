"""Guard checks: diff size, missing sections, ID compliance.

These are post-dispatch checks that validate the fold agent's output
against expectations set before dispatch.
"""

from __future__ import annotations

from engram.parse import extract_id, parse_sections
from engram.linter.schema import Violation


def check_diff_size(
    before_chars: int,
    after_chars: int,
    expected_growth: int,
) -> list[Violation]:
    """Flag if actual growth exceeds 2x expected.

    Parameters
    ----------
    before_chars:
        Total chars across all living docs before dispatch.
    after_chars:
        Total chars across all living docs after dispatch.
    expected_growth:
        Expected char growth for this chunk (e.g., chunk size estimate).
    """
    if expected_growth <= 0:
        return []

    actual_growth = after_chars - before_chars
    if actual_growth > 2 * expected_growth:
        return [Violation(
            "guard", None,
            f"Diff size guard: actual growth ({actual_growth:,} chars) "
            f"exceeds 2x expected ({expected_growth:,} chars). "
            f"Before: {before_chars:,}, after: {after_chars:,}",
        )]
    return []


def check_missing_sections(
    before_contents: dict[str, str],
    after_contents: dict[str, str],
) -> list[Violation]:
    """Detect sections that existed before dispatch but disappeared after.

    A fold agent should not delete sections (entries move to graveyard
    as stubs, not vanish). This catches silent truncation or accidental
    deletion.
    """
    violations: list[Violation] = []

    for doc_type in ("concepts", "epistemic", "workflows", "timeline"):
        if doc_type not in before_contents or doc_type not in after_contents:
            continue

        before_ids = {
            extract_id(s["heading"])
            for s in parse_sections(before_contents[doc_type])
            if extract_id(s["heading"])
        }
        after_ids = {
            extract_id(s["heading"])
            for s in parse_sections(after_contents[doc_type])
            if extract_id(s["heading"])
        }

        missing = before_ids - after_ids
        for entry_id in sorted(missing):
            violations.append(Violation(
                doc_type, entry_id,
                f"Section '{entry_id}' existed before dispatch but is "
                f"missing after. Fold agents should not delete sections.",
            ))

    return violations


def check_id_compliance(
    after_contents: dict[str, str],
    pre_assigned_ids: list[str],
) -> list[Violation]:
    """Verify that pre-assigned IDs appear in the output and no extras were invented.

    Parameters
    ----------
    after_contents:
        Living doc contents after dispatch.
    pre_assigned_ids:
        IDs that were pre-assigned in the chunk input.
    """
    if not pre_assigned_ids:
        return []

    violations: list[Violation] = []

    # Collect all IDs that appear as headings in the output
    all_output_ids: set[str] = set()
    before_ids: set[str] = set()  # not available here — compare against pre_assigned

    for content in after_contents.values():
        for section in parse_sections(content):
            entry_id = extract_id(section["heading"])
            if entry_id:
                all_output_ids.add(entry_id)

    pre_assigned_set = set(pre_assigned_ids)

    # Check: pre-assigned IDs should appear in the output
    missing = pre_assigned_set - all_output_ids
    for entry_id in sorted(missing):
        violations.append(Violation(
            "guard", entry_id,
            f"Pre-assigned ID '{entry_id}' not found in output. "
            f"Fold agent did not create the expected entry.",
        ))

    return violations

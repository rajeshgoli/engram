"""Timeline compaction: collapse old phases to single-paragraph summaries.

When timeline.md exceeds a configurable threshold, phases older than a
cutoff age are collapsed to single-paragraph summaries preserving ID
references. Full narratives are preserved in git history.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from engram.parse import PHASE_RE, extract_referenced_ids, parse_sections

# Default threshold (chars) before compaction triggers
DEFAULT_THRESHOLD_CHARS = 50_000

# Default age cutoff — phases older than this get compacted
DEFAULT_AGE_MONTHS = 6

# Matches date ranges in phase headings like "(Jan 2025 – Jun 2025)" or "(2025-01-15 – 2025-06-30)"
_DATE_RANGE_RE = re.compile(
    r'\(([^)]+)\)\s*$'
)

# Matches common date formats in phase headings
_MONTH_YEAR_RE = re.compile(
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{4})',
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')
_YEAR_MONTH_RE = re.compile(r'(\d{4})-(\d{2})')

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_phase_end_date(heading: str) -> date | None:
    """Extract the end date from a phase heading's date range.

    Tries several common formats. Returns None if no date found.
    """
    range_match = _DATE_RANGE_RE.search(heading)
    if not range_match:
        return None

    date_text = range_match.group(1)

    # Try ISO date (take last one as end date)
    iso_dates = _ISO_DATE_RE.findall(date_text)
    if iso_dates:
        y, m, d = iso_dates[-1]
        return date(int(y), int(m), int(d))

    # Try YYYY-MM format
    ym_dates = _YEAR_MONTH_RE.findall(date_text)
    if ym_dates:
        y, m = ym_dates[-1]
        return date(int(y), int(m), 1)

    # Try "Month YYYY" format
    month_years = _MONTH_YEAR_RE.findall(date_text)
    if month_years:
        year = int(month_years[-1])
        # Find the month name preceding this year
        month_matches = re.finditer(
            r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*',
            date_text,
            re.IGNORECASE,
        )
        month_name = None
        for mm in month_matches:
            month_name = mm.group(1).lower()[:3]
        if month_name and month_name in _MONTH_MAP:
            return date(year, _MONTH_MAP[month_name], 1)

    return None


def _summarize_phase(section_text: str, heading: str) -> str:
    """Collapse a phase section to a single-paragraph summary.

    Preserves the heading and all ID references (C###, E###, W###).
    """
    # Collect all referenced IDs
    ids = extract_referenced_ids(section_text)

    # Extract the body (everything after the heading line)
    lines = section_text.split("\n")
    body_lines = [l for l in lines[1:] if l.strip()]

    # Build a summary from the first few non-empty lines
    summary_lines = []
    char_count = 0
    for line in body_lines:
        # Skip sub-headings (###, ####)
        if line.startswith("#"):
            continue
        # Skip bullet lists that are just ID references
        cleaned = line.strip().lstrip("- ")
        summary_lines.append(cleaned)
        char_count += len(cleaned)
        if char_count > 300:
            break

    summary = " ".join(summary_lines)
    # Truncate to ~300 chars at a word boundary
    if len(summary) > 300:
        summary = summary[:300].rsplit(" ", 1)[0] + "..."

    # Ensure all original IDs are preserved in the summary
    missing_ids = ids - extract_referenced_ids(summary)
    id_suffix = ""
    if missing_ids:
        id_suffix = f" (refs: {', '.join(sorted(missing_ids))})"

    return f"{heading}\n{summary}{id_suffix}"


def compact_timeline(
    content: str,
    threshold_chars: int = DEFAULT_THRESHOLD_CHARS,
    age_months: int = DEFAULT_AGE_MONTHS,
    reference_date: date | None = None,
) -> tuple[str, int]:
    """Collapse old phases in timeline.md to single-paragraph summaries.

    Only triggers if the document exceeds ``threshold_chars``. Phases
    whose end date is more than ``age_months`` before ``reference_date``
    are collapsed. ID references are preserved in summaries.

    Parameters
    ----------
    content:
        Full text of timeline.md.
    threshold_chars:
        Minimum document size before compaction triggers.
    age_months:
        Phases older than this many months get compacted.
    reference_date:
        Date to measure age from. Defaults to today.

    Returns
    -------
    tuple[str, int]
        (new_content, chars_saved). Returns original content unchanged
        if below threshold or no phases qualify for compaction.
    """
    if len(content) < threshold_chars:
        return content, 0

    ref_date = reference_date or date.today()
    cutoff = ref_date - timedelta(days=age_months * 30)

    sections = parse_sections(content)
    if not sections:
        return content, 0

    lines = content.split("\n")
    first_start = sections[0]["start"]
    preamble = "\n".join(lines[:first_start])

    parts = [preamble]
    chars_saved = 0

    for sec in sections:
        is_phase = bool(PHASE_RE.match(sec["heading"]))

        if is_phase:
            end_date = _parse_phase_end_date(sec["heading"])
            if end_date and end_date < cutoff:
                # Collapse to summary — add trailing blank line to maintain
                # valid markdown structure between sections
                summary = _summarize_phase(sec["text"], sec["heading"])
                parts.append(summary + "\n")
                chars_saved += len(sec["text"]) - len(summary) - 1
                continue

        # Keep as-is
        parts.append(sec["text"])

    if chars_saved == 0:
        return content, 0

    new_content = "\n".join(parts)
    return new_content, chars_saved

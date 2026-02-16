"""Tests for engram.fold.sources."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from engram.fold.sources import (
    extract_issue_number,
    git_diff_summary,
    get_doc_git_dates,
    parse_date,
    parse_frontmatter_date,
    pull_issues,
    render_issue_markdown,
)


class TestRenderIssueMarkdown:
    def test_basic_issue(self) -> None:
        issue = {
            "state": "OPEN",
            "labels": [{"name": "bug"}],
            "body": "Something is broken.",
            "comments": [],
        }
        result = render_issue_markdown(issue)
        assert "**State:** OPEN" in result
        assert "**Labels:** bug" in result
        assert "Something is broken." in result

    def test_no_labels(self) -> None:
        issue = {"state": "CLOSED", "labels": [], "body": "Fixed.", "comments": []}
        result = render_issue_markdown(issue)
        assert "**State:** CLOSED" in result
        assert "Labels" not in result

    def test_with_comments(self) -> None:
        issue = {
            "state": "OPEN",
            "labels": [],
            "body": "Main body.",
            "comments": [
                {
                    "author": {"login": "alice"},
                    "createdAt": "2026-02-10T12:00:00Z",
                    "body": "I can confirm this.",
                }
            ],
        }
        result = render_issue_markdown(issue)
        assert "### Comments" in result
        assert "**alice** (2026-02-10):" in result
        assert "I can confirm this." in result

    def test_none_body(self) -> None:
        issue = {"state": "OPEN", "labels": [], "body": None, "comments": []}
        result = render_issue_markdown(issue)
        assert "**State:** OPEN" in result

    def test_multiple_labels(self) -> None:
        issue = {
            "state": "OPEN",
            "labels": [{"name": "bug"}, {"name": "priority"}],
            "body": "",
            "comments": [],
        }
        result = render_issue_markdown(issue)
        assert "bug, priority" in result


class TestParseFrontmatterDate:
    def test_extracts_date(self, tmp_path: Path) -> None:
        doc = tmp_path / "test.md"
        doc.write_text("# Title\n\n**Date:** 2026-02-08\n\nContent here.")
        result = parse_frontmatter_date(doc)
        assert result == "2026-02-08T00:00:00+00:00"

    def test_no_date(self, tmp_path: Path) -> None:
        doc = tmp_path / "test.md"
        doc.write_text("# No date here\n\nJust content.")
        assert parse_frontmatter_date(doc) is None

    def test_date_before_project_start(self, tmp_path: Path) -> None:
        doc = tmp_path / "test.md"
        doc.write_text("**Date:** 2024-01-01\n")
        assert parse_frontmatter_date(doc, project_start="2025-12-10") is None

    def test_date_after_project_start(self, tmp_path: Path) -> None:
        doc = tmp_path / "test.md"
        doc.write_text("**Date:** 2026-01-15\n")
        result = parse_frontmatter_date(doc, project_start="2025-12-10")
        assert result == "2026-01-15T00:00:00+00:00"

    def test_no_project_start_filter(self, tmp_path: Path) -> None:
        doc = tmp_path / "test.md"
        doc.write_text("**Date:** 2020-01-01\n")
        result = parse_frontmatter_date(doc)
        assert result == "2020-01-01T00:00:00+00:00"

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        doc = tmp_path / "nonexistent.md"
        assert parse_frontmatter_date(doc) is None


class TestExtractIssueNumber:
    def test_valid_filename(self, tmp_path: Path) -> None:
        assert extract_issue_number(tmp_path / "1343_backtest_analysis.md") == 1343

    def test_no_number_prefix(self, tmp_path: Path) -> None:
        assert extract_issue_number(tmp_path / "readme.md") is None

    def test_number_not_at_start(self, tmp_path: Path) -> None:
        assert extract_issue_number(tmp_path / "analysis_1343.md") is None


class TestParseDate:
    def test_iso_with_timezone(self) -> None:
        dt = parse_date("2026-02-08T12:00:00-06:00")
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.day == 8

    def test_iso_with_z(self) -> None:
        dt = parse_date("2026-02-08T12:00:00Z")
        assert dt.year == 2026

    def test_date_only_fallback(self) -> None:
        dt = parse_date("2026-02-08")
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.day == 8


class TestPullIssues:
    def test_writes_issue_files(self, tmp_path: Path) -> None:
        mock_issues = [
            {"number": 1, "title": "Bug", "body": "Fix it", "createdAt": "2026-01-01"},
            {"number": 2, "title": "Feature", "body": "Add it", "createdAt": "2026-01-02"},
        ]
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(mock_issues)
        )
        with patch("engram.fold.sources.subprocess.run", return_value=mock_result):
            issues_dir = tmp_path / "issues"
            result = pull_issues("owner/repo", issues_dir)

        assert len(result) == 2
        assert (issues_dir / "1.json").exists()
        assert (issues_dir / "2.json").exists()

        loaded = json.loads((issues_dir / "1.json").read_text())
        assert loaded["title"] == "Bug"


class TestGetDocGitDates:
    def test_returns_dates(self, tmp_path: Path) -> None:
        # Mock git commands returning dates
        def mock_run(cmd, **kwargs):
            if "--diff-filter=A" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout="2026-01-01T00:00:00-06:00\nsome_file.md\n",
                )
            elif "-1" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout="2026-02-01T00:00:00-06:00\n",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="")

        doc = tmp_path / "docs" / "test.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("content")

        with patch("engram.fold.sources.subprocess.run", side_effect=mock_run):
            created, modified = get_doc_git_dates(doc, tmp_path)

        assert created == "2026-01-01T00:00:00-06:00"
        assert modified == "2026-02-01T00:00:00-06:00"

    def test_no_git_history(self, tmp_path: Path) -> None:
        def mock_run(cmd, **kwargs):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="\n")

        doc = tmp_path / "test.md"
        doc.write_text("content")

        with patch("engram.fold.sources.subprocess.run", side_effect=mock_run):
            created, modified = get_doc_git_dates(doc, tmp_path)

        assert created is None
        assert modified is None


class TestGitDiffSummary:
    def test_with_changes(self, tmp_path: Path) -> None:
        mock_output = "A\tsrc/new_file.py\nD\tsrc/old_file.py\nR100\tsrc/a.py\tsrc/b.py\n"
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=mock_output
        )
        with patch("engram.fold.sources.subprocess.run", return_value=mock_result):
            result = git_diff_summary("2026-01-01", "2026-02-01", tmp_path)

        assert "Files created (1)" in result
        assert "`src/new_file.py`" in result
        assert "Files deleted (1)" in result
        assert "`src/old_file.py`" in result
        assert "Files renamed (1)" in result

    def test_no_changes(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=""
        )
        with patch("engram.fold.sources.subprocess.run", return_value=mock_result):
            result = git_diff_summary("2026-01-01", "2026-02-01", tmp_path)

        assert result == ""

    def test_custom_source_dirs(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=""
        )
        with patch("engram.fold.sources.subprocess.run", return_value=mock_result) as mock:
            git_diff_summary(
                "2026-01-01", "2026-02-01", tmp_path,
                source_dirs=["lib/", "app/"],
            )

        # Verify custom dirs were passed to git
        call_args = mock.call_args[0][0]
        assert "lib/" in call_args
        assert "app/" in call_args

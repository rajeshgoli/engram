"""Tests for engram.fold.sources."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from engram.fold.sources import (
    _matches_branch_filter,
    extract_issue_number,
    git_diff_summary,
    get_doc_git_dates,
    parse_date,
    parse_frontmatter_date,
    pull_issues,
    pull_prs,
    render_issue_markdown,
    render_pr_markdown,
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


class TestRenderPrMarkdown:
    def test_basic_pr(self) -> None:
        pr = {
            "baseRefName": "dev",
            "headRefName": "feature/123-fix",
            "additions": 50,
            "deletions": 10,
            "changedFiles": 3,
            "body": "Fixed the bug.",
            "files": [],
            "reviews": [],
            "comments": [],
        }
        result = render_pr_markdown(pr)
        assert "**Merged** into `dev` from `feature/123-fix`" in result
        assert "+50 -10 across 3 files" in result
        assert "Fixed the bug." in result

    def test_with_files(self) -> None:
        pr = {
            "baseRefName": "dev",
            "headRefName": "fix/abc",
            "additions": 20,
            "deletions": 5,
            "changedFiles": 2,
            "body": "",
            "files": [
                {"path": "src/main.py", "additions": 15, "deletions": 3},
                {"path": "tests/test_main.py", "additions": 5, "deletions": 2},
            ],
            "reviews": [],
            "comments": [],
        }
        result = render_pr_markdown(pr)
        assert "### Files changed" in result
        assert "`src/main.py` (+15 -3)" in result
        assert "`tests/test_main.py` (+5 -2)" in result

    def test_with_reviews(self) -> None:
        pr = {
            "baseRefName": "epic/1808",
            "headRefName": "feature/fix",
            "additions": 10,
            "deletions": 0,
            "changedFiles": 1,
            "body": "PR body.",
            "files": [],
            "reviews": [
                {
                    "author": {"login": "reviewer1"},
                    "state": "APPROVED",
                    "body": "LGTM",
                },
                {
                    "author": {"login": "bot"},
                    "state": "COMMENTED",
                    "body": "",  # empty body — should be filtered
                },
            ],
            "comments": [],
        }
        result = render_pr_markdown(pr)
        assert "### Reviews" in result
        assert "**reviewer1** (APPROVED):" in result
        assert "LGTM" in result
        # Empty review body should not appear
        assert "bot" not in result

    def test_with_comments(self) -> None:
        pr = {
            "baseRefName": "dev",
            "headRefName": "feature/x",
            "additions": 1,
            "deletions": 0,
            "changedFiles": 1,
            "body": "",
            "files": [],
            "reviews": [],
            "comments": [
                {
                    "author": {"login": "alice"},
                    "createdAt": "2026-03-10T12:00:00Z",
                    "body": "Can we add a test?",
                }
            ],
        }
        result = render_pr_markdown(pr)
        assert "### Comments" in result
        assert "**alice** (2026-03-10):" in result
        assert "Can we add a test?" in result

    def test_none_body(self) -> None:
        pr = {
            "baseRefName": "dev",
            "headRefName": "fix/y",
            "additions": 0,
            "deletions": 0,
            "changedFiles": 0,
            "body": None,
            "files": [],
            "reviews": [],
            "comments": [],
        }
        result = render_pr_markdown(pr)
        assert "**Merged**" in result


class TestPullPrs:
    def test_writes_pr_files(self, tmp_path: Path) -> None:
        mock_prs = [
            {
                "number": 100,
                "title": "Fix bug",
                "body": "Fixed",
                "createdAt": "2026-03-01T00:00:00Z",
                "mergedAt": "2026-03-02T00:00:00Z",
                "baseRefName": "dev",
                "headRefName": "fix/100",
                "additions": 10,
                "deletions": 2,
                "changedFiles": 1,
                "files": [],
                "reviews": [],
                "comments": [],
            },
        ]
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(mock_prs)
        )
        with patch("engram.fold.sources.subprocess.run", return_value=mock_result):
            prs_dir = tmp_path / "prs"
            result = pull_prs("owner/repo", prs_dir)

        assert len(result) == 1
        assert (prs_dir / "100.json").exists()

    def test_filters_by_base_branch(self, tmp_path: Path) -> None:
        mock_prs = [
            {"number": 1, "baseRefName": "dev", "title": "To dev"},
            {"number": 2, "baseRefName": "epic/1808", "title": "To epic"},
            {"number": 3, "baseRefName": "epic/2040", "title": "To epic 2"},
            {"number": 4, "baseRefName": "main", "title": "To main"},
        ]
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(mock_prs)
        )
        with patch("engram.fold.sources.subprocess.run", return_value=mock_result):
            prs_dir = tmp_path / "prs"
            result = pull_prs("owner/repo", prs_dir, base_branches=["epic/*"])

        assert len(result) == 2
        assert {pr["number"] for pr in result} == {2, 3}

    def test_empty_base_branches_returns_all(self, tmp_path: Path) -> None:
        mock_prs = [
            {"number": 1, "baseRefName": "dev"},
            {"number": 2, "baseRefName": "epic/1808"},
        ]
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(mock_prs)
        )
        with patch("engram.fold.sources.subprocess.run", return_value=mock_result):
            prs_dir = tmp_path / "prs"
            result = pull_prs("owner/repo", prs_dir, base_branches=None)

        assert len(result) == 2


class TestMatchesBranchFilter:
    def test_exact_match(self) -> None:
        assert _matches_branch_filter("dev", ["dev"]) is True

    def test_glob_match(self) -> None:
        assert _matches_branch_filter("epic/1808", ["epic/*"]) is True
        assert _matches_branch_filter("epic/2040", ["epic/*"]) is True

    def test_no_match(self) -> None:
        assert _matches_branch_filter("main", ["dev", "epic/*"]) is False

    def test_multiple_patterns(self) -> None:
        assert _matches_branch_filter("dev", ["dev", "epic/*"]) is True
        assert _matches_branch_filter("epic/1808", ["dev", "epic/*"]) is True


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

    def test_created_date_uses_relative_path_and_follow(self, tmp_path: Path) -> None:
        seen_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            seen_cmds.append(cmd)
            if "--diff-filter=A" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout="2026-01-01T00:00:00-06:00\n",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="2026-02-01T00:00:00-06:00\n",
            )

        doc = tmp_path / "docs" / "nested" / "test.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("content")

        with patch("engram.fold.sources.subprocess.run", side_effect=mock_run):
            get_doc_git_dates(doc, tmp_path)

        created_cmd = next(cmd for cmd in seen_cmds if "--diff-filter=A" in cmd)
        assert "--follow" in created_cmd
        assert "docs/nested/test.md" in created_cmd
        assert "**/test.md" not in created_cmd

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

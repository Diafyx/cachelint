"""Read-only, fail-closed checks for the first PyPI release."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

REPO = "Diafyx/cachelint"
VERSION = "0.1.0a1"
RELEASE_ISSUE = 2
PREPARATION_ISSUE = 10
MILESTONE = 1
REQUIRED_JOBS = {
    "Quality and coverage",
    "Build and smoke test",
    "Test (ubuntu-latest, Python 3.11)",
    "Test (ubuntu-latest, Python 3.12)",
    "Test (ubuntu-latest, Python 3.13)",
    "Test (ubuntu-latest, Python 3.14)",
    "Test (windows-latest, Python 3.11)",
    "Test (windows-latest, Python 3.14)",
    "Test (macos-latest, Python 3.14)",
}


class ReleaseGateError(ValueError):
    """A required release condition is missing or contradictory."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseGateError(message)


def source_version(root: Path) -> str:
    source = (root / "src/cachelint/_version.py").read_text(encoding="utf-8")
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                return value
    raise ReleaseGateError("Source version is missing")


def github(path: str, token: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{REPO}/{path}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "cachelint-release-gate",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            require(response.status == 200, f"GitHub returned {response.status}")
            result = json.load(response)
    except (urllib.error.URLError, ValueError) as error:
        raise ReleaseGateError(f"GitHub read failed: {path}") from error
    if isinstance(result, list):
        # A full page may conceal more results; reject it instead of guessing.
        require(len(result) < 100, f"GitHub result is not fully enumerated: {path}")
        return {"items": result, "total_count": len(result)}
    require(isinstance(result, dict), f"Invalid GitHub response: {path}")
    return cast("dict[str, Any]", result)


def pypi_project_absent() -> bool:
    request = urllib.request.Request(
        "https://pypi.org/pypi/cachelint/json",
        headers={"Accept": "application/json", "User-Agent": "cachelint-release-gate"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            require(response.status == 200, "Unexpected PyPI response")
            result = json.load(response)
            require(isinstance(result, dict), "Invalid PyPI response")
            return False
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return True
        raise ReleaseGateError("PyPI name check failed") from error
    except (urllib.error.URLError, ValueError) as error:
        raise ReleaseGateError("PyPI name check failed") from error


def check_tag(get: Any, sha: str, version: str) -> None:
    ref = get(f"git/ref/tags/v{version}")
    obj = ref.get("object")
    require(isinstance(obj, dict), "Tag object is missing")
    for _ in range(4):
        if obj.get("type") == "commit":
            require(obj.get("sha") == sha, "Tag points to a different commit")
            return
        require(obj.get("type") == "tag", "Tag has an unexpected object type")
        tag = get(f"git/tags/{obj.get('sha')}")
        obj = tag.get("object")
        require(isinstance(obj, dict), "Annotated tag target is missing")
    raise ReleaseGateError("Tag chain is too deep")


def check_ci(get: Any, sha: str) -> None:
    data = get(f"actions/workflows/ci.yml/runs?event=push&head_sha={sha}&per_page=100")
    runs = data.get("workflow_runs")
    require(isinstance(runs, list) and len(runs) > 0, "No CI run for candidate")
    matches = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
    ]
    require(bool(matches), "No push-to-main CI run for candidate")
    run = max(matches, key=lambda item: (item.get("run_number", 0), item.get("id", 0)))
    require(
        run.get("status") == "completed" and run.get("conclusion") == "success",
        "Candidate CI is not successful",
    )
    jobs_data = get(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
    jobs = jobs_data.get("jobs")
    require(
        isinstance(jobs, list) and len(jobs) == len(REQUIRED_JOBS),
        "CI job count differs",
    )
    by_name = {job.get("name"): job for job in jobs if isinstance(job, dict)}
    require(set(by_name) == REQUIRED_JOBS, "Required CI jobs differ")
    require(
        all(
            job.get("status") == "completed" and job.get("conclusion") == "success"
            for job in by_name.values()
        ),
        "Required CI job is not successful",
    )


def check_no_overlap(get: Any, current: dict[str, Any]) -> None:
    runs_data = get("actions/workflows/release.yml/runs?per_page=100")
    runs = runs_data.get("workflow_runs")
    require(
        isinstance(runs, list)
        and runs_data.get("total_count") == len(runs)
        and len(runs) < 100,
        "Cannot enumerate all release runs",
    )
    current_id = current.get("id")
    created_at = current.get("created_at")
    require(isinstance(current_id, int), "Current run ID is missing")
    require(
        isinstance(created_at, str) and bool(created_at), "Run creation time is missing"
    )
    require(
        sum(run.get("id") == current_id for run in runs) == 1,
        "Current run is absent or duplicated",
    )
    for run in runs:
        if run.get("id") == current_id:
            continue
        other_created = run.get("created_at")
        other_updated = run.get("updated_at")
        require(
            isinstance(other_created, str) and isinstance(other_updated, str),
            "Other release run timestamps are missing",
        )
        if other_created < created_at:
            require(
                run.get("status") == "completed" and other_updated < created_at,
                "A prior release run overlapped this dispatch",
            )


def check_issues(get: Any) -> None:
    milestone = get(f"milestones/{MILESTONE}")
    require(
        milestone.get("title") == VERSION and milestone.get("state") == "open",
        "Release milestone is not open",
    )
    release = get(f"issues/{RELEASE_ISSUE}")
    require(
        release.get("title") == f"release: {VERSION}"
        and release.get("state") == "open"
        and release.get("milestone", {}).get("number") == MILESTONE,
        "Publication issue is not open in the milestone",
    )
    preparation = get(f"issues/{PREPARATION_ISSUE}")
    require(
        preparation.get("state") == "closed"
        and preparation.get("milestone", {}).get("number") == MILESTONE,
        "Release-preparation issue is not closed",
    )
    open_issues = get(f"issues?milestone={MILESTONE}&state=open&per_page=100")
    issues = open_issues.get("items")
    # The GitHub issues endpoint returns a JSON list. The network wrapper wraps it.
    require(isinstance(issues, list), "Cannot enumerate milestone issues")
    require(
        open_issues.get("total_count") == 1
        and len(issues) == 1
        and issues[0].get("number") == RELEASE_ISSUE,
        "Other release-bound issues remain open",
    )


def check_environment(get: Any) -> None:
    environment = get("environments/pypi")
    require(environment.get("name") == "pypi", "PyPI environment is missing")
    policy = environment.get("deployment_branch_policy")
    require(
        isinstance(policy, dict)
        and policy.get("custom_branch_policies") is True
        and policy.get("protected_branches") is False,
        "PyPI environment branch policy is not selective",
    )
    rules = environment.get("protection_rules")
    require(isinstance(rules, list), "PyPI environment protection rules are missing")
    reviewers = [rule for rule in rules if rule.get("type") == "required_reviewers"]
    require(len(reviewers) == 1, "PyPI environment requires an exact reviewer rule")
    require(reviewers[0].get("prevent_self_review") is False, "Self review is blocked")
    people = reviewers[0].get("reviewers")
    require(
        isinstance(people, list)
        and len(people) == 1
        and people[0].get("type") == "User"
        and people[0].get("reviewer", {}).get("login") == "SEPURI-SAI-KRISHNA",
        "PyPI environment reviewer differs",
    )
    branch_data = get("environments/pypi/deployment-branch-policies?per_page=100")
    branches = branch_data.get("branch_policies")
    require(
        branch_data.get("total_count") == 1
        and isinstance(branches, list)
        and len(branches) == 1
        and branches[0].get("name") == "main"
        # The list endpoint can omit type; reject an explicit tag policy.
        and branches[0].get("type") in (None, "branch"),
        "PyPI environment must allow only main",
    )


def check_release_state(get: Any, sha: str, version: str) -> None:
    main = get("git/ref/heads/main")
    require(main.get("object", {}).get("sha") == sha, "Main has moved")
    check_tag(get, sha, version)
    check_issues(get)
    require(pypi_project_absent(), "PyPI name is already in use")


def preflight(get: Any, env: dict[str, str], root: Path) -> None:
    sha = env.get("CANDIDATE_SHA", "")
    require(env.get("GITHUB_REPOSITORY") == REPO, "Wrong repository")
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "Wrong event")
    require(env.get("GITHUB_REF") == "refs/heads/main", "Wrong dispatch ref")
    require(re.fullmatch(r"[0-9a-f]{40}", sha) is not None, "Candidate SHA is invalid")
    require(env.get("GITHUB_SHA") == sha, "Dispatch commit differs from candidate")
    require(env.get("VERSION") == VERSION == source_version(root), "Version differs")
    require(env.get("CONFIRMATION") == f"publish {VERSION}", "Confirmation differs")
    run = get(f"actions/runs/{env.get('GITHUB_RUN_ID', '')}")
    require(run.get("run_attempt") == 1, "Rerun is not permitted")
    require(run.get("head_sha") == sha, "Run commit differs")
    require(run.get("event") == "workflow_dispatch", "Run event differs")
    check_no_overlap(get, run)
    check_release_state(get, sha, VERSION)
    check_ci(get, sha)
    check_environment(get)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "recheck"))
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN", "")
    try:
        require(bool(token), "GitHub token is missing")

        def get(path: str) -> dict[str, Any]:
            return github(path, token)

        if args.phase == "preflight":
            preflight(get, dict(os.environ), Path(__file__).resolve().parents[1])
        else:
            sha = os.environ.get("CANDIDATE_SHA", "")
            require(
                os.environ.get("RELEASE_APPROVED_VERSION") == VERSION,
                "Approval differs",
            )
            require(os.environ.get("VERSION") == VERSION, "Version differs")
            require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Wrong ref")
            check_release_state(get, sha, VERSION)
            check_environment(get)
        print(f"Release {args.phase} passed for {VERSION}")
        return 0
    except ReleaseGateError as error:
        print(f"Release {args.phase} blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

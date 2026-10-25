"""Release checks fail closed before a publish step can run."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from tools import release_artifacts, release_gate

SHA = "a" * 40


def release_responses() -> dict[str, dict[str, Any]]:
    jobs = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in release_gate.REQUIRED_JOBS
    ]
    return {
        "actions/runs/123": {
            "id": 123,
            "created_at": "2026-10-24T12:00:00Z",
            "run_attempt": 1,
            "head_sha": SHA,
            "event": "workflow_dispatch",
        },
        "actions/workflows/release.yml/runs?per_page=100": {
            "total_count": 1,
            "workflow_runs": [{"id": 123, "created_at": "2026-10-24T12:00:00Z"}],
        },
        "git/ref/heads/main": {"object": {"sha": SHA}},
        "git/ref/tags/v0.1.0a2": {"object": {"type": "tag", "sha": "b" * 40}},
        f"git/tags/{'b' * 40}": {"object": {"type": "commit", "sha": SHA}},
        f"actions/workflows/ci.yml/runs?event=push&head_sha={SHA}&per_page=100": {
            "workflow_runs": [
                {
                    "id": 456,
                    "run_number": 1,
                    "head_sha": SHA,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        },
        "actions/runs/456/jobs?filter=latest&per_page=100": {"jobs": jobs},
        "milestones/2": {"title": "0.1.0a2", "state": "open"},
        "issues/12": {
            "title": "release: 0.1.0a2",
            "state": "open",
            "milestone": {"number": 2},
        },
        "issues/23": {"state": "closed", "milestone": {"number": 2}},
        "issues?milestone=2&state=open&per_page=100": {
            "items": [{"number": 12}],
            "total_count": 1,
        },
        "environments/pypi": {
            "name": "pypi",
            "deployment_branch_policy": {
                "custom_branch_policies": True,
                "protected_branches": False,
            },
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "prevent_self_review": False,
                    "reviewers": [
                        {
                            "type": "User",
                            "reviewer": {"login": "SEPURI-SAI-KRISHNA"},
                        }
                    ],
                }
            ],
        },
        "environments/pypi/deployment-branch-policies?per_page=100": {
            "total_count": 1,
            "branch_policies": [{"name": "main"}],
        },
    }


def release_env() -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY": "Diafyx/cachelint",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SHA,
        "GITHUB_RUN_ID": "123",
        "CANDIDATE_SHA": SHA,
        "VERSION": "0.1.0a2",
        "CONFIRMATION": "publish 0.1.0a2",
    }


def test_preflight_accepts_exact_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_gate, "check_pypi_project", lambda: None)
    responses = release_responses()
    root = Path(__file__).resolve().parents[1]
    release_gate.preflight(responses.__getitem__, release_env(), root)


def test_recheck_requires_exact_approval_and_unchanged_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_gate, "check_pypi_project", lambda: None)
    responses = release_responses()
    env = release_env()
    env["RELEASE_APPROVED_VERSION"] = "0.1.0a2"
    release_gate.recheck(responses.__getitem__, env)
    env["RELEASE_APPROVED_VERSION"] = "0.1.0a1"
    with pytest.raises(release_gate.ReleaseGateError, match="Approval differs"):
        release_gate.recheck(responses.__getitem__, env)
    env["RELEASE_APPROVED_VERSION"] = "0.1.0a2"
    responses["git/ref/heads/main"]["object"]["sha"] = "c" * 40
    with pytest.raises(release_gate.ReleaseGateError, match="Main has moved"):
        release_gate.recheck(responses.__getitem__, env)


@pytest.mark.parametrize(
    ("path", "update"),
    [
        ("actions/runs/123", {"run_attempt": 2}),
        ("git/ref/heads/main", {"object": {"sha": "c" * 40}}),
        ("git/ref/tags/v0.1.0a2", {"object": {"type": "commit", "sha": "c" * 40}}),
        ("milestones/2", {"state": "closed"}),
        ("issues/12", {"state": "closed"}),
        ("issues/23", {"state": "open"}),
        ("issues?milestone=2&state=open&per_page=100", {"total_count": 2}),
        ("environments/pypi", {"protection_rules": []}),
        (
            "environments/pypi/deployment-branch-policies?per_page=100",
            {"branch_policies": [{"name": "release/*", "type": "branch"}]},
        ),
    ],
)
def test_preflight_rejects_remote_changes(
    monkeypatch: pytest.MonkeyPatch, path: str, update: dict[str, Any]
) -> None:
    monkeypatch.setattr(release_gate, "check_pypi_project", lambda: None)
    responses = release_responses()
    responses[path].update(update)
    with pytest.raises(release_gate.ReleaseGateError):
        release_gate.preflight(
            responses.__getitem__, release_env(), Path(__file__).resolve().parents[1]
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_REF", "refs/tags/v0.1.0a2"),
        ("CANDIDATE_SHA", "c" * 40),
        ("VERSION", "0.1.0a1"),
        ("CONFIRMATION", "publish 0.1.0a1"),
    ],
)
def test_preflight_rejects_dispatch_input(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setattr(release_gate, "check_pypi_project", lambda: None)
    env = release_env()
    env[key] = value
    with pytest.raises(release_gate.ReleaseGateError):
        release_gate.preflight(
            release_responses().__getitem__, env, Path(__file__).resolve().parents[1]
        )


def test_preflight_rejects_failed_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_gate, "check_pypi_project", lambda: None)
    responses = release_responses()
    key = "actions/runs/456/jobs?filter=latest&per_page=100"
    responses[key]["jobs"][0]["conclusion"] = "skipped"
    with pytest.raises(release_gate.ReleaseGateError, match="CI job"):
        release_gate.preflight(
            responses.__getitem__, release_env(), Path(__file__).resolve().parents[1]
        )


def test_preflight_rejects_overlapping_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_gate, "check_pypi_project", lambda: None)
    responses = release_responses()
    runs = responses["actions/workflows/release.yml/runs?per_page=100"]
    runs["total_count"] = 2
    runs["workflow_runs"].append(
        {
            "id": 122,
            "created_at": "2026-10-24T11:00:00Z",
            "updated_at": "2026-10-24T12:01:00Z",
            "status": "completed",
        }
    )
    with pytest.raises(release_gate.ReleaseGateError, match="overlapped"):
        release_gate.preflight(
            responses.__getitem__, release_env(), Path(__file__).resolve().parents[1]
        )


def test_preflight_allows_later_dispatch_after_terminal_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_gate, "check_pypi_project", lambda: None)
    responses = release_responses()
    runs = responses["actions/workflows/release.yml/runs?per_page=100"]
    runs["total_count"] = 2
    runs["workflow_runs"].append(
        {
            "id": 122,
            "created_at": "2026-10-24T11:00:00Z",
            "updated_at": "2026-10-24T11:30:00Z",
            "status": "completed",
        }
    )
    release_gate.preflight(
        responses.__getitem__, release_env(), Path(__file__).resolve().parents[1]
    )


def pypi_project() -> dict[str, Any]:
    return {
        "info": {
            "name": "cachelint",
            "version": "0.1.0a1",
            "project_urls": {"Source": "https://github.com/Diafyx/cachelint"},
        },
        "ownership": {
            "roles": [{"role": "Owner", "user": "saikrishnapypi"}],
        },
        "releases": {
            "0.1.0a1": [
                {"filename": "cachelint-0.1.0a1-py3-none-any.whl"},
                {"filename": "cachelint-0.1.0a1.tar.gz"},
            ]
        },
    }


class PyPIResponse(io.BytesIO):
    status = 200


def mock_pypi_project(monkeypatch: pytest.MonkeyPatch, project: object) -> None:
    def read(request: urllib.request.Request, timeout: int) -> PyPIResponse:
        assert request.full_url == "https://pypi.org/pypi/cachelint/json"
        assert timeout == 20
        return PyPIResponse(json.dumps(project).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", read)


def test_existing_pypi_project_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_pypi_project(monkeypatch, pypi_project())
    release_gate.check_pypi_project()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("name", "identity differs"),
        ("source", "identity differs"),
        ("owner", "owner differs"),
        ("old_files", "files differ"),
        ("target_exists", "already exists"),
        ("project_missing", "read failed"),
    ],
)
def test_pypi_project_changes_block_release(
    monkeypatch: pytest.MonkeyPatch, change: str, message: str
) -> None:
    project = pypi_project()
    if change == "name":
        project["info"]["name"] = "other"
    elif change == "source":
        project["info"]["project_urls"]["Source"] = "https://github.com/other"
    elif change == "owner":
        project["ownership"]["roles"][0]["user"] = "other"
    elif change == "old_files":
        project["releases"]["0.1.0a1"] = []
    elif change == "project_missing":

        def missing(request: urllib.request.Request, timeout: int) -> PyPIResponse:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", Message(), None
            )

        monkeypatch.setattr(urllib.request, "urlopen", missing)
        with pytest.raises(release_gate.ReleaseGateError, match=message):
            release_gate.check_pypi_project()
        return
    else:
        project["releases"]["0.1.0a2"] = []
    mock_pypi_project(monkeypatch, project)
    with pytest.raises(release_gate.ReleaseGateError, match=message):
        release_gate.check_pypi_project()


def test_pypi_project_read_failure_blocks_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(request: urllib.request.Request, timeout: int) -> PyPIResponse:
        raise urllib.error.URLError("unavailable")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    with pytest.raises(release_gate.ReleaseGateError, match="read failed"):
        release_gate.check_pypi_project()


def test_artifact_hashes_and_extra_files_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "build"
    source.mkdir()
    for name in release_artifacts.FILES:
        (source / name).write_bytes(name.encode("ascii"))
    target = tmp_path / "retained"
    release_artifacts.package(source, target)
    assert set(release_artifacts.verify(target)) == release_artifacts.FILES
    (target / "dist" / release_artifacts.WHEEL).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash differs"):
        release_artifacts.verify(target)
    (target / "dist" / release_artifacts.WHEEL).write_bytes(
        release_artifacts.WHEEL.encode("ascii")
    )
    (target / "dist" / "unexpected.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected distributions"):
        release_artifacts.verify(target)


def test_pypi_file_bytes_must_match_retained_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "build"
    source.mkdir()
    for name in release_artifacts.FILES:
        (source / name).write_bytes(name.encode("ascii"))
    target = tmp_path / "retained"
    release_artifacts.package(source, target)
    hashes = release_artifacts.verify(target)
    metadata = {
        "info": {"name": "cachelint", "version": "0.1.0a2"},
        "urls": [
            {
                "filename": name,
                "digests": {"sha256": hashes[name]},
                "url": f"https://files.pythonhosted.org/{name}",
            }
            for name in sorted(release_artifacts.FILES)
        ],
    }

    def correct_download(url: str, timeout: int) -> io.BytesIO:
        assert timeout in (20, 60)
        if url.endswith("/json"):
            return io.BytesIO(json.dumps(metadata).encode("utf-8"))
        return io.BytesIO(url.rsplit("/", 1)[1].encode("ascii"))

    monkeypatch.setattr(urllib.request, "urlopen", correct_download)
    release_artifacts.check_pypi(target)

    def changed_download(url: str, timeout: int) -> io.BytesIO:
        if url.endswith("/json"):
            return io.BytesIO(json.dumps(metadata).encode("utf-8"))
        return io.BytesIO(b"changed")

    monkeypatch.setattr(urllib.request, "urlopen", changed_download)
    with pytest.raises(ValueError, match="downloaded file hash differs"):
        release_artifacts.check_pypi(target)

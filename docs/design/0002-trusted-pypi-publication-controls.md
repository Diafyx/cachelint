# ADR 0002: Trusted PyPI publication controls

- **Status:** Accepted on merge
- **Issue:** [#9](https://github.com/Diafyx/cachelint/issues/9)
- **Initial target:** `0.1.0a1`
- **Reviewed GitHub and PyPI documentation:** 2026-10-17

## Context

The first `cachelint` release needs to publish one reviewed wheel and one sdist
from a reviewed `main` commit. The repository currently has CI but no publishing
workflow, PyPI publisher, or configured release environment. Publication is a
separate action from merging the release candidate. The
[release issue](https://github.com/Diafyx/cachelint/issues/2) remains open until
PyPI and GitHub artifacts are verified.

PyPI [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) uses a
short-lived identity instead of a stored PyPI API token. Its security depends
on the exact trusted workflow and on who can change or run that workflow; it
does not establish that package code is safe. PyPI recommends a dedicated
publishing workflow, job-scoped permissions, and a protected environment. GitHub
can create an environment with **no protection rules** when a workflow names an
environment that does not exist, so the workflow name alone is not an approval
gate. See the [PyPI security model](https://docs.pypi.org/trusted-publishers/security-model/)
and [GitHub environment behavior](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments).

## Decision

### Identity and repository controls

The only production publisher identity is:

| PyPI field | Required value |
| --- | --- |
| Project | `cachelint` |
| GitHub owner | `Diafyx` |
| Repository | `cachelint` |
| Workflow filename | `release.yml` in `.github/workflows/` |
| Environment | `pypi` |

An authorized PyPI owner registers this exact identity. If `cachelint` does not
yet exist on PyPI, use a [pending Trusted Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
for the first upload; it does **not** reserve the name. Recheck availability and
the pending mapping immediately before publication. For an existing project,
verify the publisher on that project's Publishing page. Do not configure a
long-lived PyPI token or a second production publisher as a fallback. A
workflow rename, repository transfer, or environment rename requires a new
review of the PyPI mapping before publication. PyPI matches repository owner,
repository, workflow filename, and optional environment for GitHub publishers;
see its [publisher configuration](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

`main` remains protected by reviewed PRs and required CI. Two active `v*` tag
rulesets separate creation from immutability: the creation rule allows only
the maintainer as a reviewed bypass actor; the update/deletion rule has no
bypass actor. This permits the first tag push without granting the creator a
routine way to move or delete it. Repository administrators can still change
rulesets, so the maintainer reviews both active rulesets before tagging. The
workflow independently checks the tag's peeled commit. GitHub documents
[tag rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository)
and [creation, update, and deletion restrictions](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).

Create the `pypi` environment **before** registering or dispatching the
workflow. It must allow only branch `main`, require approval by maintainer
`SEPURI-SAI-KRISHNA`, and disallow administrator bypass. With one maintainer,
leave "Prevent self-review" off so that the person who dispatches can approve;
enabling it would block this release until another reviewer is appointed. This
is an explicit single-maintainer tradeoff, not an independent review claim.
Record a manual review of the candidate and workflow before approval. GitHub
documents [required reviewers and self-review behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
and [deployment review](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments).

The environment holds a short-lived, non-secret configuration variable named
`RELEASE_APPROVED_VERSION`. The maintainer sets it to the exact version, for
example `0.1.0a1`, only after reviewing the release candidate. The publishing
job checks it **after** the environment approval and before any upload. An
absent or different value fails the job. The maintainer removes it after
verification or a terminal failed/cancelled release attempt; it cannot
authorize a later version. GitHub makes environment variables available to the
job after the environment is declared on the runner; see
[variables reference](https://docs.github.com/en/actions/reference/workflows-and-actions/variables)
and [contexts reference](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts).

### Trigger and preflight

`.github/workflows/release.yml` has only a manual `workflow_dispatch` trigger.
It accepts `version`, `candidate_sha`, and `confirmation` inputs. The
confirmation must be the literal `publish VERSION` for the same version. The
maintainer dispatches from `main`, never from a tag or feature branch. No push,
pull request, GitHub Release, schedule, or reusable-workflow trigger can reach
the publisher. GitHub requires a manually dispatched workflow file on the
default branch and allows a ref selection, so the workflow must check its own
ref; see [manual workflow runs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).

Before building, an unprivileged preflight job must verify all of these facts
and stop on any missing, ambiguous, or inaccessible response:

1. Repository is `Diafyx/cachelint`; event is `workflow_dispatch`; selected ref
   is `refs/heads/main`; this is the first run attempt. The event SHA,
   `candidate_sha` input, and current `main` head are the same full commit ID.
2. `version` is the exact PEP 440 package version in the reviewed source. The
   tag is exactly `vVERSION`, exists on the remote, and, after peeling an
   annotated tag if needed, points to that same commit. The tag is not moved.
3. The required `push`-to-`main` CI workflow has a successful completed run for that exact
   commit, including quality, supported Python/OS jobs, and package checks;
   missing, skipped, cancelled, or failed required jobs do not count as green.
   Dependency and security findings receive manual review.
4. The `VERSION` milestone is open, all its release-bound issues except the
   publication issue are closed, and the `release: VERSION` issue is open in
   that milestone. For `0.1.0a1`, that publication issue is #2 and the
   [release-preparation issue](https://github.com/Diafyx/cachelint/issues/10)
   must already be merged and closed.
5. The `pypi` environment already exists with the expected required reviewer
   and a selected branch policy containing only `main`. The preflight must
   read the environment and its
   [branch policy](https://docs.github.com/en/rest/deployments/branch-policies)
   before any job references the environment, avoiding GitHub's implicit
   unprotected-environment creation. The maintainer separately confirms the
   no-bypass setting and PyPI mapping, which are administrative controls.
6. No `cachelint` file for this version is already on PyPI. For the first
   release, the project name and pending publisher are still available.

Use read-only GitHub and PyPI APIs for these checks; do not infer success from
an empty API result. The preflight has only the read permissions it needs, such
as `contents: read`, `actions: read`, and `issues: read`. GitHub's
[environment API](https://docs.github.com/en/rest/deployments/environments)
and [workflow-run API](https://docs.github.com/en/rest/actions/workflow-runs)
provide the required readback. The implementation PR must test each rejected
case without a publish step. No upload job runs during PR CI.

### Build, approval, and artifact identity

After preflight, a separate build job checks out the verified SHA and builds
exactly one wheel and one sdist once. It verifies metadata, filenames, version,
license, contents, and entry point with Twine and the project's artifact
inspector, then smoke-installs the wheel from outside the checkout. It records
SHA-256 for each file and uploads the two files plus a manifest as a retained
workflow artifact. The build job has no `id-token: write` or repository write
permission. GitHub's [workflow artifact guidance](https://docs.github.com/en/actions/tutorials/store-and-share-data)
documents transfer and digest validation across jobs; the per-file manifest
remains authoritative for PyPI/GitHub byte comparison.

Only after that job succeeds may the publishing job wait at the protected
`pypi` environment. The maintainer reviews the run ID, candidate SHA, version,
tag, checks, artifact names, and SHA-256 manifest before approving deployment.
The publish job downloads the retained artifact and checks that it contains
exactly the expected wheel, sdist, and manifest with matching hashes. It then
rechecks the current `main` head, tag target, release issue, milestone, absence
of files on PyPI, and `RELEASE_APPROVED_VERSION` **after** approval. Any change
or unavailable response stops before upload. It runs no package build or
repository code after receiving OIDC permission.

Only this job receives job-scoped `id-token: write` for Trusted Publishing.
Keep its other permissions read-only and use the pinned, reviewed PyPA
`gh-action-pypi-publish` action with no username/password, no `skip-existing`,
and attestations enabled. Pin all external actions to reviewed full commit
SHAs in the implementation PR. PyPI's [Trusted Publisher example](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
and [PyPA action guidance](https://github.com/pypa/gh-action-pypi-publish)
recommend a separate build and a minimally privileged publishing job. The
action generates [PyPI attestations](https://docs.pypi.org/attestations/producing-attestations/)
by default for Trusted Publishing; post-publication verification checks that
they exist and identify the expected publisher.

The workflow uses a fixed `cachelint-pypi-release` concurrency group with
`cancel-in-progress: false` and rejects rerun attempts. The maintainer waits
for a terminal run state before dispatching another release; GitHub can replace
an already pending run in a concurrency group, so a queue is not an approval
mechanism. Concurrency limits overlap but does not replace the PyPI
file-existence and exact-version gates; see
[GitHub concurrency behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).

### GitHub release and verification

After successful PyPI upload, a separate job verifies PyPI serves exactly the
expected filenames and SHA-256 values, checks the downloaded release files
against the manifest again, then creates the `vVERSION` GitHub prerelease
from the reviewed notes and attaches those **same downloaded files**.
This job alone receives `contents: write`; it has no PyPI OIDC permission.
The workflow records the run URL, candidate SHA, tag, artifact hashes, PyPI
URLs, and GitHub prerelease URL. The maintainer independently verifies PyPI
metadata, both hashes, provenance identity, a fresh install and documented
minimal CLI use, and GitHub release assets, then records evidence in the
still-open release issue. Only after that does the maintainer remove the
approval variable and close the release issue and milestone. PyPI exposes
[provenance through its Integrity API](https://docs.pypi.org/api/integrity/).

### Failure and recovery

No gate has a permissive fallback. Missing approval, an unprotected or missing
environment, a moved branch/tag, non-green CI, a mismatched artifact, a closed
release issue, wrong milestone, existing PyPI filename, or unavailable API
means **no upload**. A rejected or timed-out environment review fails the run.
Do not use `skip-existing`, automatic re-upload, or a rebuilt artifact to
recover a partial release.

If a run fails or is cancelled after upload starts, first establish its
terminal state and inspect PyPI for each expected filename and hash. If either
file arrived, do not rerun the publisher automatically. PyPI does not permit
reusing a previously used filename, even after deletion; see
[PyPI's filename policy](https://pypi.org/help/). If both expected files are
present and match, finish the GitHub prerelease through a separately reviewed
recovery action that has no PyPI publishing capability. If files are missing or
wrong, document the partial release and prepare a new version; consider a
[yank](https://docs.pypi.org/project-management/yanking/) if the published
version is broken or unsafe. Never retarget a published tag or overwrite files.
Keep the publication issue open until the actual external state is verified.

## Maintainer action sequence

These are distinct review points; completion of one does not authorize the
next automatically.

1. Merge [issue #10's release-preparation PR](https://github.com/Diafyx/cachelint/issues/10)
   through the GitHub UI and review the exact `main` commit, CI, package files,
   release notes, PyPI name, publisher identity, environment, and tag rulesets.
2. Set `pypi` environment variable `RELEASE_APPROVED_VERSION` to the exact
   version. Create an annotated `vVERSION` tag at that reviewed commit locally.
3. Push only that tag in a separate command. Read back the remote tag target;
   stop if it differs from the reviewed commit.
4. Dispatch `release.yml` from `main` with the version, full candidate SHA,
   and literal `publish VERSION` confirmation. Review the preflight and build
   outputs and file hashes before approving the waiting `pypi` deployment in
   the GitHub UI.
5. After the run, independently verify PyPI files, metadata, hashes, and
   attestations, install from PyPI in a fresh environment, and verify the
   GitHub prerelease and identical attached files. Record evidence in the
   still-open release issue, remove the one-time approval variable, then close
   the release issue and milestone.

## Required negative checks for the implementation

| Condition | Required result before upload |
| --- | --- |
| Dispatch from a tag or non-`main` branch, or input SHA differs from current `main` | Stop. |
| Rerun attempt or second dispatch before the previous release run is terminal | Stop. |
| Missing, moved, or wrongly named `vVERSION` tag | Stop. |
| Required CI run absent, incomplete, skipped, cancelled, or failed | Stop. |
| Publication issue closed, wrong milestone, or another release-bound issue open | Stop. |
| `pypi` environment missing, lacking the reviewer, or allowing another branch | Stop before referencing it. |
| Approval variable missing or different after deployment review | Stop. |
| Wheel/sdist count, version, manifest hash, or PyPI file-existence check differs | Stop. |
| GitHub or PyPI API response is unavailable or cannot be parsed | Stop. |

## Alternatives considered

- Long-lived PyPI API tokens would add a stored credential with a larger
  exposure window than Trusted Publishing.
- Publishing on `v*` pushes or GitHub Release publication would make tag or
  release creation itself a deployment trigger, losing the separate exact
  confirmation and approval step.
- Building again after approval could produce different bytes from the reviewed
  artifact. The approved files must be the files uploaded to both destinations.
- A required independent reviewer is desirable when another maintainer joins,
  but enabling "Prevent self-review" today would deadlock this sole-maintainer
  repository. Revisit the environment rule when ownership changes.

## Compatibility, risks, and follow-up

This design changes no installed CLI or Python API and does not publish or
configure anything. Its controls apply to later prereleases by substituting
their exact version, release issue, tag, and notes. Main risks are an incorrect
publisher identity, an unprotected environment, a changed workflow, or a
partial upload. The readback gates, pinned actions, separate privileges, and
manual review address those risks; they cannot prove package code is safe.

[Issue #10](https://github.com/Diafyx/cachelint/issues/10) implements and
tests the release workflow, curated notes, and candidate checks after this ADR
merges. Repository settings, PyPI mapping, tag creation/push, dispatch,
deployment approval, upload, and issue closure remain separate maintainer
actions under [the release process](../release-process.md) and the open
[publication issue #2](https://github.com/Diafyx/cachelint/issues/2).

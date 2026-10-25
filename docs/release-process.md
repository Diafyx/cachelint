# Release process

This is the maintainer's reviewed release sequence. The
[trusted publication design](design/0002-trusted-pypi-publication-controls.md)
defines the controls implemented by `.github/workflows/release.yml`. The
[0.1.0a2 release notes](releases/0.1.0a2.md) are the reviewed GitHub prerelease
body for the current candidate. Merging the release-preparation PR does not
publish anything.

1. Complete every product issue in the version milestone. Keep a separate `release: VERSION` issue open through publication and verification.
2. Merge a distinct release-preparation PR with the exact version, accurate metadata, compatibility notes, and curated release notes. Check supported Python CI, dependency and security findings, wheel and sdist contents, and the clean-wheel smoke test on the candidate commit.
3. Review the release workflow, protected `pypi` environment, PyPI Trusted Publisher mapping, tag protection, and a short-lived exact-version approval. Recheck the distribution name before the first publication.
4. The maintainer creates `vVERSION` at the reviewed `main` commit, then pushes that tag in a separate command. Never move a published tag.
5. Dispatch the reviewed release workflow from `main` with an exact confirmation. It must fail closed if version, tag, commit, CI, milestone, release issue, environment, or approval disagree.
6. Build wheel and sdist once from the verified commit. Validate and preserve those exact bytes, publish them through the protected environment after maintainer approval, then create the GitHub release or prerelease with reviewed notes and those same files.
7. Verify PyPI metadata, files, hashes, provenance, a fresh installation, minimal usage, and the GitHub release. Record evidence in the release issue, remove one-time approval, then close the release issue and milestone.

If a release run fails or is cancelled, confirm its terminal state before retrying. If a version reached PyPI, its files are immutable. Document the defect and prepare a new version or yank when justified; do not upload different bytes under the same version.

## First publication setup for 0.1.0a1

After issue #10 merges and its exact `main` CI run succeeds, review the release
candidate commit, wheel, sdist, source version, metadata, README, license,
CLI smoke test, supported Python matrix, dependency findings, and these notes.
The workflow refuses publication while any 0.1.0a1 milestone issue except
[release issue #2](https://github.com/Diafyx/cachelint/issues/2) is open.

1. Create the GitHub `pypi` environment before any workflow dispatch. Set
   required reviewer to `SEPURI-SAI-KRISHNA`, allow only the selected branch
   `main`, and disable administrator bypass. With the current sole maintainer,
   leave “Prevent self-review” off. Review those settings in the UI. The
   workflow checks the reviewer and exact branch policy through GitHub's API;
   review the branch policy type and no-bypass control manually because the
   branch-policy list API can omit the type.
2. Set up the two reviewed `v*` tag rulesets from
   [ADR 0002](design/0002-trusted-pypi-publication-controls.md): only the
   maintainer can create a tag, and nobody can update or delete one. Review
   the active rulesets before pushing the release tag.
3. Register the PyPI Trusted Publisher for project `cachelint`, GitHub owner
   `Diafyx`, repository `cachelint`, workflow `release.yml`, and environment
   `pypi`. For the first upload, use a pending publisher if the project does
   not yet exist. Confirm the mapping in PyPI's UI and repeat the project name
   check immediately before publishing. A pending publisher does not reserve
   the name.
4. Set the `pypi` environment variable `RELEASE_APPROVED_VERSION` to
   `0.1.0a1` only after reviewing the exact candidate. Record its commit SHA.
   Create the annotated `v0.1.0a1` tag at that commit and push the tag as a
   distinct maintainer action. Read back the remote tag target.
5. From `main`, dispatch **Publish PyPI prerelease** with version `0.1.0a1`,
   the full 40-character candidate SHA, and confirmation
   `publish 0.1.0a1`. The workflow has no push or PR trigger. It stops if
   the branch, tag, version, CI, milestone, issues, environment, or PyPI name
   differ. It builds one wheel and one sdist, verifies them, and retains
   SHA-256 hashes in the `cachelint-0.1.0a1` workflow artifact.
6. Before approving the waiting `pypi` job in the GitHub UI, compare the run
   ID, SHA, tag, filenames, and manifest with your review. Approval is the
   separate upload decision. The job rechecks the remote state and approval
   variable, then uploads those exact files with PyPI Trusted Publishing.
   The next job checks PyPI metadata and downloaded bytes before attaching
   the same files to the GitHub prerelease.
7. Verify the PyPI file hashes and provenance identity, a fresh install and
   minimal CLI use, the GitHub prerelease, and its two attached files. Record
   the links, SHA, manifest, and results in release issue #2. Remove the
   one-time approval variable, then close the release issue and milestone.

If upload starts but the run fails, first inspect PyPI for both filenames and
their hashes. A partial upload cannot be safely retried under the same version.
Keep release issue #2 open and follow the recovery rule in ADR 0002.

## Second publication: 0.1.0a2

The project already exists on PyPI. The current release gate checks its exact
name, source repository, reviewed owner, and two `0.1.0a1` files, then requires
`0.1.0a2` to be absent. It checks the same project again after deployment
approval. On 2026-10-25 the public PyPI JSON API showed `0.1.0a1` as the only
release, with owner `saikrishnapypi` and Source
`https://github.com/Diafyx/cachelint`. The local package still needs the final
candidate and publication review below. PyPI's
[project JSON API](https://docs.pypi.org/api/json/) documents the fields; public
metadata is a gate, not a substitute for reviewing the PyPI Trusted Publisher
mapping and project ownership in the UI.

1. Merge issue #23's release-preparation PR. Confirm the exact `main` commit has
   green CI across the supported Python matrix and no unresolved relevant
   security findings. Review the `0.1.0a2` wheel and sdist, optional dependency
   metadata, rule data, notes, license, typing marker, and clean-wheel CLI smoke
   test. Leave [release issue #12](https://github.com/Diafyx/cachelint/issues/12)
   open.
2. Recheck the protected `pypi` environment: only `main` may deploy, the
   required reviewer is `SEPURI-SAI-KRISHNA`, administrator bypass is disabled,
   and the existing Trusted Publisher still names owner `Diafyx`, repository
   `cachelint`, workflow `release.yml`, and environment `pypi`. Review the
   active `v*` tag rulesets. Set the short-lived environment variable
   `RELEASE_APPROVED_VERSION` to `0.1.0a2` only for this candidate.
3. At the reviewed `main` commit, create the annotated `v0.1.0a2` tag and push
   it as a separate maintainer action. Read back its remote target. Never move
   an existing release tag.
4. Dispatch **Publish PyPI prerelease** from `main` with version `0.1.0a2`, the
   full candidate SHA, and confirmation `publish 0.1.0a2`. The workflow refuses
   a different branch, version, commit, tag, milestone, issue state, approval,
   environment, existing PyPI project identity, or already present version.
5. Review the run ID, SHA, tag, retained `cachelint-0.1.0a2` artifact, filenames,
   and SHA-256 manifest before approving the waiting `pypi` deployment in the
   GitHub UI. The job rechecks remote state and PyPI before Trusted Publishing.
6. Verify PyPI metadata, provenance, exact file hashes, and a fresh core and
   optional-extra install. Run documented CLI examples. Confirm the GitHub
   prerelease and its two attached files match the retained manifest. Record
   evidence in issue #12, remove `RELEASE_APPROVED_VERSION`, then close issue
   #12 and milestone `0.1.0a2`.

If the run fails, confirm it is terminal and inspect PyPI before retrying. If
either `0.1.0a2` file was accepted, do not reuse that version; follow the
recovery rule above.

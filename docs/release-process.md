# Release process

This is the maintainer's reviewed release sequence. The initial repository foundation does not include a publishing workflow or authorize a release. The [trusted publication design](design/0002-trusted-pypi-publication-controls.md) defines the controls that the release-preparation workflow must implement.

1. Complete every product issue in the version milestone. Keep a separate `release: VERSION` issue open through publication and verification.
2. Merge a distinct release-preparation PR with the exact version, accurate metadata, compatibility notes, and curated release notes. Check supported Python CI, dependency and security findings, wheel and sdist contents, and the clean-wheel smoke test on the candidate commit.
3. Review the release workflow, protected `pypi` environment, PyPI Trusted Publisher mapping, tag protection, and a short-lived exact-version approval. Recheck the distribution name before the first publication.
4. The maintainer creates `vVERSION` at the reviewed `main` commit, then pushes that tag in a separate command. Never move a published tag.
5. Dispatch the reviewed release workflow from `main` with an exact confirmation. It must fail closed if version, tag, commit, CI, milestone, release issue, environment, or approval disagree.
6. Build wheel and sdist once from the verified commit. Validate and preserve those exact bytes, publish them through the protected environment after maintainer approval, then create the GitHub release or prerelease with reviewed notes and those same files.
7. Verify PyPI metadata, files, hashes, provenance, a fresh installation, minimal usage, and the GitHub release. Record evidence in the release issue, remove one-time approval, then close the release issue and milestone.

If a release run fails or is cancelled, confirm its terminal state before retrying. If a version reached PyPI, its files are immutable. Document the defect and prepare a new version or yank when justified; do not upload different bytes under the same version.

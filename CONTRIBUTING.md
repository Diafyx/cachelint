# Contributing

Start with a focused GitHub issue so the problem, expected behavior, and compatibility impact are clear before implementation. Use the issue form that fits the change. For security problems, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Development

Use Python 3.11 or newer. Create a virtual environment and install the package and development dependency group:

```console
python -m pip install --upgrade pip
python -m pip install --group dev -e .
ruff format --check .
ruff check .
mypy src tests tools
pytest --cov=cachelint --cov-branch --cov-report=term-missing
```

The development group is kept out of published runtime requirements. Add tests and documentation alongside behavior changes. Keep examples and fixtures free of secrets and customer content.

## Pull requests and sign-off

Use an issue-numbered branch, such as `feat/123-short-description`, and complete every section of the PR template. Keep one reviewable concern per PR. Human-authored commits need a Developer Certificate of Origin sign-off:

```console
git commit --signoff -m "feat: describe the change"
```

The sign-off records that you have the right to submit the contribution under the project's license. It is separate from cryptographic commit signing. Maintainers review compatibility, test evidence, and docs before merging.

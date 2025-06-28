# GitHub Configuration

This directory contains GitHub-specific configuration files:

- **workflows/**: GitHub Actions CI/CD pipelines
  - `ci.yml`: Runs tests, linting, and type checking on every push/PR
  - `release.yml`: Handles package releases and PyPI publishing

## Setting up for your fork

If you're forking this repository, you'll need to:

1. Update the PyPI API token in your repository secrets (Settings → Secrets → Actions):
   - `PYPI_API_TOKEN`: Your PyPI API token for publishing releases

2. Enable GitHub Actions in your repository settings

3. Configure branch protection rules for `main` branch (recommended)
# Contributing to Ashlr AO

## Development Setup

```bash
git clone https://github.com/ashlrai/ashlr-ao.git
cd ashlr-ao
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Running Tests

```bash
# Full suite (1559 tests, ~90s)
pytest

# With coverage (65% threshold enforced in CI)
pytest --cov=ashlr_ao --cov-fail-under=65

# Single file
pytest tests/test_integration.py -x -q
```

Tests use `pytest-asyncio` and `pytest-aiohttp`. No real tmux sessions or network calls are made — everything is mocked.

## Code Style

- **Python 3.11+** with type hints on all public functions
- **Dataclasses** for all models (see `ashlr_ao/models.py`)
- **async/await** throughout — no blocking calls on the event loop
- **ruff** for linting: `ruff check ashlr_ao/`
- Security: never trust user input — validate paths, sanitize output, enforce ownership
- All dict iterations use `list()` snapshots to prevent `RuntimeError` during async mutation

## Project Structure

See [CLAUDE.md](CLAUDE.md) for the full architecture reference, including all 16 modules, API endpoints, WebSocket protocol, and data models.

## Commit Messages

Follow conventional commit format:

```
feat: add fleet template variable substitution
fix: prevent symlink path traversal in working_dir
refactor: extract background tasks to background.py
test: add circuit breaker edge case coverage
chore: bump CI coverage threshold to 65%
```

## Pull Requests

1. Create a feature branch from `main`
2. Make your changes with tests
3. Ensure `pytest` passes and coverage stays above 65%
4. Run `ruff check ashlr_ao/` — no lint errors
5. Open a PR with a clear description of what changed and why

## Reporting Issues

Open an issue at [github.com/ashlrai/ashlr-ao/issues](https://github.com/ashlrai/ashlr-ao/issues). Include:

- Python version and OS
- Steps to reproduce
- Expected vs. actual behavior
- Relevant logs (redact any API keys)

## Security Vulnerabilities

See [SECURITY.md](SECURITY.md) for responsible disclosure instructions. Do not open public issues for security vulnerabilities.

# Enterprise Redaction Utility (sample target repo)

A throwaway project the autofix agent operates on. It contains:

- `src/redactor.py` - a dependency-free PII redaction helper
- `tests/` - fast unit tests (the `tests_pass` validation gate)
- `requirements.txt` - pinned dependencies that intentionally match the
  simulated DotConnector findings

The agent copies this folder to a temporary workspace, initializes git, and
opens fix branches there -- the original is never modified.

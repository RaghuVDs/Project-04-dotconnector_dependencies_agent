"""Shared pytest configuration.

Puts the project root *and* the tests directory on sys.path so:
  * the package under test (`src`, `mock_dotconnector`, `run_poc`) imports, and
  * every test module (in unit/ functional/ integration/) can `import helpers`.
"""
import os
import sys

_TESTS_DIR = os.path.dirname(__file__)
_ROOT = os.path.dirname(_TESTS_DIR)

for _p in (_ROOT, _TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

"""Test-harness package marker and Hugo-availability helpers.

The end-to-end harness (real ``sphinx-build -b hugo`` followed by a real
``hugo`` build) is implemented across later milestones. M0 provides the
directory, the Hugo-detection helper, and the ``test_runner.py`` entry point so
the harness contract (detect ``hugo``, SKIP with a recorded reason when absent)
is in place from the start.
"""

from __future__ import annotations

import shutil


def hugo_available() -> bool:
    """Return True when a ``hugo`` binary is on PATH."""
    return shutil.which("hugo") is not None

"""End-to-end test harness package for doxtr-hugo.

Runs the real build pipeline — a real ``sphinx-build -b hugo`` on the fixture
docs (:mod:`test_harness.docs`) and, when the binary is present, a real
``hugo`` build against the emitted built-in skeleton. See
:mod:`test_harness.test_runner` for the stage implementations and
:mod:`test_harness.test_harness` for the pytest wrappers.
"""

# Contributing to doxtr-hugo

Thanks for your interest in improving **doxtr-hugo**. This document covers how to
set up a development environment, run the tests, and submit changes.

## Development setup

```bash
git clone https://github.com/doxtr/doxtr-hugo
cd doxtr-hugo
pip install -e ".[dev]"
```

The optional diagram/math rendering backends (`plantuml`, `dot`, `mmdc`, `d2`,
matplotlib) are only needed to exercise the corresponding code paths. Tests that
require an external binary skip (never silently pass) when it is absent.

## Running the tests

The project has two test layers:

```bash
# Fast, binary-free unit suite (backend calls are monkeypatched).
python -m pytest tests/ -q

# Integration harness: a real `sphinx-build -b hugo` followed by a real `hugo`
# build plus the multi-theme render matrix. Stages that need `hugo` or a
# diagram binary are skipped with a recorded reason when the tool is missing.
python -m pytest test_harness/ -q
```

Coverage is gated at ≥ 75% in CI. Please keep new code covered.

## Submitting changes

1. Open an issue first for anything beyond a small fix, so we can agree on the
   approach.
2. Create a feature branch; do not push directly to `main`.
3. Keep commits small and focused, with a clear message.
4. Ensure `python -m pytest tests/ -q` is green and coverage stays above the
   gate before opening a pull request.
5. Update `CHANGELOG.md` under the `[Unreleased]` section for any user-facing
   change.
6. Open a pull request describing the change, what you tested, and any
   limitations.

## Style

Match the surrounding code: type hints, module/class/function docstrings on
public symbols, and deterministic (sorted/stable) output. The builder emits
only formats Hugo renders natively — avoid introducing a dependency on Hugo
modules, render-hook JavaScript, or the asset pipeline on the consumer side.

## Reporting bugs

Please include the Sphinx and doxtr-hugo versions, a minimal `conf.py` and
source document that reproduces the problem, and the full `sphinx-build -b hugo`
output (run with `-v` if helpful).

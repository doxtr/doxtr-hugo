"""Sphinx configuration for the doxtr-hugo end-to-end harness fixture.

This is a real (minimal) Sphinx project used by ``test_harness/test_runner.py``
to drive a real ``sphinx-build -b hugo`` and (when the binary is present) a real
``hugo`` build against the emitted built-in skeleton.
"""

import shutil

project = "doxtr-hugo harness"
author = "doxtr"
copyright = "2026, doxtr"

extensions = ["doxtr_hugo"]

# Exercise the diagram render pipeline end to end *when* a backend binary is
# available. Graphviz's ``dot`` ships in the reactor/CI image (guide §7.3), so
# the diagram page below renders to a real image there. In a binary-free
# environment ``dot`` is absent: enabling the extension + page would emit a
# missing-backend warning that the harness's strict ``-W`` build turns into an
# error, so the diagram page is only wired in when the binary is present. This
# keeps the fixture a genuine diagram-heavy proof on CI while staying green
# where no backend exists (the harness records that as graceful degradation,
# never a silent pass).
_HAS_GRAPHVIZ = shutil.which("dot") is not None
if _HAS_GRAPHVIZ:
    extensions.append("sphinx.ext.graphviz")

# Keep the build deterministic and self-contained.
root_doc = "index"
exclude_patterns = ["_build"]
if not _HAS_GRAPHVIZ:
    # Drop the diagram page from the build when its backend is unavailable.
    exclude_patterns.append("guide/diagrams.rst")

# Exercise the default (full) skeleton so the emitted tree is a runnable Hugo
# site: hugo.toml + layouts/ + content/**.
doxtr_hugo_emit_skeleton = "full"
doxtr_hugo_front_matter = "toml"

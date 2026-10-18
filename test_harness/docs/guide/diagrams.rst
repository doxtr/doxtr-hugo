:orphan:

Diagrams
========

This orphan fixture page exercises the diagram render pipeline end to end so
the harness proves a real diagram node is rendered to a co-located image and
recorded in the build manifest (guide §4). It is only built when a diagram
backend binary (Graphviz ``dot``) is available; ``conf.py`` excludes it
otherwise so a binary-free build stays warning-clean.

A Graphviz diagram:

.. graphviz::

   digraph G {
       rankdir=LR;
       author -> sphinx -> hugo;
   }

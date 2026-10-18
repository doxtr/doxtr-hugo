"""Toctree → Hugo section/weight/menu model.

Sphinx records the resolved toctree structure on the build environment as
``env.toctree_includes``: a mapping ``parent_docname -> [child_docname, ...]``
that preserves the *authoring order* of every ``.. toctree::`` entry (nested
toctrees appear as additional keys whose parent is the including document).
That mapping, together with ``env.titles`` and ``config.root_doc``, is enough
to derive the Hugo site structure deterministically — no path arithmetic and no
guessing.

This module turns that raw mapping into a small, explicit model:

* a :class:`TocNode` per document, carrying its title, parent, ordering
  ``weight``, nesting ``depth``, and whether it is a **branch** (a Hugo section
  with an ``_index.md`` branch bundle) or a **leaf** (a single page);
* a :class:`TocModel` that owns the node set, exposes the root, and provides
  deterministic lookups for the builder, front matter, and site skeleton.

Weights are assigned per parent in authoring order, spaced by
:data:`WEIGHT_STEP` (default 10) so a consumer can hand-insert pages between
generated ones without renumbering — matching the ``weight = 20`` style in the
guide (§5.3). The model is intentionally free of any Sphinx/builder object
references once constructed, so it is cheap to build once in
``prepare_writing`` and trivial to unit-test from a plain includes mapping.

A document that is reachable from the root through toctrees is *in* the model;
documents that are never referenced by any toctree (orphans) are still added as
top-level leaves under the root so every emitted page has a defined weight and
parent. The root document itself is always the branch root of the tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Spacing between successive sibling weights. A step > 1 leaves room for a
#: consumer to interleave their own pages without renumbering generated ones.
WEIGHT_STEP = 10


@dataclass
class TocNode:
    """A single document's place in the Hugo site structure.

    Attributes:
        docname: Sphinx docname (forward-slash path, no suffix).
        title:   Resolved document title (plain text), or the docname if the
                 environment has no recorded title.
        parent:  Docname of the parent branch, or ``None`` for the root.
        weight:  Deterministic ordering weight among its siblings.
        depth:   Nesting depth from the root (root = 0).
        children: Ordered child docnames (empty for a leaf).
        is_branch: ``True`` when the node owns a Hugo section (``_index.md``),
                   i.e. it has at least one child or is the root document.
    """

    docname: str
    title: str
    parent: str | None
    weight: int
    depth: int
    children: list[str] = field(default_factory=list)
    is_branch: bool = False


class TocModel:
    """The derived section/weight/menu model for one Sphinx build.

    Build it with :meth:`from_env` (authoritative, from a live build) or with
    :meth:`from_includes` (pure data, for unit tests). Both produce the same
    deterministic structure.
    """

    def __init__(self, root: str, nodes: dict[str, TocNode]) -> None:
        """Store the pre-built node map.

        Prefer the :meth:`from_env` / :meth:`from_includes` classmethods, which
        compute the deterministic weight/depth/parent structure; this
        constructor just records ``root`` (the root docname) and ``nodes`` (the
        docname → :class:`TocNode` mapping) as-is.
        """
        self.root = root
        self.nodes = nodes

    # -- construction ----------------------------------------------------

    @classmethod
    def from_env(cls, env, root_doc: str) -> "TocModel":
        """Build the model from a Sphinx build environment.

        ``env.toctree_includes`` gives parent → ordered-children; ``env.titles``
        gives per-doc title nodes. Orphan documents (present in
        ``env.found_docs`` but never referenced by a toctree) are attached as
        top-level leaves so every page is represented.
        """
        includes = {
            parent: list(children)
            for parent, children in getattr(env, "toctree_includes", {}).items()
        }
        titles = {
            docname: node.astext()
            for docname, node in getattr(env, "titles", {}).items()
        }
        all_docs = set(getattr(env, "found_docs", []) or [])
        return cls.from_includes(root_doc, includes, titles, all_docs)

    @classmethod
    def from_includes(
        cls,
        root_doc: str,
        includes: dict[str, list[str]],
        titles: dict[str, str] | None = None,
        all_docs: set[str] | None = None,
    ) -> "TocModel":
        """Build the model from a plain ``parent -> [children]`` mapping.

        This is the pure-data constructor used by unit tests. ``titles`` maps a
        docname to its plain-text title (falling back to the docname).
        ``all_docs`` is the full document set used to attach orphans; when
        omitted it is inferred from the mapping keys/values plus the root.
        """
        titles = dict(titles or {})
        if all_docs is None:
            all_docs = {root_doc}
            for parent, children in includes.items():
                all_docs.add(parent)
                all_docs.update(children)

        def title_of(docname: str) -> str:
            return titles.get(docname, docname)

        nodes: dict[str, TocNode] = {}

        # The root is always a branch; depth 0, no parent, weight 0.
        nodes[root_doc] = TocNode(
            docname=root_doc,
            title=title_of(root_doc),
            parent=None,
            weight=0,
            depth=0,
            is_branch=True,
        )

        # Walk the toctree graph breadth-first from the root so each node's
        # depth/parent is assigned by its first (shallowest) inclusion. A guard
        # set prevents infinite loops if a malformed toctree is cyclic.
        queue: list[str] = [root_doc]
        seen: set[str] = {root_doc}
        while queue:
            parent = queue.pop(0)
            children = includes.get(parent, [])
            parent_node = nodes[parent]
            for order, child in enumerate(children):
                if child in seen:
                    # Already placed under an earlier parent; record the edge
                    # for completeness but do not re-parent it.
                    if child not in parent_node.children:
                        parent_node.children.append(child)
                    continue
                seen.add(child)
                child_is_branch = bool(includes.get(child))
                child_node = TocNode(
                    docname=child,
                    title=title_of(child),
                    parent=parent,
                    weight=(order + 1) * WEIGHT_STEP,
                    depth=parent_node.depth + 1,
                    is_branch=child_is_branch,
                )
                nodes[child] = child_node
                parent_node.children.append(child)
                queue.append(child)

        # Attach orphans (never reached from the root toctree) as top-level
        # leaves, ordered after the explicit root children for determinism.
        root_node = nodes[root_doc]
        orphans = sorted(d for d in all_docs if d not in nodes)
        next_weight = (len(root_node.children) + 1) * WEIGHT_STEP
        for i, docname in enumerate(orphans):
            nodes[docname] = TocNode(
                docname=docname,
                title=title_of(docname),
                parent=root_doc,
                weight=next_weight + i * WEIGHT_STEP,
                depth=1,
                is_branch=bool(includes.get(docname)),
            )
            root_node.children.append(docname)

        return cls(root_doc, nodes)

    # -- queries ---------------------------------------------------------

    def __contains__(self, docname: str) -> bool:
        """Return whether ``docname`` is represented in the model."""
        return docname in self.nodes

    def get(self, docname: str) -> TocNode | None:
        """Return the node for ``docname`` or ``None`` if it is not modelled."""
        return self.nodes.get(docname)

    def weight_of(self, docname: str) -> int | None:
        """Ordering weight for ``docname`` (``None`` if unknown)."""
        node = self.nodes.get(docname)
        return node.weight if node else None

    def is_branch(self, docname: str) -> bool:
        """``True`` when ``docname`` is a Hugo section (has an ``_index.md``)."""
        node = self.nodes.get(docname)
        return bool(node and node.is_branch)

    def branches(self) -> list[str]:
        """Docnames that are branch sections, ordered by (depth, weight)."""
        return [
            n.docname
            for n in sorted(
                (n for n in self.nodes.values() if n.is_branch),
                key=lambda n: (n.depth, n.weight, n.docname),
            )
        ]

    def leaves(self) -> list[str]:
        """Docnames that are leaf pages, ordered by (depth, weight)."""
        return [
            n.docname
            for n in sorted(
                (n for n in self.nodes.values() if not n.is_branch),
                key=lambda n: (n.depth, n.weight, n.docname),
            )
        ]

    def ordered(self) -> list[TocNode]:
        """All nodes in a stable (depth, weight, docname) order."""
        return sorted(
            self.nodes.values(),
            key=lambda n: (n.depth, n.weight, n.docname),
        )

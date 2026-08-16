# Python API

Everything the MCP server and CLI do is available directly. Load a graph,
wrap it in a query object, ask.

```python
from sphinxcontrib.nexus.export import load_sqlite
from sphinxcontrib.nexus.project import resolve_db
from sphinxcontrib.nexus.query import GraphQuery

# `<project root>/.nexus/graph.db` — derived from the directory holding
# `.nexus/`, so ask for it rather than writing the path out.
kg = load_sqlite(resolve_db())
q = GraphQuery(kg)

q.query("ndarray", node_types=["external"])
q.impact("py:function:pkg.sn.solver.solve_sn", direction="upstream")
q.provenance_chain("py:function:pkg.sn.sweep.sweep_spherical")
q.dead_references()
```

## The three objects worth knowing

:::{note}
The roles below are live cross-references into this project's own code, so
they double as a check on the extension: if reference resolution regresses,
nexus's own documentation build is where it shows up first.
:::

**The graph** — {class}`sphinxcontrib.nexus.graph.KnowledgeGraph` wraps a
`networkx.MultiDiGraph` with the node and edge vocabulary from
{doc}`vocabulary`. Node types are
{class}`sphinxcontrib.nexus.graph.NodeType`, edge types
{class}`sphinxcontrib.nexus.graph.EdgeType`.

**The queries** — {class}`sphinxcontrib.nexus.query.GraphQuery` carries the
whole read surface. Most MCP tools are a thin wrapper over one of its
methods, so anything an agent can ask, you can ask in a REPL. A few tools
compose several calls — `context` assembles {meth}`~sphinxcontrib.nexus.query.GraphQuery.get_node`
and {meth}`~sphinxcontrib.nexus.query.GraphQuery.neighbors` rather than
mapping to one method — so the tool list in {doc}`tools` is not a
method-for-method index.

**The checkout** — {class}`sphinxcontrib.nexus.workspace.Workspace` pairs a
checkout with the graph built inside it. A graph is a snapshot of one
checkout; this is what lets a session working in a git worktree find the
right one.

## Loading and saving

{func}`sphinxcontrib.nexus.export.load_sqlite` and
{func}`sphinxcontrib.nexus.export.write_sqlite` are the primary format —
SQLite with FTS5 indexes, fast enough to load on every tool call.
{func}`sphinxcontrib.nexus.export.read_sqlite_metadata` reads just the
metadata table, which is how workspace discovery reports provenance for
several checkouts without paying a full load.

```python
from sphinxcontrib.nexus.export import read_sqlite_metadata
from sphinxcontrib.nexus.project import resolve_db

meta = read_sqlite_metadata(resolve_db())
print(meta["provenance"]["git_commit"])
```

## Building a graph without Sphinx

{func}`sphinxcontrib.nexus.ast_analyzer.analyze_directory` runs the AST
half on its own, for projects with code but no docs build yet:

```python
from pathlib import Path
from sphinxcontrib.nexus.ast_analyzer import analyze_directory

kg = analyze_directory(
    source_dir=Path("src"),
    project_root=Path("."),
    exclude_patterns=["scratch/*"],
)
```

{func}`sphinxcontrib.nexus.merge.merge_graphs` folds an AST graph into a
Sphinx one. Reconciliation is deliberately *not* part of it — call
{func}`sphinxcontrib.nexus.merge.reconcile_unresolved` once after every
merge, so it decides against the complete graph rather than one directory
at a time.

## Reference

```{eval-rst}
.. autoclass:: sphinxcontrib.nexus.graph.KnowledgeGraph
   :members: add_node, add_edge, node_count, edge_count

.. autoclass:: sphinxcontrib.nexus.query.GraphQuery
   :members: query, get_node, neighbors, impact, provenance_chain,
             dead_references, verification_coverage, staleness,
             graph_query, retest

.. autofunction:: sphinxcontrib.nexus.export.load_sqlite

.. autofunction:: sphinxcontrib.nexus.export.read_sqlite_metadata

.. autofunction:: sphinxcontrib.nexus.ast_analyzer.analyze_directory

.. autofunction:: sphinxcontrib.nexus.merge.reconcile_unresolved
```

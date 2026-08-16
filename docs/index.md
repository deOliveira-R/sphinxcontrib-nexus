# sphinxcontrib-nexus

One queryable knowledge graph over **code structure** (call graphs, imports,
inheritance, type annotations) and **documentation structure** (equations,
cross-references, citations, theory pages).

The premise: a docstring citing an equation, a test verifying it, and the
function implementing it are three facts about the same thing. Kept in
separate tools they drift silently. Kept in one graph they can be queried —
and the drift becomes a finding.

```{toctree}
:maxdepth: 2
:caption: Guide

guide/authoring
guide/vocabulary
guide/tools
guide/cli
guide/python-api
```

## Which page do I want?

| You are | Start at |
|---|---|
| Instrumenting a project — writing docstrings, `.rst`, directives | {doc}`guide/authoring` |
| Reading a graph someone else built — node ids, edge meanings | {doc}`guide/vocabulary` |
| Driving the MCP server from an agent | {doc}`guide/tools` |
| At a terminal, or wiring a hook | {doc}`guide/cli` |
| Working with the graph from Python | {doc}`guide/python-api` |

## The graph of this project

These docs are built with `sphinxcontrib.nexus` enabled, so building them
writes a graph of nexus itself to `.nexus/graph.db` at the repository
root, and the interactive explorer to `_build/html/graph/graph.html`. That
is both a demonstration and a test: if the extension breaks on a real
codebase, its own documentation build is the first thing to notice.

```{nexus-graph}
:height: 600px
```

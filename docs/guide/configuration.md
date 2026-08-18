# Configuration

Everything nexus can be told lives in one file: `.nexus/config.toml`, at
the project root, next to the graph it governs. It is tracked in git, so
every worktree and every clone gets it with no scaffolding step.

The file is optional. An unconfigured project works, using the shipped
value for every setting below.

## What belongs here, and what does not

> **A number is a setting when changing it changes how much nexus SAYS
> or how long it WAITS. It is a constant when it changes what nexus
> MEANS.**

Reply sizes, list lengths and timeouts are settings: the right answer
depends on your corpus, your machine, and how big a context window is
this year. Format versions, the id grammar and the fingerprint shingle
size are constants — changing one does not tune nexus, it makes two
installations disagree about what a graph says.

A key nexus does not recognise is **reported**, not ignored — a typo that
passes silently is the same defect as an empty result that reads like a
measurement.

### The vocabulary is not here — it is in `ontology.toml`

`config.toml` tunes *behaviour*. Which node types and edge types exist,
what may connect to what, and which types stand for something outside
the project are **semantics**, and they live in a file of their own:
`.nexus/ontology.toml`, described in {doc}`vocabulary`.

That file is an extension point, not a fixed list. A project declares
its own types and widens the base ones:

```toml
# .nexus/ontology.toml
[node.equation_variant]
description = "A project-specific flavour of equation."
origin = "sphinx"

[extend.edge.implements]
range = ["equation_variant"]      # widening only; narrowing is refused
```

Everything that asks *"is this a real type?"* or *"is this a
placeholder?"* asks the ontology **with your extension loaded** — the id
grammar check, result ranking, `god_nodes`, `dead_references`' phantom
set. So a type you declare is a first-class type everywhere, not merely
one nexus tolerates.

```{note}
The base vocabulary's *names* live in `graph.NodeType` / `graph.EdgeType`
and its *semantics* in the shipped `ontology.toml`; a test pins the two
to each other so neither can drift. Your project file extends that pair
— it never replaces it, and widening is monotone, so no pass written
against the base vocabulary can be invalidated by a project.
```

## `[replies]` — how much a tool may say

A tool's answer lands in an agent's context and stays there, so its size
is a correctness property, not a nicety. These are the knobs most likely
to want raising as context windows grow.

```toml
[replies]
# The most a single tool reply may be, in characters. Anything larger is
# trimmed — never silently: the reply carries a `truncated` block naming
# the true totals and how to get the rest.
#
# Characters, not tokens, because that is what nexus can measure without
# a tokenizer. Roughly 4 characters per token, so 20000 ≈ 5000 tokens.
max_characters = 20000

# Default length for any tool that returns a list (`dead_functions`,
# `twin_paths`, `discriminations`, `graph_query`, the `runtime_*`
# family, …). Raise this one line to lengthen all of them; every tool
# still takes its own `limit` argument for a single call.
items_per_list = 50

# How many neighbours `context` shows per edge type before reporting the
# rest as a count. A hub node has hundreds.
neighbors_per_edge_type = 25

# How many nodes `impact` shows per depth level. `total_affected` is
# always the true count regardless.
nodes_per_impact_depth = 50

# Items a `file_brief` line spells out before collapsing to `(+N)`.
# The brief is injected on every edit, so it is the most
# frequently-rendered surface nexus has.
items_per_brief_line = 3
```

## `[briefing]` — what the session briefing shows

`session_briefing` is produced *before anyone has asked a question*, so
every session pays for it whether or not it is read. It is deliberately
an **index**: a count, a few examples, and the tool that expands each.
Every section reports how much of itself you are seeing.

```toml
[briefing]
# The project's most connected symbols — its structural hubs. stdlib and
# installed-package nodes are excluded, or this reports `numpy.array`
# and `int` rather than your architecture.
project_hubs = 5

# Drifted doc pages to name. `staleness()` has all of them.
stale_pages = 5

# Affected symbols listed per drifted page. The page's own count is in
# its `stale_reason`, so these are examples, not the census.
symbols_per_stale_page = 3

# Equations that have code but no test. `verification_audit()` has all
# of them, with grouping.
coverage_gaps = 5
```

## `[graph]` — what gets analysed, and where it goes

```toml
[graph]
# Extra source trees to analyse beyond the Sphinx source dir.
extra_source_dirs = ["src", "tools"]

# Glob patterns excluded from analysis.
exclude_patterns = ["**/migrations/**"]

# Whether test files are analysed at all, and what counts as one.
analyze_tests = true
test_patterns = ["test_*.py", "*_test.py"]

# Whether to infer `implements` edges from shared name tokens. Inferred
# edges land at confidence=0.7; on a large corpus most `implements`
# edges will be inferred ones, so this is worth setting deliberately.
infer_implements = true

# YAML files declaring verification edges from outside the code.
verification_registry = ["verification.yaml"]

# Node ceiling for the interactive explorer page.
max_viz_nodes = 2000

# Subdirectory of the Sphinx HTML output holding the explorer page.
output = "_nexus"

# How long any single git call may take. A property of the REPOSITORY —
# its size, its filesystem, whether it is on a network mount — not of
# nexus.
git_timeout_seconds = 10
```

```{note}
The graph database is **not** configurable. It lives at
`.nexus/graph.db`, derived from the project root rather than declared,
so no two surfaces can disagree about where the graph is. Ask
`nexus config db` if you need the path.
```

## `[scope]` — what counts as this project, for runtime overlays

```toml
[scope]
# Path prefixes considered in-scope when ingesting a trace. A LIST,
# deliberately: profiling a test suite produces `tests → package`
# records, so either directory alone drops one endpoint of every one of
# them, while the repository root sweeps in the virtualenv.
prefixes = ["src/mypkg", "tests"]
```

## `[catalog]` — where the project's failure modes are declared

```toml
[catalog]
# ONE project-relative path (a string, not a list) to a file listing the
# project's failure modes. Optional, and usually unnecessary.
errors = "docs/verification/error_catalog.rst"
```

Optional because the catalogue's real home is the corpus: `.. error-entry::`
declares an entry wherever it is written, `@pytest.mark.catches` resolves
against those nodes, and `errors` / `verification_gaps` read them straight
from the graph. This key exists for a project that keeps its catalogue
*outside* the corpus and still wants `nexus gaps` to count against it.

:::{warning}
A **list** here raises `TypeError` — this page documented one until
2026-08-17, and `nexus config` crashes on the spelling it showed. If you
copied that form, make it a bare string.
:::

## Seeing what is in effect

`nexus config` prints the resolved values and the file they came from,
which is the quickest way to check that a setting is spelled the way
nexus expects.

---
description: Documentation health — dead references and drift, computed now
allowed-tools: Bash(nexus:*)
---

The following was computed from the knowledge graph at the moment you were
invoked. It is a finding, not a suggestion to go look for one.

## Dead documentation references

!`nexus dead-references --db docs/_build/html/_nexus/graph.db --format text --limit 25`

## Timestamp drift

!`nexus staleness --db docs/_build/html/_nexus/graph.db 2>/dev/null | head -40`

---

Address the dead references above: each one is a doc, docstring, or quoted
type annotation citing code or an equation label that **no longer exists**.
Sphinx renders them as plain text and emits no warning at any severity, so
they will not surface any other way.

For each target, decide and act:

- the symbol was **renamed** → update the reference to the new name;
- the symbol was **deleted** and the prose is now wrong → rewrite or remove
  the passage, don't just unlink it;
- the reference is to something **never implemented** → say so explicitly in
  the prose, or drop the claim.

Report what you changed and what you deliberately left, with reasons. If a
reported target actually resolves at runtime through `__getattr__` or
metaclass machinery, say so — static analysis cannot see that, and the
finding is a false positive worth recording.

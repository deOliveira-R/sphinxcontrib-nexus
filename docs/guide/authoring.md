# Authoring: what you write, what you get

This page is for whoever writes the docstrings, `.rst` pages, and tests.
Every construct below is something you already write for Sphinx — the point
is what each one *becomes* in the graph, so you can tell which prose is
load-bearing and which is decoration.

Nothing here is required. Nexus extracts a useful graph from an
uninstrumented project; the directives buy precision where inference
guesses.

## The short version

| You write | You get |
|---|---|
| `` :func:`pkg.mod.solve` `` in a docstring | `documents` edge → that function |
| `` :eq:`transport-balance` `` | `equation_ref` edge → the equation |
| `.. math:: :label: foo` | an `equation` node, referenceable anywhere |
| `@pytest.mark.verifies("foo")` | `tests` edge, test → equation |
| `.. verifies:: foo` | the same edge, declared from the doc side |
| `.. error-entry:: ERR-051` | an `error` node — a catalogued failure mode |
| `@pytest.mark.catches("ERR-051")` | `catches` edge, test → error |
| `.. implements:: foo` | `implements` edge, code → equation |
| `.. discretizes:: foo` | `discretizes` edge between two statements |
| `#: prose with :class:`X`` above an attribute | edges from that attribute |
| `Cls.attr = ...` after the class body | an `attribute` node |

## Referencing code from prose

Standard Sphinx roles in a docstring or `.rst` page become edges. No
configuration:

```python
def solve_sn(mesh, quad):
    """Solve the S_N transport equation.

    Uses :class:`pkg.sn.mesh.SNMesh` and the quadrature from
    :func:`pkg.quadrature.gauss_legendre`. Implements
    :eq:`transport-balance`.
    """
```

That docstring produces three edges: two `documents` (to the class and the
function) and one `equation_ref` (to the equation label).

### Relative references resolve like Sphinx does

You do not have to fully qualify. A reference resolves against the
namespace of the thing it is written in, following
`PythonDomain.find_obj`:

1. `modname.classname.target`
2. `modname.target`
3. `target` — as a **fully qualified key**

Step 3 is the one that surprises people. The registry is keyed by full
dotted names, so a bare `` :func:`solve` `` does **not** resolve just
because its module is `automodule`-d — it resolves only if a *top-level*
`solve` exists. This is why adding autodoc coverage does not fix bare
references.

Inside a class, `` :meth:`apply` `` finds that class's own `apply`. In a
different class it finds a different one. The same spelling in two places
is two different edges, which is why the graph resolves them per-reference
rather than once per name.

### What will not resolve

A name that is neither in the referring namespace nor a top-level symbol
stays unresolved and is reported by {ref}`dead-references`. That is
faithful — Sphinx renders those as plain text with no warning, so the
graph is showing you a link your readers are not getting.

## Defining equations

A labelled math block becomes a node other things can point at:

```rst
.. math::
   :label: transport-balance

   \Omega \cdot \nabla \psi + \Sigma_t \psi = q
```

Labels defined in **docstrings** work too. Nexus parses docstring math
blocks even when Sphinx never renders that docstring, so a label defined
in un-rendered prose is still a real node rather than a dangling
reference.

Reference it with `` :eq:`transport-balance` `` or
`` :math:numref:`transport-balance` `` — both bind to the same node.

## Relating statements to each other

Equations are not a flat list. Three directives declare the spine, and
each names its target as the argument:

```rst
.. math::
   :label: sn-dd-closure

   \psi_c = \tfrac{1}{2}(\psi_L + \psi_R)

.. discretizes:: sn-transport-continuous
```

- `discretizes` — a discrete form → the continuous one it discretizes
- `derives-from` — a specialization → the parent it was reduced from
- `approximates` — a closure or truncation → the exact form it stands in for

Direction is always **specific → general**. The source is the nearest
preceding labelled statement, or an explicit `:label:` when you need to
declare out of order:

```rst
.. approximates:: transport-continuous
   :label: p1-closure
```

They work on [`sphinx-proof`](https://github.com/executablebooks/sphinx-proof)
environments too — a `` .. prf:theorem:: `` with a `:label:` is a statement
like any other, so a theorem can `derives-from` a definition.

Misuse warns and is dropped rather than breaking the build: no bindable
source, a statement related to itself, or a target that does not exist.

## Declaring verification

Two directions, same edge. From the test:

```python
@pytest.mark.verifies("transport-balance")
def test_transport_balance_slab():
    ...
```

Or from the doc side, when the test is somebody else's:

```rst
.. verifies:: transport-balance
   :by: tests.sn.test_transport.test_balance_slab
```

And to say which code *is* an equation:

```rst
.. implements:: transport-balance
   :by: pkg.sn.solver.solve_sn
```

An explicit `implements` always beats the inferred one. Nexus infers
`implements` edges from shared name tokens between a page's equations and
the code it documents, at `confidence=0.7`; a declared edge is `1.0` and
suppresses inference for that pair.

:::{note}
A test **verifies** an equation — it does not implement one. Nexus never
infers `implements` onto test code, because an equation whose only
implementer is a test class would read as implemented when nothing
implements it.
:::

## Documenting attributes

Both forms are indexed, including the two that are easy to lose.

**Attribute comments.** Sphinx's `#:` form documents the statement below
it, and that prose is scanned for references:

```python
#: Spatial axis names, positional-by-axis — the crosswalk that
#: :class:`FaceLayout` and :attr:`SNMesh.bc` both key on.
AXIS_NAMES = ("x", "y", "z")
```

Comments are invisible to `ast`, so this is read from the token stream. A
`#:` inside a string literal is not mistaken for one. The trailing form
(`LIMIT = 10  #: capped by ...`) documents its own line.

**Attributes bound after the class body.** The enum-like-singleton idiom
is indexed:

```python
@dataclass(frozen=True)
class BC:
    kind: str

BC.vacuum = BC("vacuum")      # type: ignore[attr-defined]
BC.reflective = BC("reflective")
```

`BC.vacuum` is a real attribute at import time and autodoc documents it,
so `` :data:`BC.vacuum` `` renders as a working link — it is now a node
too. Only classes defined in the same module are extended;
`os.environ = {}` does not forge an attribute onto a foreign symbol.

`self.x: T` annotations in `__init__` are indexed as well, which matters
because PEP 526 records them nowhere at runtime — an import-based checker
cannot see them.

(dead-references)=
## Reading the drift report

`nexus dead-references` reports doc and docstring references whose target
does not exist. Sphinx renders these as plain text with no warning at any
severity, which is what makes them silent.

Each finding may carry **`minted_by`**: the source files whose own code
(an import, call, annotation, or base class) created the placeholder the
reference bound to. When those all sit in one unmaintained corner of the
tree, that directory is the finding, not the reference:

```
  pkg.retired.compute  [python] — 1 site(s)
      doc:theory/method  (theory/method.rst)
      minted by code in:
        /proj/scratch/probe.py
```

Prose does not count as minting. A page mentioning a vanished symbol is
the reference being reported, not its cause.

:::{warning}
Any directory you scan contributes to the namespace that bare references
resolve against. A prototyping directory importing a module retired months
ago mints placeholder nodes that bare roles elsewhere can bind to. Exclude
it:

```python
nexus_source_exclude_patterns = ["scratch/*", "student_resources/*"]
```
:::

## The error catalogue

A test declares two things about itself: which equation it **verifies**,
and which known failure mode it **catches**. Only the first had somewhere
to point until `error` became a node type — `@pytest.mark.catches("ERR-051")`
was a string in an attribute naming nothing, so *"which tests catch
ERR-051?"* was a grep rather than a query.

Declare each entry once, wherever your catalogue lives:

```rst
.. error-entry:: ERR-051
   :title: Galerkin idempotency asserted without the 4π convention

   The gate asserted ``Π R = I`` instead of ``Π R = 4π · I``. It survived
   a full merge cycle because its only test fed it a deliberately
   non-orthogonal ``Y``, so the wrong invariant still produced the
   expected failure.
```

Then `@pytest.mark.catches("ERR-051")` on any test resolves to it, and
`impact`, `callers` and the verification queries can all see the link.

Two deliberate constraints:

- **The directive is not called `.. error::`.** That name belongs to
  docutils' admonition, and taking it would change how every existing
  `.. error::` block renders.
- **A marker cannot create an entry.** If `catches` names something no
  `.. error-entry::` declares, nexus warns and writes nothing. A typo
  must not be able to mint the thing it claims to catch — otherwise the
  miss looks exactly like coverage, which is the one direction a V&V
  graph must never fail in. This is the same rule `.. math:: :label:`
  and `verifies` already follow.

If your project has `catches` markers but no entries at all, you get a
single line saying so — not one per marker.

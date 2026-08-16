"""The graph's ontology — node types, edge types, and what may connect what.

``graph.NodeType`` / ``graph.EdgeType`` say which *names* exist. They say
nothing about meaning: that ``implements`` runs code → statement and not
the reverse, that ``discriminates_on`` may only target a ``tag``, that an
inferred ``implements`` is worth 0.7 while a declared one is worth 1.0.
Today that knowledge is spread across extractors, restated in prose in
``docs/guide/vocabulary.md``, and enforced nowhere.

This module makes it data. ``ontology.toml`` ships beside this file as the
base vocabulary; a project extends it with ``.nexus/ontology.toml``.

Three things follow, in increasing order of value:

1. The vocabulary documentation can be *generated* rather than
   hand-maintained beside the code it describes.
2. A project can add attributes and their permitted values without a
   nexus release — which is what stops "every new label costs a release".
3. Domain/range becomes checkable. The fix for "a test verifies an
   equation, it does not implement one" was a hardcoded exclusion inside
   ``_infer_implements``; expressed here it is a declared constraint, and
   the *class* of defect closes rather than the instance.

⚠ What this does NOT do: produce edges. Declaring ``exercises: test →
code`` costs three lines; *populating* it still needs an extractor. The
ontology removes the hardcoded vocabulary, not the extraction.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from sphinxcontrib.nexus.project import CONFIG_DIR, find_project_root

logger = logging.getLogger(__name__)

#: The base ontology, shipped as package data beside this module.
BASE_ONTOLOGY = Path(__file__).with_name("ontology.toml")

#: A project's extension, relative to the project root.
PROJECT_ONTOLOGY = f"{CONFIG_DIR}/ontology.toml"

#: Wildcard in a ``domain``/``range`` list: any node type is admissible.
ANY = "*"


class Enforcement(str, Enum):
    """What to do with an edge that violates its declared domain/range."""

    ERROR = "error"
    WARN = "warn"
    NONE = "none"


@dataclass(frozen=True)
class NodeSpec:
    name: str
    description: str = ""
    origin: str = ""
    placeholder: bool = False
    attributes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EdgeSpec:
    name: str
    description: str = ""
    domain: tuple[str, ...] = (ANY,)
    range: tuple[str, ...] = (ANY,)
    default_confidence: float | None = None
    enforcement: Enforcement = Enforcement.NONE
    sources: tuple[str, ...] = ()
    attributes: tuple[str, ...] = ()
    #: Node attributes that disqualify a *source* node, e.g.
    #: ``{"in_test_file": True}`` — a test may not implement an equation.
    forbid_source_attr: Mapping[str, Any] = field(default_factory=dict)

    def admits_source(self, node_type: str) -> bool:
        return ANY in self.domain or node_type in self.domain

    def admits_target(self, node_type: str) -> bool:
        return ANY in self.range or node_type in self.range


@dataclass(frozen=True)
class AttributeSpec:
    name: str
    applies_to: str = "node"
    description: str = ""
    type: str = "string"
    values: tuple[str, ...] = ()
    groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Violation:
    """One edge that its own declaration does not admit."""

    edge_type: str
    source: str
    target: str
    reason: str
    enforcement: Enforcement

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"{self.source} --{self.edge_type}--> {self.target}: {self.reason}"


def _as_tuple(value: Any, default: Iterable[str] = ()) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        return (value,)
    return tuple(value)


@dataclass(frozen=True)
class Ontology:
    """The vocabulary in force for one project."""

    nodes: Mapping[str, NodeSpec]
    edges: Mapping[str, EdgeSpec]
    attributes: Mapping[str, AttributeSpec]
    #: Files this was assembled from, base first.
    sources: tuple[Path, ...] = ()

    # -- construction ----------------------------------------------------

    @classmethod
    def load(cls, start: Path | str | None = None) -> "Ontology":
        """Base ontology, extended by the project's own if one exists.

        ``start`` is any path inside the project; the extension is looked
        up from the nearest ancestor carrying a config directory. Passing
        ``None`` loads the base alone, which is what a caller with no
        project context (a unit test, a bare CLI invocation) wants.
        """
        payloads: list[tuple[Path, Mapping[str, Any]]] = [
            (BASE_ONTOLOGY, _read(BASE_ONTOLOGY))
        ]

        if start is not None:
            root = find_project_root(start)
            if root is not None:
                extension = root / PROJECT_ONTOLOGY
                if extension.is_file():
                    payloads.append((extension, _read(extension)))

        nodes: dict[str, NodeSpec] = {}
        edges: dict[str, EdgeSpec] = {}
        attributes: dict[str, AttributeSpec] = {}
        base_names: set[str] = set()

        for path, data in payloads:
            is_base = path == BASE_ONTOLOGY
            for name, spec in (data.get("node") or {}).items():
                _guard_redefinition("node", name, is_base, base_names, path)
                nodes[name] = NodeSpec(
                    name=name,
                    description=spec.get("description", ""),
                    origin=spec.get("origin", ""),
                    placeholder=bool(spec.get("placeholder", False)),
                    attributes=_as_tuple(spec.get("attributes")),
                )
                if is_base:
                    base_names.add(f"node:{name}")

            for name, spec in (data.get("edge") or {}).items():
                _guard_redefinition("edge", name, is_base, base_names, path)
                edges[name] = EdgeSpec(
                    name=name,
                    description=spec.get("description", ""),
                    domain=_as_tuple(spec.get("domain"), (ANY,)),
                    range=_as_tuple(spec.get("range"), (ANY,)),
                    default_confidence=spec.get("default_confidence"),
                    enforcement=Enforcement(spec.get("enforcement", "none")),
                    sources=_as_tuple(spec.get("sources")),
                    attributes=_as_tuple(spec.get("attributes")),
                    forbid_source_attr=dict(spec.get("forbid_source_attr") or {}),
                )
                if is_base:
                    base_names.add(f"edge:{name}")

            for name, spec in (data.get("attribute") or {}).items():
                _guard_redefinition("attribute", name, is_base, base_names, path)
                groups = {
                    key: _as_tuple(val)
                    for key, val in (spec.get("groups") or {}).items()
                }
                attributes[name] = AttributeSpec(
                    name=name,
                    applies_to=spec.get("applies_to", "node"),
                    description=spec.get("description", ""),
                    type=spec.get("type", "string"),
                    values=_as_tuple(spec.get("values")),
                    groups=groups,
                )
                if is_base:
                    base_names.add(f"attribute:{name}")

            # Extensions run after this payload's own definitions, so a file
            # may define and widen in either order.
            extend = data.get("extend") or {}
            if extend and is_base:
                raise ValueError(
                    f"{BASE_ONTOLOGY}: the base ontology DEFINES the "
                    f"vocabulary; it has nothing to extend. Edit the [node] / "
                    f"[edge] tables directly."
                )
            for kind, registry in (("node", nodes), ("edge", edges)):
                for name, patch in (extend.get(kind) or {}).items():
                    current = registry.get(name)
                    if current is None:
                        raise ValueError(
                            f"{path}: [extend.{kind}.{name}] widens a "
                            f"{kind} named {name!r}, which does not exist. "
                            f"Use [{kind}.{name}] to declare a new one."
                        )
                    registry[name] = _widen(current, kind, name, patch, path)

        return cls(
            nodes=nodes,
            edges=edges,
            attributes=attributes,
            sources=tuple(path for path, _ in payloads),
        )

    # -- validation ------------------------------------------------------

    def check_edge(
        self,
        edge_type: str,
        source_type: str,
        target_type: str,
        *,
        source_attrs: Mapping[str, Any] | None = None,
        source_id: str = "",
        target_id: str = "",
    ) -> Violation | None:
        """The first way this edge violates its declaration, or ``None``.

        An edge type absent from the ontology is *not* a violation — it is
        an undescribed type, reported separately by
        :meth:`undescribed_edge_types`. Treating it as a violation would
        make every extractor addition fail before its description lands,
        which trains people to disable the check.
        """
        spec = self.edges.get(edge_type)
        if spec is None or spec.enforcement is Enforcement.NONE:
            return None

        def violation(reason: str) -> Violation:
            return Violation(
                edge_type=edge_type,
                source=source_id or source_type,
                target=target_id or target_type,
                reason=reason,
                enforcement=spec.enforcement,
            )

        if not spec.admits_source(source_type):
            return violation(
                f"source is {source_type!r}; domain is {list(spec.domain)}"
            )
        if not spec.admits_target(target_type):
            return violation(
                f"target is {target_type!r}; range is {list(spec.range)}"
            )
        for key, forbidden in spec.forbid_source_attr.items():
            if source_attrs is not None and source_attrs.get(key) == forbidden:
                return violation(f"source carries {key}={forbidden!r}")
        return None

    # -- drift detection -------------------------------------------------

    def undescribed(self, node_names: Iterable[str], edge_names: Iterable[str]):
        """``(nodes, edges)`` present in code but absent from the ontology."""
        return (
            sorted(set(node_names) - set(self.nodes)),
            sorted(set(edge_names) - set(self.edges)),
        )

    def orphaned(self, node_names: Iterable[str], edge_names: Iterable[str]):
        """``(nodes, edges)`` described here but absent from the code."""
        return (
            sorted(set(self.nodes) - set(node_names)),
            sorted(set(self.edges) - set(edge_names)),
        )


def _read(path: Path) -> Mapping[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _guard_redefinition(
    kind: str, name: str, is_base: bool, base_names: set[str], path: Path
) -> None:
    """A project extension may add, never silently redefine a base entry."""
    if is_base:
        return
    if f"{kind}:{name}" in base_names:
        raise ValueError(
            f"{path}: {kind} {name!r} is defined by the base ontology and may "
            f"not be redefined by a project extension — use [extend.{kind}."
            f"{name}] to WIDEN its domain/range/sources/attributes, choose "
            f"another name, or propose the change upstream."
        )


#: Fields an extension may widen, per kind. Every one is a SET, and the only
#: operation is union — which is what makes widening safe to reason about:
#: anything the base admitted, the extension still admits.
#:
#: Absent by design:
#:
#: ``enforcement`` / ``default_confidence`` — scalars. There is no "more
#: permissive" value to union toward; setting one is a redefinition.
#:
#: ``forbid_source_attr`` — a set, but one whose members *subtract*. Adding a
#: forbid NARROWS the edge, which is exactly what this mechanism exists to
#: refuse. Its set-ness is a trap, not a licence.
_WIDENABLE: Mapping[str, frozenset[str]] = {
    "edge": frozenset({"domain", "range", "sources", "attributes"}),
    "node": frozenset({"attributes"}),
}


def _widen(spec: Any, kind: str, name: str, patch: Mapping[str, Any], path: Path):
    """Return ``spec`` with the named sets unioned — never narrowed.

    The monotonicity property this exists to guarantee, and the one its test
    asserts over every node type::

        base.admits_target(t)  ⟹  extended.admits_target(t)

    Union gives it by construction, so no consumer of the base vocabulary can
    be invalidated by a project's extension: an edge the base would have
    admitted is still admitted. That is the whole reason narrowing is refused
    rather than merely discouraged — a project that could *remove* a type from
    a range would silently break the passes written against the base.

    The result is built with :func:`dataclasses.replace`, so whatever
    invariants the spec's construction enforces re-run on the widened value
    instead of being restated here.
    """
    allowed = _WIDENABLE[kind]
    unknown = sorted(set(patch) - allowed)
    if unknown:
        raise ValueError(
            f"{path}: [extend.{kind}.{name}] may only widen "
            f"{sorted(allowed)}, not {unknown}. Those fields either are not "
            f"sets (a scalar has no wider value) or would NARROW the "
            f"declaration, which an extension may never do."
        )

    updates: dict[str, tuple[str, ...]] = {}
    for field_name, added in patch.items():
        current = getattr(spec, field_name)
        extra = tuple(v for v in _as_tuple(added) if v not in current)
        if not extra:
            continue
        if ANY in current:
            # Legal and monotone, but it does nothing — and a silent no-op in
            # a vocabulary file reads as a change that took effect.
            logger.info(
                "%s: [extend.%s.%s] adds %s to %r, which already admits "
                "everything (%r) — the extension has no effect",
                path, kind, name, list(extra), field_name, ANY,
            )
            continue
        updates[field_name] = current + extra

    return replace(spec, **updates) if updates else spec

"""Non-LLM verification registry loader.

Reads a YAML file that declares explicit verification facts and writes
them as graph edges. Bypasses the LLM-driven ``ingest.py`` path for
deterministic, repository-level metadata that you want in the graph on
every build.

Schema (``version: 1``)::

    version: 1
    verifications:
      - test: <node_id>        # pytest-style id or py:function:/py:method:
        verifies: [label1, label2]
        catches: [FM-07, ERR-003]
        level: L0
    implementations:
      - function: <node_id>
        implements: [label1]
        confidence: 1.0

All registry-sourced edges are tagged with ``source="registry"``.
Missing nodes (test / function / equation) are logged and skipped —
the registry loader never creates phantom nodes. Rebuilds are
idempotent: re-running ``load_registry`` on the same graph with the
same YAML produces zero additional edges on the second pass.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml

from sphinxcontrib.nexus.graph import EdgeType

if TYPE_CHECKING:
    import networkx as nx

logger = logging.getLogger(__name__)


#: Schema version this loader understands. Bump only on incompatible
#: changes to the file layout.
REGISTRY_SCHEMA_VERSION = 1


class RegistryError(ValueError):
    """Raised when a registry YAML is structurally invalid."""


def _as_list(node: Any, field: str, context: str) -> list[Any]:
    if node is None:
        return []
    if not isinstance(node, list):
        raise RegistryError(
            f"{context}: field {field!r} must be a list, got {type(node).__name__}"
        )
    return node


def _as_str_list(
    node: Any, field: str, context: str,
) -> tuple[str, ...]:
    items = _as_list(node, field, context)
    out: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, str):
            raise RegistryError(
                f"{context}: {field}[{i}] must be a string, got {type(item).__name__}"
            )
        out.append(item)
    return tuple(out)


def load_registry(path: Path | str, graph: "nx.MultiDiGraph") -> int:
    """Apply a registry YAML file to an existing knowledge graph.

    Returns the number of edges written. Raises ``RegistryError`` on
    structural issues (bad schema version, non-list where a list is
    required, etc.). Missing graph nodes are logged as warnings and
    skipped — they are not an error because the registry may name
    symbols that don't exist yet in a stub project.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise RegistryError(f"cannot read registry {p}: {e}") from e

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise RegistryError(f"invalid YAML in {p}: {e}") from e

    if data is None:
        logger.info("registry: %s is empty, no edges written", p)
        return 0

    if not isinstance(data, dict):
        raise RegistryError(
            f"{p}: top-level must be a mapping, got {type(data).__name__}"
        )

    version = data.get("version")
    if version != REGISTRY_SCHEMA_VERSION:
        raise RegistryError(
            f"{p}: unsupported registry schema version {version!r} "
            f"(this build of sphinxcontrib-nexus supports version "
            f"{REGISTRY_SCHEMA_VERSION})"
        )

    written = 0
    written += _apply_verifications(
        data.get("verifications"), graph, context=f"{p}::verifications",
    )
    written += _apply_implementations(
        data.get("implementations"), graph, context=f"{p}::implementations",
    )

    if written:
        logger.info("registry: %s wrote %d edges", p, written)
    return written


def _apply_verifications(
    entries: Any,
    g: "nx.MultiDiGraph",
    context: str,
) -> int:
    items = _as_list(entries, "verifications", context)
    written = 0
    for i, item in enumerate(items):
        item_ctx = f"{context}[{i}]"
        if not isinstance(item, dict):
            raise RegistryError(
                f"{item_ctx}: entry must be a mapping, got {type(item).__name__}"
            )
        test_id = item.get("test")
        if not isinstance(test_id, str) or not test_id:
            raise RegistryError(
                f"{item_ctx}: required field 'test' missing or not a string"
            )
        verifies = _as_str_list(item.get("verifies"), "verifies", item_ctx)
        catches = _as_str_list(item.get("catches"), "catches", item_ctx)
        level = item.get("level")
        if level is not None and not isinstance(level, str):
            raise RegistryError(
                f"{item_ctx}: 'level' must be a string if provided"
            )

        if test_id not in g:
            logger.warning(
                "registry: %s names test %r which is not in the graph — skipping",
                item_ctx, test_id,
            )
            continue

        # Enrich the test node's metadata with declared markers so
        # downstream consumers (verification_gaps, audit grouping)
        # see them alongside the AST-extracted ones. Merge, don't
        # overwrite — honoring values that are already set keeps the
        # registry strictly additive.
        node_attrs = g.nodes[test_id]
        if verifies and "verifies" not in node_attrs:
            node_attrs["verifies"] = verifies
        if catches and "catches" not in node_attrs:
            node_attrs["catches"] = catches
        if level and "vv_level" not in node_attrs:
            node_attrs["vv_level"] = level

        for label in verifies:
            eq_id = f"math:equation:{label}"
            if eq_id not in g:
                logger.warning(
                    "registry: %s verifies %r but %s is not in the graph — skipping",
                    item_ctx, label, eq_id,
                )
                continue
            # Idempotent: skip if an equivalent registry edge already
            # exists for this (test, equation) pair.
            existing = g.get_edge_data(test_id, eq_id, default={})
            if any(
                d.get("type") == EdgeType.TESTS.value
                and d.get("source") == "registry"
                for d in existing.values()
            ):
                continue
            g.add_edge(
                test_id,
                eq_id,
                type=EdgeType.TESTS.value,
                source="registry",
                confidence=1.0,
            )
            written += 1
    return written


def _apply_implementations(
    entries: Any,
    g: "nx.MultiDiGraph",
    context: str,
) -> int:
    items = _as_list(entries, "implementations", context)
    written = 0
    for i, item in enumerate(items):
        item_ctx = f"{context}[{i}]"
        if not isinstance(item, dict):
            raise RegistryError(
                f"{item_ctx}: entry must be a mapping, got {type(item).__name__}"
            )
        fn_id = item.get("function")
        if not isinstance(fn_id, str) or not fn_id:
            raise RegistryError(
                f"{item_ctx}: required field 'function' missing or not a string"
            )
        implements = _as_str_list(
            item.get("implements"), "implements", item_ctx,
        )
        confidence_raw = item.get("confidence", 1.0)
        if not isinstance(confidence_raw, (int, float)):
            raise RegistryError(
                f"{item_ctx}: 'confidence' must be a number if provided"
            )
        confidence = float(confidence_raw)

        if fn_id not in g:
            logger.warning(
                "registry: %s names function %r which is not in the graph — skipping",
                item_ctx, fn_id,
            )
            continue

        for label in implements:
            eq_id = f"math:equation:{label}"
            if eq_id not in g:
                logger.warning(
                    "registry: %s implements %r but %s is not in the graph — skipping",
                    item_ctx, label, eq_id,
                )
                continue
            existing = g.get_edge_data(fn_id, eq_id, default={})
            if any(
                d.get("type") == EdgeType.IMPLEMENTS.value
                and d.get("source") == "registry"
                for d in existing.values()
            ):
                continue
            g.add_edge(
                fn_id,
                eq_id,
                type=EdgeType.IMPLEMENTS.value,
                source="registry",
                confidence=confidence,
            )
            written += 1
    return written

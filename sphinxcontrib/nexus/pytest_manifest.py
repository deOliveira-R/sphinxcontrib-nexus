"""Capture pytest's RESOLVED markers as a manifest nexus can ingest.

Nexus lifts markers by AST-parsing decorators, which sees what was
*spelled* on a function. pytest resolves something different and richer:
module-level ``pytestmark``, class-level marks, marks a ``conftest.py``
attaches during collection, and any project-specific precedence between
them. `[M]` on one real project the gap is not marginal — ``vv_level``
resolved on **1524 of 5273** collected tests by AST, because **254
files** apply their markers through module-level ``pytestmark``, and a
conftest hook decides the final level by a five-rule precedence that
exists nowhere in the source text of the test.

An AST walk cannot see any of that, and no amount of configuring which
marker NAMES to look for would help: the defect is *where* the marker
comes from, not which one it is.

So capture is consumer-side, exactly as the runtime overlay already
works — you run your own suite, nexus ingests the artifact::

    pytest --collect-only -q \\
           -p sphinxcontrib.nexus.pytest_manifest \\
           --nexus-manifest=.nexus/traces/markers.json

    nexus runtime-ingest .nexus/traces/markers.json --kind pytest --run markers

``--collect-only`` means nothing executes: this is a collection pass, so
it costs seconds and is safe to run in CI on every commit.

The hook runs ``trylast`` so that every conftest that modifies items has
already had its say — capturing before them would record the spelling
again and reintroduce the very gap this exists to close.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

#: Marks pytest itself adds for mechanics rather than meaning. They say
#: nothing about what a test CLAIMS, and they are numerous enough to
#: swamp the ones that do.
_MECHANICAL = frozenset({"parametrize", "usefixtures", "filterwarnings"})


def pytest_addoption(parser: Any) -> None:
    parser.addoption(
        "--nexus-manifest",
        action="store",
        default=None,
        metavar="PATH",
        help="Write a nexus marker manifest (resolved at collection) to PATH.",
    )


def _mark_value(mark: Any) -> Any:
    """A JSON-safe value for one mark.

    Three shapes, because markers carry meaning three ways and
    flattening them would lose the distinction a consumer needs:
    ``@mark.slow`` is a FLAG, ``@mark.verifies("a", "b")`` is a SET, and
    ``@mark.vv_level(level="L1")`` is a MAPPING.
    """
    args = [a for a in mark.args if isinstance(a, (str, int, float, bool))]
    kwargs = {
        k: v for k, v in mark.kwargs.items()
        if isinstance(v, (str, int, float, bool))
    }
    if not args and not kwargs:
        return True
    if args and not kwargs:
        return args[0] if len(args) == 1 else args
    return {"args": args, **kwargs} if args else kwargs


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: Any, items: Any) -> None:
    """Record every collected item with the marks pytest resolved for it.

    ⚠ ``trylast`` is load-bearing, not politeness: a project's conftest
    computes marks HERE, and running before it would capture the
    pre-resolution state — i.e. exactly the AST answer this replaces.
    """
    out_path = config.getoption("--nexus-manifest")
    if not out_path:
        return

    rootdir = Path(str(config.rootdir)).resolve()
    records = []
    for item in items:
        marks: dict[str, Any] = {}
        # iter_markers walks function -> class -> module, so a
        # module-level `pytestmark` arrives here and a decorator walk
        # never sees it. Closest wins, which is pytest's own precedence.
        for mark in item.iter_markers():
            if mark.name in _MECHANICAL or mark.name in marks:
                continue
            marks[mark.name] = _mark_value(mark)
        if not marks:
            continue
        relpath, lineno, _name = item.location
        records.append({
            "nodeid": item.nodeid,
            # `item.location` is 0-based; every other position in the
            # graph is 1-based, and mixing the two silently binds each
            # test to whatever precedes it.
            "file": str((rootdir / relpath).resolve()),
            "lineno": (lineno or 0) + 1,
            "markers": marks,
        })

    payload = {
        "schema": 1,
        "rootdir": str(rootdir),
        "collected": len(items),
        "with_markers": len(records),
        "tests": records,
    }
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=1), encoding="utf-8")

"""LSP↔graph parity oracle — the analyzer drift guard.

Pyright sees the same source files through a completely independent
implementation (its own parser, its own import resolution, its own
call resolution). Sampling that independent view against the graph
catches whole classes of analyzer bugs automatically — symbols
silently dropped, symbols invented for files that don't exist (the
51%-worktree-contamination class), call edges resolved to the wrong
target.

Two probes, per the structural-independence doctrine:

1. ``documentSymbol`` per file vs the graph's def-like nodes for that
   file. Equality modulo the analyzer's DELIBERATE granularity: nested
   defs are not extracted (closures aren't importable API — pinned by
   ``test_node_at_function_body``), and variables/constants are not
   def-like.
2. ``callHierarchy/incomingCalls`` per function vs graph ``callers``.
   Static calls must agree exactly; for dynamic dispatch
   (``self.method()``) the expectation is graph ⊆ pyright — the gap is
   the Phase-F4 quantity.

The whole module skips when ``pyright-langserver`` is unavailable
(it's an optional dev dependency; the pip wrapper downloads the npm
bundle on first run).
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sphinxcontrib.nexus.ast_analyzer import analyze_directory
from sphinxcontrib.nexus.graph import NodeType
from sphinxcontrib.nexus.query import GraphQuery


def _find_pyright_langserver() -> str | None:
    """The venv's own script first (matches the pinned dev dep), then
    whatever is on PATH."""
    venv_script = Path(sys.executable).parent / "pyright-langserver"
    if venv_script.exists():
        return str(venv_script)
    return shutil.which("pyright-langserver")


PYRIGHT_LS = _find_pyright_langserver()

pytestmark = pytest.mark.skipif(
    PYRIGHT_LS is None,
    reason="pyright-langserver not installed (optional dev dependency)",
)

# LSP SymbolKind values (the spec's enum, abridged to what we compare)
KIND_CLASS = 5
KIND_METHOD = 6
KIND_CONSTRUCTOR = 9
KIND_FUNCTION = 12
DEF_LIKE_KINDS = {KIND_CLASS, KIND_METHOD, KIND_CONSTRUCTOR, KIND_FUNCTION}

_TIMEOUT_S = 120.0  # first run may download the npm bundle


class LspClient:
    """Minimal JSON-RPC-over-stdio LSP client.

    Synchronous single-outstanding-request pump: while waiting for a
    response it answers server→client requests (``workspace/...``)
    with empty results and drops notifications. Reads are
    timeout-guarded via ``selectors`` so a wedged server fails the
    test instead of hanging the suite.
    """

    def __init__(self, command: str, root: Path) -> None:
        self._proc = subprocess.Popen(
            [command, "--stdio"],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._buf = bytearray()
        self._next_id = 0
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._proc.stdout, selectors.EVENT_READ)

    # -- framing ----------------------------------------------------

    def _read_message(self) -> dict:
        while True:
            header_end = self._buf.find(b"\r\n\r\n")
            if header_end != -1:
                headers = bytes(self._buf[:header_end]).decode("ascii")
                length = next(
                    int(line.split(":", 1)[1])
                    for line in headers.split("\r\n")
                    if line.lower().startswith("content-length")
                )
                body_start = header_end + 4
                if len(self._buf) >= body_start + length:
                    body = bytes(self._buf[body_start:body_start + length])
                    del self._buf[:body_start + length]
                    return json.loads(body)
            if not self._selector.select(timeout=_TIMEOUT_S):
                raise TimeoutError(
                    f"pyright-langserver sent nothing for {_TIMEOUT_S}s"
                )
            chunk = os.read(self._proc.stdout.fileno(), 65536)  # type: ignore[union-attr]
            if not chunk:
                raise RuntimeError("pyright-langserver closed its stdout")
            self._buf.extend(chunk)

    def _write_message(self, msg: dict) -> None:
        body = json.dumps(msg).encode("utf-8")
        frame = b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
        assert self._proc.stdin is not None
        self._proc.stdin.write(frame)
        self._proc.stdin.flush()

    # -- protocol ---------------------------------------------------

    def notify(self, method: str, params: dict) -> None:
        self._write_message(
            {"jsonrpc": "2.0", "method": method, "params": params}
        )

    def request(self, method: str, params: dict):
        self._next_id += 1
        req_id = self._next_id
        self._write_message(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        while True:
            msg = self._read_message()
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg["result"]
            if "method" in msg and "id" in msg:
                # Server→client request: answer emptily but correctly —
                # workspace/configuration wants one entry per item.
                if msg["method"] == "workspace/configuration":
                    items = msg["params"].get("items", [])
                    result: object = [None] * len(items)
                else:
                    result = None
                self._write_message(
                    {"jsonrpc": "2.0", "id": msg["id"], "result": result}
                )
            # Notifications (logMessage, publishDiagnostics): drop.

    def initialize(self, root: Path) -> None:
        self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": root.as_uri(),
            "capabilities": {},
        })
        self.notify("initialized", {})

    def open_file(self, path: Path) -> None:
        self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": path.as_uri(),
                "languageId": "python",
                "version": 1,
                "text": path.read_text(),
            }
        })

    def document_symbols(self, path: Path) -> list[dict]:
        """Flat ``SymbolInformation[]`` (we advertise no hierarchical
        support, so the server flattens with ``containerName``)."""
        return self.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": path.as_uri()}},
        ) or []

    def incoming_calls(self, path: Path, line0: int, char0: int) -> list[dict]:
        items = self.request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line0, "character": char0},
        }) or []
        assert items, f"no call-hierarchy item at {path}:{line0 + 1}"
        return self.request(
            "callHierarchy/incomingCalls", {"item": items[0]}
        ) or []

    def close(self) -> None:
        try:
            self.request("shutdown", {})
            self.notify("exit", {})
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()
        finally:
            self._selector.close()


# ---------------------------------------------------------------------------
# Fixture project — covers the constructs whose extraction could drift:
# module functions, methods, classes, nested defs (deliberately
# excluded), cross-module calls, same-module calls, dynamic dispatch.
# ---------------------------------------------------------------------------

ALPHA_SRC = '''\
from beta import helper

CONSTANT = 3.0


def top(x):
    return helper(x) + _local(x)


def _local(x):
    def inner(y):
        return y
    return inner(x)


class Solver:
    def run(self):
        return top(1.0)

    def _step(self):
        return self.run()


def dispatch(s: Solver):
    return s.run()
'''

BETA_SRC = '''\
def helper(x):
    return x * 2.0


def unused():
    return helper(0.0)
'''


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("parity_project")
    (root / "alpha.py").write_text(ALPHA_SRC)
    (root / "beta.py").write_text(BETA_SRC)
    return root


@pytest.fixture(scope="module")
def graph(project: Path) -> GraphQuery:
    return GraphQuery(analyze_directory(project, project_root=project))


@pytest.fixture(scope="module")
def lsp(project: Path):
    assert PYRIGHT_LS is not None
    client = LspClient(PYRIGHT_LS, project)
    client.initialize(project)
    for name in ("alpha.py", "beta.py"):
        client.open_file(project / name)
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Probe 1: documentSymbol vs graph nodes, per file
# ---------------------------------------------------------------------------


def _graph_def_symbols(q: GraphQuery, module: str) -> set[tuple[str | None, str]]:
    """(container, name) pairs of the graph's def-like nodes in one
    module — the same shape pyright's flat symbols reduce to."""
    def_like = {NodeType.FUNCTION.value, NodeType.METHOD.value, NodeType.CLASS.value}
    out: set[tuple[str | None, str]] = set()
    for _, data in q.knowledge_graph.nxgraph.nodes(data=True):
        if data.get("type") not in def_like:
            continue
        qname = data.get("name", "")
        parts = qname.split(".")
        if not qname.startswith(f"{module}."):
            continue
        local = parts[1:]  # strip the module
        container = local[-2] if len(local) > 1 else None
        out.add((container, local[-1]))
    return out


def _lsp_def_symbols(symbols: list[dict]) -> set[tuple[str | None, str]]:
    """(container, name) pairs of pyright's def-like symbols, with the
    analyzer's deliberate exclusions removed: a def nested inside a
    FUNCTION (closure) is not importable API and the graph skips it."""
    function_names = {
        s["name"] for s in symbols if s["kind"] == KIND_FUNCTION
    }
    out: set[tuple[str | None, str]] = set()
    for s in symbols:
        if s["kind"] not in DEF_LIKE_KINDS:
            continue
        container = s.get("containerName") or None
        if container in function_names:
            continue  # closure — deliberately not in the graph
        out.add((container, s["name"]))
    return out


def test_document_symbols_match_graph_nodes(graph, lsp, project):
    """Per file, pyright's independently-derived def-like symbol set
    must EQUAL the graph's. A symbol the analyzer dropped or invented
    is drift; tolerated differences (closures, variables) are encoded
    in the reducers, not in a weaker assertion."""
    for module in ("alpha", "beta"):
        lsp_set = _lsp_def_symbols(lsp.document_symbols(project / f"{module}.py"))
        graph_set = _graph_def_symbols(graph, module)
        assert graph_set == lsp_set, (
            f"{module}.py drift: graph-only={graph_set - lsp_set}, "
            f"pyright-only={lsp_set - graph_set}"
        )


def test_graph_excludes_closure_pyright_sees(graph, lsp, project):
    """The granularity boundary itself, pinned from both sides: pyright
    sees ``inner`` (it IS a symbol in the file), the graph deliberately
    does not (closures aren't importable API)."""
    raw_names = {s["name"] for s in lsp.document_symbols(project / "alpha.py")}
    assert "inner" in raw_names
    assert all(
        not data.get("name", "").endswith(".inner")
        for _, data in graph.knowledge_graph.nxgraph.nodes(data=True)
    )


# ---------------------------------------------------------------------------
# Probe 2: incomingCalls vs graph callers
# ---------------------------------------------------------------------------


def _def_position(path: Path, name: str) -> tuple[int, int]:
    """0-based (line, char) of ``name`` in its def/class statement —
    call-hierarchy requests must point at the symbol name itself."""
    for lineno0, line in enumerate(path.read_text().splitlines()):
        stripped = line.lstrip()
        if stripped.startswith((f"def {name}(", f"class {name}:", f"class {name}(")):
            return lineno0, line.index(name)
    raise AssertionError(f"{name} not found in {path}")


def _lsp_caller_names(calls: list[dict]) -> set[str]:
    return {c["from"]["name"] for c in calls}


def _graph_caller_names(q: GraphQuery, node_id: str) -> set[str]:
    return {n.name.rsplit(".", 1)[-1] for n in q.callers(node_id).nodes}


def test_static_callers_agree_exactly(graph, lsp, project):
    """Cross-module and same-module STATIC calls: the two views must
    agree exactly. A miss here means import/call resolution drifted."""
    line0, char0 = _def_position(project / "beta.py", "helper")
    lsp_callers = _lsp_caller_names(
        lsp.incoming_calls(project / "beta.py", line0, char0)
    )
    graph_callers = _graph_caller_names(graph, "py:function:beta.helper")
    assert graph_callers == lsp_callers == {"top", "unused"}


def test_dynamic_dispatch_callers_graph_subset_of_pyright(graph, lsp, project):
    """Dynamic dispatch in two strengths. ``self.run()`` the analyzer
    DOES resolve (same-class self-dispatch — pinned here so the
    capability can't silently regress). ``s.run()`` through an
    annotated parameter needs type inference: pyright resolves it,
    the syntactic analyzer does not. The contract is therefore
    direction, not equality — graph ⊆ pyright — and the difference is
    the Phase-F4 decision input (pyright-enriched call edges)."""
    line0, char0 = _def_position(project / "alpha.py", "run")
    lsp_callers = _lsp_caller_names(
        lsp.incoming_calls(project / "alpha.py", line0, char0)
    )
    # pyright resolves both dispatch strengths
    assert {"_step", "dispatch"} <= lsp_callers
    graph_callers = _graph_caller_names(graph, "py:method:alpha.Solver.run")
    # the analyzer resolves self-dispatch — capability pin
    assert "_step" in graph_callers
    assert graph_callers <= lsp_callers, (
        f"graph claims callers pyright refutes: {graph_callers - lsp_callers}"
    )

"""Structural body fingerprint for twin-path (Type-2/3 clone) detection.

A function body is reduced to a set of normalized *k-gram shingles* over its
AST. Identifiers and literals are normalized — variable renaming does not
change the fingerprint (Type-2 robustness) — while operators (``@``, ``*``,
…), attribute/method names (``einsum``, ``solve``, ``reshape``, …) and
subscripting are KEPT. Those carry the actual computation that distinguishes
one numeric kernel from another, and which the *call graph cannot see*:
operator overloads and array indexing produce no call edges, so two functions
that both do ``psi @ A`` + ``np.einsum(...)`` + slicing look identical to the
call graph yet are perfectly captured here.

Two functions whose shingle sets have high :func:`jaccard` overlap are
independent reimplementations of the same computation — the coding-elegance
Pattern-2 / twin-path smell. The single source of truth for the fingerprint
scheme: the extractor stamps it onto each function node, the query layer reads
it back, and tests exercise it through this module.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Collection

#: k-gram width over the normalized token stream. 4 is wide enough that a
#: shared shingle reflects a shared *sub-structure*, not an incidental
#: single-node coincidence, while short enough to survive small edits.
SHINGLE_K = 4


def _token(node: ast.AST) -> str:
    """Normalized token for one AST node.

    Names and literals collapse to a placeholder (rename-invariance); the
    computation-bearing nodes keep their discriminating detail.
    """
    if isinstance(node, ast.Attribute):
        return "A:" + node.attr               # einsum / solve / reshape / T / sum …
    if isinstance(node, ast.Call):
        return "Call"
    if isinstance(node, ast.Name):
        return "N"                             # normalize identifiers (Type-2)
    if isinstance(node, ast.Constant):
        return "C"                             # normalize literals
    if isinstance(node, ast.BinOp):
        return "B:" + type(node.op).__name__   # captures MatMult '@', Mult, Add, …
    if isinstance(node, ast.UnaryOp):
        return "U:" + type(node.op).__name__
    if isinstance(node, ast.AugAssign):
        return "Aug:" + type(node.op).__name__
    if isinstance(node, ast.Compare):
        return "Cmp"
    if isinstance(node, ast.Subscript):
        return "Sub"                           # slicing / indexing
    if isinstance(node, ast.arg):
        return "arg"
    return type(node).__name__


def _token_stream(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Pre-order node-type tokens over the body.

    The signature is excluded (we fingerprint behaviour, not the contract)
    and a leading string-literal docstring is dropped (documentation is not
    computation).
    """
    out: list[str] = []

    def rec(node: ast.AST) -> None:
        out.append(_token(node))
        for child in ast.iter_child_nodes(node):
            rec(child)

    for stmt in func.body:
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue  # docstring
        rec(stmt)
    return out


def _hash_shingle(gram: tuple[str, ...]) -> int:
    """Deterministic 64-bit hash of a k-gram.

    Uses blake2b rather than the builtin ``hash`` so the value is stable
    across processes (``hash`` is salted by ``PYTHONHASHSEED``) — the
    fingerprint is persisted in the graph and must compare equal between the
    build that wrote it and any later session that reads it.
    """
    digest = hashlib.blake2b("\x1f".join(gram).encode("utf-8"), digest_size=8)
    return int.from_bytes(digest.digest(), "big")


def body_fingerprint(
    func: ast.FunctionDef | ast.AsyncFunctionDef, k: int = SHINGLE_K,
) -> tuple[list[int], int]:
    """Return ``(sorted unique shingle hashes, token count)`` for a body.

    The token count gauges substance: a bare ``return self.x``, a one-line
    delegation, or an empty stub carries too little structure to judge
    similarity, so consumers filter on a minimum-token threshold rather than
    drowning in trivial-template matches.
    """
    tokens = _token_stream(func)
    n = len(tokens)
    if n < k:
        return [], n
    shingles = {
        _hash_shingle(tuple(tokens[i : i + k])) for i in range(n - k + 1)
    }
    return sorted(shingles), n


def jaccard(a: Collection[int], b: Collection[int]) -> float:
    """Jaccard similarity of two shingle-hash sets; 0.0 if either is empty."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    if not inter:
        return 0.0
    return inter / (len(sa) + len(sb) - inter)

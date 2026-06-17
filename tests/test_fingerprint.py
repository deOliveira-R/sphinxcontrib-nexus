"""AST body fingerprint — the twin-path clone signal."""
from __future__ import annotations

import ast

from sphinxcontrib.nexus.fingerprint import body_fingerprint, jaccard


def _fp(src: str) -> tuple[list[int], int]:
    return body_fingerprint(ast.parse(src).body[0])  # type: ignore[arg-type]


# Same computation, different identifiers and loop variables (Type-2 clone).
WDD_A = """
def sweep_a(psi, sig, dx, mu):
    out = np.zeros_like(psi)
    for i in range(len(psi)):
        num = mu[i] * psi[i - 1] + 0.5 * sig[i] * dx[i] * psi[i]
        den = mu[i] + sig[i] * dx[i]
        out[i] = num / den
    return out
"""
WDD_B = """
def march_b(flux, xs, h, omega):
    result = np.zeros_like(flux)
    for k in range(len(flux)):
        top = omega[k] * flux[k - 1] + 0.5 * xs[k] * h[k] * flux[k]
        bot = omega[k] + xs[k] * h[k]
        result[k] = top / bot
    return result
"""
# Structurally unrelated: registry lookup + raise, no array math.
DIFFERENT = """
def lookup(name, registry):
    factory = registry.get(name)
    if factory is None:
        raise ValueError(name)
    return factory(name)
"""


def test_alpha_rename_is_identical():
    a, _ = _fp(WDD_A)
    b, _ = _fp(WDD_B)
    assert a == b                       # variable/loop renaming is invisible
    assert jaccard(a, b) == 1.0


def test_different_computation_low_similarity():
    a, _ = _fp(WDD_A)
    d, _ = _fp(DIFFERENT)
    assert jaccard(a, d) < 0.3


def test_docstring_is_dropped():
    no_doc = "def f(a, b):\n    c = a + b\n    return c * a\n"
    with_doc = 'def f(a, b):\n    """Add then scale."""\n    c = a + b\n    return c * a\n'
    assert _fp(no_doc)[0] == _fp(with_doc)[0]


def test_deterministic_across_calls():
    # blake2b hashing — stable regardless of PYTHONHASHSEED
    assert _fp(WDD_A)[0] == _fp(WDD_A)[0]


def test_token_count_tracks_substance():
    _, big = _fp(WDD_A)
    _, small = _fp("def f(x):\n    return x + 1\n")
    assert big > small


def test_trivial_body_yields_no_shingles():
    shingles, ntokens = _fp("def p():\n    pass\n")
    assert shingles == []                # fewer than k tokens
    assert ntokens < 4


def test_jaccard_edges():
    assert jaccard([], [1, 2]) == 0.0
    assert jaccard([1, 2, 3], [1, 2, 3]) == 1.0
    assert jaccard([1, 2], [3, 4]) == 0.0
    assert jaccard([1, 2, 3, 4], [3, 4, 5, 6]) == 2 / 6

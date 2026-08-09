"""Tracked-install behaviour for `nexus setup`.

The load-bearing property: a consumer's local edit is field-tested
knowledge and must survive an upgrade. `nexus setup` copies instruction
files into a project where they then evolve; overwriting that evolution
silently destroys the only record of what real usage taught. These tests
pin the no-clobber contract and the drift reporting that makes the
harvest loop possible.
"""

from __future__ import annotations

import json

import pytest

from sphinxcontrib.nexus import __version__
from sphinxcontrib.nexus.install import (
    MANIFEST_NAME,
    classify,
    diff_payload,
    install_payload,
    iter_payloads,
    load_manifest,
    write_manifest,
)


@pytest.fixture
def bundle(tmp_path):
    """A miniature package tree: one skill file plus one rule."""
    pkg = tmp_path / "pkg"
    (pkg / "skills" / "demo").mkdir(parents=True)
    (pkg / "skills" / "demo" / "SKILL.md").write_text("shipped v1\n")
    (pkg / "rules").mkdir(parents=True)
    (pkg / "rules" / "demo-rule.md").write_text("rule v1\n")
    return pkg


@pytest.fixture
def targets(tmp_path):
    claude = tmp_path / "project" / ".claude"
    return claude / "skills", claude / "rules", claude / MANIFEST_NAME


def _payloads(bundle, targets, *, with_rules=True):
    skills_target, rules_target, _ = targets
    return list(iter_payloads(
        bundle, skills_target, rules_target if with_rules else None,
    ))


def _install_all(bundle, targets):
    payloads = _payloads(bundle, targets)
    for payload in payloads:
        install_payload(payload, backup=False)
    write_manifest(targets[2], payloads, {})
    return payloads


# ---------------------------------------------------------------------------
# Payload discovery
# ---------------------------------------------------------------------------


def test_iter_payloads_covers_skills_and_rules(bundle, targets):
    keys = {p.key for p in _payloads(bundle, targets)}
    assert keys == {"skills/demo/SKILL.md", "rules/demo-rule.md"}


def test_rules_excluded_when_declined(bundle, targets):
    keys = {p.key for p in _payloads(bundle, targets, with_rules=False)}
    assert keys == {"skills/demo/SKILL.md"}


def test_skill_dir_without_skill_md_is_skipped(bundle, targets):
    (bundle / "skills" / "notaskill").mkdir()
    (bundle / "skills" / "notaskill" / "notes.md").write_text("x\n")
    keys = {p.key for p in _payloads(bundle, targets)}
    assert "skills/notaskill/notes.md" not in keys


# ---------------------------------------------------------------------------
# classify — the four states
# ---------------------------------------------------------------------------


def test_missing_before_install(bundle, targets):
    payload = _payloads(bundle, targets)[0]
    assert classify(payload, {}).state == "missing"


def test_up_to_date_right_after_install(bundle, targets):
    payloads = _install_all(bundle, targets)
    manifest = load_manifest(targets[2])
    assert all(classify(p, manifest).state == "up-to-date" for p in payloads)


def test_stale_when_upstream_moves_and_consumer_did_not(bundle, targets):
    _install_all(bundle, targets)
    (bundle / "skills" / "demo" / "SKILL.md").write_text("shipped v2\n")
    payload = _payloads(bundle, targets)[0]
    assert classify(payload, load_manifest(targets[2])).state == "stale"


def test_modified_when_consumer_edits_and_upstream_did_not(bundle, targets):
    payloads = _install_all(bundle, targets)
    payloads[0].dest.write_text("shipped v1\nLOCAL EDIT\n")
    status = classify(payloads[0], load_manifest(targets[2]))
    # Plain "modified": upstream has nothing new for this file, so an
    # upgrade has nothing to offer and must not touch it.
    assert status.state == "modified"


def test_modified_and_stale_when_both_sides_move(bundle, targets):
    payloads = _install_all(bundle, targets)
    payloads[0].dest.write_text("shipped v1\nLOCAL EDIT\n")
    (bundle / "skills" / "demo" / "SKILL.md").write_text("shipped v2\n")
    status = classify(payloads[0], load_manifest(targets[2]))
    assert status.state == "modified-and-stale"


def test_untracked_existing_file_counts_as_modified(bundle, targets):
    """No manifest entry = no proof the content is ours to overwrite."""
    payload = _payloads(bundle, targets)[0]
    payload.dest.parent.mkdir(parents=True)
    payload.dest.write_text("hand-written by the consumer\n")
    assert classify(payload, {}).state == "modified"


def test_corrupt_manifest_degrades_to_modified_not_crash(bundle, targets):
    payloads = _install_all(bundle, targets)
    targets[2].write_text("{not json")
    manifest = load_manifest(targets[2])
    assert manifest == {}
    # Safe direction: everything looks modified, so nothing is clobbered.
    payloads[0].dest.write_text("whatever\n")
    assert classify(payloads[0], manifest).state == "modified"


def test_installed_version_is_reported(bundle, targets):
    payloads = _install_all(bundle, targets)
    status = classify(payloads[0], load_manifest(targets[2]))
    assert status.installed_version == __version__


# ---------------------------------------------------------------------------
# Manifest bookkeeping
# ---------------------------------------------------------------------------


def test_skipped_files_keep_their_previous_manifest_entry(bundle, targets):
    """A skipped (locally-modified) file must retain the baseline of what
    we last WROTE — otherwise the next classify loses the ability to tell
    consumer edits from upstream moves."""
    payloads = _install_all(bundle, targets)
    skill, rule = payloads[0], payloads[1]
    before = load_manifest(targets[2])["files"][skill.key]["shipped_sha256"]

    skill.dest.write_text("consumer edit\n")
    (bundle / "skills" / "demo" / "SKILL.md").write_text("shipped v2\n")
    # Second run writes only the rule; the skill is skipped.
    write_manifest(targets[2], [rule], load_manifest(targets[2]))

    after = load_manifest(targets[2])["files"][skill.key]["shipped_sha256"]
    assert after == before
    assert classify(skill, load_manifest(targets[2])).state == "modified-and-stale"


def test_manifest_is_valid_json_with_version(bundle, targets):
    _install_all(bundle, targets)
    data = json.loads(targets[2].read_text())
    assert data["nexus_version"] == __version__
    assert set(data["files"]) == {"skills/demo/SKILL.md", "rules/demo-rule.md"}


# ---------------------------------------------------------------------------
# Diff + backup
# ---------------------------------------------------------------------------


def test_diff_direction_is_consumer_additions(bundle, targets):
    """'+' must be the CONSUMER's content — that is the harvest direction."""
    payloads = _install_all(bundle, targets)
    payloads[0].dest.write_text("shipped v1\nCONSUMER WISDOM\n")
    text = "".join(diff_payload(payloads[0]))
    assert "+CONSUMER WISDOM" in text


def test_backup_preserves_the_local_edit(bundle, targets):
    payloads = _install_all(bundle, targets)
    payloads[0].dest.write_text("precious local edit\n")
    install_payload(payloads[0], backup=True)
    backup = payloads[0].dest.with_suffix(payloads[0].dest.suffix + ".bak")
    assert backup.read_text() == "precious local edit\n"
    assert payloads[0].dest.read_text() == "shipped v1\n"


def test_binary_payload_is_copied_not_diffed(bundle, targets):
    (bundle / "skills" / "demo" / "scripts").mkdir()
    blob = bundle / "skills" / "demo" / "scripts" / "probe.bin"
    blob.write_bytes(b"\xff\xfe\x00binary")
    payload = next(
        p for p in _payloads(bundle, targets) if p.key.endswith("probe.bin")
    )
    install_payload(payload, backup=False)
    assert payload.dest.read_bytes() == b"\xff\xfe\x00binary"
    assert diff_payload(payload) == []
    # Unreadable-as-text on both sides must not masquerade as drift.
    assert classify(payload, {}).state == "up-to-date"


# ---------------------------------------------------------------------------
# The shipped bundle itself
# ---------------------------------------------------------------------------


def test_real_bundle_ships_skills_and_the_routing_rule(tmp_path):
    from pathlib import Path

    import sphinxcontrib.nexus as nexus_pkg

    package_root = Path(nexus_pkg.__file__).parent
    payloads = list(iter_payloads(
        package_root, tmp_path / "skills", tmp_path / "rules",
    ))
    keys = {p.key for p in payloads}
    assert "rules/nexus-tools.md" in keys, "routing rule must ship"
    assert any(k.startswith("skills/nexus-exploring/") for k in keys)
    assert any(k.startswith("skills/nexus-elegance/") for k in keys)


def test_routing_rule_names_the_deferred_tool_gotcha(tmp_path):
    """The dominant live cause of an agent silently avoiding the graph.
    If this line is ever dropped the rule loses its highest-value note."""
    from pathlib import Path

    import sphinxcontrib.nexus as nexus_pkg

    rule = (Path(nexus_pkg.__file__).parent / "rules" / "nexus-tools.md").read_text()
    assert "ToolSearch" in rule
    assert "deferral is not unavailability" in rule.lower()

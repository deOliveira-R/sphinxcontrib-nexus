"""Tracked installation of bundled skills and rules into a consumer project.

`nexus setup` copies instruction files (skills, an always-on routing rule)
into a consuming project, where they then **evolve against real sessions**.
That evolution is valuable — a consumer's edit is field-tested against
usage the shipped copy never saw — so this module treats the consumer's
tree as a peer, not a cache:

* a **manifest** records what was written and the hash of the SHIPPED
  content at install time, so a later run can tell "consumer edited this"
  apart from "we shipped a new version";
* a locally-modified file is never silently overwritten (``--force``
  opts in, and always leaves a ``.bak``);
* ``--check`` reports drift in both directions and ``--diff`` prints it,
  so upstream can harvest what downstream learned.

The manifest lives beside the installed files rather than inside them:
stamping a version into skill frontmatter would edit the very content
whose modification we are trying to detect.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sphinxcontrib.nexus import __version__

#: Manifest filename, written into the target's `.claude/` directory.
MANIFEST_NAME = "nexus-install-manifest.json"

#: Manifest format version — bumped only if the on-disk shape changes.
MANIFEST_SCHEMA = 1


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path: Path) -> str | None:
    """File content, or ``None`` when absent/unreadable.

    Binary or undecodable payloads (a stray image in a skill's
    ``scripts/``) read as ``None`` and are treated as untracked: copied,
    never diffed.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


@dataclass(frozen=True)
class Payload:
    """One bundled file and where it installs to.

    ``key`` is the manifest key and the human-facing name: a path
    relative to the target root (e.g. ``skills/nexus-guide/SKILL.md``).
    """

    key: str
    source: Path
    dest: Path


@dataclass(frozen=True)
class FileStatus:
    """Install state of one payload, as reported by ``--check``."""

    key: str
    state: str  # missing | up-to-date | stale | modified | modified-and-stale
    installed_version: str | None
    dest: Path

    @property
    def needs_attention(self) -> bool:
        return self.state != "up-to-date"


def iter_payloads(
    package_root: Path,
    skills_target: Path,
    rules_target: Path | None,
) -> Iterator[Payload]:
    """Every bundled file to install, with its destination.

    Skills come from ``<package>/skills/<name>/**`` and land under
    ``skills_target``. The routing rule comes from ``<package>/rules/``
    and lands under ``rules_target`` when one is given — it is an
    always-on context cost, so callers may decline it.
    """
    skills_src = package_root / "skills"
    if skills_src.is_dir():
        for skill_dir in sorted(p for p in skills_src.iterdir() if p.is_dir()):
            if not (skill_dir / "SKILL.md").is_file():
                continue
            for item in sorted(skill_dir.rglob("*")):
                if not item.is_file() or item.name == ".DS_Store":
                    continue
                rel = item.relative_to(skills_src)
                yield Payload(
                    key=f"skills/{rel.as_posix()}",
                    source=item,
                    dest=skills_target / rel,
                )

    rules_src = package_root / "rules"
    if rules_target is not None and rules_src.is_dir():
        for item in sorted(rules_src.glob("*.md")):
            yield Payload(
                key=f"rules/{item.name}",
                source=item,
                dest=rules_target / item.name,
            )


def load_manifest(manifest_path: Path) -> dict:
    """Prior install record, or an empty one.

    A corrupt manifest degrades to "no record" rather than raising: the
    worst case is that every file looks locally modified, which is the
    safe direction — nothing gets clobbered.
    """
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def classify(payload: Payload, manifest: dict) -> FileStatus:
    """Compare shipped, installed, and last-installed content.

    Three hashes decide the verdict: what we ship now, what is on disk,
    and what we shipped when this file was last installed.

    * on disk == shipped now                       → up-to-date
    * on disk == what we shipped then, != now      → stale (safe to update)
    * on disk != what we shipped then              → modified (never clobber)
    * modified AND upstream also moved since       → modified-and-stale

    The last case is the one that needs a human: both sides changed, so
    neither copy is a superset. ``modified`` alone means only the
    consumer moved — upstream has nothing new to offer that file.

    A file with no manifest entry but present on disk counts as modified:
    with no record of what we wrote, we cannot prove the difference is
    ours to overwrite.
    """
    entry = (manifest.get("files") or {}).get(payload.key) or {}
    installed_version = entry.get("version")

    shipped_now = _read(payload.source)
    on_disk = _read(payload.dest)

    if not payload.dest.exists():
        return FileStatus(payload.key, "missing", installed_version, payload.dest)
    if on_disk is None or shipped_now is None:
        # Unreadable/binary on either side — treat as up-to-date so it is
        # copied without diffing rather than reported as spurious drift.
        return FileStatus(payload.key, "up-to-date", installed_version, payload.dest)

    on_disk_hash = _sha256(on_disk)
    if on_disk_hash == _sha256(shipped_now):
        return FileStatus(payload.key, "up-to-date", installed_version, payload.dest)

    shipped_then = entry.get("shipped_sha256")
    if shipped_then is None:
        return FileStatus(payload.key, "modified", installed_version, payload.dest)
    if on_disk_hash == shipped_then:
        return FileStatus(payload.key, "stale", installed_version, payload.dest)
    # Consumer edited it. Whether upstream ALSO moved decides which of
    # the two modified states applies — comparing against what we
    # shipped then, not against the consumer's copy.
    state = (
        "modified" if shipped_then == _sha256(shipped_now)
        else "modified-and-stale"
    )
    return FileStatus(payload.key, state, installed_version, payload.dest)


def diff_payload(payload: Payload) -> list[str]:
    """Unified diff from the shipped file to the installed one.

    Direction matters: ``+`` lines are what the CONSUMER has and we do
    not. That is the harvest direction — the thing worth reading.
    """
    shipped = _read(payload.source)
    on_disk = _read(payload.dest)
    if shipped is None or on_disk is None:
        return []
    return list(difflib.unified_diff(
        shipped.splitlines(keepends=True),
        on_disk.splitlines(keepends=True),
        fromfile=f"shipped/{payload.key}",
        tofile=f"installed/{payload.key}",
    ))


def install_payload(payload: Payload, *, backup: bool) -> None:
    """Copy one payload, optionally preserving what is already there."""
    payload.dest.parent.mkdir(parents=True, exist_ok=True)
    if backup and payload.dest.exists():
        shutil.copy2(payload.dest, payload.dest.with_suffix(payload.dest.suffix + ".bak"))
    shutil.copy2(payload.source, payload.dest)


def write_manifest(
    manifest_path: Path,
    written: list[Payload],
    previous: dict,
) -> None:
    """Record what this run wrote, preserving entries it skipped.

    Skipped (locally-modified) files keep their OLD entry: their record
    still describes the last content this tool actually wrote, which is
    exactly the baseline the next ``classify`` needs.
    """
    files = dict(previous.get("files") or {})
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for payload in written:
        content = _read(payload.source)
        if content is None:
            continue
        files[payload.key] = {
            "shipped_sha256": _sha256(content),
            "version": __version__,
            "installed_at": stamp,
        }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "nexus_version": __version__,
                "updated_at": stamp,
                "files": files,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

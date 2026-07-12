#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0
"""Cascading release chain for the terok package family.

Plan-then-execute architecture: generate a release plan (JSON), validate
it, then execute step-by-step with crash-recovery.  Supports full and
GitHub-prerelease releases, master and from-PR sources, and any mix.

Chain spec grammar (positional arg to ``quick`` and ``plan``):
    pkg              one package (equivalent to ``pkg..pkg``)
    pkg:NUM          release pkg from PR #NUM
    pkg%LEVEL        bump pkg at LEVEL instead of --version-step
    A..B             range; intermediates filled from CHAIN order
    A..B%LEVEL       range; every expanded package bumps at LEVEL
    A,B,C            literal list — released exactly as named, no cascade
    A,B:NUM%LEVEL..C any combination

Selection is literal: a bare name or comma-list is taken at face value.
For a full cascade through dependents, use a range (``sandbox..terok``).
Non-contiguous selections (e.g. ``clearance,executor``) are allowed but
produce a yellow warning when an unbumped intermediate (here: sandbox)
means a downstream release won't see the new upstream version.

Publish targets (``--target``):
    pypi             production — auto-triggered ``release.yml`` lands the
                     wheel on PyPI after tag push (default)
    testpypi         first-release validation per package, or occasional
                     workflow-change dry-run; chain script dispatches
                     ``release.yml`` with target=testpypi to publish to
                     TestPyPI instead
    gh-only          GitHub Release only, no PyPI/TestPyPI

Version steps:
    ``--version-step`` (default ``patch``) sets the bump level for every
    released package; a ``%LEVEL`` suffix in the chain spec overrides it
    per package.  The level reflects each package's own API delta, which
    the chain can't infer — spell it out wherever it differs from the
    run's default.  A suffix on a range applies to every package the
    range expands to; for per-package granularity inside a range, write
    the literal list instead.

Dev-cycle integration tags (pre-release bump levels):
    ``alpha``, ``beta`` and ``rc`` cut ``vX.Y.ZaN``/``bN``/``rcN``
    pre-release tags between real PyPI releases so a cross-repo PR chain
    can pin to tagged wheels on master instead of git-branch refs.  Each
    stage takes an optional base size — ``alpha-patch`` (what bare
    ``alpha`` means), ``alpha-minor``, ``alpha-major``, likewise for
    ``beta-*`` and ``rc-*`` — choosing how far the series jumps past the
    last release; a later stage on the same base restarts the counter
    (``0.8.6a3`` + ``beta`` → ``0.8.6b1``), and stepping back down a
    stage on the same base is rejected.  Shortcuts: ``maj``/``min`` for
    the final levels, ``a``/``amin``/``amaj``, ``b``/``bmin``/``bmaj``,
    ``rcmin``/``rcmaj``.  Pre-release levels always imply
    ``--target=gh-only`` and the GH prerelease flag; every repo goes
    ``X.Y.Z`` → ``X.Y.(Z+1)a1``-style rather than silently promoting to
    final.  Pre-release is all-or-nothing: those plan-wide implications
    can't hold for half a run, so mixing pre-release and final levels
    via ``%LEVEL`` overrides is rejected (stages and sizes may vary).
    Promote to a real release at the end of the cycle with a final
    level — the suffix is dropped and PyPI publish resumes.

Usage:
    terok-release quick sandbox
    terok-release quick sandbox..terok --open-top
    terok-release quick sandbox:42,executor:55,terok:706 --open-top
    terok-release quick clearance,sandbox:221..terok
    terok-release quick util..shield%minor,executor:412%patch,terok%patch
    terok-release quick mkdocs --target=testpypi
    terok-release quick sandbox..terok --version-step=alpha
    terok-release quick sandbox..terok --version-step=bmin
    terok-release quick util..terok --version-step=rc
    terok-release open feat/comms clearance
    terok-release plan sandbox..terok -o plan.json
    terok-release show plan.json
    terok-release execute plan.json
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple, Never

import click
import tomlkit
from pydantic import VERSION as _pydantic_ver, BaseModel, Field
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

if int(_pydantic_ver.split(".")[0]) < 2:
    raise SystemExit(f"pydantic >= 2 required (found {_pydantic_ver}): pip install 'pydantic>=2'")

console = Console(stderr=True)


# ── Chain ─────────────────────────────────────────────────────────────────

CHAIN = [
    "mkdocs-terok",
    "terok-util",
    "terok-clearance",
    "terok-shield",
    "terok-sandbox",
    "terok-executor",
    "terok",
]

# When you add a new inter-package dep, update this table and the
# consuming package's ``pyproject.toml`` in the same PR — the planner
# cross-checks the two and aborts the next release otherwise.
#
# ``mkdocs-terok`` is a docs-only sibling: every other repo uses it for
# their docs build but it is *not* a runtime pin in any pyproject, so it
# stays an empty-deps leaf — release it on its own when it changes.
#
# ``terok-util`` is the bottom of the runtime chain — every other
# package depends on it; it depends on nothing in the ecosystem.
DEPS: DepGraph = {
    "mkdocs-terok": [],
    "terok-util": [],
    "terok-clearance": ["terok-util"],
    "terok-shield": ["terok-util"],
    "terok-sandbox": ["terok-util", "terok-shield", "terok-clearance"],
    "terok-executor": ["terok-util", "terok-sandbox"],
    "terok": ["terok-util", "terok-executor", "terok-sandbox", "terok-shield", "terok-clearance"],
}

ALIASES = (
    {repo.removeprefix("terok-"): repo for repo in CHAIN}
    | {repo: repo for repo in CHAIN}
    | {"mkdocs": "mkdocs-terok"}  # the only repo without a `terok-` prefix
)


# ── Tuning ────────────────────────────────────────────────────────────────
#
# Seconds everywhere unless noted.

DEFAULT_CHECK_TIMEOUT = 1800  # 30 min — long enough for a full CI matrix
DEFAULT_WHEEL_TIMEOUT = 300
DEFAULT_PYPI_TIMEOUT = 600  # 10 min — TestPyPI propagation can be slow

CHECK_POLL_INTERVAL = 2
CHECK_GRACE_WINDOW = 30  # leniency before missing check data becomes a hard fail
CHECK_STATE_RECHECK = 10  # cadence for PR-state (MERGED/CLOSED) lookups

WHEEL_POLL_INTERVAL = 5
WHEEL_HEAD_TIMEOUT = 10  # per HEAD probe of the actual download URL

PYPI_POLL_INTERVAL = 5
PYPI_HTTP_TIMEOUT = 10
LOCK_INDEX_LAG_TIMEOUT = 600  # 10 min — one full max-age of PyPI's /simple/ page
LOCK_INDEX_LAG_RETRY_INTERVAL = 5
WORKFLOW_DISCOVERY_POLL_INTERVAL = 2
WORKFLOW_DISCOVERY_TIMEOUT = 60  # seconds to wait for the release.yml run to register

MERGE_RACE_POLL_COUNT = 15
MERGE_RACE_POLL_INTERVAL = 2

RELEASE_BRANCH_PREFIX = "chore/release-"
BUMP_DEPS_BRANCH_PREFIX = "chore/bump-deps"
RELEASE_COMMIT_PREFIX = "release:"
BUMP_DEPS_COMMIT = "chore: bump sibling deps"
AUTOMATED_RELEASE_LABEL = "automated-release"


def die(msg: str) -> Never:
    """Print error and exit."""
    console.print(f"[bold red]ERROR:[/] {msg}")
    raise SystemExit(1)


def normalise(name: str) -> str:
    """Accept short names (shield) and full names (terok-shield)."""
    return ALIASES.get(name) or die(f"Unknown repo: {name}")


def pkg_name(repo: str) -> str:
    """terok-shield -> terok_shield."""
    return repo.replace("-", "_")


def slugify(text: str) -> str:
    """Normalize a human-readable name to a safe machine token: [a-z0-9-]."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


_VER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")

#: Pre-release stages in promotion order, with their PEP 440 letters.
STAGE_LETTERS = {"alpha": "a", "beta": "b", "rc": "rc"}

#: Bump levels that cut a real (PyPI-publishable) release.
FINAL_STEPS = ("major", "minor", "patch")

# The bump levels ``--version-step`` accepts — and, prefixed with ``%``,
# the per-package overrides the chain spec accepts.  A bare stage name
# means its ``-patch`` variant; STEP_SHORTCUTS adds the terse spellings.
VERSION_STEPS = (
    *FINAL_STEPS,
    *STAGE_LETTERS,
    *(f"{stage}-{size}" for stage in STAGE_LETTERS for size in FINAL_STEPS),
)

STEP_SHORTCUTS = {
    "maj": "major",
    "min": "minor",
    "a": "alpha",
    "amin": "alpha-minor",
    "amaj": "alpha-major",
    "b": "beta",
    "bmin": "beta-minor",
    "bmaj": "beta-major",
    "rcmin": "rc-minor",
    "rcmaj": "rc-major",
}

#: Every accepted ``--version-step`` / ``%LEVEL`` spelling.
ACCEPTED_STEPS = (*VERSION_STEPS, *STEP_SHORTCUTS)


def canonical_step(level: str) -> str:
    """Resolve shortcuts and bare stage names to a canonical bump level.

    ``a`` → ``alpha`` → ``alpha-patch``; final levels pass through.
    """
    level = STEP_SHORTCUTS.get(level, level)
    return f"{level}-patch" if level in STAGE_LETTERS else level


def bump_version(ver: str, level: str = "patch") -> str:
    """``X.Y.Z`` or ``X.Y.Z{a|b|rc}N`` → next version at the given level.

    Pre-release levels — ``alpha``/``beta``/``rc``, each with a
    ``-patch``/``-minor``/``-major`` base size (bare stage = ``-patch``)
    — cut or continue a dev-cycle integration tag (no PyPI): on a final
    version they open a new series one base-bump ahead; on a running
    series whose base already carries the requested size they increment
    it; a later stage on the same base restarts the counter
    (``0.8.6a3`` + ``beta`` → ``0.8.6b1``).  Stepping back down a stage
    on the same base is rejected.

    Final levels applied to a pre-release *promote* — the suffix is
    dropped, then the base version is bumped (except ``patch``, which
    promotes in place: ``X.Y.ZaN`` → ``X.Y.Z``).
    """
    m = _VER_RE.match(ver) or die(f"unparseable version: {ver}")
    major, minor, patch = int(m[1]), int(m[2]), int(m[3])
    cur_letter = m[4]
    match canonical_step(level):
        case "major":
            return f"{major + 1}.0.0"
        case "minor":
            return f"{major}.{minor + 1}.0"
        case "patch":
            return (
                f"{major}.{minor}.{patch}"  # promote pre-release → final
                if cur_letter
                else f"{major}.{minor}.{patch + 1}"
            )
        case step:
            stage, _, size = step.partition("-")
            return _bump_prerelease(ver, stage, size)


def _bump_prerelease(ver: str, stage: str, size: str) -> str:
    """Cut, continue, or stage-promote a pre-release series.

    The numeric triple of a running series is already the future release
    (``0.8.6a2`` targets ``0.8.6``), so the requested *size* is measured
    against that: a base that already carries the bump stays (the series
    continues, or a later *stage* restarts its counter on it); anything
    else opens a fresh series one *size*-bump ahead.
    """
    m = _VER_RE.match(ver)
    major, minor, patch = int(m[1]), int(m[2]), int(m[3])
    cur_letter, cur_n = m[4], int(m[5]) if m[5] else 0
    letter = STAGE_LETTERS[stage]

    in_series = cur_letter is not None
    base = {
        "patch": (major, minor, patch) if in_series else (major, minor, patch + 1),
        "minor": (major, minor, 0) if in_series and patch == 0 else (major, minor + 1, 0),
        "major": (major, 0, 0) if in_series and minor == patch == 0 else (major + 1, 0, 0),
    }[size]
    if base != (major, minor, patch):
        return "{}.{}.{}{}1".format(*base, letter)

    order = list(STAGE_LETTERS.values())
    if order.index(letter) < order.index(cur_letter):
        die(
            f"{ver} cannot step back to {stage} on the same base — "
            f"open the next cycle instead (e.g. {stage}-minor)"
        )
    if letter == cur_letter:
        return f"{major}.{minor}.{patch}{letter}{cur_n + 1}"
    return f"{major}.{minor}.{patch}{letter}1"


def build_chain(start: str, end: str | None = None) -> list[str]:
    """Slice CHAIN from start to end (inclusive)."""
    i = CHAIN.index(start) if start in CHAIN else die(f"Unknown: {start}")
    if not end:
        return CHAIN[i:]
    j = CHAIN.index(end) if end in CHAIN else die(f"Unknown: {end}")
    return CHAIN[i : j + 1] if j >= i else die(f"{end} is not downstream of {start}")


def wheel_filename(repo: str, version: str) -> str:
    """terok-sandbox 0.0.50 -> terok_sandbox-0.0.50-py3-none-any.whl."""
    return f"{pkg_name(repo)}-{version}-py3-none-any.whl"


def wheel_url(org: str, repo: str, version: str) -> str:
    """Construct the GitHub release wheel URL."""
    return (
        f"https://github.com/{org}/{repo}/releases/download/"
        f"v{version}/{wheel_filename(repo, version)}"
    )


def published_url(target: str, org: str, repo: str, version: str) -> str:
    """URL of the just-released package, for the end-of-run summary.

    For ``pypi`` / ``testpypi`` targets the project page on the chosen
    index; for ``gh-only`` the GitHub Release page (no PyPI artifact
    was created).
    """
    if target == "testpypi":
        return f"https://test.pypi.org/project/{repo}/{version}/"
    if target == "pypi":
        return f"https://pypi.org/project/{repo}/{version}/"
    return f"https://github.com/{org}/{repo}/releases/tag/v{version}"


# ── Domain types ──────────────────────────────────────────────────────────

# Package → in-chain packages it depends on.
type DepGraph = dict[str, list[str]]

# Sibling package → version string to pin for it.
type SiblingVersions = dict[str, str]

# Package → GitHub PR number (the release-from-PR workflow).
type PrSpecs = dict[str, int]

# Package → bump level from a chain-spec ``%LEVEL`` override.
type LevelSpecs = dict[str, str]

# Package → new version string, for packages already processed in this run.
type ReleasedVersions = dict[str, str]


# ── Plan model ────────────────────────────────────────────────────────────


class StepKind(StrEnum):
    CLONE_SYNC = "clone_sync"
    CHECKOUT = "checkout"
    VERSION_BUMP = "version_bump"
    DEP_UPDATE = "dep_update"
    CHANGELOG_UPDATE = "changelog_update"
    LOCK = "lock"
    GIT_COMMIT = "git_commit"
    GIT_PUSH = "git_push"
    PR_CREATE = "pr_create"
    PR_LABEL = "pr_label"
    PR_MERGE = "pr_merge"
    TAG = "tag"
    RELEASE = "release"
    WHEEL_POLL = "wheel_poll"
    WORKFLOW_DISPATCH = "workflow_dispatch"
    WORKFLOW_WAIT = "workflow_wait"
    PYPI_POLL = "pypi_poll"


class Action(StrEnum):
    RELEASE_MASTER = "release_master"
    RELEASE_PR = "release_pr"
    DEPS_ONLY = "deps_only"
    SKIP = "skip"


class Step(BaseModel):
    """One atomic operation in the release plan."""

    id: str
    kind: StepKind
    package: str
    params: dict[str, Any] = {}
    status: str = "pending"
    result: dict[str, Any] = {}


class PackagePlan(BaseModel):
    """What to do with one package in the chain."""

    repo: str
    action: Action
    current_version: str
    new_version: str | None = None
    pr_number: int | None = None
    pr_branch: str | None = None
    pr_url: str | None = None
    """Populated up-front for ``:PR`` overrides; set by ``PR_CREATE`` for
    master releases as soon as the script opens its own PR.  Surfaced at the
    operator-attention points (per-package banner, merge-with-failures
    prompt, exception handler, end-of-run summary)."""
    pr_title: str | None = None
    """PR title — used to seed release notes for ``:PR`` releases, where
    ``gh api releases/generate-notes`` can't see the unmerged PR's commits
    yet.  Synthesised ``* <title> in <pr_url>`` line is appended to the
    notes body so the seeded draft reflects what the release will include."""
    sibling_deps: dict[str, str] = {}
    notes_path: str | None = None
    """Filesystem path (string for JSON round-trip) to the per-release
    Markdown notes file.  Seeded by ``_seed_notes()`` after plan generation
    with the output of ``gh api releases/generate-notes``; consumed by the
    ``RELEASE`` step (``gh release create --notes-file``) and, for final
    releases only, by ``CHANGELOG_UPDATE``.  Falls back to
    ``--generate-notes`` if the file is missing at execute time."""
    previous_release_version: str | None = None
    """Anchor version for the auto-generated release notes.  For final
    cuts, the most recent non-prerelease tag (so the summary spans the
    whole alpha cycle, not just the promotion diff); for alpha cuts, the
    latest tag of any kind (per-iteration delta); ``None`` for first-ever
    releases (gh-api falls back to since-beginning-of-history)."""


class Plan(BaseModel):
    """Complete release plan — serializable to JSON."""

    packages: list[PackagePlan]
    steps: list[Step]
    gh_org: str
    gh_fork: str
    release_name: str = ""
    prerelease: bool = False
    """When True, publish as a GitHub prerelease (hidden from the "Latest"
    badge on the repo homepage).  Useful for batching half-done work that
    downstream packages need to pin against, without promoting it to the
    public release pointer."""
    target: str = "pypi"
    """Where each release publishes its Python wheel:

    - ``"pypi"`` — production. Tag push auto-triggers ``release.yml`` and
      its ``pypi-publish`` job lands the wheel on PyPI.
    - ``"testpypi"`` — first-release validation per package (or occasional
      workflow-change dry-runs). Chain script dispatches ``release.yml``
      with ``target=testpypi`` so the ``testpypi-publish`` job routes the
      wheel to TestPyPI instead.
    - ``"gh-only"`` — GitHub Release without any PyPI publish. For
      non-PyPI projects or release candidates that should not hit any
      index. Chain script dispatches with ``target=gh-only``.

    Either way the GitHub Release is created with the wheel attached."""
    pin_style: str = "pypi"
    """How sibling deps are pinned in the released wheel's pyproject:

    - ``"pypi"`` — version specifier (``terok-shield = "^0.6.38"``).
      The default for ``pypi``/``testpypi`` targets — consumers
      resolve siblings via the published index.
    - ``"url"`` — GH release wheel URL (``terok-shield = { url = "..." }``).
      The default for ``gh-only`` — consumers need the GH release wheel
      to install, since nothing went to PyPI.

    PyPI rejects uploads with direct-URL deps, so ``pin_style=url`` is
    incompatible with ``target ∈ {pypi, testpypi}`` and the planner
    refuses the combination."""
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


# ── Runtime context ───────────────────────────────────────────────────────


@dataclass
class Ctx:
    """Mutable runtime state threaded through executor calls."""

    cache_dir: Path
    dry_run: bool = False
    auto_yes: bool = False
    skip_checks: bool = False
    check_timeout: int = DEFAULT_CHECK_TIMEOUT
    wheel_timeout: int = DEFAULT_WHEEL_TIMEOUT
    pypi_timeout: int = DEFAULT_PYPI_TIMEOUT
    plan_path: Path | None = None


# ── Shell helpers ─────────────────────────────────────────────────────────


def sh(
    *args: str, cwd: Path | None = None, capture: bool = False, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess — always surfaces stderr on failure.

    With ``capture=False`` (default) stdout streams to the terminal and
    stderr is tee'd: it's both displayed in real time *and* buffered so
    the failure message can include it.  Previously stderr was inherited
    from the parent process without buffering, so when an unattended
    command failed the chain script reported "exit N" with no details
    visible because the offending lines had been overwritten by Rich's
    progress indicators or otherwise lost.

    With ``capture=True`` both stdout and stderr are captured silently
    and surfaced on failure (existing behaviour, unchanged).
    """
    if capture:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    else:
        # Stream stderr through Python so we both display and buffer it.
        proc = subprocess.Popen(args, cwd=cwd, stderr=subprocess.PIPE, text=True)
        stderr_buf = io.StringIO()
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                sys.stderr.write(line)
                sys.stderr.flush()
                stderr_buf.write(line)
        finally:
            proc.wait()
        r = subprocess.CompletedProcess(
            args=proc.args,
            returncode=proc.returncode,
            stdout="",
            stderr=stderr_buf.getvalue(),
        )

    if check and r.returncode:
        parts: list[str] = []
        if r.stderr and r.stderr.strip():
            parts.append(r.stderr.strip())
        if r.stdout and r.stdout.strip():
            parts.append(r.stdout.strip())
        cmd = " ".join(args)
        msg = f"Command failed (exit {r.returncode}): {cmd}"
        if parts:
            msg += "\n" + "\n".join(parts)
        die(msg)
    return r


# ── TOML ops ──────────────────────────────────────────────────────────────
#
# Uses tomlkit to preserve comments and formatting.
#
# Runtime deps live in PEP 621 ``[project.dependencies]`` as an array of
# PEP 508 strings (``name @ url``, ``name>=X,<Y``, ``name @ git+url@ref``).
# The chain script finds entries by their leading project name and rewrites
# the whole string in place.  The no-git-metadata version fallback
# (``[project].dynamic = ["version"]``) lives at
# ``[tool.hatch.version].fallback-version`` (read by hatch-vcs);
# ``set_version_toml`` writes it.


_PEP508_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _pep508_name(s: str) -> str:
    """Extract the project name from a PEP 508 dep string (everything up to
    the first version/URL/marker boundary)."""
    m = _PEP508_NAME_RE.match(s)
    return m.group(1) if m else ""


def _toml_deps(path: Path) -> tuple[tomlkit.TOMLDocument, Any]:
    """Return ``(doc, runtime_deps_array)`` for ``[project.dependencies]``."""
    doc = tomlkit.parse(path.read_text())
    return doc, doc["project"]["dependencies"]


def _find_dep_index(deps: Any, dep_repo: str) -> int | None:
    """Index of the PEP 508 string in *deps* whose name matches *dep_repo*.

    Tries both hyphen (``terok-shield``) and underscore (``terok_shield``)
    forms since PEP 503 treats them equivalently.
    """
    targets = {dep_repo, pkg_name(dep_repo)}
    for i, entry in enumerate(deps):
        if _pep508_name(str(entry)) in targets:
            return i
    return None


def set_version_toml(path: Path, version: str):
    """Set the no-git-metadata fallback version in pyproject.toml.

    The real release version always comes from the git tag; this field only
    keeps tarball/no-git builds honest.  It lives at
    ``[tool.hatch.version].fallback-version`` (read by hatch-vcs).
    """
    doc = tomlkit.parse(path.read_text())
    doc["tool"]["hatch"]["version"]["fallback-version"] = version
    path.write_text(tomlkit.dumps(doc))


def lock_repo(repo_dir: Path):
    """Regenerate the repo's lockfile."""
    sh("uv", "lock", cwd=repo_dir)


def set_dep_url(path: Path, dep_repo: str, version: str, org: str):
    """Set a sibling dep to a wheel URL via PEP 508 ``name @ url`` syntax."""
    doc, deps = _toml_deps(path)
    idx = _find_dep_index(deps, dep_repo)
    if idx is None:
        return
    name = _pep508_name(str(deps[idx])) or dep_repo
    deps[idx] = f"{name} @ {wheel_url(org, dep_repo, version)}"
    path.write_text(tomlkit.dumps(doc))


def set_dep_pypi(path: Path, dep_repo: str, version: str):
    """Set a sibling dep to a PyPI version range (PEP 508, caret-equivalent).

    ``terok-shield = "^0.6.38"`` (Poetry) → ``terok-shield>=0.6.38,<0.7.0``
    (PEP 508), matching Poetry caret semantics: the upper bound bumps the
    leftmost non-zero segment.  Operators wanting strict ``==`` pins can
    edit the resulting string manually; the chain script doesn't
    second-guess that decision per release.
    """
    doc, deps = _toml_deps(path)
    idx = _find_dep_index(deps, dep_repo)
    if idx is None:
        return
    name = _pep508_name(str(deps[idx])) or dep_repo
    parts = [int(p) for p in version.split(".")]
    major = parts[0]
    minor = parts[1] if len(parts) > 1 else 0
    patch = parts[2] if len(parts) > 2 else 0
    if major > 0:
        upper = f"{major + 1}.0.0"
    elif minor > 0:
        upper = f"0.{minor + 1}.0"
    else:
        upper = f"0.0.{patch + 1}"
    deps[idx] = f"{name}>={version},<{upper}"
    path.write_text(tomlkit.dumps(doc))


def set_branch_dep(path: Path, dep_repo: str, branch: str, fork: str):
    """Set a sibling dep to a git+branch ref via PEP 508."""
    doc, deps = _toml_deps(path)
    idx = _find_dep_index(deps, dep_repo)
    if idx is None:
        return
    name = _pep508_name(str(deps[idx])) or dep_repo
    deps[idx] = f"{name} @ git+https://github.com/{fork}/{dep_repo}.git@{branch}"
    path.write_text(tomlkit.dumps(doc))


def pinned_version(path: Path, dep_repo: str, org: str) -> str | None:
    """Extract version from a URL-pinned sibling dep, or None if git/missing.

    Regex against raw file text — format-agnostic (works whether the pin
    is the old ``{url = ...}`` table or the new PEP 508 ``name @ <url>``).
    """
    m = re.search(rf"{org}/{dep_repo}/releases/download/v([^/]+)/", path.read_text())
    return m.group(1) if m else None


# ── Dep-graph verifier ────────────────────────────────────────────────────
#
# A stale sibling pin in a pyproject.toml (or a missing entry in DEPS)
# would ship a release with a broken transitive pin, so: reconcile the
# two before planning; on any drift, fail fast with a diff.


def _discover_sibling_deps(pyproject_path: Path, family: list[str]) -> list[str]:
    """Members of *family* that appear as PEP 508 entries in ``pyproject_path``.

    ``family`` must be the full package family (typically ``CHAIN``) — not
    a slice.  A sliced family would miss legitimate upstream pins and
    produce false drift reports.  Matches both hyphen (``terok-shield``)
    and underscore (``terok_shield``) forms since PEP 503 treats them
    equivalently.
    """
    _, deps = _toml_deps(pyproject_path)
    names = {_pep508_name(str(d)) for d in deps}
    return [m for m in family if m in names or pkg_name(m) in names]


def _verify_dep_graph(chain: list[str], cache_dir: Path) -> DepGraph:
    """Cross-check vendored ``DEPS`` against each cloned ``pyproject.toml``.

    Walks the whole chain first, collects every discrepancy, then calls
    ``die()`` once with a combined diff — one bad run should surface *all*
    drift in a single shot so the operator can fix everything before the
    next attempt, not one mismatch at a time.  Returns the verified live
    graph (identical to ``DEPS`` after a successful check).
    """
    live: DepGraph = {}
    mismatches: list[str] = []
    for repo in chain:
        found = _discover_sibling_deps(cache_dir / repo / "pyproject.toml", CHAIN)
        declared = DEPS.get(repo, [])
        live[repo] = found
        if set(found) != set(declared):
            mismatches.append(
                f"  {repo}:\n"
                f"    declared in DEPS:   {declared or '[]'}\n"
                f"    found in pyproject: {found or '[]'}"
            )
    if mismatches:
        die(
            "Dependency graph mismatch between vendored DEPS and live pyproject.toml:\n\n"
            + "\n".join(mismatches)
            + "\n\nReconcile before releasing: either update DEPS in this script "
            "(if the sibling dep is legitimate and newly added) or remove the "
            "stale pin from the package's pyproject.toml."
        )
    return live


# ── Clone cache ───────────────────────────────────────────────────────────


def ensure_clone(repo: str, cache_dir: Path, org: str, fork: str, pr: int | None = None):
    """Create or sync a repo clone in the release cache.

    Fetches with ``--tags --prune-tags`` so tags deleted on the remote
    also disappear from the cache.  Otherwise stale local tags can
    fool ``latest_version`` / dep-graph checks into thinking versions
    still exist that have been yanked or rewritten upstream.

    When *pr* is set, the working tree is checked out at that PR's
    head ref (``refs/pull/<N>/head``) instead of ``upstream/master``.
    Dep-graph verification and any other read-pyproject step then sees
    the PR-branch ``pyproject.toml`` — the only correct source when
    the operator is releasing from open PRs (e.g. cross-repo refactors
    that add a new sibling dep on every consumer in one wave).
    """
    repo_dir = cache_dir / repo
    upstream_url = f"git@github.com:{org}/{repo}.git"
    fork_url = f"git@github.com:{fork}/{repo}.git"
    if (repo_dir / ".git").is_dir():
        ref_label = f"PR #{pr}" if pr is not None else "upstream/master"
        console.print(f"  [cyan]{repo:<16}[/] syncing to {ref_label}...", end="\r")
        # Normalize remote URLs every sync in case the operator switched
        # between fork-based and same-org workflows (eg. ``--fork`` flag
        # changed) — saves a manual ``git remote set-url`` on each clone.
        sh("git", "remote", "set-url", "upstream", upstream_url, cwd=repo_dir)
        sh("git", "remote", "set-url", "origin", fork_url, cwd=repo_dir)
        sh(
            "git", "fetch", "upstream", "--quiet", "--tags", "--prune-tags", "--force",
            cwd=repo_dir,
        )  # fmt: skip
    else:
        ref_label = f"PR #{pr}" if pr is not None else "master"
        console.print(f"  [cyan]{repo:<16}[/] cloning ({ref_label})...", end="\r")
        sh("git", "clone", "--quiet", upstream_url, str(repo_dir))
        sh("git", "remote", "rename", "origin", "upstream", cwd=repo_dir)
        sh("git", "remote", "add", "origin", fork_url, cwd=repo_dir)
    _checkout_release_ref(repo_dir, pr=pr)
    console.print(f"  [cyan]{repo:<16}[/] ready     ")


def _checkout_release_ref(repo_dir: Path, *, pr: int | None) -> None:
    """Reset *repo_dir* to ``upstream/master`` (or *pr*'s head ref).

    Splits the actual checkout out of [`ensure_clone`][ensure_clone] so
    re-running with a different *pr* on a cached clone works without
    needing the full sync.  Falls back to ``upstream/master`` when
    *pr* is ``None`` so a mixed run (some packages with PRs, others
    without) lands every clone on the right ref.
    """
    if pr is not None:
        ref = f"refs/pull/{pr}/head"
        sh(
            "git", "fetch", "upstream", "--quiet", "--force",
            f"{ref}:refs/remotes/upstream/pr-{pr}",
            cwd=repo_dir,
        )  # fmt: skip
        sh("git", "reset", "--hard", f"upstream/pr-{pr}", "-q", cwd=repo_dir)
    else:
        sh("git", "reset", "--hard", "upstream/master", "-q", cwd=repo_dir)
    sh("git", "clean", "-fd", "--quiet", cwd=repo_dir)


# ── GitHub ops ────────────────────────────────────────────────────────────


def latest_version(repo: str, org: str) -> str:
    """Most recent release of *repo*, including prereleases.

    Used by ``bump_version`` to derive the next version — prereleases
    must be visible so an alpha-to-final promotion (``v0.7.8a2`` + patch
    → ``v0.7.8``) detects the suffix correctly.
    """
    r = sh(
        "gh",
        "release",
        "list",
        "--repo",
        f"{org}/{repo}",
        "--limit",
        "1",
        "--json",
        "tagName",
        "--jq",
        ".[0].tagName",
        capture=True,
    )
    return r.stdout.strip().lstrip("v") or die(f"No releases for {repo}")


def latest_final_version(repo: str, org: str) -> str | None:
    """Most recent non-prerelease release of *repo*, or ``None`` if there is none.

    Used for ``previous_tag_name`` when generating notes for a final
    release — alpha tags in between would otherwise truncate the
    summary to just the promotion diff and hide the whole cycle's work.
    """
    r = sh(
        "gh", "release", "list",
        "--repo", f"{org}/{repo}",
        "--limit", "50",  # generous window — first non-pre wins
        "--json", "tagName,isPrerelease",
        "--jq", "first(.[] | select(.isPrerelease == false) | .tagName) // empty",
        capture=True,
    )  # fmt: skip
    return r.stdout.strip().lstrip("v") or None


def generate_release_notes(
    org: str,
    repo: str,
    new_version: str,
    previous: str | None,
    pr_number: int | None = None,
    pr_title: str | None = None,
) -> str:
    """Ask GitHub for a draft release-notes body for *new_version*.

    Wraps ``gh api releases/generate-notes`` — the same auto-summary you
    get from ``gh release create --generate-notes``, but materialised
    upfront so the operator can curate it before the tag is pushed.

    *previous* picks the diff anchor: latest non-prerelease for final
    cuts (whole-cycle summary), latest of any kind for alpha cuts
    (per-iteration delta), ``None`` for first-ever releases (gh-api
    falls back to since-beginning-of-history).

    *pr_number* / *pr_title* — when releasing from an unmerged PR, gh's
    auto-summary covers only what's already on master since *previous*,
    so the PR's own commits are invisible.  Splice a synthetic
    ``* <title> in <pr_url>`` line into "What's Changed"; if gh returned
    no body at all, build a minimal one from the PR plus a Full
    Changelog compare link.
    """
    args = [
        "gh", "api",
        f"/repos/{org}/{repo}/releases/generate-notes",
        "-f", f"tag_name=v{new_version}",
        *(["-f", f"previous_tag_name=v{previous}"] if previous else []),
        "--jq", ".body",
    ]
    r = sh(*args, capture=True, check=False)
    body = r.stdout if r.returncode == 0 else ""

    if pr_number and pr_title:
        pr_line = f"* {pr_title} in https://github.com/{org}/{repo}/pull/{pr_number}"
        if "## What's Changed\n" in body:
            body = body.replace(
                "## What's Changed\n",
                f"## What's Changed\n{pr_line}\n",
                1,
            )
        elif body.strip():
            body = f"## What's Changed\n{pr_line}\n\n{body}"
        else:
            compare = (
                f"\n**Full Changelog**: https://github.com/{org}/{repo}/compare/"
                f"v{previous}...v{new_version}\n"
                if previous
                else ""
            )
            body = f"## What's Changed\n{pr_line}\n{compare}"

    return body or (
        f"<!-- gh api generate-notes failed; write notes for v{new_version} here. -->\n"
    )


def pr_info(number: int, gh_repo: str) -> dict:
    """Get PR metadata."""
    r = sh(
        "gh",
        "pr",
        "view",
        str(number),
        "--repo",
        gh_repo,
        "--json",
        "headRefName,state,title,url",
        capture=True,
    )
    return json.loads(r.stdout)


def pr_state(url: str, gh_repo: str) -> str:
    """Query PR state: OPEN, MERGED, CLOSED."""
    r = sh(
        "gh",
        "pr",
        "view",
        url,
        "--repo",
        gh_repo,
        "--json",
        "state",
        "--jq",
        ".state",
        capture=True,
    )
    return r.stdout.strip()


def _require_open_prs(pr_specs: dict[str, int], org: str) -> None:
    """Abort if any PR-pinned repo is not in the ``OPEN`` state.

    The clone cache will happily fetch ``refs/pull/N/head`` for a merged
    or closed PR, leaving the dep-graph check to fail later on whatever
    pyproject shape that PR captured.  Catching it here means a typo
    like ``clearance:12`` (long-merged) fails fast with a clear message
    instead of a ``tomlkit.NonExistentKey: 'project'`` traceback.

    Release officers know what to do with a closed/merged PR (re-run
    against master, pick the right number); we just bail.
    """
    for repo, pr in pr_specs.items():
        state = pr_state(str(pr), f"{org}/{repo}")
        if state != "OPEN":
            die(f"{repo}#{pr} is {state}, expected OPEN — check the PR number")


_MIN_GH_VERSION = (2, 73, 0)
"""Minimum ``gh`` version for ``gh pr checks --json``."""


def _check_gh_version() -> None:
    """Abort early if ``gh`` is too old for the JSON flags we rely on."""
    try:
        r = subprocess.run(["gh", "--version"], capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        die("'gh' (GitHub CLI) not found on PATH")
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout)
    if not m:
        die(f"Cannot parse gh version from: {r.stdout.strip()}")
    installed = tuple(int(x) for x in m.groups())
    if installed < _MIN_GH_VERSION:
        need = ".".join(str(x) for x in _MIN_GH_VERSION)
        have = ".".join(str(x) for x in installed)
        die(f"gh >= {need} required (found {have}). Upgrade: https://github.com/cli/cli/releases")


def _poll_checks(pr_url: str, gh_repo: str, *, in_grace: bool) -> list[dict]:
    """Fetch the PR's checks once.  Empty list means "not ready — keep polling".

    Covers two cases that both want the caller to wait:
    - ``gh`` succeeded but returned an empty list (CI hasn't registered the
      push yet).  Fail-closed: never treat absent CI as "passed" — operators
      whose repo genuinely has none must say so with ``--skip-checks``.
    - ``gh`` errored without stdout (transient API blip, common during the
      grace window right after a fresh push).

    Hard ``gh`` failures outside the grace window die immediately.  ``gh
    pr checks`` exits 8 when checks are failing or pending but still emits
    valid JSON, so 8 is treated as success here.
    """
    r = subprocess.run(
        ["gh", "pr", "checks", pr_url, "--repo", gh_repo, "--json", "name,bucket"],
        capture_output=True,
        text=True,
    )
    if r.returncode not in (0, 8) and not r.stdout.strip():
        if in_grace:
            return []
        die(f"gh pr checks failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()}")
    return json.loads(r.stdout) if r.stdout.strip() else []


def wait_for_checks(pr_url: str, gh_repo: str, ctx: Ctx) -> str:
    """Block until CI settles on the PR.

    Returns ``"passed"`` when checks are green, ``"merged"`` if somebody
    merged the PR out-of-band while waiting.  Failing checks prompt the
    operator to force-merge; flat timeout calls ``die()``.  The grace
    window tolerates the brief gap between push and check registration.
    """
    if ctx.skip_checks:
        console.print("[yellow]Skipping CI checks[/]")
        return "passed"

    console.print(f"Waiting for PR checks (timeout {ctx.check_timeout}s)...")

    for elapsed in range(0, ctx.check_timeout, CHECK_POLL_INTERVAL):
        # Every CHECK_STATE_RECHECK seconds, notice if the PR was merged or
        # closed out-of-band so we don't poll its checks forever.
        if elapsed and elapsed % CHECK_STATE_RECHECK == 0:
            match pr_state(pr_url, gh_repo):
                case "MERGED":
                    console.print("[green]PR merged externally.[/]")
                    return "merged"
                case "CLOSED":
                    die("PR closed without merging.")

        checks = _poll_checks(pr_url, gh_repo, in_grace=elapsed < CHECK_GRACE_WINDOW)
        if not checks or any(c["bucket"] == "pending" for c in checks):
            time.sleep(CHECK_POLL_INTERVAL)
            continue

        failing = [c for c in checks if c["bucket"] in ("fail", "cancel")]
        if not failing:
            console.print("[green]All checks passed![/]")
            return "passed"

        # Colon before the URL so a click-aware terminal doesn't slurp it
        # into the link target (`.../pull/293:` → 404).
        console.print(f"[yellow]Checks failed on:[/] {pr_url}")
        for c in failing:
            console.print(f"  {c['name']}: {c['bucket']}")
        if ctx.auto_yes:
            console.print("[yellow]Force-merging (--yes)[/]")
        elif not alert_confirm(f"Force merge anyway? ({pr_url})", default=False):
            die("Aborted.")
        return "passed"

    die(f"Timed out after {ctx.check_timeout}s")


def _gh_merge_commit(pr_url: str, gh_repo: str) -> str:
    """Commit SHA that the PR was merged into."""
    r = sh(
        "gh", "pr", "view", pr_url, "--repo", gh_repo,
        "--json", "mergeCommit", "--jq", ".mergeCommit.oid",
        capture=True,
    )  # fmt: skip
    return r.stdout.strip()


def squash_merge(pr_url: str, gh_repo: str) -> str:
    """Squash-merge the PR and return the resulting master commit SHA.

    The squash commit's subject/body come from the repo's "Default
    commit message for squash merge" setting (set via Settings → General
    → Pull Requests → "Pull request title and description" / "Pull
    request title").  No client-side override — the repo owns the
    convention.

    Tolerates a narrow race: ``gh pr merge`` can report "already in
    progress" or "already merged" when another automation (or a fast
    operator) got there first — in that case we poll PR state briefly
    rather than giving up.
    """
    console.print("Squash-merging PR...")
    r = subprocess.run(
        [
            "gh", "pr", "merge", pr_url, "--repo", gh_repo,
            "--squash", "--delete-branch", "--admin",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        err = r.stderr + r.stdout
        if "already in progress" in err or "already been merged" in err:
            console.print("[yellow]Merge race — waiting...[/]")
            for _ in range(MERGE_RACE_POLL_COUNT):
                if pr_state(pr_url, gh_repo) == "MERGED":
                    break
                time.sleep(MERGE_RACE_POLL_INTERVAL)
            else:
                die(
                    f"PR still not merged after {MERGE_RACE_POLL_COUNT * MERGE_RACE_POLL_INTERVAL}s"
                )
        else:
            die(f"Merge failed: {err.strip()}")

    sha = _gh_merge_commit(pr_url, gh_repo)
    console.print(f"[green]Merged ({sha[:12]})[/]")
    return sha


def _wheel_downloadable(url: str) -> bool:
    """Whether the wheel is actually downloadable right now (past the GitHub CDN)."""
    req = urllib.request.Request(url, method="HEAD")  # noqa: S310 — GitHub release URL
    try:
        with urllib.request.urlopen(req, timeout=WHEEL_HEAD_TIMEOUT) as resp:  # noqa: S310
            return resp.status == 200  # noqa: PLR2004
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False


def wait_for_wheel(repo: str, version: str, org: str, timeout: int = DEFAULT_WHEEL_TIMEOUT) -> None:
    """Block until the released wheel is downloadable.

    Two-phase check: the GitHub API lists the asset name first, then the
    actual download URL goes live a few seconds later as the CDN
    propagates.  Only both together mean consumers can resolve it.
    """
    expected = wheel_filename(repo, version)
    url = wheel_url(org, repo, version)
    console.print(f"Waiting for {expected}...")
    api_ready = False
    for _elapsed in range(0, timeout, WHEEL_POLL_INTERVAL):
        if not api_ready:
            r = sh(
                "gh", "release", "view", f"v{version}", "--repo", f"{org}/{repo}",
                "--json", "assets", "-q", ".assets[].name",
                capture=True, check=False,
            )  # fmt: skip
            if expected in (r.stdout or ""):
                api_ready = True
        if api_ready and _wheel_downloadable(url):
            console.print("[green]Wheel available![/]")
            return
        time.sleep(WHEEL_POLL_INTERVAL)
    die(f"Timed out waiting for {expected}")


# ── PyPI / Trusted-Publishing helpers ─────────────────────────────────────


def _find_release_run(gh_repo: str, ref: str, event: str) -> str:
    """Return the most recent ``release.yml`` run for *ref* fired by *event*.

    Polls briefly because the run takes a moment to register after a
    tag push (auto-trigger) or ``gh workflow run`` (dispatch). ``ref``
    is the tag name without the ``refs/tags/`` prefix; ``event`` is one
    of ``"push"`` (for ``target=pypi`` — auto-triggered run) or
    ``"workflow_dispatch"`` (for ``target=testpypi`` or ``target=gh-only``
    — script-dispatched).
    """
    for _ in range(0, WORKFLOW_DISCOVERY_TIMEOUT, WORKFLOW_DISCOVERY_POLL_INTERVAL):
        r = sh(
            "gh", "run", "list",
            "--repo", gh_repo,
            "--workflow", "release.yml",
            "--event", event,
            "--branch", ref,            # head_branch == tag for both events
            "--limit", "1",
            "--json", "databaseId,status",
            capture=True, check=False,
        )  # fmt: skip
        if r.returncode == 0 and r.stdout.strip():
            runs = json.loads(r.stdout)
            if runs:
                return str(runs[0]["databaseId"])
        time.sleep(WORKFLOW_DISCOVERY_POLL_INTERVAL)
    die(f"No release.yml run found for {gh_repo} ref {ref} (event={event})")


def wait_for_release_run(gh_repo: str, run_id: str, ref: str, ctx: Ctx) -> None:
    """Watch a ``release.yml`` run, alerting when it pauses for approval.

    GitHub's deployment-protection rules (required reviewers on the
    ``pypi`` environment) park the run in ``waiting`` status until
    approved.  This wrapper polls run status in a plain stdout loop
    (no separate TUI mode, no ``gh run watch`` — Ctrl+C in this loop
    just stops the script, it does not cancel the workflow run), and
    on the first ``waiting`` observation rings the bell + prints a
    banner with both the approval URL *and* the GH release-edit URL.

    The waiting state is the natural moment to edit the auto-generated
    release notes: github-release has already completed by then and
    created the release with default notes.  Surfacing the release-
    edit URL alongside the approval URL turns "waiting for approval"
    into a deliberate review-and-edit-and-approve checkpoint per
    package — exactly what publishing to PyPI deserves.
    """
    alerted = False
    last_status: str | None = None
    approval_url = f"https://github.com/{gh_repo}/actions/runs/{run_id}"
    release_url = f"https://github.com/{gh_repo}/releases/edit/{ref}"
    console.print(f"Watching run {run_id} ({approval_url})...")

    while True:
        r = sh(
            "gh", "run", "view", run_id,
            "--repo", gh_repo,
            "--json", "status,conclusion",
            capture=True, check=False,
        )  # fmt: skip
        if r.returncode != 0 or not r.stdout.strip():
            time.sleep(WORKFLOW_DISCOVERY_POLL_INTERVAL)
            continue
        info = json.loads(r.stdout)
        status = info["status"]

        # Log status transitions as plain lines — one per change, not per
        # poll.  The polling loop stays in the regular CLI flow; no
        # screen-clearing, no captured Ctrl+C.
        if status != last_status:
            console.print(f"  [dim]→ {status}[/]")
            last_status = status

        if status == "waiting" and not alerted:
            console.bell()
            console.print(
                f"\n[black on bright_yellow] APPROVAL NEEDED [/]  {gh_repo}\n"
                f"  [bold]Edit release notes:[/] {release_url}\n"
                f"  [bold]Approve PyPI publish:[/] {approval_url}\n"
            )
            alerted = True

        if status == "completed":
            conclusion = info.get("conclusion") or "?"
            if conclusion == "success":
                console.print(f"[green]Run completed: {conclusion}[/]")
                return
            die(f"Run ended with conclusion '{conclusion}' — see {approval_url}")

        time.sleep(WORKFLOW_DISCOVERY_POLL_INTERVAL)


def pypi_has(repo: str, version: str, target: str) -> bool:
    """Whether *repo*-*version* is indexed on the chosen index right now.

    Asks PyPI's JSON metadata endpoint — the authoritative "this release
    exists" signal (a 200 means the upload is fully registered).  ``target``
    is one of ``"pypi"`` or ``"testpypi"``.
    """
    base = "https://test.pypi.org" if target == "testpypi" else "https://pypi.org"
    url = f"{base}/pypi/{repo}/{version}/json"
    try:
        req = urllib.request.Request(url)  # noqa: S310 — public PyPI endpoint
        with urllib.request.urlopen(req, timeout=PYPI_HTTP_TIMEOUT) as resp:  # noqa: S310
            return resp.status == 200  # noqa: PLR2004
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False


def wait_for_pypi(repo: str, version: str, target: str, timeout: int) -> None:
    """Block until *repo*-*version* resolves on the chosen index."""
    console.print(f"Waiting for {repo} {version} on {target}...")
    for _elapsed in range(0, timeout, PYPI_POLL_INTERVAL):
        if pypi_has(repo, version, target):
            console.print(f"[green]Available on {target}![/]")
            return
        time.sleep(PYPI_POLL_INTERVAL)
    die(f"Timed out waiting for {repo} {version} on {target}")


# ── Planner ───────────────────────────────────────────────────────────────


def _step(pkg: str, seq: int, kind: StepKind, **params: Any) -> Step:
    return Step(id=f"{pkg}.{seq}.{kind}", kind=kind, package=pkg, params=params)


def _branch_for(pkg: PackagePlan, release_name: str) -> str:
    """Branch the work for *pkg* will land on.

    PR-bound packages reuse the PR's own branch.  A new release cuts a
    ``chore/release-<ver>`` branch off ``upstream/master``.  A deps-only
    bump (the open-top default when no PR was supplied) goes onto a single
    shared ``chore/bump-deps[-<slug>]`` branch.
    """
    if pkg.pr_branch:
        return pkg.pr_branch
    if pkg.new_version:
        return f"{RELEASE_BRANCH_PREFIX}{pkg.new_version}"
    suffix = slugify(release_name)
    return f"{BUMP_DEPS_BRANCH_PREFIX}{'-' + suffix if suffix else ''}"


def plan_steps(
    pkg: PackagePlan,
    org: str,
    fork: str,
    name: str,
    target: str,
    pin_style: str,
    prerelease: bool,
) -> list[Step]:
    """Linear step sequence that realises one package's work in the plan."""
    do_release = pkg.action in (Action.RELEASE_MASTER, Action.RELEASE_PR)
    needs_new_pr = pkg.action == Action.RELEASE_MASTER or (
        pkg.action == Action.DEPS_ONLY and not pkg.pr_branch
    )

    branch = _branch_for(pkg, name)
    title = f"{pkg.new_version} {name}".strip() if pkg.new_version else ""
    commit_msg = f"{RELEASE_COMMIT_PREFIX} {title}" if do_release else BUMP_DEPS_COMMIT

    steps: list[Step] = []

    def add(kind: StepKind, **params: Any) -> None:
        steps.append(_step(pkg.repo, len(steps), kind, **params))

    add(StepKind.CLONE_SYNC)
    add(
        StepKind.CHECKOUT,
        branch=branch,
        **({"source": "pr"} if pkg.pr_branch else {"base": "upstream/master"}),
    )
    # Releasing from an existing PR: stamp it with the CodeRabbit skip
    # label *before* the bump commit is pushed, so the auto-review that
    # the push would otherwise trigger never fires.  Freshly-opened
    # release PRs get the same label at PR_CREATE time instead.
    if pkg.action == Action.RELEASE_PR:
        add(StepKind.PR_LABEL, label=AUTOMATED_RELEASE_LABEL)
    for dep, ver in pkg.sibling_deps.items():
        add(StepKind.DEP_UPDATE, dep_repo=dep, dep_version=ver, pin_style=pin_style)
    if do_release:
        add(StepKind.VERSION_BUMP, version=pkg.new_version)
        # Final releases prepend a `## vX.Y.Z — Title` section to
        # ``CHANGELOG.md``; prereleases (alpha/beta/rc cuts) skip — they
        # are cycle-internal integration tags, not user-facing release
        # events.
        if not prerelease:
            add(StepKind.CHANGELOG_UPDATE, version=pkg.new_version, title=name)
    add(StepKind.LOCK)
    add(StepKind.GIT_COMMIT, message=commit_msg)
    add(StepKind.GIT_PUSH, branch=branch, fork=fork)
    if needs_new_pr:
        pr_body = (
            f"Automated release bump to v{pkg.new_version}."
            if do_release
            else "Automated dependency update."
        )
        add(StepKind.PR_CREATE, branch=branch, title=commit_msg, body=pr_body)
    if do_release:
        tag = f"v{pkg.new_version}"
        add(StepKind.PR_MERGE)
        add(StepKind.TAG, tag=tag, title=title)
        add(StepKind.RELEASE, tag=tag, title=title)
        add(StepKind.WHEEL_POLL, version=pkg.new_version)
        # The workflow's ``pypi-publish`` and ``testpypi-publish`` jobs are
        # workflow_dispatch-only — tag push runs ``github-release`` and
        # nothing else.  ``target=gh-only`` is therefore satisfied by the
        # tag push alone (no dispatch needed); pypi/testpypi targets need
        # an explicit dispatch to fire the publish job we want.
        if target != "gh-only":
            add(StepKind.WORKFLOW_DISPATCH, ref=tag)
            add(StepKind.WORKFLOW_WAIT, ref=tag)
            add(StepKind.PYPI_POLL, version=pkg.new_version)
    return steps


def seed_notes(plan: Plan, cache_dir: Path) -> None:
    """Materialise per-package release-notes drafts under *cache_dir*/notes/.

    Idempotent: existing files are preserved, never overwritten — operator
    edits made between ``plan`` and ``execute`` survive.  To force a fresh
    draft, delete the notes file and re-run.
    """
    notes_dir = cache_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    for pkg in plan.packages:
        if not pkg.new_version:
            continue
        path = notes_dir / f"{pkg.repo}-v{pkg.new_version}.md"
        pkg.notes_path = str(path)
        if path.exists():
            continue
        path.write_text(
            generate_release_notes(
                plan.gh_org,
                pkg.repo,
                pkg.new_version,
                pkg.previous_release_version,
                pkg.pr_number,
                pkg.pr_title,
            )
        )


def edit_notes(plan: Plan) -> None:
    """Open ``$EDITOR`` once per releasing package's notes file.

    Click respects ``$EDITOR`` directly; set ``EDITOR=true`` for headless
    / agent-mode runs to make the editor a no-op so the seeded notes ship
    as-is.
    """
    import click as _click  # lazy: editor not needed for plan/execute

    for pkg in plan.packages:
        if pkg.notes_path and Path(pkg.notes_path).exists():
            console.print(f"  Editing notes: [bold]{pkg.repo}[/] → {pkg.notes_path}")
            _click.edit(filename=pkg.notes_path, require_save=False, extension=".md")


def prepend_changelog(path: Path, version: str, title: str, body: str) -> None:
    """Insert ``## v<version> — <title>\\n\\n<body>`` above the first ``## v…`` section.

    No-op when the section is already present (idempotent on resume); falls
    back to appending if the file has no prior versioned sections yet.
    """
    content = path.read_text()
    if f"## v{version}" in content:
        return
    header = f"## v{version} — {title}\n\n" if title else f"## v{version}\n\n"
    section = header + body.rstrip() + "\n\n"
    lines = content.splitlines(keepends=True)
    insert_at = next(
        (i for i, line in enumerate(lines) if line.startswith("## v")),
        len(lines),
    )
    lines.insert(insert_at, section)
    path.write_text("".join(lines))


def _resolve_sibling_version(
    dep: str,
    repo_deps: list[str],
    released: ReleasedVersions,
    planned_pins: dict[str, str],
    repo_dir: Path,
    org: str,
    upgrade_pinned: bool,
) -> str:
    """Version to pin for *dep* in the current repo — most-local first."""
    if dep in released:
        return released[dep]
    # Two downstream repos sharing an upstream must agree on its version
    # even if neither is being released in this run.
    for other in repo_deps:
        if other == dep or other not in released:
            continue
        if from_sibling := planned_pins.get(f"{other}:{dep}"):
            return from_sibling
    current = pinned_version(repo_dir / "pyproject.toml", dep, org)
    if current and not upgrade_pinned:
        return current
    return latest_version(dep, org)


def generate_plan(
    chain: list[str],
    live_deps: DepGraph,
    *,
    org: str,
    fork: str,
    release_name: str,
    version_step: str,
    cache_dir: Path,
    stop_at: str | None = None,
    upgrade_pinned: bool = False,
    pr_specs: PrSpecs | None = None,
    level_specs: LevelSpecs | None = None,
    prerelease: bool = False,
    target: str = "pypi",
    pin_style: str = "pypi",
) -> Plan:
    """Build the full, serialisable release plan for *chain*.

    *live_deps* is the verified live dep graph for the full ``CHAIN``
    family (callers run ``_verify_dep_graph(CHAIN, cache_dir)`` up front
    and pass the result here — and to ``_resolve_chain``, so gap detection
    runs on the same authoritative view).  Emits one ``PackagePlan`` +
    step sequence per repo, in order; downstream repos pick sibling
    versions from what upstream repos ship in the same run.
    """
    packages: list[PackagePlan] = []
    all_steps: list[Step] = []
    released: ReleasedVersions = {}
    planned_pins: dict[str, str] = {}

    for repo in chain:
        current = latest_version(repo, org)
        gh_repo = f"{org}/{repo}"
        repo_dir = cache_dir / repo

        # Determine action — stop_at wins over pr_specs (deps-only, no release)
        pr_num: int | None = None
        pr_branch: str | None = None
        pr_url: str | None = None
        pr_title: str | None = None
        if pr_specs and repo in pr_specs:
            info = pr_info(pr_specs[repo], gh_repo)
            if info.get("state") != "OPEN":
                die(
                    f"PR #{pr_specs[repo]} for {repo} is {info.get('state', 'unknown')} — must be OPEN"
                )
            pr_num, pr_branch, pr_url, pr_title = (
                pr_specs[repo],
                info["headRefName"],
                info["url"],
                info["title"],
            )

        if repo == stop_at:
            action = Action.DEPS_ONLY
        elif pr_num is not None:
            action = Action.RELEASE_PR
        else:
            action = Action.RELEASE_MASTER

        # Chain-wide default, overridden where the spec says ``%LEVEL``.
        level = (level_specs or {}).get(repo, version_step)
        new_ver = bump_version(current, level) if action != Action.DEPS_ONLY else None

        repo_deps = live_deps[repo]
        sibling_deps: SiblingVersions = {}
        for dep in repo_deps:
            ver = _resolve_sibling_version(
                dep, repo_deps, released, planned_pins, repo_dir, org, upgrade_pinned
            )
            sibling_deps[dep] = ver
            planned_pins[f"{repo}:{dep}"] = ver

        # Notes anchor: skip prereleases for final cuts so the summary
        # spans the whole pre-release cycle (not just the promotion
        # diff); use the latest tag of any kind for alpha/beta/rc cuts
        # (per-iteration delta).
        previous = (
            current
            if (prerelease or new_ver is None)
            else (latest_final_version(repo, org) or None)
        )

        pkg = PackagePlan(
            repo=repo,
            action=action,
            current_version=current,
            new_version=new_ver,
            previous_release_version=previous if new_ver else None,
            pr_number=pr_num,
            pr_branch=pr_branch,
            pr_url=pr_url,
            pr_title=pr_title,
            sibling_deps=sibling_deps,
        )
        packages.append(pkg)
        all_steps.extend(
            plan_steps(pkg, org, fork, release_name, target, pin_style, prerelease)
        )
        if new_ver:
            released[repo] = new_ver

    return Plan(
        packages=packages,
        steps=all_steps,
        gh_org=org,
        gh_fork=fork,
        release_name=release_name,
        prerelease=prerelease,
        target=target,
        pin_style=pin_style,
    )


# ── Executor ──────────────────────────────────────────────────────────────


def _package(plan: Plan, repo: str) -> PackagePlan:
    """Locate the ``PackagePlan`` for *repo* in *plan*."""
    return next(p for p in plan.packages if p.repo == repo)


def _find_pr_url(package: str, plan: Plan) -> str:
    """URL of the PR the executor should act on for *package*.

    Prefers ``PackagePlan.pr_url`` (set up-front for ``:PR`` overrides or
    populated by an earlier ``PR_CREATE`` step); falls back to the PR
    number when only that is known.
    """
    pkg = _package(plan, package)
    if pkg.pr_url:
        return pkg.pr_url
    if pkg.pr_number:
        return str(pkg.pr_number)
    die(f"No PR URL found for {package}")


def _merge_sha_for(package: str, plan: Plan) -> str | None:
    """Commit SHA recorded by *package*'s PR_MERGE step, if it ran."""
    for s in plan.steps:
        if s.package == package and s.kind == StepKind.PR_MERGE:
            return s.result.get("merge_sha")
    return None


def _branch_matches_upstream(repo_dir: Path) -> bool:
    """Whether HEAD is already at upstream/master — no release payload to ship.

    Hit when the version bump + lockfile update already landed via an earlier
    feature PR: the release cut has nothing to commit, push, or PR — just tag.
    """
    head = sh("git", "rev-parse", "HEAD", cwd=repo_dir, capture=True).stdout.strip()
    tip = sh("git", "rev-parse", "upstream/master", cwd=repo_dir, capture=True).stdout.strip()
    return head == tip


def _canonical(name: str) -> str:
    """PEP 503 name normalisation — resolver output vs. our repo names."""
    return name.lower().replace("_", "-")


def _pypi_pinned_deps(package: str, plan: Plan) -> dict[str, str]:
    """Sibling deps this plan pypi-pins for *package*, canonical name → version.

    These are the versions *package*'s ``uv lock`` must be able to
    see on the index; a resolver complaint about anything else is not
    release-propagation lag.
    """
    return {
        _canonical(s.params["dep_repo"]): s.params["dep_version"]
        for s in plan.steps
        if s.package == package
        and s.kind == StepKind.DEP_UPDATE
        and s.params.get("pin_style", plan.pin_style) == "pypi"
    }


def lock_riding_index_lag(
    repo_dir: Path, pins: dict[str, str], target: str, timeout: int = LOCK_INDEX_LAG_TIMEOUT
) -> None:
    """Run ``uv lock``, riding out PyPI simple-index propagation lag.

    The publish gate (``wait_for_pypi``) polls PyPI's JSON API — the
    authoritative "this release exists" signal.  uv, however, resolves
    from the separately rendered ``/simple/`` index, whose CDN copy can
    lag the JSON API by minutes right after an upload.

    Polling therefore has to be earned: uv's resolver prose varies too
    much for one complaint regex, so the failure must at least mention
    a dep this plan pinned (*pins*), and the JSON API must confirm
    every mentioned pin exists — only then is there actually something
    to wait for.  Retries pass ``--refresh``, which bypasses uv's
    cached index pages wholesale.  Any other failure dies immediately,
    like a plain checked ``sh`` call.
    """
    for _elapsed in range(0, timeout, LOCK_INDEX_LAG_RETRY_INTERVAL):
        refresh = ["--refresh"] if _elapsed else []
        r = sh("uv", "lock", *refresh, cwd=repo_dir, check=False)
        if r.returncode == 0:
            return
        failure = f"Command failed (exit {r.returncode}): uv lock\n{r.stderr.strip()}"
        stderr_flat = _canonical(" ".join(r.stderr.split()))
        mentioned = {dep: ver for dep, ver in pins.items() if dep in stderr_flat}
        if not mentioned:
            die(failure)
        for dep, version in mentioned.items():
            if not pypi_has(dep, version, target):
                die(f"{failure}\n({dep} {version} is not on {target} at all — nothing to wait for)")
        console.print(
            f"[yellow]{', '.join(mentioned)} on {target}'s JSON API but uv cannot resolve yet "
            f"— retrying with --refresh in {LOCK_INDEX_LAG_RETRY_INTERVAL}s...[/]"
        )
        time.sleep(LOCK_INDEX_LAG_RETRY_INTERVAL)
    die(f"uv lock still failing after {timeout}s of index-lag retries")


def execute_step(step: Step, plan: Plan, ctx: Ctx):
    """Perform the side-effect prescribed by one plan step.

    All irreversible operations live in this dispatch — the planner
    decides, the executor acts.  Each case is idempotent where possible
    so a resumed plan doesn't re-push, re-merge, or re-tag.
    """
    repo_dir = ctx.cache_dir / step.package
    gh_repo = f"{plan.gh_org}/{step.package}"
    p = step.params

    match step.kind:
        case StepKind.CLONE_SYNC:
            ensure_clone(step.package, ctx.cache_dir, plan.gh_org, plan.gh_fork)

        case StepKind.CHECKOUT:
            if p.get("source") == "pr":
                sh("git", "fetch", "origin", p["branch"], cwd=repo_dir)
                sh("git", "checkout", "-B", p["branch"], f"origin/{p['branch']}", cwd=repo_dir)
            else:
                sh("git", "checkout", "-B", p["branch"], p["base"], cwd=repo_dir)

        case StepKind.VERSION_BUMP:
            set_version_toml(repo_dir / "pyproject.toml", p["version"])

        case StepKind.DEP_UPDATE:
            # Plans generated before the pin-style flag landed don't carry
            # the parameter — fall back to the plan-level setting (which
            # defaults to ``"pypi"`` for the same reason).
            pin_style = p.get("pin_style", plan.pin_style)
            if pin_style == "url":
                set_dep_url(
                    repo_dir / "pyproject.toml", p["dep_repo"], p["dep_version"], plan.gh_org
                )
            else:
                set_dep_pypi(repo_dir / "pyproject.toml", p["dep_repo"], p["dep_version"])

        case StepKind.CHANGELOG_UPDATE:
            changelog = repo_dir / "CHANGELOG.md"
            if not changelog.exists():
                console.print(
                    f"[dim]No CHANGELOG.md in {step.package} — skipping prepend.[/]"
                )
                return
            pkg = _package(plan, step.package)
            notes = Path(pkg.notes_path) if pkg.notes_path else None
            if not notes or not notes.exists():
                console.print(
                    f"[dim]No notes file for {step.package} — skipping CHANGELOG update.[/]"
                )
                return
            prepend_changelog(changelog, p["version"], p.get("title", ""), notes.read_text())

        case StepKind.LOCK:
            lock_riding_index_lag(repo_dir, _pypi_pinned_deps(step.package, plan), plan.target)

        case StepKind.GIT_COMMIT:
            paths = ["pyproject.toml", "uv.lock"]
            if (repo_dir / "CHANGELOG.md").exists():
                paths.append("CHANGELOG.md")
            sh("git", "add", *paths, cwd=repo_dir)
            # Idempotent: HEAD already carries this commit message (re-run of a
            # previously-committed step), or nothing is staged (a prior feature
            # PR already landed the version bump + lockfile on master, so the
            # release cut has nothing to commit — just tag and ship).
            head = sh("git", "log", "-1", "--format=%s", cwd=repo_dir, capture=True)
            if head.stdout.strip() == p["message"]:
                console.print("[dim]Already committed — skipping.[/]")
            elif (
                sh("git", "diff", "--cached", "--quiet", cwd=repo_dir, check=False).returncode == 0
            ):
                console.print("[dim]Nothing to commit — release payload already on master.[/]")
            else:
                sh("git", "commit", "-m", p["message"], cwd=repo_dir)

        case StepKind.GIT_PUSH:
            if _branch_matches_upstream(repo_dir):
                console.print("[dim]Branch is at upstream/master — nothing to push.[/]")
            else:
                sh("git", "push", "-u", "origin", p["branch"], "--force-with-lease", cwd=repo_dir)

        case StepKind.PR_CREATE:
            if _branch_matches_upstream(repo_dir):
                console.print("[dim]Branch is at upstream/master — no PR needed.[/]")
                return
            # Idempotent: reuse existing PR for this head branch
            r = sh(
                "gh",
                "pr",
                "view",
                "--repo",
                gh_repo,
                "--head",
                f"{plan.gh_fork}:{p['branch']}",
                "--json",
                "url",
                "--jq",
                ".url",
                capture=True,
                check=False,
            )
            if r.returncode == 0 and r.stdout.strip():
                url = r.stdout.strip()
                console.print(f"PR already exists: {url}")
            else:
                r = sh(
                    "gh", "pr", "create", "--repo", gh_repo,
                    "--base", "master",
                    "--head", f"{plan.gh_fork}:{p['branch']}",
                    "--title", p["title"], "--body", p["body"],
                    "--label", AUTOMATED_RELEASE_LABEL,
                    capture=True,
                )  # fmt: skip
                url = r.stdout.strip()
                console.print(f"PR created: {url}")
            step.result["pr_url"] = url
            # Mirror onto the PackagePlan so the per-package banner, exception
            # handler, and end-of-run summary can all read URLs from one place.
            _package(plan, step.package).pr_url = url

        case StepKind.PR_LABEL:
            # Idempotent: ``--add-label`` is a no-op when the label is
            # already present (a re-run, or a PR opened by an earlier
            # PR_CREATE).  The label must already exist in the repo —
            # the same prerequisite PR_CREATE relies on.
            pr_url = _find_pr_url(step.package, plan)
            sh(
                "gh", "pr", "edit", pr_url,
                "--repo", gh_repo,
                "--add-label", p["label"],
            )  # fmt: skip
            console.print(f"Labelled {pr_url} → {p['label']}")

        case StepKind.PR_MERGE:
            if _branch_matches_upstream(repo_dir):
                # No PR was opened.  Pin ``merge_sha`` to the current
                # upstream/master commit so TAG doesn't re-resolve the
                # moving ref and accidentally tag someone else's push
                # that landed between this step and the tag step.  HEAD
                # equals upstream/master here by the condition above.
                step.result["merge_sha"] = sh(
                    "git", "rev-parse", "HEAD", cwd=repo_dir, capture=True
                ).stdout.strip()
                console.print(
                    f"[dim]No PR to merge — pinning tag to {step.result['merge_sha'][:12]}.[/]"
                )
                return
            pr_url = _find_pr_url(step.package, plan)
            # Idempotent: if already merged, just capture the SHA
            if pr_state(pr_url, gh_repo) == "MERGED":
                step.result["merge_sha"] = _gh_merge_commit(pr_url, gh_repo)
                console.print(f"[dim]Already merged ({step.result['merge_sha'][:12]})[/]")
            elif wait_for_checks(pr_url, gh_repo, ctx) == "merged":
                step.result["merge_sha"] = _gh_merge_commit(pr_url, gh_repo)
            else:
                step.result["merge_sha"] = squash_merge(pr_url, gh_repo)

        case StepKind.TAG:
            sh(
                "git", "fetch", "upstream", "--tags", "--prune-tags", "--force",
                cwd=repo_dir,
            )  # fmt: skip
            target = _merge_sha_for(step.package, plan) or "upstream/master"
            # Idempotent: skip if tag already exists on the expected target
            r = sh(
                "git", "rev-parse", f"refs/tags/{p['tag']}", cwd=repo_dir, capture=True, check=False
            )
            if r.returncode == 0:
                console.print(f"[dim]Tag {p['tag']} already exists — skipping.[/]")
            else:
                # ``-a`` for an annotated tag (-m without -a would be ignored
                # for lightweight tags); operators with ``tag.gpgSign = true``
                # also get a signed annotated tag with this message instead
                # of having git pop an editor for them to fill in by hand.
                message = p.get("title") or p["tag"]
                sh(
                    "git", "tag", "-af", p["tag"], "-m", message, target,
                    cwd=repo_dir,
                )  # fmt: skip
                sh("git", "push", "upstream", p["tag"], cwd=repo_dir)

        case StepKind.RELEASE:
            # Idempotent: skip if release already exists
            r = sh(
                "gh",
                "release",
                "view",
                p["tag"],
                "--repo",
                gh_repo,
                "--json",
                "tagName",
                capture=True,
                check=False,
            )
            if r.returncode == 0:
                console.print(f"[dim]Release {p['tag']} already exists — skipping.[/]")
            else:
                cmd = [
                    "gh", "release", "create", p["tag"],
                    "--repo", gh_repo,
                    "--title", p["title"],
                ]  # fmt: skip
                pkg = _package(plan, step.package)
                notes = Path(pkg.notes_path) if pkg.notes_path else None
                if notes and notes.exists():
                    cmd += ["--notes-file", str(notes)]
                else:
                    cmd.append("--generate-notes")
                if plan.prerelease:
                    cmd.append("--prerelease")
                sh(*cmd)

        case StepKind.WHEEL_POLL:
            wait_for_wheel(step.package, p["version"], plan.gh_org, ctx.wheel_timeout)

        case StepKind.WORKFLOW_DISPATCH:
            sh(
                "gh", "workflow", "run", "release.yml",
                "--repo", gh_repo,
                "--ref", p["ref"],
                "-f", f"target={plan.target}",
            )  # fmt: skip

        case StepKind.WORKFLOW_WAIT:
            # The pypi-publish and testpypi-publish jobs are dispatched
            # explicitly by the preceding WORKFLOW_DISPATCH step — we
            # never wait on a push-triggered run for those jobs.
            run_id = _find_release_run(gh_repo, p["ref"], "workflow_dispatch")
            step.result["run_id"] = run_id
            wait_for_release_run(gh_repo, run_id, p["ref"], ctx)

        case StepKind.PYPI_POLL:
            wait_for_pypi(step.package, p["version"], plan.target, ctx.pypi_timeout)


def save_plan(plan: Plan, path: Path):
    """Snapshot the plan to disk so a crashed run can resume from where it failed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(indent=2))


class ExecMode(StrEnum):
    EXECUTE = "execute"
    """Run every step from scratch."""
    RESUME = "resume"
    """Skip already-completed steps; run the rest (after a crash)."""


def execute_plan(plan: Plan, *, mode: ExecMode, ctx: Ctx) -> Plan:
    """Walk the plan step by step, persisting status between steps.

    On failure the step is marked ``failed``, the plan is saved, and
    the exception propagates — the operator fixes the root cause and
    re-runs ``execute`` on the same plan file to resume.
    """
    last_pkg: str | None = None
    for step in plan.steps:
        if mode == ExecMode.RESUME and step.status == "completed":
            console.print(f"[dim]Skipping completed: {step.id}[/]")
            continue

        # Per-package banner once per repo: shows ``:PR``-supplied URL
        # immediately; for master releases the URL surfaces from PR_CREATE's
        # own log line later in the package's step sequence.
        if step.package != last_pkg:
            pkg = _package(plan, step.package)
            url_suffix = f"  {pkg.pr_url}" if pkg.pr_url else ""
            console.print(f"\n[bold cyan]== {step.package} =={url_suffix}[/]")
            last_pkg = step.package
        console.print(f"  [dim]{step.kind.value}[/]")

        step.status = "running"
        if ctx.plan_path:
            save_plan(plan, ctx.plan_path)
        try:
            execute_step(step, plan, ctx)
            step.status = "completed"
        except (subprocess.CalledProcessError, SystemExit) as exc:
            step.status = "failed"
            step.result["error"] = str(exc)
            if ctx.plan_path:
                save_plan(plan, ctx.plan_path)
                console.print(f"[red]Plan state saved:[/] {ctx.plan_path}")
                console.print(
                    f"[red]Resume with: terok-release execute {ctx.plan_path}[/]"
                )
            pkg = _package(plan, step.package)
            if pkg.pr_url:
                console.print(f"[red]Step operated on:[/] {pkg.pr_url}")
            raise
        if ctx.plan_path:
            save_plan(plan, ctx.plan_path)
    return plan


# ── Operator attention prompts ────────────────────────────────────────────
#
# Long stages (clones, CI waits) tempt the operator to wander; these
# helpers pull their attention back when input is actually needed.


def _alert(prompt_fn: Callable[..., Any], prompt: str, **kwargs: Any) -> Any:
    """Bell + banner + prompt — pull a distracted operator back to the terminal."""
    console.bell()
    console.print("\n[black on bright_yellow] INPUT NEEDED [/]")
    return prompt_fn(prompt, **kwargs)


def alert_confirm(prompt: str, **kwargs: Any) -> bool:
    """Ask a yes/no question loudly enough that a distracted operator notices."""
    return _alert(click.confirm, prompt, **kwargs)


def alert_prompt(prompt: str, **kwargs: Any) -> Any:
    """Ask for free-form input loudly enough that a distracted operator notices."""
    return _alert(click.prompt, prompt, **kwargs)


# ── CLI ───────────────────────────────────────────────────────────────────


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _common_ctx(
    org: str,
    fork: str,
    cache_dir: str,
    pretend: bool,
    yes: bool,
    skip_checks: bool,
    check_timeout: int,
) -> tuple[str, str, Path, Ctx]:
    # ``--fork`` / ``TEROK_GH_FORK`` is mandatory.  For release-officer
    # use (warden in its distrobox), the wrapper injects
    # ``TEROK_GH_FORK=terok-ai`` so release-prep branches land on the org
    # directly; for local runs against a personal fork the operator sets
    # it themselves.  We deliberately don't default it to the org here —
    # silently pushing release branches into the canonical repo for a
    # non-warden operator running locally would be a surprise.
    fork = fork or die("TEROK_GH_FORK is not set (e.g. TEROK_GH_FORK=sliwowitz)")
    cd = Path(cache_dir)
    cd.mkdir(parents=True, exist_ok=True)
    return (
        org,
        fork,
        cd,
        Ctx(
            cache_dir=cd,
            dry_run=pretend,
            auto_yes=yes,
            skip_checks=skip_checks,
            check_timeout=check_timeout,
        ),
    )


class ChainEntry(NamedTuple):
    """One package resolved from the chain spec, with its per-entry overrides."""

    repo: str
    pr: int | None
    level: str | None


def _parse_endpoint(s: str) -> tuple[str, int | None]:
    """Parse one ``pkg`` or ``pkg:PR`` token from a chain spec."""
    if ":" in s:
        name, pr = s.split(":", 1)
        try:
            return normalise(name.strip()), int(pr.strip())
        except ValueError:
            die(f"Bad PR number in '{s}': must be an integer")
    return normalise(s.strip()), None


def _split_level(part: str) -> tuple[str, str | None]:
    """Strip the optional trailing ``%LEVEL`` off one comma-part of a chain spec.

    The suffix goes at the end of the whole part — after the PR number,
    after the range end.  A ``%`` anywhere else (``a%minor..b``) leaves
    range syntax inside the would-be level and fails validation here.
    """
    if "%" not in part:
        return part, None
    rest, level = part.rsplit("%", 1)
    if level not in ACCEPTED_STEPS:
        die(
            f"Bad bump level in '{part}': %LEVEL must end the entry, "
            f"LEVEL one of {', '.join(VERSION_STEPS)} "
            f"(shortcuts: {', '.join(STEP_SHORTCUTS)})"
        )
    return rest, canonical_step(level)


def parse_chain_spec(spec: str) -> list[ChainEntry]:
    """Parse a chain spec into ordered ``ChainEntry`` items.

    Grammar: ``pkg[:PR]`` entries combined with ``,`` and ``..``, each
    comma-part optionally ending in ``%LEVEL``.  A range fills intermediate
    packages from ``CHAIN`` order (each as bare master); its ``%LEVEL``
    applies to every package it expands to.  Each entry tracks its own
    PR override; duplicates raise.
    """
    entries: list[ChainEntry] = []
    seen: set[str] = set()

    def add(repo: str, pr: int | None, level: str | None) -> None:
        if repo in seen:
            die(f"Duplicate package in chain spec: {repo}")
        entries.append(ChainEntry(repo, pr, level))
        seen.add(repo)

    for raw in (p.strip() for p in spec.split(",")):
        if not raw:
            die(f"Empty entry in chain spec: '{spec}'")
        part, level = _split_level(raw)
        if ".." in part:
            start_s, end_s = part.split("..", 1)
            start_repo, start_pr = _parse_endpoint(start_s)
            end_repo, end_pr = _parse_endpoint(end_s)
            for repo in build_chain(start_repo, end_repo):
                pr = start_pr if repo == start_repo else end_pr if repo == end_repo else None
                add(repo, pr, level)
        else:
            name, pr = _parse_endpoint(part)
            add(name, pr, level)
    return entries


def _gap_pairs(resolved: list[str], graph: DepGraph) -> list[tuple[str, str]]:
    """Find ``(downstream, upstream)`` pairs where releases won't propagate.

    A downstream release Q only "sees" a new upstream release P if every
    intermediate package on a dep path Q → … → P is also being released
    (its URL pin gets updated in this run).  Otherwise the unbumped
    intermediate's frozen pin chain still points at the *old* P.

    Returned pairs are exactly those where Q transitively depends on P in
    the full graph but no dep path Q → … → P lies entirely inside the
    resolved set — pure-literal partial subgraphs that the operator may
    or may not have intended.  Caller renders these as a warning.
    """
    sel = set(resolved)
    gaps: list[tuple[str, str]] = []

    def reachable(start: str, *, gate: set[str] | None) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            for d in graph.get(stack.pop(), []):
                if d in seen:
                    continue
                seen.add(d)
                if gate is None or d in gate:
                    stack.append(d)
        return seen

    for q in resolved:
        full = reachable(q, gate=None)
        via_sel = reachable(q, gate=sel)
        gaps.extend((q, p) for p in resolved if p != q and p in full and p not in via_sel)
    return gaps


# Steps an operator can't cleanly walk back — flagged in the tree.
#   - PR_MERGE lands on master; undoing it takes another commit, the
#     hardest of these to reverse cleanly.
#   - WORKFLOW_DISPATCH uploads to PyPI/TestPyPI, where a version number,
#     once published, can never be reused — genuinely unrecoverable.
# TAG and RELEASE are intentionally *not* flagged: until the PyPI upload an
# upstream admin can still delete the tag, or the release and its wheel.
# GIT_PUSH is excluded for the same reason — a force-with-lease to a fork
# branch is trivial to redo.
_IRREVERSIBLE_STEPS = frozenset({StepKind.PR_MERGE, StepKind.WORKFLOW_DISPATCH})

# Human names for the publish targets, echoed on the dispatch line so the
# operator sees *where* the unrecoverable upload lands.
_PUBLISH_ENV = {"pypi": "PyPI", "testpypi": "TestPyPI"}


def _step_line(step: Step, target: str) -> str:
    """One colourised line describing a step for the plan tree.

    *target* is the plan's publish target — named explicitly on the
    ``workflow_dispatch`` line, since that step is the upload to PyPI /
    TestPyPI and thus the point of no return.
    """
    p = step.params
    if step.kind == StepKind.WORKFLOW_DISPATCH:
        detail = f"publish → {_PUBLISH_ENV.get(target, target)}"
    elif step.kind == StepKind.DEP_UPDATE:
        detail = f"{p['dep_repo']} v{p['dep_version']}"
    else:
        detail = p.get("version") or p.get("tag") or p.get("branch") or ""
    detail_hint = f" [dim]{detail}[/]" if detail else ""
    flag = " [bold red]⚠ irreversible[/]" if step.kind in _IRREVERSIBLE_STEPS else ""
    return f"{step.kind.value}{detail_hint}{flag}"


def _render_plan_steps(plan: Plan) -> None:
    """Per-step tree grouped by package — the detailed view behind the
    ``show`` verb.  Shows *what will run*, step by step, rather than the
    summary table's one-row-per-package overview.
    """
    by_pkg: dict[str, list[Step]] = {}
    for step in plan.steps:
        by_pkg.setdefault(step.package, []).append(step)
    tree = Tree("[bold]Steps[/]")
    for pkg in plan.packages:
        steps = by_pkg.get(pkg.repo, [])
        if not steps:
            continue
        ver = f" [green]v{pkg.new_version}[/]" if pkg.new_version else " [dim](deps only)[/]"
        branch = tree.add(f"[cyan]{pkg.repo}[/]{ver}")
        for step in steps:
            branch.add(_step_line(step, plan.target))
    console.print(tree)


def _render_plan_preview(plan: Plan) -> None:
    """Print the plan as a table — the operator's last look before we commit."""
    kind_hint = "[yellow]prerelease[/]" if plan.prerelease else "[green]release[/]"
    target_hint = {
        "pypi": "[bold red]→ PyPI (production)[/]",
        "testpypi": "[bold yellow]→ TestPyPI (validation)[/]",
        "gh-only": "[dim]→ GitHub Release only (no PyPI)[/]",
    }.get(plan.target, plan.target)
    pin_hint = (
        "[dim]url-pin deps[/]" if plan.pin_style == "url" else "[dim]pypi-pin deps[/]"
    )
    console.print(f"\n[bold]Release plan ({kind_hint}) {target_hint} {pin_hint}:[/]\n")
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", width=3)
    table.add_column("Package", style="cyan")
    table.add_column("Action")
    table.add_column("Version")
    table.add_column("Deps")
    for i, pkg in enumerate(plan.packages, 1):
        ver = (
            f"{pkg.current_version} -> [green]{pkg.new_version}[/]"
            if pkg.new_version
            else pkg.current_version
        )
        dep_str = ", ".join(f"{d} v{v}" for d, v in pkg.sibling_deps.items())
        action = pkg.action.value + (f" #{pkg.pr_number}" if pkg.pr_number else "")
        table.add_row(str(i), pkg.repo, action, ver, dep_str)
    console.print(table)
    notes = [pkg.notes_path for pkg in plan.packages if pkg.notes_path]
    if notes:
        console.print(
            "\n[dim]Release notes seeded; edit before proceeding:[/]\n"
            + "\n".join(f"  [dim]•[/] {p}" for p in notes)
        )


def _resolve_chain(
    spec: str,
    graph: DepGraph,
    *,
    open_top: bool = False,
) -> tuple[list[str], str | None, PrSpecs, LevelSpecs]:
    """Parse the chain spec, order it by ``CHAIN``, return planner inputs.

    Selection is literal — exactly what was named (ranges expanded), no
    auto-cascade through dependents.  Want a full propagation? Write the
    range explicitly (``sandbox..terok``).

    *graph* is the verified live dep graph (callers run
    ``_verify_dep_graph(CHAIN, cache_dir)`` first); used here only to
    detect non-contiguous selections — pairs where a downstream release
    transitively depends on an upstream release through an intermediate
    that isn't being bumped, so the downstream release won't see the new
    upstream pin.  Such pairs produce a yellow warning but do not abort.

    Returns ``(ordered_chain, stop_at, pr_specs, level_specs)``.  With
    ``--open-top``, the last package becomes ``DEPS_ONLY`` (deps update
    only, no release) — a ``%LEVEL`` on that package contradicts the
    flag, so the combination is rejected.
    """
    entries = parse_chain_spec(spec)
    pr_specs = {e.repo: e.pr for e in entries if e.pr is not None}
    level_specs = {e.repo: e.level for e in entries if e.level is not None}
    named = {e.repo for e in entries}
    chain = [r for r in CHAIN if r in named]

    for downstream, upstream in _gap_pairs(chain, graph):
        console.print(
            f"[yellow]Note: {downstream} transitively pins {upstream} through an "
            f"unbumped intermediate — its new release won't see new {upstream}.[/]"
        )

    stop_at = chain[-1] if open_top else None
    if stop_at and stop_at in level_specs:
        die(
            f"{stop_at} is deps-only under --open-top — "
            f"%{level_specs[stop_at]} would bump it anyway. Drop one or the other."
        )
    return chain, stop_at, pr_specs, level_specs


_CLICK_CONTEXT = {"help_option_names": ["-h", "--help"]}


def _stack(*decorators: Callable) -> Callable:
    """Compose Click option decorators in declaration order."""

    def wrap(f: Callable) -> Callable:
        for d in reversed(decorators):
            f = d(f)
        return f

    return wrap


# ── Shared option groups ─────────────────────────────────────────────────
#
# Click decorators are stacked the same way on `quick` and `plan_cmd`; pulling
# them into one decorator keeps the two commands' shape in sync and shrinks
# the CLI definition by ~40 lines.

_remote_options = _stack(
    click.option("--org", default=_env("TEROK_GH_ORG", "terok-ai")),
    click.option("--fork", default=_env("TEROK_GH_FORK")),
    click.option(
        "--cache-dir",
        default=_env("TEROK_RELEASE_DIR", str(Path.home() / ".cache/terok-release")),
    ),
)
"""``--org / --fork / --cache-dir`` triple shared by every subcommand."""

_chain_options = _stack(
    click.argument("chain_spec"),
    click.option(
        "--version-step",
        default="patch",
        type=click.Choice(list(ACCEPTED_STEPS)),
        help=(
            "Default bump level; a ``%LEVEL`` suffix in the chain spec "
            "overrides it per package. ``alpha``/``beta``/``rc`` cut "
            "PEP 440 pre-release tags (``X.Y.ZaN``/``bN``/``rcN``) for "
            "dev-cycle integration — gh-only, marked as GH prereleases; "
            "an optional ``-patch``/``-minor``/``-major`` suffix picks "
            "the base bump (bare stage = ``-patch``). Final levels "
            "applied to a pre-release base promote (drop the suffix). "
            "Shortcuts: maj min a amin amaj b bmin bmaj rcmin rcmaj."
        ),
    ),
    click.option("-n", "--name", "release_name", default="", help="Release name suffix"),
    click.option("--upgrade-pinned", is_flag=True),
    click.option("--open-top", is_flag=True, help="Top package: update deps only, no release"),
    click.option(
        "--prerelease",
        is_flag=True,
        help="Publish as a GitHub prerelease (hidden from the repo's 'Latest' badge)",
    ),
    click.option(
        "--target",
        default="pypi",
        type=click.Choice(["pypi", "testpypi", "gh-only"]),
        help="Publish target — pypi (production), testpypi (validation), gh-only (no PyPI)",
    ),
    click.option(
        "--pin-style",
        default=None,
        type=click.Choice(["pypi", "url"]),
        help=(
            "Sibling dep pin style. Default is 'pypi' (version specifiers) for "
            "pypi/testpypi targets, 'url' (GH release wheel URLs) for gh-only."
        ),
    ),
    _remote_options,
)
"""Chain-spec positional + planner options shared by ``quick`` and ``plan``."""


def _resolve_prerelease_constraints(
    levels: set[str], target: str, prerelease: bool
) -> tuple[str, bool]:
    """Apply the pre-release bump levels' implications.

    Alpha/beta/rc tags are dev-cycle integration artefacts: always
    gh-only and always GH prereleases.  Those are plan-wide switches, so
    *levels* — the effective bump level of every package releasing in
    this run — must be all-pre-release or pre-release-free; a mix can't
    honour both halves (stages and base sizes may vary freely).
    Explicit pypi/testpypi targets are rejected up front.
    """
    pre = {level for level in levels if level not in FINAL_STEPS}
    if not pre:
        return target, prerelease
    if pre != levels:
        die(
            "pre-release bump levels (alpha/beta/rc) can't mix with final levels in one "
            "run — the gh-only prerelease implications are plan-wide. Split the release."
        )
    if target in ("pypi", "testpypi"):
        die(
            f"a pre-release bump level is incompatible with --target={target} — "
            "alpha/beta/rc tags are gh-only by design. Use a final level to publish to PyPI."
        )
    return "gh-only", True


def _resolve_pin_style(pin_style: str | None, target: str) -> str:
    """Pick the right pin style for the target if the operator didn't override.

    PyPI rejects uploads with direct-URL deps, so ``pin_style=url`` is
    incompatible with the ``pypi``/``testpypi`` targets — fail fast on
    that combination rather than building a wheel PyPI will reject.
    """
    if pin_style is None:
        pin_style = "url" if target == "gh-only" else "pypi"
    if pin_style == "url" and target in ("pypi", "testpypi"):
        die(
            f"--pin-style=url is incompatible with --target={target} — PyPI "
            "rejects uploads with direct-URL deps. Use --pin-style=pypi or "
            "switch to --target=gh-only."
        )
    return pin_style


@click.group(context_settings=_CLICK_CONTEXT)
def cli():
    """Cascading release chain for the terok package family."""
    _check_gh_version()


@cli.command(context_settings=_CLICK_CONTEXT)
@_chain_options
@click.option("-y", "--yes", is_flag=True, help="Auto-approve normal confirmations")
@click.option("--skip-checks", is_flag=True)
@click.option("--check-timeout", default=DEFAULT_CHECK_TIMEOUT, type=int)
def quick(
    chain_spec,
    version_step,
    release_name,
    upgrade_pinned,
    open_top,
    prerelease,
    target,
    pin_style,
    org,
    fork,
    cache_dir,
    yes,
    skip_checks,
    check_timeout,
):
    """Plan and execute a release chain in one shot.

    \b
    CHAIN_SPEC grammar:
      pkg                              one package (== pkg..pkg)
      pkg:NUM                          release pkg from PR #NUM
      pkg%LEVEL                        bump pkg at LEVEL, not --version-step
      A..B                             range; intermediates filled from CHAIN
      A..B%LEVEL                       range; LEVEL for every package in it
      A,B,C                            literal list — no cascade
      A,B:NUM%LEVEL..C                 any combination

    \b
    Selection is literal. For a full cascade through dependents, write the
    range (``sandbox..terok``). Non-contiguous lists are allowed but warned
    when an unbumped intermediate breaks pin propagation.

    \b
    Examples:
      quick sandbox                       just sandbox
      quick sandbox..terok                full cascade through to terok
      quick sandbox..terok --open-top     terok stays as deps-only PR
      quick sandbox:155                   release from one PR
      quick sandbox:155,executor:167,terok:706 --open-top
                                          PR chain; terok deps-only on its PR
      quick clearance,sandbox:221..terok  mixed; literal union
      quick sandbox..terok --prerelease   prerelease badge on each
      quick sandbox%minor,executor%minor,terok
                                          API-breaking lower pair, patch top
    """
    org, fork, cd, ctx = _common_ctx(org, fork, cache_dir, False, yes, skip_checks, check_timeout)

    # Prompt for release name if not given
    if not release_name:
        release_name = alert_prompt("Release name (empty for version-only)", default="")

    # Extract PR overrides up front so the clone-cache lands each repo
    # on the right ref before dep-graph verification reads its
    # pyproject.toml — checking against ``upstream/master`` for a repo
    # whose PR introduces a new sibling dep would always false-positive.
    pr_specs = {e.repo: e.pr for e in parse_chain_spec(chain_spec) if e.pr is not None}
    _require_open_prs(pr_specs, org)

    # Clone the WHOLE family up front so gap detection and dep validation
    # run on the verified live dep graph — a stale vendored ``DEPS`` would
    # otherwise miss a pyproject mismatch on a repo not in this run.
    console.print("\n[bold]Syncing clones...[/]")
    for repo in CHAIN:
        ensure_clone(repo, cd, org, fork, pr=pr_specs.get(repo))
    live_deps = _verify_dep_graph(CHAIN, cd)

    chain, stop_at, _pr_specs, level_specs = _resolve_chain(
        chain_spec, live_deps, open_top=open_top
    )
    assert _pr_specs == pr_specs  # invariant: both extractions agree

    releasing_levels = {
        canonical_step(level_specs.get(r, version_step)) for r in chain if r != stop_at
    }
    target, prerelease = _resolve_prerelease_constraints(releasing_levels, target, prerelease)
    pin_style = _resolve_pin_style(pin_style, target)
    plan = generate_plan(
        chain,
        live_deps,
        org=org,
        fork=fork,
        release_name=release_name,
        version_step=version_step,
        cache_dir=cd,
        stop_at=stop_at,
        upgrade_pinned=upgrade_pinned,
        pr_specs=pr_specs,
        level_specs=level_specs,
        prerelease=prerelease,
        target=target,
        pin_style=pin_style,
    )
    seed_notes(plan, cd)

    _render_plan_preview(plan)

    if not yes:
        # Default-N because the interactive operator usually wants to edit;
        # plain Enter opens $EDITOR.  ``-y`` skips the prompt entirely and
        # ships the seeded notes as-is, which matches ``-y``'s "auto-accept
        # defaults" semantics — answering "edit" in unattended mode is
        # impossible anyway.
        if not alert_confirm(
            "Accept default release notes? (n to edit)", default=False
        ):
            edit_notes(plan)
        alert_confirm("Proceed?", default=True, abort=True)

    # Save plan
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = slugify(release_name) or "release"
    plan_path = cd / "plans" / f"{ts}-{slug}.json"
    ctx.plan_path = plan_path
    save_plan(plan, plan_path)
    console.print(f"\n[bold]Plan saved:[/] {plan_path}")
    console.print(f"[dim]Resume on failure: terok-release execute {plan_path}[/]")

    # Execute
    start_ts = time.monotonic()
    execute_plan(plan, mode=ExecMode.EXECUTE, ctx=ctx)
    elapsed = time.monotonic() - start_ts

    # Summary
    console.print("\n[bold green]All releases complete![/]\n")
    for pkg in plan.packages:
        if pkg.new_version:
            url = published_url(plan.target, plan.gh_org, pkg.repo, pkg.new_version)
            console.print(
                f"  [green]*[/] {pkg.repo} v{pkg.new_version}  [dim]{url}[/]"
            )
        else:
            url_suffix = f" → {pkg.pr_url}" if pkg.pr_url else ""
            console.print(f"  [yellow]*[/] {pkg.repo}  (deps only){url_suffix}")
    console.print(f"\nElapsed: {elapsed:.0f}s")


@cli.command("open", context_settings=_CLICK_CONTEXT)
@click.argument("branch")
@click.argument("repos", nargs=-1, required=True)
@click.option("-p", "--pretend", is_flag=True, help="Dry run")
@_remote_options
def open_chain(branch, repos, pretend, org, fork, cache_dir):
    """Open a PR chain for cross-cutting development.

    Creates a branch in each repo, wires sibling deps as git-branch
    references (PEP 508), and opens PRs.  During an open chain, develop
    with the repo's own locker — `uv sync` — not pipx.

    \b
    Examples:
        terok-release-chain.py open feat/comms clearance
        terok-release-chain.py open feat/my-feature sandbox terok
    """
    org, fork, cd, ctx = _common_ctx(org, fork, cache_dir, pretend, True, True, 0)
    start = normalise(repos[0])
    end = normalise(repos[1]) if len(repos) > 1 else None
    chain = build_chain(start, end)

    console.print(f"\n[bold]Opening PR chain:[/] {branch}")
    console.print(f"  Repos: {' '.join(chain)}\n")

    for repo in chain:
        ensure_clone(repo, cd, org, fork)
    console.print()

    pr_urls: list[str] = []
    for i, repo in enumerate(chain):
        repo_dir = cd / repo
        gh_repo = f"{org}/{repo}"

        console.print(f"[cyan]{repo}[/]: creating branch {branch}")
        if not ctx.dry_run:
            sh("git", "checkout", "-B", branch, "upstream/master", cwd=repo_dir)

        # Wire in-chain deps as git-branch references (skip the leaf repo)
        if i > 0:
            for dep in DEPS.get(repo, []):
                if dep in chain:
                    console.print(f"  wiring {dep} -> branch {branch}")
                    if not ctx.dry_run:
                        set_branch_dep(repo_dir / "pyproject.toml", dep, branch, fork)
            if not ctx.dry_run:
                lock_repo(repo_dir)
                sh("git", "add", "pyproject.toml", "uv.lock", cwd=repo_dir)
                sh("git", "commit", "-m", f"chore: wire {branch} branch deps", cwd=repo_dir)

        console.print("  pushing to fork")
        if not ctx.dry_run:
            sh("git", "push", "-u", "origin", branch, "--force-with-lease", cwd=repo_dir)

        # Open PR (detect "already exists" gracefully)
        if ctx.dry_run:
            console.print("  [yellow][pretend][/] would create PR")
            pr_urls.append("(pretend)")
        else:
            r = sh(
                "gh",
                "pr",
                "view",
                "--repo",
                gh_repo,
                "--head",
                f"{fork}:{branch}",
                "--json",
                "url",
                "--jq",
                ".url",
                capture=True,
                check=False,
            )
            if r.returncode == 0 and r.stdout.strip():
                pr_urls.append(r.stdout.strip())
                console.print(f"  PR already exists: {pr_urls[-1]}")
            else:
                r = sh(
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    gh_repo,
                    "--base",
                    "master",
                    "--head",
                    f"{fork}:{branch}",
                    "--title",
                    branch,
                    "--body",
                    f"Part of `{branch}` PR chain.",
                    capture=True,
                )
                pr_urls.append(r.stdout.strip())
                console.print(f"  [green]PR created: {pr_urls[-1]}[/]")
        console.print()

    console.print("[bold green]PR chain opened![/]\n")
    for repo, url in zip(chain, pr_urls, strict=True):
        console.print(f"  {repo}  {url}")
    console.print()


@cli.command("plan", context_settings=_CLICK_CONTEXT)
@_chain_options
@click.option("-o", "--output", type=click.Path(), help="Output plan file")
def plan_cmd(
    chain_spec,
    version_step,
    release_name,
    upgrade_pinned,
    open_top,
    prerelease,
    target,
    pin_style,
    org,
    fork,
    cache_dir,
    output,
):
    """Generate a release plan without executing it.

    Same CHAIN_SPEC grammar as ``quick``; see ``quick --help`` for examples.
    """
    org, fork, cd, ctx = _common_ctx(org, fork, cache_dir, True, True, True, 0)
    if not release_name:
        console.print(
            "[yellow]Warning: no release name (-n). Release titles will be version-only.[/]"
        )

    # Extract PR overrides up front so the clone-cache lands each repo
    # on the right ref before dep-graph verification reads its
    # pyproject.toml (see ``quick`` for the full rationale).
    pr_specs = {e.repo: e.pr for e in parse_chain_spec(chain_spec) if e.pr is not None}
    _require_open_prs(pr_specs, org)

    # Clone the WHOLE family up front so gap detection and dep validation
    # run on the verified live dep graph (see ``quick`` for the rationale).
    for repo in CHAIN:
        ensure_clone(repo, cd, org, fork, pr=pr_specs.get(repo))
    live_deps = _verify_dep_graph(CHAIN, cd)

    chain, stop_at, _pr_specs, level_specs = _resolve_chain(
        chain_spec, live_deps, open_top=open_top
    )
    assert _pr_specs == pr_specs  # invariant: both extractions agree

    releasing_levels = {
        canonical_step(level_specs.get(r, version_step)) for r in chain if r != stop_at
    }
    target, prerelease = _resolve_prerelease_constraints(releasing_levels, target, prerelease)
    pin_style = _resolve_pin_style(pin_style, target)
    plan = generate_plan(
        chain,
        live_deps,
        org=org,
        fork=fork,
        release_name=release_name,
        version_step=version_step,
        cache_dir=cd,
        stop_at=stop_at,
        upgrade_pinned=upgrade_pinned,
        pr_specs=pr_specs,
        level_specs=level_specs,
        prerelease=prerelease,
        target=target,
        pin_style=pin_style,
    )
    seed_notes(plan, cd)

    out = Path(output) if output else cd / "plans" / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    save_plan(plan, out)
    console.print(f"Plan written to {out}")
    console.print(f"[dim]View it:    terok-release show {out}[/]")
    console.print(f"[dim]Execute it: terok-release execute {out}[/]")


@cli.command(context_settings=_CLICK_CONTEXT)
@click.argument("plan_file", type=click.Path(exists=True))
def show(plan_file):
    """Display a saved plan: summary table + per-step tree.

    Pure rendering of the plan JSON — reads nothing from GitHub and runs
    no steps, so it needs neither org/fork nor a clone cache.
    """
    plan = Plan.model_validate_json(Path(plan_file).read_text())
    _render_plan_preview(plan)
    _render_plan_steps(plan)


@cli.command(context_settings=_CLICK_CONTEXT)
@click.argument("plan_file", type=click.Path(exists=True))
@click.option("-y", "--yes", is_flag=True)
@click.option("--skip-checks", is_flag=True)
@click.option("--check-timeout", default=DEFAULT_CHECK_TIMEOUT, type=int)
@_remote_options
def execute(plan_file, yes, skip_checks, check_timeout, org, fork, cache_dir):
    """Execute (or resume) a release plan."""
    org, fork, cd, ctx = _common_ctx(
        org, fork, cache_dir, False, yes, skip_checks, check_timeout
    )
    plan_path = Path(plan_file)
    plan = Plan.model_validate_json(plan_path.read_text())
    plan.gh_org = org or plan.gh_org
    plan.gh_fork = fork or plan.gh_fork or die(
        "Fork required: set TEROK_GH_FORK or embed in plan"
    )
    ctx.plan_path = plan_path

    has_completed = any(s.status == "completed" for s in plan.steps)
    mode = ExecMode.RESUME if has_completed else ExecMode.EXECUTE
    if has_completed:
        console.print("[yellow]Resuming partially-executed plan...[/]")

    execute_plan(plan, mode=mode, ctx=ctx)
    console.print("\n[bold green]All releases complete![/]")


if __name__ == "__main__":
    cli()

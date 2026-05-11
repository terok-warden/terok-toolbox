#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0
"""Prune historic GitHub Releases for the terok-* family.

For each sibling repo (clearance, shield, sandbox, executor), keeps only
the releases whose tags are referenced by at least one of the kept terok
releases' ``pyproject.toml``.  Anything else — release object, attached
wheel asset, and underlying git tag — is deleted.

The terok repo itself is pruned to whatever subset of its own tags the
operator picks via ``--keep-pattern`` or ``--keep-from``.  ``mkdocs-terok``
is not included in the closure: it is a docs-only sibling never pinned in
runtime deps, so the closure tells you nothing about which of its tags
matter.  Prune it independently if desired.

After pruning, master commits in any sibling that referenced a pruned
release are no longer pip-installable from those URLs.  Source remains
in git history under the surviving tags; only the wheels go away.

Usage:
    python3 scripts/prune-historic-releases.py compute --keep-from v0.7.0
    python3 scripts/prune-historic-releases.py prune   --keep-from v0.7.0
    python3 scripts/prune-historic-releases.py prune   --keep-from v0.7.0 --execute
"""

from __future__ import annotations

import base64
import re
import subprocess
from typing import Never

import click
from rich.console import Console
from rich.table import Table

console = Console(stderr=True)

ORG = "terok-ai"
TEROK = "terok"
SIBLINGS = ["terok-clearance", "terok-shield", "terok-sandbox", "terok-executor"]
GH_RELEASE_LIST_LIMIT = 500  # high enough to capture every historic release


def die(msg: str) -> Never:
    """Print an error and exit non-zero."""
    console.print(f"[bold red]ERROR:[/] {msg}")
    raise SystemExit(1)


def gh(*args: str) -> str:
    """Run ``gh``, return stdout, surface stderr on failure."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        die(f"gh {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def list_release_tags(repo: str) -> list[str]:
    """All release tags on *repo*, newest first."""
    out = gh(
        "release", "list", "--repo", f"{ORG}/{repo}",
        "--limit", str(GH_RELEASE_LIST_LIMIT),
        "--json", "tagName",
        "--jq", ".[].tagName",
    )  # fmt: skip
    return [t for t in out.splitlines() if t.strip()]


def pyproject_at(repo: str, ref: str) -> str | None:
    """``pyproject.toml`` content at *ref*, or ``None`` if the API call fails.

    Old tags occasionally lack the file (rename, restructure) and that is
    treated as "this tag contributes nothing to the closure" rather than
    a hard error — one missing pyproject shouldn't abort a 200-tag sweep.
    """
    r = subprocess.run(
        [
            "gh", "api", f"repos/{ORG}/{repo}/contents/pyproject.toml?ref={ref}",
            "--jq", ".content",
        ],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return base64.b64decode(r.stdout.strip()).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def extract_sibling_pins(pyproject: str) -> dict[str, set[str]]:
    """Map sibling repo → set of pinned versions found in URL deps.

    Only catches the canonical wheel-URL form
    ``<sibling>/releases/download/vX.Y.Z/...``.  Git-source and PyPI version
    pins contribute nothing to the closure (which is correct — they don't
    require a particular GitHub release to exist).
    """
    pins: dict[str, set[str]] = {}
    for sibling in SIBLINGS:
        versions = re.findall(
            rf"{re.escape(sibling)}/releases/download/v([^/\"' ]+)/",
            pyproject,
        )
        if versions:
            pins[sibling] = set(versions)
    return pins


def compute_keep_sets(keep_terok_tags: list[str]) -> dict[str, set[str]]:
    """Closure: per-sibling keep set from kept terok tags' pyproject pins."""
    keep: dict[str, set[str]] = {s: set() for s in SIBLINGS}
    missing: list[str] = []
    for tag in keep_terok_tags:
        pp = pyproject_at(TEROK, tag)
        if pp is None:
            missing.append(tag)
            continue
        for sibling, vers in extract_sibling_pins(pp).items():
            keep[sibling].update(f"v{v}" for v in vers)
    if missing:
        console.print(
            f"[yellow]Warning:[/] {len(missing)} terok tag(s) had no readable "
            f"pyproject.toml and contributed nothing to the closure: "
            f"{', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}"
        )
    return keep


def _version_key(tag: str) -> tuple:
    """Sort key for ``v0.1.2``-style tags; falls back to lexicographic on garbage."""
    parts = re.split(r"[.\-+]", tag.lstrip("v"))
    return tuple(int(p) if p.isdigit() else p for p in parts)


def select_keep_terok_tags(pattern: str | None, since: str | None) -> list[str]:
    """Filter terok's full tag list down to the keep set."""
    all_tags = list_release_tags(TEROK)
    if pattern and since:
        die("Pass at most one of --keep-pattern and --keep-from")
    if pattern:
        rx = re.compile(pattern)
        return [t for t in all_tags if rx.search(t)]
    if since:
        threshold = _version_key(since)
        return [t for t in all_tags if _version_key(t) >= threshold]
    return all_tags


def render_keep_table(keep_terok: list[str], keep_siblings: dict[str, set[str]]) -> None:
    """Print the proposed keep sets as a Rich table."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("Keep count", justify="right")
    table.add_column("Tags (newest first)", overflow="fold")

    table.add_row(
        TEROK,
        str(len(keep_terok)),
        ", ".join(sorted(keep_terok, key=_version_key, reverse=True)),
    )
    for sibling in SIBLINGS:
        tags = sorted(keep_siblings.get(sibling, set()), key=_version_key, reverse=True)
        table.add_row(sibling, str(len(tags)), ", ".join(tags))
    console.print(table)


def delete_release(repo: str, tag: str, *, execute: bool) -> None:
    """Delete one release (with assets and tag), or pretend to in dry-run."""
    label = f"{repo}@{tag}"
    if not execute:
        console.print(f"  [dim][dry-run][/] would delete {label}")
        return
    gh("release", "delete", tag, "--repo", f"{ORG}/{repo}", "--cleanup-tag", "--yes")
    console.print(f"  [red]deleted[/] {label}")


# ── CLI ───────────────────────────────────────────────────────────────────


_CLICK_CONTEXT = {"help_option_names": ["-h", "--help"]}

_keep_options = [
    click.option("--keep-pattern", help="Regex over tag names; tags matching are kept"),
    click.option("--keep-from", help="Lowest version to keep, e.g. v0.7.0"),
]


def _stack(*decorators):
    def wrap(f):
        for d in reversed(decorators):
            f = d(f)
        return f

    return wrap


@click.group(context_settings=_CLICK_CONTEXT)
def cli():
    """Prune historic GitHub Releases for the terok-* family."""


@cli.command("compute", context_settings=_CLICK_CONTEXT)
@_stack(*_keep_options)
def compute_cmd(keep_pattern, keep_from):
    """Compute and print the keep sets — read-only, no side effects."""
    keep_terok = select_keep_terok_tags(keep_pattern, keep_from)
    if not keep_terok:
        die("No terok tags matched the keep filter")
    keep_siblings = compute_keep_sets(keep_terok)
    render_keep_table(keep_terok, keep_siblings)


@cli.command("prune", context_settings=_CLICK_CONTEXT)
@_stack(*_keep_options)
@click.option("--execute", is_flag=True, help="Actually delete; without this, dry-run")
def prune_cmd(keep_pattern, keep_from, execute):
    """Delete releases not in the keep set (dry-run by default).

    Processes leaf siblings before consumers before terok itself, so an
    aborted run still leaves the chain internally consistent — every
    surviving terok release's pyproject pins resolve to a kept sibling
    release.
    """
    keep_terok = select_keep_terok_tags(keep_pattern, keep_from)
    if not keep_terok:
        die("No terok tags matched the keep filter")

    keep_siblings = compute_keep_sets(keep_terok)
    render_keep_table(keep_terok, keep_siblings)

    if execute:
        console.print(
            "\n[bold red]This will delete releases, attached wheels, and tags.[/]"
        )
        if not click.confirm("Proceed?", default=False):
            die("aborted")
    else:
        console.print(
            "\n[yellow]DRY RUN — re-run with --execute to actually delete.[/]\n"
        )

    keep_per_repo: dict[str, set[str]] = {**keep_siblings, TEROK: set(keep_terok)}

    # Leaf-first ordering: prune each sibling before pruning the terok
    # release that pinned them.  Aborting mid-sweep still leaves the
    # surviving terok tags pointing at surviving sibling releases.
    for repo in [*SIBLINGS, TEROK]:
        console.print(f"\n[bold cyan]== {repo} ==[/]")
        present = list_release_tags(repo)
        keep = keep_per_repo.get(repo, set())
        to_delete = [t for t in present if t not in keep]
        if not to_delete:
            console.print("  [dim]nothing to delete[/]")
            continue
        for tag in to_delete:
            delete_release(repo, tag, execute=execute)


if __name__ == "__main__":
    cli()

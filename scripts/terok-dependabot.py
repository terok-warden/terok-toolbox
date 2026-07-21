#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0
"""Survey and merge Dependabot PRs across the terok-ai package family.

Dependabot opens its weekly wave across every repo at once.  This tool
reads that wave fresh on each run (no persisted state — unlike the
release chain, a cancelled run leaves nothing to recover), sorts the PRs
into two kinds, and drives them to master:

``everything else`` — CI bumps (``github-actions`` ecosystem) and the
    grouped ``dev-dependencies`` update.  These never touch shipped code,
    so ``merge`` automerges them one by one, gating each on green CI.

``runtime`` — the grouped ``production-dependencies`` update: the deps a
    plain ``pip install`` of the package pulls in.  These reach end
    users, so they are listed for review by default and only merged
    one-by-one behind a confirmation prompt (``--offer``).

Classification keys off the Dependabot head-branch name, the one signal
that is machine-reliable and consistent fleet-wide:

    dependabot/github_actions/...              -> everything else (CI)
    dependabot/{uv,pip}/dev-dependencies-...   -> everything else (dev)
    dependabot/{uv,pip}/production-dependen...  -> runtime
    anything else under {uv,pip}/              -> runtime (fail safe:
                                                 review, never automerge)

Merge mechanics the fleet imposes (verified against branch settings):

* ``master`` has no branch protection, so ``gh pr merge`` would happily
  land a red PR — this tool gates on ``gh pr checks`` itself rather than
  trusting the merge-state.
* The CI PRs edit ``.github/workflows/*``; GitHub refuses to let a token
  without ``workflow`` scope merge those.  A preflight reports the gap up
  front and the merge loop degrades gracefully if it is hit anyway.
* Merging one ``uv.lock`` PR flips its sibling to CONFLICTING until
  Dependabot rebases; the merge loop waits that out (nudging once with
  ``@dependabot rebase``) rather than failing.  Ctrl-C at any point exits
  clean — remedy the stuck PR by hand and re-run; the next run reads the
  world fresh.

Usage:
    terok-dependabot                       # summary of the open wave
    terok-dependabot --repo terok-util     # ...narrowed to one repo
    terok-dependabot merge                 # automerge everything-else
    terok-dependabot merge --offer         # ...then walk runtime deps
    terok-dependabot merge --pretend       # dry run: print, never merge
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Never

import click
from rich.console import Console
from rich.table import Table

ORG = "terok-ai"

# The fleet, spelled out.  kanban-tui, terok-nix and docs-inventories are
# deliberately excluded — they are not part of the shipped package family
# this tool shepherds.
FLEET = [
    "terok",
    "terok-util",
    "terok-sandbox",
    "terok-executor",
    "terok-clearance",
    "terok-shield",
    "mkdocs-terok",
    "pages",
]

DEPENDABOT_AUTHOR = "app/dependabot"

# CI-check polling: a grace window swallows the gap between a fresh push
# and CI registering, then we poll until every check settles.
CHECK_POLL_INTERVAL = 15
CHECK_GRACE_WINDOW = 45
CHECK_TIMEOUT_DEFAULT = 1800

# Rebase waiting: after merging a sibling, a uv.lock PR goes CONFLICTING
# until Dependabot rebases it.  Poll for that, nudge once if it stalls.
REBASE_POLL_INTERVAL = 20
REBASE_NUDGE_AFTER = 120
REBASE_TIMEOUT = 1800

console = Console()


def die(msg: str) -> Never:
    """Abort with a red banner and non-zero exit."""
    console.print(f"[bold red]error:[/] {msg}")
    raise SystemExit(1)


class Bucket(StrEnum):
    """Which of Dependabot's three streams a PR came from."""

    CI = "ci"
    DEV = "dev"
    RUNTIME = "runtime"

    @property
    def is_runtime(self) -> bool:
        """Runtime deps ship to end users and get the careful path."""
        return self is Bucket.RUNTIME

    @property
    def label(self) -> str:
        """Human tag for the summary table."""
        return {Bucket.CI: "CI", Bucket.DEV: "dev", Bucket.RUNTIME: "runtime"}[self]


def classify(head_ref: str) -> Bucket:
    """Map a Dependabot head-branch name to its bucket.

    Fail safe: any Python-ecosystem branch we can't positively tie to the
    dev group is treated as runtime, so an unrecognised production dep is
    reviewed by a human rather than silently automerged.
    """
    if "/github_actions/" in head_ref:
        return Bucket.CI
    if "/dev-dependencies" in head_ref:
        return Bucket.DEV
    if "/production-dependencies" in head_ref:
        return Bucket.RUNTIME
    # Unknown Python (or any other) ecosystem update — err toward review.
    return Bucket.RUNTIME


@dataclass
class PR:
    """One open Dependabot pull request."""

    repo: str
    number: int
    title: str
    head_ref: str
    mergeable: str
    merge_state: str
    rollup: list[dict] = field(default_factory=list)

    @property
    def bucket(self) -> Bucket:
        """The stream this PR belongs to."""
        return classify(self.head_ref)

    @property
    def ref(self) -> str:
        """Short human reference for logs."""
        return f"{self.repo}#{self.number}"

    @property
    def gh_repo(self) -> str:
        """Org-qualified repo for ``gh --repo``."""
        return f"{ORG}/{self.repo}"

    @property
    def checks(self) -> str:
        """Coarse CI verdict from the list-time rollup: pass/pending/fail/none."""
        return rollup_state(self.rollup)


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output; surface stderr on failure."""
    r = subprocess.run(args, capture_output=True, text=True, check=False)
    if check and r.returncode:
        die(f"`{' '.join(args)}` failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()}")
    return r


def gh_json(*args: str) -> object:
    """Run a ``gh`` command expected to emit JSON and parse it."""
    return json.loads(sh("gh", *args).stdout or "null")


def rollup_state(rollup: Iterable[dict]) -> str:
    """Reduce a ``statusCheckRollup`` array to pass/pending/fail/none.

    Handles both check-runs (``status`` + ``conclusion``) and legacy
    status contexts (``state``).  Pending dominates fail dominates pass,
    so an in-flight suite never reads as green.
    """
    failing = {"FAILURE", "CANCELLED", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}
    seen = pending = fail = False
    for check in rollup:
        seen = True
        if (status := check.get("status")) and status != "COMPLETED":
            pending = True
        state = check.get("conclusion") or check.get("state") or ""
        if state.upper() == "PENDING":
            pending = True
        elif state.upper() in failing:
            fail = True
    if not seen:
        return "none"
    if pending:
        return "pending"
    return "fail" if fail else "pass"


def list_repo_prs(repo: str) -> list[PR]:
    """Fetch every open Dependabot PR in one repo, classified."""
    rows = gh_json(
        "pr", "list", "--repo", f"{ORG}/{repo}",
        "--author", DEPENDABOT_AUTHOR, "--state", "open", "--limit", "100",
        "--json", "number,title,headRefName,mergeable,mergeStateStatus,statusCheckRollup",
    )
    return [
        PR(
            repo=repo,
            number=row["number"],
            title=row["title"],
            head_ref=row["headRefName"],
            mergeable=row["mergeable"],
            merge_state=row["mergeStateStatus"],
            rollup=row.get("statusCheckRollup") or [],
        )
        for row in rows or []
    ]


def survey(repos: Sequence[str]) -> list[PR]:
    """Gather the whole open Dependabot wave across the given repos."""
    prs: list[PR] = []
    for repo in repos:
        prs.extend(list_repo_prs(repo))
    return prs


def workflow_scope() -> str:
    """Report whether the active token can merge workflow-editing PRs.

    Returns ``present``/``absent`` for a classic token (its scopes ride
    in the ``X-Oauth-Scopes`` header), or ``unknown`` for a fine-grained
    token, where the header is empty and only an attempted merge tells.
    """
    r = sh("gh", "api", "-i", "rate_limit", check=False)
    for line in r.stdout.splitlines():
        if line.lower().startswith("x-oauth-scopes:"):
            scopes = {s.strip() for s in line.split(":", 1)[1].split(",")}
            if scopes == {""}:
                return "unknown"
            return "present" if "workflow" in scopes else "absent"
    return "unknown"


_CHECK_STYLE = {"pass": "green", "pending": "yellow", "fail": "red", "none": "dim"}
_MERGE_STYLE = {"MERGEABLE": "green", "CONFLICTING": "red", "UNKNOWN": "yellow"}


def _bucket_table(title: str, prs: list[PR]) -> Table:
    """Render one bucket's PRs as a rich table."""
    table = Table(title=title, title_justify="left", header_style="bold", expand=False)
    table.add_column("PR")
    table.add_column("kind")
    table.add_column("mergeable")
    table.add_column("checks")
    table.add_column("title", overflow="fold")
    for pr in sorted(prs, key=lambda p: (p.repo, p.number)):
        table.add_row(
            pr.ref,
            pr.bucket.label,
            f"[{_MERGE_STYLE.get(pr.mergeable, 'default')}]{pr.mergeable}[/]",
            f"[{_CHECK_STYLE[pr.checks]}]{pr.checks}[/]",
            pr.title,
        )
    return table


def render_summary(prs: list[PR]) -> tuple[list[PR], list[PR]]:
    """Print the two-bucket summary; return (everything_else, runtime)."""
    everything_else = [p for p in prs if not p.bucket.is_runtime]
    runtime = [p for p in prs if p.bucket.is_runtime]

    if not prs:
        console.print("[green]No open Dependabot PRs across the fleet.[/]")
        return everything_else, runtime

    console.print(
        f"\n[bold]{len(prs)}[/] open Dependabot PR(s): "
        f"[bold]{len(everything_else)}[/] everything-else, "
        f"[bold]{len(runtime)}[/] runtime\n"
    )
    if everything_else:
        console.print(_bucket_table("Everything else — automerge", everything_else))
    if runtime:
        console.print(_bucket_table("Runtime deps — review", runtime))
    return everything_else, runtime


def refresh(pr: PR) -> tuple[str, str]:
    """Re-read a PR's live (state, mergeable) — state is MERGED/CLOSED/OPEN."""
    data = gh_json(
        "pr", "view", str(pr.number), "--repo", pr.gh_repo,
        "--json", "state,mergeable",
    )
    return data["state"], data["mergeable"]


def check_buckets(pr: PR) -> list[dict]:
    """Fetch the PR's checks as ``{name, bucket}`` rows (bucket is gh's verdict).

    ``gh pr checks`` exits 8 while checks fail or run but still emits valid
    JSON, so 8 is a success here.  An empty result means CI has not yet
    registered — the caller keeps waiting rather than reading it as green.
    """
    r = sh(
        "gh", "pr", "checks", str(pr.number), "--repo", pr.gh_repo,
        "--json", "name,bucket", check=False,
    )
    if r.returncode not in (0, 8) and not r.stdout.strip():
        return []
    return json.loads(r.stdout) if r.stdout.strip() else []


def wait_for_green(pr: PR, timeout: int, skip_checks: bool) -> str:
    """Block until the PR's CI settles.

    Returns ``passed`` (green), ``failed`` (a check failed), ``merged``
    (landed out-of-band while waiting), or ``timeout``.
    """
    if skip_checks:
        return "passed"
    console.print(f"  waiting for checks on {pr.ref} (timeout {timeout}s)...")
    for elapsed in range(0, timeout, CHECK_POLL_INTERVAL):
        if elapsed and elapsed % (CHECK_POLL_INTERVAL * 4) == 0:
            if refresh(pr)[0] == "MERGED":
                return "merged"
        checks = check_buckets(pr)
        if not checks or any(c["bucket"] == "pending" for c in checks):
            time.sleep(CHECK_POLL_INTERVAL)
            continue
        if any(c["bucket"] in ("fail", "cancel") for c in checks):
            return "failed"
        return "passed"
    return "timeout"


def wait_mergeable(pr: PR, timeout: int) -> str:
    """Wait out a CONFLICTING/UNKNOWN state until the PR is mergeable.

    Dependabot rebases its PRs when the base moves; this polls for that,
    nudging once with ``@dependabot rebase`` if it stalls.  Returns
    ``ready``, ``merged`` (landed already), ``gone`` (closed), or
    ``timeout``.
    """
    nudged = False
    for elapsed in range(0, timeout, REBASE_POLL_INTERVAL):
        state, mergeable = refresh(pr)
        if state == "MERGED":
            return "merged"
        if state == "CLOSED":
            return "gone"
        if mergeable == "MERGEABLE":
            return "ready"
        if mergeable == "CONFLICTING" and not nudged and elapsed >= REBASE_NUDGE_AFTER:
            console.print(f"  {pr.ref} still conflicting — nudging @dependabot rebase")
            sh("gh", "pr", "comment", str(pr.number), "--repo", pr.gh_repo,
               "--body", "@dependabot rebase", check=False)
            nudged = True
        else:
            console.print(f"  {pr.ref} {mergeable.lower()}; waiting for rebase...")
        time.sleep(REBASE_POLL_INTERVAL)
    return "timeout"


def merge_pr(pr: PR, *, admin: bool, pretend: bool) -> bool:
    """Squash-merge one PR; return True on success.

    ``admin`` bypasses the (non-)protection to force a red merge when the
    operator opts in.  The workflow-scope refusal is caught and reported
    rather than aborting the whole run.
    """
    cmd = ["gh", "pr", "merge", str(pr.number), "--repo", pr.gh_repo,
           "--squash", "--delete-branch"]
    if admin:
        cmd.append("--admin")
    if pretend:
        console.print(f"  [dim]pretend:[/] {' '.join(cmd)}")
        return True
    r = sh(*cmd, check=False)
    if r.returncode == 0:
        console.print(f"  [green]merged[/] {pr.ref}")
        return True
    err = (r.stderr + r.stdout).strip()
    if "refusing to allow" in err and "workflow" in err:
        console.print(f"  [red]skip[/] {pr.ref}: token lacks `workflow` scope for this PR")
    else:
        console.print(f"  [red]merge failed[/] {pr.ref}: {err}")
    return False


@dataclass
class MergeCtx:
    """Knobs shared across the merge loops."""

    pretend: bool
    skip_checks: bool
    check_timeout: int


def _merge_one(pr: PR, ctx: MergeCtx) -> str:
    """Drive a single PR to merged: wait mergeable, gate on green, merge.

    Returns ``merged``, ``skipped`` (conflict timed out / already gone /
    red-and-declined) or ``failed``.
    """
    outcome = wait_mergeable(pr, REBASE_TIMEOUT)
    if outcome == "merged":
        console.print(f"  [dim]{pr.ref} already merged[/]")
        return "merged"
    if outcome == "gone":
        console.print(f"  [dim]{pr.ref} closed — skipping[/]")
        return "skipped"
    if outcome == "timeout":
        console.print(f"  [yellow]{pr.ref} still conflicting after {REBASE_TIMEOUT}s — skipping[/]")
        return "skipped"

    green = wait_for_green(pr, ctx.check_timeout, ctx.skip_checks)
    if green == "merged":
        return "merged"
    if green == "timeout":
        console.print(f"  [yellow]{pr.ref} checks didn't settle — skipping[/]")
        return "skipped"
    admin = False
    if green == "failed":
        console.bell()
        console.print(f"\n[black on bright_yellow] INPUT NEEDED [/] {pr.ref} has failing checks")
        choice = click.prompt("  force-merge (f), skip (s)", type=click.Choice(["f", "s"]),
                              default="s", show_default=True)
        if choice == "s":
            return "skipped"
        admin = True

    return "merged" if merge_pr(pr, admin=admin, pretend=ctx.pretend) else "failed"


def automerge(prs: list[PR], ctx: MergeCtx) -> None:
    """Merge the everything-else bucket one PR at a time."""
    if not prs:
        console.print("[green]Nothing to automerge.[/]")
        return
    console.print(f"\n[bold]Automerging {len(prs)} everything-else PR(s)[/]")
    tally = {"merged": 0, "skipped": 0, "failed": 0}
    for pr in sorted(prs, key=lambda p: (p.repo, p.number)):
        console.print(f"\n[bold]{pr.ref}[/] — {pr.title}")
        tally[_merge_one(pr, ctx)] += 1
    console.print(
        f"\n[bold]Automerge done:[/] "
        f"[green]{tally['merged']} merged[/], "
        f"{tally['skipped']} skipped, "
        f"[red]{tally['failed']} failed[/]"
    )


def offer_runtime(prs: list[PR], ctx: MergeCtx, *, offer: bool) -> None:
    """List runtime deps; with ``offer``, walk them one-by-one behind a prompt."""
    if not prs:
        return
    console.print(f"\n[bold]Runtime deps ({len(prs)})[/] — reach shipped installs, review each:")
    for pr in sorted(prs, key=lambda p: (p.repo, p.number)):
        console.print(f"  {pr.ref}  {pr.title}")
    if not offer:
        console.print("[dim]Re-run with --offer to merge these one-by-one.[/]")
        return
    for pr in sorted(prs, key=lambda p: (p.repo, p.number)):
        console.print(f"\n[bold]{pr.ref}[/] — {pr.title}")
        if not click.confirm("  merge this runtime dep?", default=False):
            console.print("  [dim]left open[/]")
            continue
        _merge_one(pr, ctx)


def _preflight(prs: list[PR]) -> None:
    """Warn before merging if the token can't land the CI PRs."""
    ci_count = sum(1 for p in prs if p.bucket is Bucket.CI)
    if not ci_count:
        return
    match workflow_scope():
        case "absent":
            console.print(
                f"[yellow]warning:[/] token lacks `workflow` scope — the "
                f"{ci_count} CI PR(s) editing .github/workflows/* will be skipped."
            )
        case "unknown":
            console.print(
                "[dim]note: fine-grained token — CI PRs merge only if it grants "
                "workflow write; refusals are skipped, not fatal.[/]"
            )


@click.group(invoke_without_command=True)
@click.option("--repo", "repos", multiple=True,
              help="Limit to these repo(s); repeatable. Default: the whole fleet.")
@click.pass_context
def cli(ctx: click.Context, repos: tuple[str, ...]) -> None:
    """Survey and merge Dependabot PRs across the terok-ai fleet."""
    ctx.ensure_object(dict)
    ctx.obj["repos"] = list(repos) or FLEET
    if ctx.invoked_subcommand is None:
        render_summary(survey(ctx.obj["repos"]))


@cli.command()
@click.pass_context
def summary(ctx: click.Context) -> None:
    """Print the open Dependabot wave, split into the two buckets."""
    render_summary(survey(ctx.obj["repos"]))


@cli.command()
@click.option("--pretend", is_flag=True, help="Print merge commands without running them.")
@click.option("--offer", is_flag=True, help="After automerge, walk runtime deps one-by-one.")
@click.option("--skip-checks", is_flag=True, help="Merge without waiting for CI (dangerous).")
@click.option("--check-timeout", default=CHECK_TIMEOUT_DEFAULT, show_default=True,
              help="Seconds to wait for a PR's checks to settle.")
@click.option("--yes", is_flag=True, help="Skip the pre-merge confirmation.")
@click.pass_context
def merge(ctx: click.Context, pretend: bool, offer: bool, skip_checks: bool,
          check_timeout: int, yes: bool) -> None:
    """Automerge everything-else, then review runtime deps."""
    prs = survey(ctx.obj["repos"])
    everything_else, runtime = render_summary(prs)
    if not prs:
        return
    _preflight(prs)
    if not (yes or pretend) and not click.confirm(
        f"\nAutomerge {len(everything_else)} everything-else PR(s)?", default=False
    ):
        die("aborted by operator")
    mctx = MergeCtx(pretend=pretend, skip_checks=skip_checks, check_timeout=check_timeout)
    automerge(everything_else, mctx)
    offer_runtime(runtime, mctx, offer=offer)


def main() -> None:
    """Entry point — turn Ctrl-C into a clean, actionable exit."""
    try:
        cli(obj={})
    except KeyboardInterrupt:
        console.print(
            "\n[yellow]Interrupted.[/] Remedy any stuck PR by hand and re-run — "
            "the next run reads the wave fresh."
        )
        sys.exit(130)


if __name__ == "__main__":
    main()

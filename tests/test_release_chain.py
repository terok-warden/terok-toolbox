# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Release preparation follows each PR's repository without disturbing other remotes."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "terok-release-chain.py"
UPSTREAM_OWNER = "terok-ai"
FORK_OWNER = "sliwowitz"
PACKAGE = "mkdocs-terok"
SECOND_PACKAGE = "terok-util"
BRANCH = "fix/material-dependencies"
PR_NUMBER = 149
PR_URL = f"https://github.com/{UPSTREAM_OWNER}/{PACKAGE}/pull/{PR_NUMBER}"
AUTHOR_EMAIL = "release-test@example.invalid"
SSH_REPOSITORY_URL = "git@github.com:{owner}/{repo}.git"


@dataclass(frozen=True)
class Repositories:
    """Isolated local repositories representing a release checkout and its two owners."""

    cache: Path
    checkout: Path
    upstream: Path
    origin: Path
    head: Path
    head_repo: str


@pytest.fixture
def release(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    """Load the CLI without invoking it and isolate git from operator configuration."""
    spec = importlib.util.spec_from_file_location("release_chain_under_test", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Release Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", AUTHOR_EMAIL)
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Release Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", AUTHOR_EMAIL)
    popen = subprocess.Popen

    def git_popen(args, *positional, **kwargs):
        """Reject external commands, including subprocess.run's underlying execution."""
        assert not isinstance(args, str) and args[0] == "git", args
        return popen(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", git_popen)
    return module


@pytest.fixture
def repositories(
    release: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[..., Repositories]:
    """Create local bare remotes, including a colliding branch with different content."""
    remotes = tmp_path / "remotes"
    monkeypatch.setattr(release, "GITHUB_SSH_URL", str(remotes / "{repo}.git"), raising=False)

    def create(*, head_owner: str = FORK_OWNER, origin_owner: str = UPSTREAM_OWNER) -> Repositories:
        owners = {UPSTREAM_OWNER, head_owner, origin_owner}
        paths = {owner: remotes / owner / f"{PACKAGE}.git" for owner in owners}
        for path in paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            _git(tmp_path, "init", "--bare", "--initial-branch=master", str(path))
        seed = tmp_path / "seed"
        _git(tmp_path, "init", "--initial-branch=master", str(seed))
        _commit(seed, "base")
        for path in paths.values():
            _git(seed, "push", str(path), "master")
        _git(seed, "checkout", "-b", BRANCH)
        _commit(seed, "origin branch with the same name")
        _git(seed, "push", str(paths[origin_owner]), BRANCH)
        if head_owner != origin_owner:
            _commit(seed, "actual PR head")
            _git(seed, "push", str(paths[head_owner]), BRANCH)
        cache = tmp_path / "cache"
        cache.mkdir()
        checkout = cache / PACKAGE
        _git(tmp_path, "clone", str(paths[UPSTREAM_OWNER]), str(checkout))
        _git(checkout, "remote", "rename", "origin", "upstream")
        _git(checkout, "remote", "add", "origin", str(paths[origin_owner]))
        _git(checkout, "fetch", "origin")
        return Repositories(
            cache,
            checkout,
            paths[UPSTREAM_OWNER],
            paths[origin_owner],
            paths[head_owner],
            f"{head_owner}/{PACKAGE}",
        )

    return create


def test_pr_metadata_requests_the_head_repository(release, monkeypatch):
    """Metadata must identify the repository, not only a potentially colliding branch."""
    info = _pr_info(f"{FORK_OWNER}/{PACKAGE}")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(info), ""))
    monkeypatch.setattr(release, "sh", run)
    assert release.pr_info(PR_NUMBER, f"{UPSTREAM_OWNER}/{PACKAGE}") == info
    args = run.call_args.args
    fields = args[args.index("--json") + 1].split(",")
    assert {"headRepository", "headRepositoryOwner"} <= set(fields)


def test_mixed_pr_plan_preserves_each_head_repository_in_json(release, monkeypatch, tmp_path):
    """Same-org and foreign-fork PRs retain independent destinations when saved."""
    heads = {
        PR_NUMBER: f"{FORK_OWNER}/{PACKAGE}",
        PR_NUMBER + 1: f"{UPSTREAM_OWNER}/{SECOND_PACKAGE}",
    }
    monkeypatch.setattr(release, "latest_version", lambda *_: "0.8.2")
    monkeypatch.setattr(release, "latest_final_version", lambda *_: "0.8.2")
    monkeypatch.setattr(release, "pr_info", lambda number, _: _pr_info(heads[number]))
    plan = release.generate_plan(
        [PACKAGE, SECOND_PACKAGE],
        {PACKAGE: [], SECOND_PACKAGE: []},
        org=UPSTREAM_OWNER,
        fork=UPSTREAM_OWNER,
        release_name="Test Release",
        version_step="patch",
        cache_dir=tmp_path,
        pr_specs={PACKAGE: PR_NUMBER, SECOND_PACKAGE: PR_NUMBER + 1},
    )
    restored = release.Plan.model_validate_json(plan.model_dump_json())
    assert [pkg.pr_head_repo for pkg in restored.packages] == list(heads.values())
    assert all(step.kind != release.StepKind.PR_CREATE for step in restored.steps)


@pytest.mark.parametrize(
    "missing_field",
    [None, ("headRepositoryOwner", "login"), ("headRepository", "name")],
    ids=["gh-2.87-metadata", "missing-owner", "missing-repository-name"],
)
def test_quick_validates_pr_heads_before_executing_any_package(
    release, monkeypatch, tmp_path, missing_field
):
    """Mixed quick plans resolve both fork owners before the first master release starts."""
    metadata = {
        repo: _pr_info(f"{FORK_OWNER}/{repo}") for repo in ("terok-clearance", "terok-shield")
    }
    if missing_field:
        section, field = missing_field
        metadata["terok-clearance"][section][field] = ""
    for name, result in (
        ("ensure_clone", None),
        ("_verify_dep_graph", release.DEPS),
        ("latest_version", "0.4.0"),
        ("pr_state", "OPEN"),
        ("seed_notes", None),
    ):
        monkeypatch.setattr(release, name, Mock(return_value=result))
    monkeypatch.setattr(release, "pr_info", lambda _, repo: metadata[repo.split("/", 1)[1]])
    execute = Mock()
    monkeypatch.setattr(release, "execute_plan", execute)
    arguments = {
        "chain_spec": "util%amin,shield:485,clearance:267",
        "version_step": "auto",
        "release_name": "Shield Harvester",
        "upgrade_pinned": True,
        "open_top": False,
        "prerelease": False,
        "target": "gh-only",
        "pin_style": None,
        "org": UPSTREAM_OWNER,
        "fork": UPSTREAM_OWNER,
        "cache_dir": str(tmp_path),
        "yes": True,
        "skip_checks": False,
        "check_timeout": 900,
    }
    if missing_field:
        with pytest.raises(SystemExit):
            release.quick.callback(**arguments)
        execute.assert_not_called()
        assert not list((tmp_path / "plans").glob("*.json"))
        return

    release.quick.callback(**arguments)

    execute.assert_called_once()
    (plan_path,) = (tmp_path / "plans").glob("*.json")
    saved = release.Plan.model_validate_json(plan_path.read_text())
    assert saved == execute.call_args.args[0]
    util, clearance, shield = saved.packages
    assert (util.repo, util.new_version, util.pr_head_repo) == ("terok-util", "0.5.0a1", None)
    assert clearance.pr_head_repo == f"{FORK_OWNER}/terok-clearance"
    assert shield.pr_head_repo == f"{FORK_OWNER}/terok-shield"
    assert all(step.status == "pending" for step in saved.steps)


@pytest.mark.parametrize("fork", [UPSTREAM_OWNER, FORK_OWNER])
@pytest.mark.parametrize("cached", [False, True], ids=["new", "cached"])
def test_master_clone_keeps_configured_remotes_and_checkout(
    release, monkeypatch, tmp_path, fork, cached
):
    """Both clone paths preserve the canonical source and configured release destination."""
    repo_dir = tmp_path / PACKAGE
    if cached:
        (repo_dir / ".git").mkdir(parents=True)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(release, "sh", run)
    upstream_url = SSH_REPOSITORY_URL.format(owner=UPSTREAM_OWNER, repo=PACKAGE)
    origin_url = SSH_REPOSITORY_URL.format(owner=fork, repo=PACKAGE)

    release.ensure_clone(PACKAGE, tmp_path, UPSTREAM_OWNER, fork)

    if cached:
        run.assert_any_call("git", "remote", "set-url", "upstream", upstream_url, cwd=repo_dir)
        run.assert_any_call("git", "remote", "set-url", "origin", origin_url, cwd=repo_dir)
    else:
        run.assert_any_call("git", "clone", "--quiet", upstream_url, str(repo_dir))
        run.assert_any_call("git", "remote", "rename", "origin", "upstream", cwd=repo_dir)
        run.assert_any_call("git", "remote", "add", "origin", origin_url, cwd=repo_dir)
    run.assert_any_call("git", "reset", "--hard", "upstream/master", "-q", cwd=repo_dir)


@pytest.mark.parametrize("fork", [UPSTREAM_OWNER, FORK_OWNER])
def test_master_release_still_uses_configured_origin(release, repositories, fork, monkeypatch):
    """Master releases create a release PR and push to the operator's configured fork."""
    repos = repositories(origin_owner=fork)
    plan = _plan(release, fork=fork, master=True)
    checkout = _step(release, plan, "CHECKOUT")
    assert checkout.params["base"] == "upstream/master"
    assert any(step.kind == release.StepKind.PR_CREATE for step in plan.steps)
    ctx = release.Ctx(cache_dir=repos.cache)
    upstream_before = _git(repos.upstream, "rev-parse", "master")
    release.execute_step(checkout, plan, ctx)
    commit = _commit(repos.checkout, "release from master")
    release.execute_step(_step(release, plan, "GIT_PUSH"), plan, ctx)
    branch = checkout.params["branch"]
    assert _git(repos.origin, "rev-parse", branch) == commit
    assert _git(repos.upstream, "rev-parse", "master") == upstream_before
    assert release.PR_HEAD_REMOTE not in _git(repos.checkout, "remote").splitlines()
    if fork != UPSTREAM_OWNER:
        assert branch not in _git(repos.upstream, "branch", "--list", branch)

    run = release.sh
    github_calls = []

    def github(*args, **kwargs):
        """Stub only the PR API while retaining local git branch checks."""
        if args[0] == "git":
            return run(*args, **kwargs)
        assert args[:2] == ("gh", "pr")
        github_calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[2] == "view" else 0, PR_URL, "")

    monkeypatch.setattr(release, "sh", github)
    release.execute_step(_step(release, plan, "PR_CREATE"), plan, ctx)
    create = next(args for args in github_calls if args[2] == "create")
    assert create[create.index("--repo") + 1] == f"{UPSTREAM_OWNER}/{PACKAGE}"
    assert create[create.index("--head") + 1] == f"{fork}:{branch}"
    assert plan.packages[0].pr_url == PR_URL


@pytest.mark.parametrize("head_owner", [UPSTREAM_OWNER, FORK_OWNER])
def test_pr_checkout_and_push_follow_actual_head(release, repositories, head_owner):
    """The PR's source and destination agree even when origin has a colliding branch."""
    repos = repositories(head_owner=head_owner)
    origin_before = _git(repos.origin, "rev-parse", BRANCH)
    plan = _plan(release, head_repo=repos.head_repo)
    ctx = release.Ctx(cache_dir=repos.cache)
    release.execute_step(_step(release, plan, "CHECKOUT"), plan, ctx)
    assert _git(repos.checkout, "rev-parse", "HEAD") == _git(repos.head, "rev-parse", BRANCH)
    commit = _commit(repos.checkout, "release preparation")
    release.execute_step(_step(release, plan, "GIT_PUSH"), plan, ctx)
    assert _git(repos.head, "rev-parse", BRANCH) == commit
    assert _git(repos.checkout, "remote", "get-url", "origin") == str(repos.origin)
    if head_owner != UPSTREAM_OWNER:
        assert _git(repos.origin, "rev-parse", BRANCH) == origin_before


def test_resume_checkout_does_not_need_clone_sync(release, repositories, monkeypatch):
    """A failed checkout can resume after clone-sync has already completed."""
    repos = repositories()
    plan = _plan(release, head_repo=repos.head_repo)
    sync = _step(release, plan, "CLONE_SYNC")
    sync.status = "completed"
    checkout = _step(release, plan, "CHECKOUT")
    checkout.status = "failed"
    plan.steps = [sync, checkout]
    monkeypatch.setattr(
        release, "ensure_clone", Mock(side_effect=AssertionError("Must not resync"))
    )
    release.execute_plan(plan, mode=release.ExecMode.RESUME, ctx=release.Ctx(cache_dir=repos.cache))
    assert checkout.status == "completed"
    assert _git(repos.checkout, "rev-parse", "HEAD") == _git(repos.head, "rev-parse", BRANCH)


def test_repaired_plan_preserves_completed_upstream_release(release, repositories, monkeypatch):
    """Repairing PR metadata resumes checkout without repeating an upstream publication."""
    repos = repositories()
    plan = _plan(release, head_repo="")
    util = release.PackagePlan(
        repo=SECOND_PACKAGE,
        action=release.Action.RELEASE_MASTER,
        current_version="0.4.0",
        new_version="0.5.0a1",
    )
    completed = release.Step(
        id=f"{SECOND_PACKAGE}:release",
        kind=release.StepKind.RELEASE,
        package=SECOND_PACKAGE,
        params={"tag": "v0.5.0a1"},
        status="completed",
        result={"release_id": 123},
    )
    plan.packages.insert(0, util)
    plan.packages[1].sibling_deps = {SECOND_PACKAGE: "0.5.0a1"}
    sync, checkout = (_step(release, plan, kind) for kind in ("CLONE_SYNC", "CHECKOUT"))
    sync.status, checkout.status = "completed", "failed"
    plan.steps = [completed, sync, checkout]
    payload = json.loads(plan.model_dump_json())
    payload["packages"][1]["pr_head_repo"] = repos.head_repo
    repaired = release.Plan.model_validate_json(json.dumps(payload))
    execute_step = release.execute_step

    def checkout_only(step, plan, ctx):
        """Fail before any completed release or clone operation can run again."""
        assert (step.package, step.kind) == (PACKAGE, release.StepKind.CHECKOUT)
        return execute_step(step, plan, ctx)

    monkeypatch.setattr(release, "execute_step", checkout_only)
    release.execute_plan(
        repaired, mode=release.ExecMode.RESUME, ctx=release.Ctx(cache_dir=repos.cache)
    )
    assert repaired.packages[0] == util
    assert repaired.packages[1].sibling_deps == {SECOND_PACKAGE: "0.5.0a1"}
    assert repaired.steps[:2] == [completed, sync]
    assert repaired.steps[2].status == "completed"
    assert _git(repos.checkout, "rev-parse", "HEAD") == _git(repos.head, "rev-parse", BRANCH)


def test_resume_push_restores_head_remote_without_resetting_work(release, repositories):
    """Resuming a failed push reselects its destination while preserving the prepared commit."""
    repos = repositories()
    plan = _plan(release, head_repo=repos.head_repo)
    ctx = release.Ctx(cache_dir=repos.cache)
    release.execute_step(_step(release, plan, "CHECKOUT"), plan, ctx)
    commit = _commit(repos.checkout, "prepared before interruption")
    _git(repos.checkout, "remote", "set-url", "pr-head", str(repos.origin))
    _git(repos.checkout, "config", "remote.pr-head.pushurl", str(repos.origin))
    push = _step(release, plan, "GIT_PUSH")
    push.status = "failed"
    plan.steps = [push]
    release.execute_plan(plan, mode=release.ExecMode.RESUME, ctx=ctx)
    assert push.status == "completed"
    assert _git(repos.checkout, "rev-parse", "HEAD") == commit
    assert _git(repos.head, "rev-parse", BRANCH) == commit


@pytest.mark.parametrize("master", [True, False], ids=["master", "fork-pr"])
def test_rejected_push_stops_before_merge_or_tag(
    release, repositories, tmp_path, monkeypatch, master
):
    """A denied push cannot continue to merge an unmodified branch or publish a release."""
    repos = repositories()
    plan = _plan(release, head_repo=repos.head_repo, master=master)
    target = repos.origin if master else repos.head
    ctx = release.Ctx(cache_dir=repos.cache, plan_path=tmp_path / "plan.json")
    release.execute_step(_step(release, plan, "CHECKOUT"), plan, ctx)
    before = _git(target, "show-ref", "--heads")
    _commit(repos.checkout, "rejected preparation")
    hook = target / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    plan.steps = [_step(release, plan, kind) for kind in ("GIT_PUSH", "PR_MERGE", "TAG")]
    execute_step = release.execute_step

    def push_only(step, plan, ctx):
        """Never permit a broken implementation to reach irreversible operations."""
        assert step.kind == release.StepKind.GIT_PUSH, "Continued after a rejected push"
        return execute_step(step, plan, ctx)

    monkeypatch.setattr(release, "execute_step", push_only)
    with pytest.raises(SystemExit):
        release.execute_plan(plan, mode=release.ExecMode.EXECUTE, ctx=ctx)
    assert [step.status for step in plan.steps] == ["failed", "pending", "pending"]
    assert _git(target, "show-ref", "--heads") == before
    restored = release.Plan.model_validate_json(ctx.plan_path.read_text())
    assert restored.steps[0].status == "failed"


@pytest.mark.parametrize("master", [True, False], ids=["master", "fork-pr"])
def test_concurrent_update_is_not_overwritten(release, repositories, tmp_path, master):
    """Force-with-lease preserves another author's work after the checkout snapshot."""
    repos = repositories()
    plan = _plan(release, head_repo=repos.head_repo, master=master)
    target = repos.origin if master else repos.head
    ctx = release.Ctx(cache_dir=repos.cache)
    release.execute_step(_step(release, plan, "CHECKOUT"), plan, ctx)
    branch = _step(release, plan, "GIT_PUSH").params["branch"]
    if master:
        _git(repos.checkout, "push", "-u", "origin", branch)
    _commit(repos.checkout, "release preparation")
    other = tmp_path / "other-author"
    _git(tmp_path, "clone", "--branch", branch, str(target), str(other))
    concurrent = _commit(other, "concurrent update")
    _git(other, "push", "origin", branch)
    with pytest.raises(SystemExit):
        release.execute_step(_step(release, plan, "GIT_PUSH"), plan, ctx)
    assert _git(target, "rev-parse", branch) == concurrent


@pytest.mark.parametrize("master", [True, False], ids=["master", "fork-pr"])
def test_push_at_upstream_master_is_a_noop(release, monkeypatch, tmp_path, master):
    """No release payload means no push or remote configuration for either source."""
    plan = _plan(release, master=master)
    monkeypatch.setattr(release, "_branch_matches_upstream", Mock(return_value=True))
    run = Mock(side_effect=AssertionError("No git command expected"))
    monkeypatch.setattr(release, "sh", run)
    release.execute_step(_step(release, plan, "GIT_PUSH"), plan, release.Ctx(cache_dir=tmp_path))
    run.assert_not_called()


def test_incomplete_pr_plan_requires_repair_before_resuming(release, repositories, capsys):
    """An incomplete saved plan requires repair without restarting completed releases."""
    repos = repositories()
    plan = _plan(release)
    with pytest.raises(SystemExit):
        release.execute_step(
            _step(release, plan, "CHECKOUT"), plan, release.Ctx(cache_dir=repos.cache)
        )
    assert "repair the saved plan before resuming" in " ".join(capsys.readouterr().err.split())


def _plan(
    release: ModuleType,
    *,
    head_repo: str | None = None,
    fork: str = UPSTREAM_OWNER,
    master: bool = False,
) -> Any:
    """Create the actual planner's release steps without making GitHub requests."""
    pkg = release.PackagePlan(
        repo=PACKAGE,
        action=release.Action.RELEASE_MASTER if master else release.Action.RELEASE_PR,
        current_version="0.8.2",
        new_version="0.8.3",
        pr_number=None if master else PR_NUMBER,
        pr_branch=None if master else BRANCH,
        pr_url=None if master else PR_URL,
        pr_head_repo=None if master else head_repo,
    )
    return release.Plan(
        packages=[pkg],
        gh_org=UPSTREAM_OWNER,
        gh_fork=fork,
        steps=release.plan_steps(pkg, UPSTREAM_OWNER, "Test Release", "pypi", "pypi", False),
    )


def _pr_info(head_repo: str) -> dict[str, Any]:
    """Return gh 2.87 metadata, which leaves the aggregate repository slug empty."""
    owner, name = head_repo.split("/", 1)
    return {
        "headRefName": BRANCH,
        "state": "OPEN",
        "title": "Prepare release",
        "url": PR_URL,
        "headRepository": {"name": name, "nameWithOwner": ""},
        "headRepositoryOwner": {"login": owner},
    }


def _step(release: ModuleType, plan: Any, kind: str) -> Any:
    """Select one planned step by its symbolic kind."""
    return next(step for step in plan.steps if step.kind == getattr(release.StepKind, kind))


def _commit(repo: Path, content: str) -> str:
    """Record a distinguishable commit and return its SHA."""
    (repo / "payload.txt").write_text(content)
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-m", content)
    return _git(repo, "rev-parse", "HEAD")


def _git(repo: Path, *args: str) -> str:
    """Run only local git operations inside the fixture's explicitly selected repository."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

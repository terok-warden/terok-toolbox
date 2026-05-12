# terok-toolbox

Operational tooling for the **terok-warden** account — the release officer for
the [terok-ai](https://github.com/terok-ai) package family.

## Contents

- `scripts/terok-release-chain.py` — orchestrates cascading releases across
  `mkdocs-terok`, `terok-clearance`, `terok-shield`, `terok-sandbox`,
  `terok-executor`, and `terok`. Supports `--target {pypi, testpypi, gh-only}`
  for production, validation, and PyPI-skipping flows.
- `warden` — host-side wrapper that creates and enters a persistent
  [distrobox](https://distrobox.it) container based on `fedora-toolbox:44`,
  with its own isolated `$HOME` and `--unshare-all` so warden's PAT, GPG key,
  and git identity never live in the operator's personal home.

## One-time setup

Requires Fedora Silverblue 44+ with `distrobox` available (`rpm-ostree install
distrobox` if not already there).

```bash
git clone git@github.com:terok-warden/terok-toolbox.git ~/warden/toolbox
ln -s ~/warden/toolbox/warden ~/.local/bin/warden

warden gh auth login                            # auto-creates the container
warden git config --global user.name  "terok warden"
warden git config --global user.email "<warden's email>"
```

The container's `$HOME` defaults to `~/warden/home` (override with
`WARDEN_HOME`). The toolbox checkout is bind-mounted at `/toolbox` inside the
container, so script edits on the host show up immediately — no rebuild
needed for script changes.

### Warden SSH key (required for `git push`)

The wrapper strips `SSH_AUTH_SOCK` from the container's environment so the
host's ssh-agent never leaks in — git pushes from inside warden would
otherwise use the host user's keys and attribute the push to the wrong
identity. The container then has no SSH keys at all by default; generate
warden's own once:

```bash
warden ssh-keygen -t ed25519 -C "terok-warden" -f ~/.ssh/id_ed25519 -N ""
warden cat ~/.ssh/id_ed25519.pub
```

Add the printed public key to `terok-warden`'s GitHub account:
**<https://github.com/settings/keys>** (logged in as terok-warden) → New SSH
key → "Authentication Key". The PAT scope used for `gh auth login` doesn't
cover SSH key management, so this step goes through the web UI.

Verify:

```bash
warden ssh -T git@github.com
# Hi terok-warden! You've successfully authenticated, but GitHub does not
# provide shell access.
```

## Daily use

```bash
warden                                          # interactive shell
warden terok-release plan sandbox..terok        # plan a chain release
warden terok-release execute plan.json
warden gh auth status                           # any tool in the image
```

## Operator-mode runs (gh-only, no warden box)

PyPI/TestPyPI publishes go through warden because the `pypi` environment
gates on `triggering_actor == terok-warden`. **`--target=gh-only` runs do
not** — the `github-release` job has no actor check (tag-creation is
gated by the org's `v*.*.*` ruleset instead). So `--version-step=alpha`
dev-cycle cuts and any other gh-only release can be driven by the
operator directly from their own Fedora toolbox, no warden detour.

One-time, inside the toolbox:

```bash
toolbox run sudo dnf install gh poetry \
    python3-click python3-tomlkit python3-pydantic python3-rich
```

Then, from a clone of this repo on the host:

```bash
# Cross-repo PR chain, top kept as a deps-only PR (--open-top),
# cut as PEP 440 alpha tags for dev-cycle integration.
toolbox run python3 ./scripts/terok-release-chain.py \
    quick executor:293,terok:912 --open-top \
    --version-step alpha -n "Mixed Hosts" --target gh-only
```

`TEROK_GH_FORK` (or `--fork`) must point at the operator's personal fork
(e.g. `sliwowitz`) — the script refuses to run without it.  `--target`
is restricted to `gh-only` in operator mode; PyPI publishes still need
the warden box because of the environment's required-reviewer gate.

## First-PyPI-release bootstrap

Before any package is on real PyPI, do a TestPyPI validation pass per package
in dep order. Each pass: cut to TestPyPI, manually verify the project page on
`test.pypi.org`, then cut to real PyPI. Each step requires approval at the
`pypi` environment gate.

```bash
# Per package, in CHAIN order (mkdocs-terok → clearance → shield → sandbox
# → executor → terok). After each TestPyPI run, eyeball the published
# project page on test.pypi.org for layout, README rendering, etc.
warden terok-release quick mkdocs --target=testpypi
# verify on test.pypi.org/project/mkdocs-terok/
warden terok-release quick mkdocs --target=pypi

warden terok-release quick clearance --target=testpypi
# verify
warden terok-release quick clearance --target=pypi

# ... and so on through the chain
```

After every package is on real PyPI, the `testpypi-publish` job is removed
from each repo's `release.yml` in a separate cleanup PR. From that point on,
testpypi mode requires a temporary one-off restoration of the job — used
only for occasional workflow-change verification.

## Refreshing the container

The container persists across host restarts. To recreate from scratch (e.g.
to pull a fresh `fedora-toolbox:44` or to discard accumulated state):

```bash
WARDEN_REBUILD=1 warden
```

## License

Apache-2.0. See `LICENSE`.

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

## Daily use

```bash
warden                                          # interactive shell
warden terok-release plan sandbox..terok        # plan a chain release
warden terok-release execute plan.json
warden gh auth status                           # any tool in the image
```

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

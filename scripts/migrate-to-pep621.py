#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0
"""One-shot migration: ``[tool.poetry]`` static metadata → PEP 621 ``[project]``.

Run once per sibling repo:

.. code-block:: bash

    python3 migrate-to-pep621.py path/to/pyproject.toml

What moves to ``[project]``:
    name, description, readme, license, authors, maintainers, keywords,
    homepage / repository / documentation → ``[project.urls]``,
    ``[tool.poetry.urls]`` table → ``[project.urls]``,
    ``[tool.poetry.scripts]`` (console-script entrypoints) → ``[project.scripts]``,
    ``[tool.poetry.dependencies]`` (incl. URL/git pins) → ``[project.dependencies]``
    as PEP 508 strings (``name @ url``, ``name>=X,<Y``, etc.),
    ``python = "<spec>"`` from deps → ``requires-python``.

What stays in ``[tool.poetry]``:
    version (marked ``dynamic`` in ``[project]``; ``poetry-dynamic-versioning``
    keeps owning it), classifiers (marked dynamic so Poetry's auto-enrichment
    of Python-version classifiers keeps working), packages, include,
    ``[tool.poetry.group.*]`` dependency groups (not deprecated).

What's deleted:
    ``"License :: OSI Approved :: ..."`` classifier — PEP 639 makes
    ``[project.license]`` the source of truth; the trove classifier is
    redundant and PyPI rejects new releases that still carry it.

Idempotent: skips files where ``[project]`` is already present.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import Table


_AUTHOR_RE = re.compile(r"^(?P<name>.+?)\s*<(?P<email>[^>]+)>\s*$")
_LICENSE_CLASSIFIER_RE = re.compile(r"^\s*License\s*::")


def _author(s: str):
    """Parse ``"Name <email>"`` into an inline table ``{name=..., email=...}``."""
    it = tomlkit.inline_table()
    m = _AUTHOR_RE.match(s)
    if m:
        it["name"] = m["name"].strip()
        it["email"] = m["email"].strip()
    else:
        it["name"] = s.strip()
    return it


def _range(ver: str) -> str:
    """Convert a Poetry version constraint to a PEP 508 specifier.

    Handles caret, tilde, exact, range, and wildcard forms.  Result is
    appended directly after the dep name (no separator) for caret/tilde/
    exact; range/comparator strings pass through unchanged.
    """
    ver = ver.strip()
    if ver in ("", "*"):
        return ""
    if ver.startswith("^"):
        v = ver[1:]
        parts = v.split(".")
        nums = [int(p) for p in parts]
        major = nums[0]
        minor = nums[1] if len(nums) > 1 else 0
        patch = nums[2] if len(nums) > 2 else 0
        if major > 0:
            upper = f"{major + 1}.0.0"
        elif minor > 0:
            upper = f"0.{minor + 1}.0"
        else:
            upper = f"0.0.{patch + 1}"
        return f">={v},<{upper}"
    if ver.startswith("~"):
        v = ver[1:]
        parts = v.split(".")
        nums = [int(p) for p in parts]
        if len(nums) >= 2:
            upper = f"{nums[0]}.{nums[1] + 1}.0"
        else:
            upper = f"{nums[0] + 1}.0.0"
        return f">={v},<{upper}"
    if re.match(r"^[<>=!~]", ver):
        return ver
    # Bare version → exact pin
    return f"=={ver}"


def _pep508(name: str, spec) -> str:
    """Build one PEP 508 dep string from a Poetry dependency entry."""
    if isinstance(spec, str):
        rng = _range(spec)
        return f"{name}{rng}"
    if not isinstance(spec, dict):
        return name
    # URL pin (sibling wheel)
    if "url" in spec:
        return f"{name} @ {spec['url']}"
    # Git pin (PR-chain branch deps)
    if "git" in spec:
        ref = spec.get("branch") or spec.get("tag") or spec.get("rev")
        suffix = f"@{ref}" if ref else ""
        return f"{name} @ git+{spec['git']}{suffix}"
    # Path pin (rare in main deps)
    if "path" in spec:
        return f"{name} @ file://{spec['path']}"
    # Version + extras + python marker
    extras = ""
    if spec.get("extras"):
        extras = "[" + ",".join(spec["extras"]) + "]"
    ver = spec.get("version", "")
    rng = _range(ver)
    out = f"{name}{extras}{rng}"
    if "python" in spec:
        out += f"; python_version {spec['python']}"
    if "markers" in spec:
        out += f"; {spec['markers']}"
    return out


def _strip_license_classifier(classifiers):
    new = tomlkit.array()
    for c in classifiers:
        if not _LICENSE_CLASSIFIER_RE.match(str(c)):
            new.append(c)
    new.multiline(True)
    return new


def _multiline_array(items):
    a = tomlkit.array()
    for it in items:
        a.append(it)
    a.multiline(True)
    return a


def migrate(path: Path) -> bool:
    """Mutate *path* in place; return True if migration ran, False if skipped."""
    doc: TOMLDocument = tomlkit.parse(path.read_text())
    if "project" in doc:
        return False

    poetry: Table = doc["tool"]["poetry"]
    project = tomlkit.table()

    project["name"] = poetry["name"]
    if "description" in poetry:
        project["description"] = poetry["description"]
    if "readme" in poetry:
        project["readme"] = poetry["readme"]
    if "license" in poetry:
        project["license"] = poetry["license"]
    if "authors" in poetry:
        project["authors"] = _multiline_array([_author(a) for a in poetry["authors"]])
    if "maintainers" in poetry:
        project["maintainers"] = _multiline_array([_author(m) for m in poetry["maintainers"]])
    if "keywords" in poetry:
        project["keywords"] = poetry["keywords"]

    poetry_deps = poetry.get("dependencies", {})
    if "python" in poetry_deps:
        py = poetry_deps["python"]
        project["requires-python"] = _range(py) if py.startswith(("^", "~")) else py

    project["dynamic"] = ["version", "classifiers"]

    runtime_deps = [_pep508(n, s) for n, s in poetry_deps.items() if n != "python"]
    if runtime_deps:
        project["dependencies"] = _multiline_array(runtime_deps)

    urls = tomlkit.table()
    for old, new in (
        ("homepage", "Homepage"),
        ("repository", "Repository"),
        ("documentation", "Documentation"),
    ):
        if old in poetry:
            urls[new] = poetry[old]
    if "urls" in poetry:
        for k, v in poetry["urls"].items():
            urls[k] = v
    if len(urls) > 0:
        project["urls"] = urls

    if "scripts" in poetry:
        scripts_tbl = tomlkit.table()
        for k, v in poetry["scripts"].items():
            scripts_tbl[k] = v
        project["scripts"] = scripts_tbl

    # Entry points (e.g. mkdocs plugins): [tool.poetry.plugins."group.name"]
    # → [project.entry-points."group.name"].  Nested dotted keys preserved.
    if "plugins" in poetry:
        entry_points = tomlkit.table()
        for group, mapping in poetry["plugins"].items():
            group_tbl = tomlkit.table()
            for k, v in mapping.items():
                group_tbl[k] = v
            entry_points[group] = group_tbl
        project["entry-points"] = entry_points

    # Strip migrated fields from [tool.poetry]
    for f in (
        "name", "description", "readme", "license", "authors", "maintainers",
        "keywords", "homepage", "repository", "documentation", "urls", "scripts",
        "plugins",
    ):
        if f in poetry:
            del poetry[f]

    # Strip python from poetry deps; drop the whole table if empty afterward
    if "python" in poetry_deps:
        del poetry_deps["python"]
    runtime_keys = [k for k in poetry_deps]
    for k in runtime_keys:
        del poetry_deps[k]
    if "dependencies" in poetry and len(poetry["dependencies"]) == 0:
        del poetry["dependencies"]

    # Strip License :: classifiers
    if "classifiers" in poetry:
        poetry["classifiers"] = _strip_license_classifier(poetry["classifiers"])

    # Inject [project] after [build-system] (or at top if absent).  tomlkit
    # exposes the document as an ordered container; we rebuild the order by
    # adding [project] before [tool] gets re-touched.
    new_doc = tomlkit.document()
    inserted = False
    for key, value in doc.body:
        if not inserted and key is not None and str(key).startswith("tool"):
            new_doc.add("project", project)
            new_doc.add(tomlkit.nl())
            inserted = True
        if key is None:
            new_doc.add(value)
        else:
            new_doc.add(key, value)
    if not inserted:
        new_doc.add("project", project)

    path.write_text(tomlkit.dumps(new_doc))
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <pyproject.toml> [<pyproject.toml>...]", file=sys.stderr)
        return 2
    rc = 0
    for arg in sys.argv[1:]:
        p = Path(arg)
        if not p.exists():
            print(f"✗ not found: {p}")
            rc = 1
            continue
        if migrate(p):
            print(f"✓ migrated {p}")
        else:
            print(f"⊘ skipped (already has [project]): {p}")
    return rc


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Generate the dependency-license inventory from local evidence only.

Every row must trace to something on this machine: a committed lockfile, an
installed distribution's own metadata, or a recorded official-registry response.
Nothing is inferred from a package's name or reputation -- a dependency whose
license cannot be evidenced is emitted with `"unresolved": true` rather than
guessed, because a confident wrong license is worse than an admitted gap.

The output is technical evidence, not legal clearance.

Usage:
    python tools/generate_dependency_licenses.py --write
    python tools/generate_dependency_licenses.py --check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
INVENTORY_PATH = REPO / "docs" / "dependency_licenses.json"
NOTICES_PATH = REPO / "THIRD_PARTY_NOTICES.md"

# Licenses whose terms could materially affect how a downstream user may deploy
# the project. Surfaced explicitly instead of being averaged into a count.
COPYLEFT_MARKERS = ("GPL", "AGPL", "LGPL", "MPL", "EPL", "CDDL", "SSPL", "OSL")


@dataclass
class Dependency:
    ecosystem: str
    name: str
    version: str | None
    license_expression: str | None
    dependency_class: str
    metadata_source: str
    bundled: bool = False
    unresolved: bool = False
    notes: str | None = None


@dataclass
class Inventory:
    schema: str = "xrml_dependency_license_inventory"
    version: str = "1.0.0"
    bundles_dependency_code: bool = False
    disclaimer: str = (
        "Technical evidence assembled from lockfiles and locally available package "
        "metadata. Not legal advice and not a license-compatibility clearance."
    )
    dependencies: list[Dependency] = field(default_factory=list)


def _normalize(name: str) -> str:
    return re.split(r"[<>=!\[;~^ ]", name, 1)[0].strip().lower().replace("_", "-")


def _installed_python_metadata(site_packages: Path) -> dict[str, tuple[str, str | None]]:
    """Read license facts out of installed `*.dist-info/METADATA` headers."""
    found: dict[str, tuple[str, str | None]] = {}
    if not site_packages.is_dir():
        return found
    for metadata in site_packages.glob("*.dist-info/METADATA"):
        name = version = expression = legacy = None
        classifiers: list[str] = []
        with metadata.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    break  # headers end at the first blank line; the body is prose
                if line.startswith("Name: "):
                    name = _normalize(line[6:])
                elif line.startswith("Version: "):
                    version = line[9:].strip()
                elif line.startswith("License-Expression: "):
                    expression = line[20:].strip()
                elif line.startswith("License: "):
                    legacy = line[9:].strip()
                elif line.startswith("Classifier: License :: "):
                    classifiers.append(line[23:].strip())
        if not name:
            continue
        resolved = expression or (classifiers[0] if classifiers else None) or legacy
        found[name] = (version or "", resolved)
    return found


def collect(site_packages: Path, registry_overrides: dict[str, str] | None = None) -> Inventory:
    overrides = {(_normalize(k)): v for k, v in (registry_overrides or {}).items()}
    inventory = Inventory()

    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    locked = {_normalize(p["name"]): p.get("version") for p in lock.get("package", [])}
    installed = _installed_python_metadata(site_packages)

    direct = {_normalize(d) for d in project.get("dependencies", [])}
    extras = {k: {_normalize(d) for d in v} for k, v in project.get("optional-dependencies", {}).items()}

    def python_row(name: str, dependency_class: str) -> Dependency:
        version = locked.get(name)
        expression = source = None
        if name in overrides:
            expression, source = overrides[name], "official PyPI JSON metadata"
        elif name in installed and installed[name][1]:
            expression, source = installed[name][1], "installed dist-info METADATA"
            version = version or installed[name][0]
        note = None
        if version is None:
            note = "declared in pyproject but absent from uv.lock; version is not pinned"
        return Dependency(
            ecosystem="python",
            name=name,
            version=version,
            license_expression=expression,
            dependency_class=dependency_class,
            metadata_source=source or "unresolved",
            unresolved=expression is None,
            notes=note,
        )

    for name in sorted(direct):
        inventory.dependencies.append(python_row(name, "direct-runtime"))
    for extra, names in sorted(extras.items()):
        klass = "development" if extra == "dev" else f"optional[{extra}]"
        for name in sorted(names - direct):
            inventory.dependencies.append(python_row(name, klass))
    accounted = direct | set().union(*extras.values()) if extras else direct
    for name in sorted(set(locked) - accounted - {_normalize(project["name"])}):
        inventory.dependencies.append(python_row(name, "transitive"))

    manifest = json.loads((REPO / "web" / "package.json").read_text(encoding="utf-8"))
    lockfile = json.loads((REPO / "web" / "package-lock.json").read_text(encoding="utf-8"))
    nodes = {k: v for k, v in lockfile.get("packages", {}).items() if k}
    by_name: dict[str, dict[str, Any]] = {}
    for key, node in nodes.items():
        by_name.setdefault(key.split("node_modules/")[-1], node)

    direct_web = set(manifest.get("dependencies", {}))
    dev_web = set(manifest.get("devDependencies", {}))
    for name, node in sorted(by_name.items()):
        if name in direct_web:
            klass = "direct-runtime"
        elif name in dev_web:
            klass = "direct-development"
        else:
            klass = "transitive-development" if node.get("dev") else "transitive-runtime"
        expression = node.get("license")
        inventory.dependencies.append(
            Dependency(
                ecosystem="npm",
                name=name,
                version=node.get("version"),
                license_expression=expression if isinstance(expression, str) else None,
                dependency_class=klass,
                metadata_source="web/package-lock.json",
                unresolved=not isinstance(expression, str),
            )
        )
    return inventory


def _summary(inventory: Inventory) -> dict[str, Any]:
    unresolved = [d for d in inventory.dependencies if d.unresolved]
    copyleft = [
        d for d in inventory.dependencies
        if d.license_expression and any(m in d.license_expression.upper() for m in COPYLEFT_MARKERS)
    ]
    direct_unresolved = [d for d in unresolved if d.dependency_class.startswith(("direct", "optional"))]
    return {
        "total": len(inventory.dependencies),
        "unresolved": len(unresolved),
        "direct_unresolved": [d.name for d in direct_unresolved],
        "copyleft": [(d.name, d.license_expression, d.dependency_class) for d in copyleft],
    }


def render_notices(inventory: Inventory) -> str:
    summary = _summary(inventory)
    lines = [
        "# Third-Party Notices",
        "",
        "This project depends on the packages below. **It does not bundle, vendor or",
        "redistribute any of their code** -- not in this repository and not in the source",
        "archive. Each is fetched from its own registry at install time and remains under",
        "its own terms. The project's own Apache-2.0 license does not extend to them.",
        "",
        "Generated by `tools/generate_dependency_licenses.py` from committed lockfiles and",
        "locally installed package metadata. The machine-readable form is",
        "[`docs/dependency_licenses.json`](docs/dependency_licenses.json).",
        "",
        "> This is technical evidence, not legal advice and not a compatibility clearance.",
        "> Unresolved entries need owner or legal review before any official release.",
        "",
    ]

    if summary["copyleft"]:
        lines += [
            "## Copyleft-family dependencies",
            "",
            "Called out separately because their terms can affect how you deploy a derived",
            "work. Whether each one reaches a default install is stated per row -- do not",
            "assume optional means absent, or transitive means harmless.",
            "",
            "| Package | Declared terms | Class | In a default install? |",
            "| --- | --- | --- | --- |",
        ]
        for name, expression, klass in summary["copyleft"]:
            default = "no -- optional extra" if klass.startswith("optional") else (
                "**yes** -- pulled in transitively" if klass.startswith("transitive") else "yes")
            lines.append(f"| `{name}` | {expression} | {klass} | {default} |")
        lines += [
            "",
            "**PyMuPDF** is the one that changes what you may do. It is installed only by the",
            "optional `fulltext` extra, every call site imports it lazily inside",
            "`try`/`except`, and the application runs without it. If you install that extra",
            "and deploy the result, the terms of that combination are yours to resolve.",
            "",
            "**certifi** is MPL-2.0 and *is* present in a default install, reached through",
            "`httpx` -> `httpcore`. MPL-2.0 is file-level copyleft: it reaches modifications",
            "of certifi's own files, not the code that imports it. This project neither",
            "modifies nor bundles it.",
            "",
        ]

    order = [
        ("direct-runtime", "Direct runtime dependencies"),
        ("optional[fulltext]", "Optional dependencies -- `fulltext` extra"),
        ("optional[codex-sdk]", "Optional dependencies -- `codex-sdk` extra"),
        ("development", "Development dependencies"),
        ("direct-development", "Direct development dependencies"),
    ]
    for klass, heading in order:
        rows = [d for d in inventory.dependencies if d.dependency_class == klass]
        if not rows:
            continue
        lines += [f"## {heading}", "", "| Ecosystem | Package | Version | Declared license | Source |",
                  "| --- | --- | --- | --- | --- |"]
        for d in sorted(rows, key=lambda x: (x.ecosystem, x.name)):
            expression = d.license_expression or "**unresolved**"
            lines.append(
                f"| {d.ecosystem} | `{d.name}` | {d.version or 'not pinned'} | {expression} | {d.metadata_source} |"
            )
        lines.append("")

    transitive = [d for d in inventory.dependencies if d.dependency_class.startswith("transitive")]
    counts: dict[str, int] = {}
    for d in transitive:
        counts[d.license_expression or "unresolved"] = counts.get(d.license_expression or "unresolved", 0) + 1
    lines += ["## Transitive dependencies", "",
              f"{len(transitive)} transitive packages. Declared license distribution:", "",
              "| Declared license | Count |", "| --- | --- |"]
    for expression, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| {expression} | {count} |")
    lines += ["",
              "## Unresolved",
              ""]
    unresolved = [d for d in inventory.dependencies if d.unresolved]
    if not unresolved:
        lines.append("None. Every dependency above carries a locally evidenced license expression.")
    else:
        lines.append("These could not be evidenced locally and require review before an official release:")
        lines.append("")
        for d in sorted(unresolved, key=lambda x: (x.ecosystem, x.name)):
            lines.append(f"- `{d.name}` ({d.ecosystem}, {d.dependency_class}) -- {d.notes or 'no license metadata found'}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site-packages", type=Path, default=Path(sys.prefix) / "Lib" / "site-packages")
    parser.add_argument("--registry-overrides", type=Path, default=None,
                        help="JSON mapping of package name to officially retrieved license expression")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true", help="fail if the committed inventory is stale")
    args = parser.parse_args(argv)

    overrides = json.loads(args.registry_overrides.read_text(encoding="utf-8")) if args.registry_overrides else {}
    inventory = collect(args.site_packages, overrides)
    summary = _summary(inventory)

    payload = json.dumps(
        {k: v for k, v in asdict(inventory).items() if k != "dependencies"}
        | {"dependencies": [asdict(d) for d in inventory.dependencies]},
        indent=2, sort_keys=False,
    ) + "\n"
    notices = render_notices(inventory)

    if args.check:
        stale = []
        if not INVENTORY_PATH.is_file() or INVENTORY_PATH.read_text(encoding="utf-8") != payload:
            stale.append(str(INVENTORY_PATH.relative_to(REPO)))
        if not NOTICES_PATH.is_file() or NOTICES_PATH.read_text(encoding="utf-8") != notices:
            stale.append(str(NOTICES_PATH.relative_to(REPO)))
        if stale:
            print("stale generated files: " + ", ".join(stale), file=sys.stderr)
            return 1
        print("dependency-license inventory is current")
        return 0

    if args.write:
        INVENTORY_PATH.write_text(payload, encoding="utf-8")
        NOTICES_PATH.write_text(notices, encoding="utf-8")
        print(f"wrote {INVENTORY_PATH.relative_to(REPO)} and {NOTICES_PATH.relative_to(REPO)}")

    print(f"  dependencies      {summary['total']}")
    print(f"  unresolved        {summary['unresolved']}")
    print(f"  direct unresolved {summary['direct_unresolved'] or 'none'}")
    print(f"  copyleft-family   {[c[0] for c in summary['copyleft']] or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

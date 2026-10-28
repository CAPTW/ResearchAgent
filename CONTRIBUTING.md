# Contributing

Thanks for looking at this project. It is a research agent built for local,
single-operator use, and contributions are welcome on that footing.

The project is licensed **Apache-2.0**. By submitting a contribution you agree it
is licensed under the same terms, per section 5 of the license. There is no
separate CLA.

Note that the license covers the project's own source only — not dependencies
(never bundled), retrieved article content, datasets, runtime data, or generated
reports. Read [`docs/LICENSE_SCOPE.md`](docs/LICENSE_SCOPE.md) before assuming
anything in this tree is redistributable.

## Getting set up

Requirements: Python >= 3.11 and Node.js >= 20. Dependency resolution uses
[`uv`](https://github.com/astral-sh/uv) for Python and npm for the web client.

```bash
uv sync --frozen --extra dev
cd web && npm ci
```

Copy `.env.example` to `.env` and fill in what you need. Nothing is required for
local work: without provider credentials the system runs in `fallback_only` mode
and generates reports deterministically. Do the same with `.mcp.json.example` if
you want MCP servers. **Never commit either real file** — both are git-ignored,
and the packaging tool aborts the build if it ever finds one tracked. That check
recognizes known shapes (`.env*`, `.mcp.json*`, key material, runtime databases);
it is a backstop, not a guarantee that no secret can be committed in some other
form. Read your own diff.

## Running the checks

```bash
uv run pytest
```

```bash
cd web && npm run test && npm run build
```

Lint and types are a separate contract with exact commands -- see
[Development toolchain](#development-toolchain). Run them from the frozen project
environment, never from a globally installed tool.

**Point the tests at a disposable runtime root.** The suite reads and writes
beneath the directory named by `XRML_DATA_DIR`; if you leave it unset it falls
back to the repository-relative `storage/`, and a test run will churn your local
research data. Set it to a throwaway directory first:

```bash
export XRML_DATA_DIR=$(mktemp -d)
```

A few tests assert the *default* storage layout and therefore expect
`XRML_DATA_DIR` to be unset. If one of those fails, re-run it on its own with
`env -u XRML_DATA_DIR` before assuming you broke something.

CI runs the general backend suite on Ubuntu 24.04. It stops at the first failure
so a long failing run retains a concrete traceback; a passing run executes the
whole suite. Windows PowerShell operator scripts are exercised only on Windows,
alongside native-host, credential, child-process and file-lock contracts in an
independent `windows-contracts` job. That job uses uv-managed Python 3.12.12 because
the setup-python release manifest has no Windows build for that patch. These
automated contracts do not replace installed-app or real-browser acceptance.

## Working on a change

- Branch from the default branch. Keep a change focused on one concern.
- Match the surrounding code. The Python is typed and uses `from __future__ import
  annotations`; the web client is TypeScript and React.
- Add a test that fails before your fix and passes after. If a test passes with
  the fix reverted, it is not testing the fix.
- Address files through the portable `run:` / `storage:` / `project:` reference
  helpers in `app/repositories/artifact_refs.py`, never through native paths.
  Portable references are what keep runs relocatable across machines.
- Do not commit runtime output. Reports, run artifacts, logs, traces, screenshots,
  bundles and archives are all products of a run, not source.

## Commit and pull request

Write commit subjects in the imperative mood with a conventional prefix
(`fix:`, `feat:`, `docs:`, `chore:`, `test:`). Describe what changed and why in
the body; the diff already shows how.

In the pull request, say what you changed, how you verified it, and what you did
not cover. Please confirm that you ran the backend and frontend checks above, and
that no secret, runtime artifact, or licensed corpus content is in the diff.

## Reporting bugs and requesting features

Open an issue with the version or commit, your OS and Python/Node versions,
what you expected, what happened, and the smallest reproduction you have.
Redact credentials and private corpus content before pasting logs.

For security issues, do not open an issue — follow [`SECURITY.md`](SECURITY.md).

## Project build environment

The designated interpreter is **Python 3.12.12** and the designated resolver is **uv 0.9.26**.
The normal full project environment is:

```bash
uv sync --frozen --all-extras --python 3.12.12
```

That command **installs the project itself**. An environment that only has the dependencies is
not this contract: `--no-install-project` is not an accepted project environment, and results
produced under it are not accepted evidence.

### Two separate authorities

Ordinary runtime and dev packages are governed by `uv.lock`. The *isolated build* environment
is not — uv resolves it fresh for every build, so it has its own committed authority:

- exact `[build-system].requires`
- exact `[tool.uv].build-constraint-dependencies`

Build constraints only narrow packages the build dependency graph **already requests**. They do
not add ordinary dependencies, and none of them becomes a runtime or dev requirement.

This is why **Hatchling can legitimately be absent from `uv.lock`'s ordinary package records**.
Its absence there is not a malformed lockfile; it means the build environment is governed by
the manifest instead. The clearest symptom is `packaging`: the build environment pins
**`packaging==26.3`** while `uv.lock` pins **`packaging==26.0`** for the project environment.
Those are two intentionally separate authorities populating two isolated environments. Do not
collapse them into one.

### Prohibited for this contract

- `--no-build-isolation` (and `--no-build-isolation-package`)
- a globally installed Hatchling
- `pip` and `uv pip`

### Changing the build backend or a constraint

Any change to `[build-system].requires` or to `build-constraint-dependencies` requires a
later independent proof: two fresh-clone empty-cache online syncs plus one offline sync from a
populated cache, re-measuring the complete closure. Editing the pins without that re-proof is
not accepted.

This section is **local provisioning authority only**. It is not release, staging or production
evidence, and it does not claim cross-platform or cross-Python reproducibility.

## Development toolchain

Python 3.12.12 is the designated validation interpreter.

Install the locked development tools:

```bash
uv sync --frozen --extra dev
```

Check that the lockfile still matches the declarations:

```bash
uv lock --check
```

Run the lint gate:

```bash
uv run --frozen --extra dev ruff check --no-cache app tests tools scripts
```

`uv.lock` pins **Ruff 0.16.2** and **mypy 1.20.2**. Both must resolve from the frozen project
environment, not from your global PATH -- a globally installed Ruff is a different version and
will report a different result, so its output is not accepted evidence.

The gate is a **bounded core contract**, not `ruff --select ALL`. `[tool.ruff.lint].select`
enumerates the exact rules Ruff 0.14.14 enabled by default; Ruff 0.16 widened its implicit
defaults from 6 findings to 894 over the same scope. Those 894 are real but unreviewed, and
adopting them silently would have turned a toolchain change into a mass refactor. Ruff core
findings must be zero. Broad `ignore`, `per-file-ignores` and `# noqa` are not accepted.

### The mypy debt baseline

Mypy is a gate, but not a cleanliness gate. The tree is classified
`mypy_debt_baselined`: **559 errors, 20 notes, 579 diagnostics across 69 files** are recorded
in `tools/mypy_baseline.json`. That is the agreed debt, not an achievement -- this repository
is **not mypy-clean**, and no change should claim otherwise.

Check the debt against the baseline:

```bash
uv run --frozen --extra dev python tools/check_mypy_baseline.py --check
```

The check is fail-closed in both directions. A new diagnostic fails it, and so does a
*removed* one: fixing a historical error is welcome but still fails until you update the
baseline in the same change. Fuzzy matching is what lets a genuinely new error hide behind a
shifted old one, so there is none. A diagnostic's identity is its path, line, column,
severity, code, message and duplicate index, together with the mypy version, execution and
target Python, the exact ordered scope and the mypy configuration digest. Any of those moving
fails the check.

The verifier itself is held to a stricter standard than the code it measures -- it is outside
the baselined scope and must stay clean:

```bash
uv run --frozen --extra dev python -m mypy --config-file pyproject.toml \
  --explicit-package-bases --no-incremental --show-error-codes --show-column-numbers \
  --no-pretty --no-color-output --no-error-summary \
  --cache-dir "$(mktemp -d)" tools/check_mypy_baseline.py
```

### Updating the baseline

Only in the same change that explains the movement, and never as a reflex after a failure:

```bash
uv run --frozen --extra dev python tools/check_mypy_baseline.py --write-baseline
```

1. Understand *why* the diagnostic set changed. If you cannot explain it, stop.
2. Run the write command above.
3. Read the JSON diff. It is the record of what you accepted.
4. Confirm no `import-not-found`, `import-untyped`, fatal or unparsed diagnostic entered the
   baseline -- an unresolved import makes every downstream diagnostic suspect.
5. Re-run the check, then the verifier self-typing above, then the Ruff gate.
6. Commit the source change and the baseline change together.

CI never runs `--write-baseline`. A gate that rewrites its own expectations is not a gate.

### Cache safety

The verifier routes its own mypy cache to a unique external directory and removes it, so a
`--check` never writes `.mypy_cache` into your tree. Direct mypy invocations do not get that
for free: pass an explicit external `--cache-dir`, or export `MYPY_CACHE_DIR` to a path
outside the repository. Never commit a tool cache.

### What these gates do not claim

Ruff core clean is not Ruff `ALL` clean. `mypy_debt_baselined` is not mypy-clean. Neither is
production readiness, staging readiness, or release approval.

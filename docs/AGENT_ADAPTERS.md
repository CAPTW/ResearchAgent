# Agent Adapters

이 문서는 local research workflow를 Codex, Gemini CLI, Claude Code에서 공통으로 쓰는 방법을 정리합니다.

## Canonical core vs adapter files

Canonical core:
- `AGENTS.md`
- `docs/LOCAL_RESEARCH_WORKFLOW.md`
- `.agents/skills/local-research-run/SKILL.md`
- `tools/local_research_workflow.py`
- `tools/local_research_mcp_server.py`
- `scripts/run_local_research.ps1`
- `scripts/run_local_research.sh`

Adapter files:
- `GEMINI.md`
- `CLAUDE.md`
- `.claude/skills/local-research-run/SKILL.md`
- `.claude/rules/local-only.md`
- `.claude/rules/reporting.md`
- `.gemini/settings.json`
- `.gemini/README.md`
- `.claude/settings.json`
- `.claude/README.md`

원칙:
- source of truth는 canonical core에 둡니다.
- generated adapter는 `tools/sync_agent_adapters.py`로 맞춥니다.
- always prefer runner/wrapper over ad-hoc shell sequences.
- Gemini/Claude에서는 MCP-first가 canonical입니다.
- docs loaded, tool available, workflow actually executed를 구분합니다.
- success는 real `run_id`가 있을 때만 성립합니다.

## Sync script

PowerShell:

```powershell
pwsh scripts/sync_agent_adapters.ps1 --write
pwsh scripts/sync_agent_adapters.ps1 --check
pwsh scripts/sync_agent_adapters.ps1 --diff-summary
```

bash:

```bash
bash scripts/sync_agent_adapters.sh --write
bash scripts/sync_agent_adapters.sh --check
bash scripts/sync_agent_adapters.sh --diff-summary
```

direct Python:

```powershell
uv run python tools/sync_agent_adapters.py --write
uv run python tools/sync_agent_adapters.py --check
uv run python tools/sync_agent_adapters.py --diff-summary
python tools/sync_agent_adapters.py --check
```

## Codex

Interactive:

```text
$local-research-run topic="XR topic" profile-name="XR profile"
```

Non-interactive:

```powershell
codex exec "$local-research-run topic='XR topic' profile-name='XR profile'"
```

또는 deterministic runner:

```powershell
uv run python tools/local_research_workflow.py --topic "XR topic" --profile-name "XR profile" --strict
```

## Gemini CLI

1. repo root에서 Gemini CLI를 시작합니다.
2. `GEMINI.md`와 `docs/LOCAL_RESEARCH_WORKFLOW.md`를 먼저 읽습니다.
3. `run_local_research` MCP tool이 있으면 그것을 먼저 사용합니다.
4. MCP tool이 없거나 policy denied일 때만 deterministic runner/wrapper fallback을 사용합니다.

권장 명령:

```powershell
uv run python tools/local_research_workflow.py --topic "XR topic" --profile-name "XR profile" --strict
```

Windows self-healing wrapper:

```powershell
pwsh -File scripts/run_gemini_local_research.ps1 -Topic "XR topic" -ProfileName "XR profile"
```

이 wrapper는 Gemini CLI + local MCP를 우선 사용하고, workspace trust 때문에 project config가 무시되면 user-scope MCP registration으로 self-heal을 시도합니다. 그래도 Gemini path가 실행되지 않으면 `fallback_used=true`를 명시한 runner fallback만 허용합니다.

optional local MCP:

```powershell
gemini mcp add xrml-local-research uv run python tools/local_research_mcp_server.py
gemini mcp list
```

Gemini failure mode that this adapter now guards against:
- docs-only read succeeded
- actual execution was blocked by missing `run_shell_command` or policy denied tools
- therefore `run_id`가 없으면 반드시 `not_executed`로 취급해야 합니다.

비공식 wrapper/extension 경로:
- `gemini skills link .agents/skills`
- `gemini skills list`
- `/skills`

이 경로는 vanilla Gemini CLI의 documented project-local skill path가 아니므로 example로만 취급합니다.

## Claude Code

1. repo root에서 Claude Code를 시작합니다.
2. `CLAUDE.md`가 자동 로드됩니다.
3. `/local-research-run` 또는 자연어 요청을 사용하되 `run_local_research` MCP tool을 먼저 사용합니다.
4. MCP tool이 없거나 denied일 때만 deterministic runner fallback을 사용합니다.

권장 명령:

```powershell
uv run python tools/local_research_workflow.py --topic "XR topic" --profile-name "XR profile" --strict
```

optional local MCP:
- `.mcp.json.example`를 참고해 `.mcp.json`로 복사한 뒤 `/mcp`로 확인합니다.
- 기본 권장 경로는 local stdio MCP입니다.
- `.mcp.json.example`의 서버 이름은 `xrml-local-research`입니다.

## Windows / WSL / macOS / Linux examples

Windows PowerShell:

```powershell
pwsh scripts/run_local_research.ps1 --topic "XR topic" --profile-name "XR profile"
uv run python tools/local_research_workflow.py --topic "XR topic" --profile-name "XR profile" --strict
```

WSL:

```bash
bash scripts/run_local_research.sh --topic "XR topic" --profile-name "XR profile"
```

macOS/Linux:

```bash
bash scripts/run_local_research.sh --topic "XR topic" --profile-name "XR profile" --storage-root /tmp/xrml-local
```

## Fallback-only note
- `OPENAI_API_KEY`가 없으면 `provider_mode=fallback_only`는 정상입니다.
- 이 경우에도 local workflow는 계속 동작해야 합니다.

## Dashboard UI release baseline

- Frontend regression baseline for dashboard design work:
  - `docs/UI_UX_STITCH_HANDOFF_RELEASE.md` (internal record; retained in the repository, not part of the public source snapshot)
  - Use this document for visual and behavioral acceptance criteria before updating dashboard UI components.

## Tooling truthfulness

- `docs loaded`: 문서만 읽음
- `tool available`: MCP tool 또는 runner command를 실제 호출할 수 있음
- `workflow actually executed`: real `run_id`가 생기고 artifact가 확인됨
- `run_id`가 없으면 `success`가 아님
- 실행 capability가 없으면 exact wrapper command만 반환하고 `status=not_executed`로 남겨야 함

## Manual fallback

PowerShell:

```powershell
pwsh scripts/run_local_research.ps1 --topic "Gaussian avatars in XR" --profile-name "Gaussian XR"
```

bash / WSL / macOS / Linux:

```bash
bash scripts/run_local_research.sh --topic "Gaussian avatars in XR" --profile-name "Gaussian XR"
```

direct runner:

```powershell
uv run python tools/local_research_workflow.py --topic "Gaussian avatars in XR" --profile-name "Gaussian XR" --strict
```

## Optional shell allowlist examples

managed policy가 없는 환경에서만 secondary path로 설명합니다.

- `run_shell_command(pwsh)`
- `run_shell_command(uv)`
- `run_shell_command(python)`
- `run_shell_command(npm)`
- `run_shell_command(node)`

## Minimal operating prompt for another AI

```text
Read docs/LOCAL_RESEARCH_WORKFLOW.md first.
Stay local-only.
Do not touch Render/public OAuth/staging.
Prefer uv run python tools/local_research_workflow.py over ad-hoc shell sequences.
Run the local workflow for <topic> with profile <profile>.
Report only: run_id, provider_mode, artifact_paths, local_ui_urls, failures.
```

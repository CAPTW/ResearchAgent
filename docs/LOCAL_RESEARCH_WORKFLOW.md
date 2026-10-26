# Local Research Workflow

이 문서는 이 repo의 반복적인 로컬 연구 작업을 긴 자연어 프롬프트 대신 고정된 workflow로 실행하는 방법을 정리합니다.

## 목적
- backend/frontend를 확인하거나 필요 시 시작
- topic profile 생성 또는 업데이트
- research run 시작
- run 완료 후 artifact와 UI 진입점 확인

## 기본 전제
- local-only workflow입니다.
- public deployment, Render, OAuth, ChatGPT connector 복구는 범위 밖입니다.
- `OPENAI_API_KEY`가 없으면 provider mode가 `fallback_only`로 보이는 것이 정상입니다.
- Stitch 상태는 계속 `Seam prepared`입니다.
- `run_id`가 없으면 workflow는 success가 아닙니다.
- docs loaded, tool available, workflow actually executed는 서로 다른 상태입니다.

## Codex에서 어떻게 부르는가

Interactive:

```text
$local-research-run topic="Egocentric XR perception" profile-name="Egocentric XR monitor"
```

Non-interactive:

```powershell
codex exec "$local-research-run topic='Egocentric XR perception' profile-name='Egocentric XR monitor'"
```

또는:

```powershell
codex exec "Run the local research workflow for Egocentric XR perception"
```

## 다른 AI나 사람이 스크립트로 실행하는 방법

가능하면 local MCP tool `run_local_research`를 먼저 사용합니다. MCP tool이 없거나 policy로 막힌 경우에만 아래 runner/wrapper command를 manual fallback으로 사용합니다.

PowerShell:

```powershell
pwsh scripts/run_local_research.ps1 --topic "Egocentric XR perception" --profile-name "Egocentric XR monitor"
```

bash / WSL / macOS / Linux:

```bash
bash scripts/run_local_research.sh --topic "Egocentric XR perception" --profile-name "Egocentric XR monitor"
```

직접 Python runner:

```powershell
uv run python tools/local_research_workflow.py --topic "Egocentric XR perception" --profile-name "Egocentric XR monitor"
```

dry-run:

```powershell
uv run python tools/local_research_workflow.py --topic "Egocentric XR perception" --profile-name "Egocentric XR monitor" --dry-run --strict
```

optional ports:

```powershell
uv run python tools/local_research_workflow.py --topic "Egocentric XR perception" --profile-name "Egocentric XR monitor" --backend-port 8001 --frontend-port 5174
```

json output:

```powershell
uv run python tools/local_research_workflow.py --topic "Egocentric XR perception" --profile-name "Egocentric XR monitor" --json-out tmp/workflow-result.json --strict
```

local MCP server:

```powershell
uv run python tools/local_research_mcp_server.py
```

## 실행 중 확인하는 엔드포인트
- `/health`
- `/app`
- `/dashboard/overview`
- `/topic-profiles`
- `/settings/status`

## 결과로 확인하는 파일
- `storage/runs/<run_id>/run.json`
- `storage/runs/<run_id>/reports/research_plan.json`
- `storage/runs/<run_id>/reports/research_plan.md`
- `storage/runs/<run_id>/reports/preview.md`
- `storage/runs/<run_id>/reports/exports.json`
- `storage/runs/<run_id>/reports/exported_*.md`

## 출력 형식
workflow runner는 항상 다음을 출력합니다.
- `status`
- `run_id`
- `provider_mode`
- `artifact_paths`
- `local_ui_urls`
- `failures`
- `executed_via`

## Canonical Adapter Contract

### Inputs
- `--topic`
- `--profile-name`
- `--storage-root`
- `--dry-run`
- `--json-out`
- `--strict`
- `--fail-if-not-executed`
- `--backend-host` (optional)
- `--backend-port` (optional)
- `--frontend-port` (optional)

### Outputs
- `status`
- `run_id`
- `provider_mode`
- `artifact_paths`
- `local_ui_urls`
- `failures`
- `executed_via`

### Fixed policy phrases
- `local-only`
- `do not touch Render/public OAuth/staging`
- `fallback_only is acceptable without OPENAI_API_KEY`
- `report only: status, run_id, provider_mode, artifact_paths, local_ui_urls, failures, executed_via`

## Execution truthfulness

- `docs loaded`: agent가 `GEMINI.md`, `CLAUDE.md`, `docs/LOCAL_RESEARCH_WORKFLOW.md`를 읽은 상태
- `tool available`: `run_local_research` MCP tool 또는 runner/wrapper shell path가 실제 호출 가능한 상태
- `workflow actually executed`: real `run_id`가 생성되고 artifact path가 확인된 상태
- `run_id`가 없으면 `success`가 아닙니다.
- execution capability가 없으면 status는 `not_executed`여야 하며, exact wrapper command만 반환해야 합니다.

## Why MCP-first is canonical

- 일부 agent 환경은 문서를 읽을 수 있지만 shell tool이 없거나 policy로 막혀 있습니다.
- local stdio MCP tool은 shell 존재를 가정하지 않고 단일-purpose execution path를 제공합니다.
- shell은 secondary fallback이며, 없거나 denied면 실행된 것처럼 말하지 않습니다.

## Gemini failure mode that prompted this change

- `GEMINI.md`와 workflow 문서는 읽을 수 있었다.
- 하지만 `run_shell_command` tool이 없었고, 일부 tool은 policy denied였다.
- 그 결과 실제 run은 시작되지 않았는데도 process exit code만 보면 성공처럼 보일 수 있었다.
- 이제 canonical path는 MCP-first이며, no-run case는 `not_executed`로 남겨야 한다.

## Manual fallback

PowerShell:

```powershell
pwsh scripts/run_local_research.ps1 --topic "Gaussian avatars in XR" --profile-name "Gaussian XR"
```

WSL/macOS/Linux:

```bash
bash scripts/run_local_research.sh --topic "Gaussian avatars in XR" --profile-name "Gaussian XR"
```

Direct runner:

```powershell
uv run python tools/local_research_workflow.py --topic "Gaussian avatars in XR" --profile-name "Gaussian XR" --strict
```

## 플랫폼별 예시

Windows PowerShell:

```powershell
pwsh scripts/run_local_research.ps1 --topic "Gaussian avatars in XR" --profile-name "Gaussian XR"
```

WSL:

```bash
bash scripts/run_local_research.sh --topic "Gaussian avatars in XR" --profile-name "Gaussian XR"
```

macOS/Linux:

```bash
bash scripts/run_local_research.sh --topic "Gaussian avatars in XR" --profile-name "Gaussian XR" --storage-root /tmp/xrml-local
```

## 주의 사항
- backend가 이미 다른 auth mode로 떠 있고 `/topic-profiles`가 인증을 요구하면, local demo backend로 다시 띄워야 합니다.
- `OPENAI_API_KEY`가 없어도 run은 계속 진행되지만 provider-backed synthesis는 비활성화되고 fallback path가 사용됩니다.
- `/app`은 backend가 시작될 때 `web/dist`가 있어야 정상 제공됩니다. dist가 없으면 workflow runner가 먼저 build를 시도합니다.

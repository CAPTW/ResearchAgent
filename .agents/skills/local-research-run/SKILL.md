---
name: local-research-run
description: 로컬에서 연구 주제를 받아 서버를 확인하고, topic profile을 만들거나 업데이트하고, research run을 시작하고, artifact와 UI 진입점을 확인하는 workflow
---

# Local Research Run

## Use this skill when
- 사용자가 로컬에서 topic/profile 기반 연구 run을 반복 실행하고 싶을 때
- 긴 자연어 지시 대신 repo 안의 고정된 workflow를 재사용해야 할 때
- `/app`과 `storage/runs/<run_id>` 산출물을 함께 확인해야 할 때

## Inputs
- `topic`: 필수
- `profile-name`: 선택. 없으면 topic 기반 기본 profile 이름을 사용
- `storage_root`: 선택. temp 또는 별도 로컬 데이터 루트가 필요할 때만 사용
- `dry_run`: 선택. mutation 없이 계획과 현재 상태만 확인할 때 사용
- `backend-port` / `frontend-port`: 선택. 기본 포트를 피해 local override가 필요할 때 사용

## Workflow
1. `AGENTS.md`의 local research operator 규칙을 먼저 따른다.
2. `run_local_research` MCP tool이 있으면 먼저 사용한다.
3. MCP tool이 없거나 policy denied면 exact runner/wrapper command만 제시하거나 직접 실행한다.
4. 필요하면 `tools/local_research_workflow.py`로 backend/frontend를 시작한다.
5. `/health`, `/app`, `/dashboard/overview`, `/topic-profiles`, `/settings/status`를 확인한다.
6. topic profile을 생성하거나 업데이트한다.
7. profile 기반 research run을 1회 시작한다.
8. run 상태를 polling한다.
9. `research_plan.json`, `research_plan.md`, preview/export 산출물을 확인한다.
10. 최종적으로 `status`, `run_id`, `provider mode`, `artifact paths`, `local UI URLs`, `failures`, `executed_via`를 출력한다.

## Commands
- PowerShell:
  - `pwsh scripts/run_local_research.ps1 --topic "XR topic" --profile-name "XR topic profile"`
- bash:
  - `bash scripts/run_local_research.sh --topic "XR topic" --profile-name "XR topic profile"`
- direct Python:
  - `uv run python tools/local_research_workflow.py --topic "XR topic" --profile-name "XR topic profile"`
- local MCP server:
  - `uv run python tools/local_research_mcp_server.py`

## Codex invocation
- Interactive:
  - `$local-research-run topic="XR topic" profile-name="XR topic profile"`
- Non-interactive:
  - `codex exec "$local-research-run topic='XR topic' profile-name='XR topic profile'"`
  - `codex exec "Run the local research workflow for XR topic"`

## Output contract
- 반드시 아래를 포함한다:
  - `status`
  - `run_id`
  - `provider_mode`
  - `artifact_paths`
  - `local_ui_urls`
  - `failures`
  - `executed_via`
- `OPENAI_API_KEY`가 없으면 `fallback_only`는 정상 상태다.
- Render/public OAuth/staging은 이 skill 범위 밖이다.
- `run_id`가 없으면 success라고 쓰지 않는다.

## Out of scope
- Render/public deployment
- ChatGPT connector/OAuth
- broad storage rewrite
- Stitch integration claim

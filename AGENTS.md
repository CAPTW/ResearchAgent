# AGENTS.md — XR/AR × ML (Image/Video) Research Agent Rules

이 프로젝트는 **AR/XR(AR/VR/MR)과 ML 기반 이미지/비디오 학습** 융합 주제를 대상으로 합니다.
Codex는 아래 규칙을 우선 준수합니다.

## Local Research Operator
- 이 repo의 기본 운영 모드는 **local-only research workflow**입니다.
- 반복되는 로컬 연구 작업은 긴 자연어 프롬프트 대신 아래 workflow 자산을 우선 사용합니다.
  - skill: `.agents/skills/local-research-run/SKILL.md`
  - Python runner: `tools/local_research_workflow.py`
  - wrappers: `scripts/run_local_research.ps1`, `scripts/run_local_research.sh`
  - optional local MCP wrapper: `tools/local_research_mcp_server.py`
- shell access를 기본 가정으로 두지 않습니다. `run_local_research` MCP tool이 있으면 이를 먼저 사용하고, 없거나 policy denied면 exact wrapper command만 반환합니다.
- 기본 로컬 실행은 `AUTH_MODE=disabled`를 전제로 하며, `OPENAI_API_KEY`가 없어도 `fallback_only`로 계속 동작해야 합니다.
- 현재 작업이 로컬 연구 workflow 범위라면 public deployment, Render, ChatGPT connector, Codex remote OAuth 작업으로 확장하지 않습니다.
- 결과 보고는 아래 필드를 우선 기준으로 삼습니다:
  - `status`
  - `run_id`
  - `provider_mode`
  - `artifact_paths`
  - `local_ui_urls`
  - `failures`
  - `executed_via`
- `run_id`가 없으면 success로 보고하지 않습니다.

## 0) 목표
- 신규 논문/기술을 **수집 → 선별 → 구조화 요약 → 주간 합성(트렌드/갭/아이디어)**
- 재현/프로토타입이 가능하면 **실험 스캐폴딩**을 생성
- 결과물은 마크다운 중심으로 저장(추후 Notion/Obsidian/GitHub Wiki로 이동 가능)

## 1) 작업 원칙 (중요)
- 근거가 없는 내용을 “사실”처럼 쓰지 않는다.
- 논문을 요약할 때는, 최소한 다음 중 하나를 근거로 삼는다.
  - (a) 제공된 초록/메타데이터
  - (b) 사용자가 제공한 PDF/텍스트 발췌
  - (c) 프로젝트 내 저장된 파일(노트/데이터)
- 전문(PDF) 미확보 상태에서는 “초록 기반 1차 요약”임을 명시한다.

## 2) 산출물 표준
### 2.1 논문 1편 요약 (`notes/papers/<paper_id>.md`)
필수 섹션:
- TL;DR (3~5줄)
- Problem / Motivation
- Key Contributions (3~7개 bullet)
- Method (핵심 파이프라인, 입력/출력, 학습/추론, 실시간성 여부)
- AR/XR Integration (센서/디바이스, 6DoF, 캘리브레이션, 렌더링, 상호작용)
- Data & Evaluation (데이터셋/지표/베이스라인/실험 세팅)
- Limitations / Risks (실시간, 안전, 프라이버시, 윤리, 편향, 일반화)
- Reproducibility (코드/가중치/라이선스/필수 의존성)
- Tags (키워드 5~12개)
- Citations (arXiv/DOI/프로젝트 링크)

### 2.2 주간 합성 (`notes/weekly/YYYY-WW.md`)
- 이번 주 Top N 논문 요약(짧게)
- 트렌드 3~6개
- 연구 갭/오픈 퀘스천 3~8개
- 다음 주 실험 제안 1~3개(가능하면 간단한 프로토타입)

## 3) 파일/폴더 규칙
- 원시 수집 결과: `data/papers/raw/*.jsonl`
- 큐/우선순위: `data/queues/*.md` 및 `data/queues/*.jsonl`
- 노트: `notes/papers`, `notes/weekly`
- 실험: `experiments/<YYYYMMDD>_<slug>/`

## 4) 실행 규칙
- 스크립트 실행 시, 먼저 `python --version` 확인(가능하면 3.10+)
- 가능한 한 표준 라이브러리로 처리(외부 의존성 최소화)
- 긴 작업(다량 수집/요약)은 중간 산출물부터 커밋 가능한 형태로 저장

### 4.1 로컬 실행 명령
- backend only:
  - `uv run python tools/local_demo.py backend`
- frontend only:
  - `uv run python tools/local_demo.py frontend`
- prod-like `/app` shell:
  - `uv run python tools/local_demo.py prod`
- one-command local research workflow:
  - PowerShell: `pwsh scripts/run_local_research.ps1 --topic "..." --profile-name "..."`
  - bash: `bash scripts/run_local_research.sh --topic "..." --profile-name "..."`
- optional ports:
  - `uv run python tools/local_research_workflow.py --topic "..." --profile-name "..." --backend-port 8001 --frontend-port 5174`

### 4.2 smoke endpoints
- `GET /health`
- `GET /app`
- `GET /dashboard/overview`
- `GET /topic-profiles`
- `GET /settings/status`

### 4.3 research run 생성 절차
1. backend와 frontend 상태를 확인한다.
2. backend가 없으면 local demo mode로 시작한다.
3. `topic profile`을 생성하거나 기존 profile을 업데이트한다.
4. `POST /dashboard/start-runs`로 profile 기반 run을 시작한다.
5. `GET /runs/{run_id}`를 polling해서 terminal status를 확인한다.
6. `GET /runs/{run_id}/preview`와 `POST /runs/{run_id}/export`로 preview/export 산출물을 고정한다.

### 4.4 artifact 확인 절차
최소 확인 대상:
- `storage/runs/<run_id>/run.json`
- `storage/runs/<run_id>/reports/research_plan.json`
- `storage/runs/<run_id>/reports/research_plan.md`
- `storage/runs/<run_id>/reports/preview.md`
- `storage/runs/<run_id>/reports/exports.json`
- `storage/runs/<run_id>/reports/exported_*.md`

### 4.5 done when
- run이 `completed` 상태다.
- provider mode가 `live_ready` 또는 `fallback_only`로 명시된다.
- status, run id, artifact paths, local UI URL, failures, executed_via가 출력된다.
- `/app`와 `/settings/status`가 열리고, artifact 존재 확인이 끝난다.

## 5) 품질 체크(요약/합성)
- 제목/ID/날짜가 맞는지 확인
- 약어는 처음에 한 번 풀어서 설명
- AR/XR과 무관한 CV 논문은 “왜 관련 있는지” 근거를 명확히 쓰거나 제외

## 6) 안전/윤리 (특히 XR)
- 사용자 프라이버시(시선추적/생체신호/실내 지도), 감시/식별 위험을 명시
- 의료/안전 크리티컬 영역은 “연구용 참고”로 제한하고 전문 검증이 필요함을 명시

## 금지 사항
- public OAuth/Render/staging/ChatGPT connector 복구 작업을 로컬 workflow 범위에 섞지 않는다.
- `storage/runs` 포맷을 깨는 broad rewrite를 하지 않는다.
- `OPENAI_API_KEY`가 없는데 provider success를 가장하지 않는다.
- Stitch는 끝까지 `Seam prepared`로만 취급한다.

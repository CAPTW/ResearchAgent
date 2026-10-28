# XRML Research Agent

XR/AR × ML 문헌을 수집하고, 실제로 확보한 근거를 검토하여 분석·Journal로
연결하는 개발 중인 로컬 연구 앱입니다. React/Vite, FastAPI, SQLite와 파일
저장소를 Tauri Windows 호스트에 연결합니다. Docker는 필수가 아닙니다.

## Current development checkpoint — 2026-10-28

- **Papers / 수집:** Paper Bank의 검색·버전·컬렉션과 snapshot/diff를 유지합니다.
  새 **브라우저에서 논문 수집**은 공식 arXiv 검색, 명시적 후보 선택, Whale
  확장/Native Messaging, 검증한 PDF의 확인·저장 및 중단 재개를 연결하는
  개발 후보입니다. [설정·사용·복구 방법](docs/BROWSER_COLLECTION.md)을 확인하세요.
  시험용 Whale에 설치했지만 실제 연결은 `native messaging host not found`로
  실패했습니다. 실제 다운로드와 인증이 필요한 출판사 사이트는 미검증입니다.
- **PDF / 분석:** 사용자 선택 PDF 복사본 연결, 페이지별 텍스트 추출과 누락
  표시, 모델 예산에 맞춘 전문·절별 입력 계획, 실행 이력과 저장 결과가 있습니다.
  OCR·표·그림 의미 이해 또는 전체 논문의 과학적 검증을 보장하지 않습니다.
- **근거 / Journal:** 원문 위치와 근거를 확인하고 별도 Journal 후보를 검토·채택해
  Markdown/JSON으로 내보냅니다. Journal 채택은 원래 분석의 검토 상태를 바꾸지
  않습니다. 유효한 인용 ID만으로 주장의 과학적 지지를 증명하지 않습니다.
  저장 분석·검토 이력의 논문 브리프는 보존된 해석과 원문 인용을 구분해
  방법·근거·한계·관련성·후속 질문을 보여줍니다. 없는 항목은 만들어 채우지 않습니다.
- **AI 연결:** 공식 CLI/adapter와 Windows DPAPI 키 보관을 사용합니다. 연결,
  모델 선택, 전송할 입력, 한 번의 실행 권한은 서로 다릅니다. 모델 변경은 과거
  결과를 다시 쓰지 않습니다. [AI 설정과 검토 흐름](docs/MODEL_CONNECTIONS.md).
- **기동:** 설치 첫 실행의 **35초 초과 실패는 미해결**입니다. 이후 **6.641초**
  성공은 그 실패를 해결한 증거가 아닙니다. 선택형 진단은 loader/import/app
  초기화 구간을 구분하며, 재부팅은 개발의 선행 조건이 아닙니다.

현재 기능은 별도 이력으로 소스가 공개된 개발 상태입니다. 안정판 출시, 모든 Provider,
모든 브라우저, 전체 논문 품질, OAuth 또는 public deployment 완료를 뜻하지
않습니다. 과거 Haiku 발췌 1회와 같은 논문의 Sonnet 추출 전문 1회는 좁은 실행
증거이며 새 유료 호출을 허가하지 않습니다. 신규 결과와 검사 범위는
[공개 준비 현황](docs/OPEN_SOURCE_READINESS.md)에 구분해 기록합니다.

## Start locally

Python 3.11 이상, Node.js 20 이상 및 npm 10 이상이 필요합니다. 저장소 루트에서:

```powershell
uv sync --frozen --extra dev --extra fulltext
cd web
npm ci
npm run build
cd ..
$env:XRML_DATA_DIR = Join-Path $env:LOCALAPPDATA 'XRML/source-workspace'
$env:AUTH_MODE = 'disabled'
$env:TOPIC_SCHEDULER_ENABLED = '0'
$env:TOPIC_SCHEDULER_MODE = 'disabled'
$env:XRML_RUN_EXECUTOR_MODE = 'disabled'
uv run python tools/local_demo.py prod
```

표시되는 로컬 `/app` 주소를 엽니다. 첫 시작은 새 작업 폴더를 권장합니다.
실제 연구 DB 복구, 예약 활성화, 계정 연결과 AI 실행은 각각 의도적으로 선택하세요.
환경 설정은 [.env.example](.env.example), 개발과 검사 명령은
[CONTRIBUTING](CONTRIBUTING.md), Windows 빌드는
[패키징 안내](docs/PACKAGING_AND_INSTALLATION.md)를 참고하세요.

일반 확인 명령은 `uv run pytest`, `uv run ruff check app tools scripts`,
`npm --prefix web test`, `npm --prefix web run build`입니다. 기존 mypy 오류
기준선이 있으므로 기준선 비교 통과를 clean typecheck라고 해석하지 않습니다.
전체 시험, focused fixture, browser, installed-native, 실제 Provider 근거는
서로 구분합니다.

## Publication boundary

기존 Apache-2.0 라이선스와 기여자·제3자 고지를 유지합니다. 논문 PDF·전문,
사용자 DB·응답·입력, 인증 정보, 브라우저 프로필, 내부 개발 기록·raw 로그와
미검토 바이너리는 공개 소스가 아닙니다. 현재 파일뿐 아니라 공개할 이력과
GitHub 자료를 검토해야 합니다. 개발 저장소에서 파일을 제외해도 과거 이력은
지워지지 않습니다. 새 공개 전환이나 원격 이력 변경은 별도 결정입니다.

## 현재 구조

- `app/`: API, 서비스, 파일 저장소, job/event helpers
- `web/`: Home/New Run, Run Detail research workbench
- `tools/`: 레거시 CLI 진입점. 내부적으로 `app/services`를 호출
- `storage/runs/`: 새 run의 authoritative artifact 위치
- `data/`, `notes/`, `reports/`: 레거시 호환용 mirror 출력

핵심 원칙:

- 새 run의 authoritative source of truth는 `storage/runs/<run_id>/...`
- `data/`, `notes/`, `reports/`는 호환성 때문에 계속 쓰지만 `last-write-wins` 뷰입니다.
- legacy mirror 쓰기는 best-effort이며, 충돌이 나도 run-scoped artifact는 계속 authoritative source로 유지됩니다.

Run Detail workbench 레이아웃:

- Left: run metadata, progress, filters, pipeline state
- Center: ranked paper table with pagination/filtering
- Right: paper detail/evidence pane 또는 report outline/preview/export pane

## 설치

운영/배포 기준 문서는 [docs/OPERATIONS.md](docs/OPERATIONS.md), [docs/DEPLOYMENT_RUNBOOK.md](docs/DEPLOYMENT_RUNBOOK.md), [docs/PACKAGING_AND_INSTALLATION.md](docs/PACKAGING_AND_INSTALLATION.md), [docs/ENVIRONMENT_REFERENCE.md](docs/ENVIRONMENT_REFERENCE.md), [docs/PRODUCTION_ENVIRONMENT.md](docs/PRODUCTION_ENVIRONMENT.md), [docs/PRODUCTION_ACTIVATION.md](docs/PRODUCTION_ACTIVATION.md)에 정리되어 있습니다.
설정·스크립트에는 local development, single-node production, shared-SQLite worker/process 프로필이 있습니다. 프로필 존재는 현재 운영·배포 검증 완료를 뜻하지 않으며, 적용 가능한 검증 범위는 [현재 상태 기록](docs/OPEN_SOURCE_READINESS.md)을 확인합니다. Cross-node distributed scheduling, SMTP without env, and production OIDC without valid IdP/session env are intentionally gated or shown as unavailable.

One-command local start:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-dev.ps1
```

Production-like backend/static SPA start:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-prod.ps1
```

Production activation preflight for real OIDC:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check-production-activation.ps1 -Strict -RequireOidc
```

Scheduler worker:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-worker.ps1
```

Queue-backed scheduler worker:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-worker.ps1 -RuntimeMode queue_backed
```

Release validation and Playwright truth pass:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/validate-release.ps1 -WithPlaywright
```

The latest release-validation evidence is visible in `/app/settings` and `/app/evidence`: status, summary path, screenshot path, expected warning classes, and the same one-command validation reference. Missing SMTP/OIDC env and gated cross-node scheduling are shown as availability boundaries, not successful production activation.

Create a reproducible package directory and archive:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/package-release.ps1 -FreshOutputDir
```

Python:

```powershell
uv sync --extra dev
```

선택 의존성:

- PDF 텍스트 추출까지 쓰려면:

```powershell
uv sync --extra fulltext
```

- ReportLab 기반 report PDF export는 기본 Python 의존성에 포함됩니다. 이 저장소에는 별도 `pdf` extra가 없습니다.

Node:

```powershell
cd web
npm ci
```

## 격리형 Prototype v0.7 Research Project Bundle

Prototype v0.7은 선택한 여러 topic의 설정, immutable snapshot, bounded evidence/claim provenance, run/report, Living Topic history, gap analysis, recommendation decision과 promoted-topic origin을 하나의 portable `.xrmlbundle`로 내보내고 새 로컬 환경에서 다시 열 수 있습니다. Export와 import는 각각 preview와 명시적 확인 단계로 분리되며, canonical JSON/JSONL과 manifest/record/semantic digest로 동일 프로젝트의 결정론적 재-export를 검증합니다.

Import는 archive를 먼저 격리해 경로, entry type, 크기·압축률, schema, digest, 참조 무결성, rights와 collision을 검증한 뒤 한 번의 atomic workspace write로 적용합니다. 실패는 기존 workspace를 보존하며, 같은 bundle의 재-import는 zero-write no-op입니다. Full article body, XML, PDF, runtime database, 로컬 절대 경로, secret 및 raw Provider output은 bundle에 포함되지 않습니다.

가져온 프로젝트의 실행 posture는 항상 `DISABLED_PENDING_LOCAL_RECONFIRMATION`입니다. Schedule, network, Provider activation, 자동 run/refresh/report는 import로 활성화되지 않으며, portable evidence는 bounded excerpt와 source locator만 제공합니다. Workspace의 `Project Bundle` 화면에서 export, quarantine validation, import, project inspect와 re-export를 수행할 수 있습니다.

v0.7은 당시 로컬 runnable Prototype의 기능 동결점입니다. `d060383a` feature history는 당시 release에 fast-forward 채택됐고, OAuth/session-backed Codex `report_draft_assist`의 typed candidate, schema/evidence validation, acceptance, report v3 persistence 및 provenance가 task-owned local runtime에서 증명됐습니다. 당시 초기 Prototype 완료 판정은 그 선언된 local scope에 한정됩니다. 현재 목표인 본문 검토·DB 축적·새 Journal 실행 연결의 전체 완료를 뜻하지 않습니다. App OAuth/OIDC, public MCP, Gemini/Claude adapter, weighted orchestration, live GitHub Actions, Ubuntu timing, staging, production 및 external beta는 이 증명에 포함되지 않습니다.

## 격리형 Prototype v0.6 Evidence-aware Research Gaps

Prototype v0.6는 선택한 immutable corpus snapshot과 그 snapshot에서 완료된 정확한 run/report를 기준으로 결정론적 study profile과 evidence matrix를 만듭니다. `Research Gaps` 탭에서는 current accepted corpus 안에서만 성립하는 recurring limitation, underrepresented facet, method, longitudinal, evidence-strength, coverage-loss 등의 관찰을 exact review/evidence/claim/change-event lineage와 함께 확인할 수 있습니다. 알 수 없는 값은 absence로 바꾸지 않고 `unknown`으로 유지합니다.

Recommendation은 validated scientific conclusion이 아니라 corpus-relative decision support입니다. 모든 후보는 적어도 하나의 rule-validated current gap과 source evidence/constraint를 참조하고, `research_recommendation_scoring_v1`의 여섯 component와 weighted score를 함께 표시합니다. Current-corpus collision 검사는 local structured records만 비교하며 global novelty를 평가하지 않습니다.

사용자가 Accept, Hold 또는 Reject를 누를 때마다 immutable decision event가 추가됩니다. Accepted current-snapshot recommendation만 별도의 2단계 확인 후 local-empty topic으로 승격할 수 있습니다. 승격은 source evidence를 복사하지 않고 snapshot, run, report, schedule, external request 또는 Provider call을 자동 생성하지 않습니다. 과거 snapshot 분석과 추천은 그대로 남으며 새 snapshot 분석과 비교할 수 있고, portable Markdown brief도 로컬에서 만들 수 있습니다.

기본 실행은 외부 network와 Provider를 모두 비활성화합니다. v0.6 분석·결정·승격·비교·export는 기존 task-owned system-temp prototype data에서만 동작하며 production, staging, external beta readiness 또는 global scientific novelty proof를 의미하지 않습니다.

## 격리형 Prototype v0.5 Living Topic

Prototype v0.5는 실제 PMC Open Access 주제를 일회성 연구에서 지속 관찰 가능한 Living Topic으로 확장합니다. 일정은 기본 비활성화이며 `manual`, `daily`, `weekly`, `monthly` 중 하나를 명시적으로 선택해 버전으로 저장합니다. 로컬 single-node scheduler는 동일 due window를 한 번만 실행하고, 겹치는 refresh를 차단하며, 놓친 여러 주기는 catch-up 한 건으로 합칩니다. 외부 접근은 기존 고정 PMC query와 NCBI allowlist를 사용하는 eligible real topic에만 허용되고 Provider는 호출하지 않습니다.

Topic의 `Schedule` 탭에서는 enabled 상태, time zone, 다음 due time, 최근 시도/성공, scheduler health와 과거 schedule version을 확인할 수 있습니다. `Briefings` 탭은 refresh 또는 명시적 curation 비교에서 생성된 immutable change event, review/evidence/claim/report impact, critical attention과 provenance link를 보여 줍니다. Refresh 결과는 preview changeset까지만 만들며 snapshot 적용, research run, report 재생성을 자동으로 수행하지 않습니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-prototype.ps1 -CleanupData -LifetimeSeconds 300
```

```powershell
python tools/prototype_launcher.py --backend-port 8000 --frontend-port 5173 --smoke --cleanup-data
```

기본 실행에서는 공식-source network도 비활성화됩니다. 일정 저장만으로 요청이 발생하지 않으며, 외부 network가 비활성화된 상태에서도 기존 schedule, refresh, delta, briefing과 attention history를 다시 열 수 있습니다. 이 기능은 task-owned system-temp data root에서 동작하는 local Prototype이며 distributed scheduling, production, staging 또는 external beta readiness를 의미하지 않습니다.

## 격리형 Prototype v0.4 다중 주제 워크스페이스

Prototype v0.4는 합성 데모, 실제 PMC Open Access, 로컬 빈 주제를 한 Dashboard에서 관리합니다. 새 주제와 안전한 설정 변경은 버전으로 남고, corpus curation은 먼저 changeset을 미리 본 뒤 명시적으로 적용할 때만 새 immutable snapshot을 만듭니다. 과거 run/report는 생성에 사용한 정확한 snapshot과 evidence set에 계속 연결됩니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-prototype.ps1 -CleanupData -LifetimeSeconds 300
```

기본 실행은 외부 네트워크와 live Provider를 모두 비활성화합니다. 실제 PMC 주제에서 `Check for corpus updates`를 명시적으로 확인한 경우에만 기존 v0.3 NCBI allowlist와 고정 query를 사용하며, 결과는 자동 적용하지 않고 changeset preview로만 제시합니다. 로컬 빈 주제 생성, 설정 버전, archive/reopen, snapshot 비교, 선택 snapshot 기반 결정론적 run, report preview/export는 네트워크와 Provider 없이 동작합니다.

```powershell
python tools/prototype_launcher.py --backend-port 8000 --frontend-port 5173 --smoke --cleanup-data
```

실행기는 시스템 임시 디렉터리 아래에만 runtime data를 만들고, `--cleanup-data` 정상 종료 시 자신이 만든 process/listener/data root만 제거합니다. 이 경로는 local runnable Prototype이며 production, staging, external beta readiness를 의미하지 않습니다.

## 격리형 Prototype v0.3 실코퍼스 증명

Prototype v0.3는 기존 합성 v0.1/v0.2 흐름을 유지하면서, 고정된 재활·임상훈련 XR/VR 주제로 PMC Open Access의 재사용 가능한 JATS 전문을 최대 4편까지 처리합니다. 외부 네트워크는 기본적으로 꺼져 있으며, 허용된 NCBI E-utilities와 PMC OAI-PMH 경로만 명시적으로 열 수 있습니다. 라이선스는 CC0 또는 CC BY만 수락하고 Provider는 호출하지 않습니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-prototype.ps1 -EnableRealCorpusProof
```

Dashboard의 `PMC Open Access full-text proof`에서 한 번 실행하면 discovery → rights verification → JATS acquisition → 본문 parsing/review → section/paragraph evidence → 결정론적 Weekly Journal이 이어집니다. Run Detail에서는 claim을 PMCID/DOI, 라이선스, content digest, section, paragraph까지 추적할 수 있고 preview/Markdown export와 zero-network cached replay를 수행할 수 있습니다. 다운로드한 XML, 본문, 캐시, 보고서와 데이터베이스는 실행기 전용 시스템 임시 루트에만 존재하며 `-CleanupData`를 지정한 실행의 정상 종료 시 제거됩니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-prototype.ps1 -EnableRealCorpusProof -CleanupData -LifetimeSeconds 300
```

이 경로는 로컬 Prototype 증명이며 production, staging 또는 external beta readiness를 뜻하지 않습니다. 일반 실행에서는 real-corpus 네트워크가 계속 비활성화됩니다.

## 격리형 Prototype v0.2

합성 논문 코퍼스에서 기존 triage/review/evidence/report/preview/export/lineage 흐름을 한 번에 확인하려면 다음 명령을 실행합니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-prototype.ps1
```

이 실행기는 운영체제 임시 디렉터리에 고유한 `XRML_DATA_DIR`를 만들고, loopback backend/frontend만 시작합니다. 기본값에서는 Live Provider가 비활성화되며 보고서는 결정론적 로컬 fallback으로 생성됩니다. 종료 시 실행기가 시작한 process group만 graceful stop하고 데이터는 재확인을 위해 유지합니다. 해당 실행의 disposable 데이터까지 제거하는 실제 vertical-slice/lineage smoke는 다음과 같습니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-prototype.ps1 -Smoke -CleanupData
```

합성 Prototype 데이터만으로 bounded Provider 후보 흐름을 열려면 명시적으로 다음 플래그를 사용합니다. 이 플래그는 endpoint availability만 열며 호출 자체를 승인하지 않습니다. Codex를 `report_draft_assist` Provider로 선택하고 Run Detail의 disclosure와 확인 절차를 완료해야 설치된 공식 Codex CLI의 기존 OAuth/session을 사용하는 단일 비대화식 호출이 가능하며, 재시도는 없습니다. 출력은 typed candidate로 격리되어 canonical schema·route·paper/evidence·content 검증을 통과한 뒤 별도 Accept를 눌러야 새 immutable report version이 됩니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-prototype.ps1 -EnableLiveProviderProof
```

Prototype mode는 canonical `storage/runs`나 runtime database를 사용하지 않습니다. Provider 실패·거부 시 결정론적 보고서가 계속 활성 상태로 남으며, 이 로컬 기능은 production 또는 staging readiness를 의미하지 않습니다.

## 테스트

Backend regression tests:

```powershell
uv run pytest
```

Frontend integration tests:

```powershell
cd web
npm run test
```

Frontend production build:

```powershell
cd web
$env:WEB_APP_BASE_PATH='/app'
npm run build
```

Browser smoke (canonical Python Playwright path):

```powershell
uv run --with playwright python tools/browser_dashboard_settings_smoke.py --base-url http://127.0.0.1:8000
```

Backend compile sanity:

```powershell
uv run python -m py_compile app/schemas/workspace.py app/schemas/auth.py app/schemas/mcp.py app/schemas/admin.py app/services/outline_history.py app/services/workspace_actions.py app/services/ownership_migration.py app/auth/config.py app/auth/security.py app/api/auth.py app/api/mcp.py app/api/admin.py app/api/runs.py app/main.py tools/mcp_connector_smoke.py
```

테스트는 checked-in `storage/runs/...` fixture를 직접 수정하지 않고, temp storage copy를 만들어 계약을 검증합니다.

## 백엔드 실행

```powershell
uv run uvicorn app.main:app --reload
```

기본 주소:

- API: `http://127.0.0.1:8000`
- Health: `http://127.0.0.1:8000/health`

## 프런트엔드 실행

```powershell
cd web
npm run dev
```

기본 주소:

- App shell: `http://127.0.0.1:5173`

필요하면 프런트에서 다른 API 주소를 보게 할 수 있습니다:

```powershell
$env:VITE_API_BASE_URL='http://127.0.0.1:8000'
npm run dev
```

## 로컬 데모 빠른 시작

가장 단순한 로컬 경로는 auth-disabled 단일 머신 모드입니다.

백엔드:

```powershell
uv run python tools/local_demo.py backend
```

프런트엔드:

```powershell
uv run python tools/local_demo.py frontend
```

prod-like 로컬 실행:

```powershell
uv run python tools/local_demo.py prod
```

이 경로의 기본값:

- `AUTH_MODE=disabled`
- `APP_BASE_URL=http://127.0.0.1:8000`
- `WEB_APP_BASE_PATH=/app`
- 저장소는 기본적으로 repo 내부 `storage/`를 사용

## 로컬 Codex / MCP

공개 배포 없이도 localhost MCP만으로 로컬 검증이 가능합니다.

```powershell
uv run python tools/local_demo.py mcp-local-smoke --json
```

로컬 MCP 엔드포인트:

- `http://127.0.0.1:8000/mcp`

향후 ChatGPT connector 테스트가 필요하면 그때만 public HTTPS tunnel을 추가하면 됩니다. 이번 로컬 데모 경로는 그 전 단계에 집중합니다.

Codex CLI 로컬 연결:

```powershell
codex mcp add xrml --url http://127.0.0.1:8000/mcp
codex mcp list
```

`AUTH_MODE=disabled` 로컬 데모에서는 `codex mcp login xrml`가 필요하지 않습니다.

## Codex CLI 원격 MCP 연결

Codex CLI와 ChatGPT Developer Mode는 같은 MCP 서버를 보도록 맞추는 것을 기준으로 합니다.

공유 배포 전제:

- `AUTH_MODE=oidc`
- `APP_BASE_URL=https://<PUBLIC_HOST>`
- `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_AUDIENCE` 설정
- 같은 public origin에서 `/app`, `/mcp`, `/.well-known/oauth-protected-resource*` 제공

권장 절차:

```powershell
codex mcp add xrml --url https://<PUBLIC_HOST>/mcp
codex mcp list
codex mcp login xrml
```

프로젝트 신뢰 후 `.codex/config.toml`에 고정하고 싶다면 [`.codex/config.toml.example`](./.codex/config.toml.example)를 복사해서 사용합니다. 예시 파일에는 `research.read`, `research.write` scope가 이미 들어 있습니다. 프로젝트 스코프 `.codex/config.toml`은 Codex가 해당 workspace를 trusted project로 볼 때만 로드됩니다.

## ChatGPT Developer Mode용 원격 MCP OAuth

이 repo는 웹앱 안에 범용 “Login with ChatGPT” 버튼을 추가하지 않습니다. 대신 같은 FastAPI 프로세스의 public HTTPS `/mcp`를 ChatGPT Developer Mode에 등록하는 경로를 준비합니다.

필수 전제:

- public HTTPS origin
- `/.well-known/oauth-protected-resource`
- `/.well-known/oauth-protected-resource/mcp`
- tool별 `securitySchemes`
- protected tool의 unauthenticated / insufficient-scope 응답에 `WWW-Authenticate` 헤더와 `_meta["mcp/www_authenticate"]`
- 토큰 검증: signature, issuer, audience, expiry, scopes

운영 절차:

1. 기존 OIDC/Auth0/Stytch/사내 IdP를 재사용합니다. 이 repo는 리소스 서버 역할만 하며 fake OAuth credentials를 만들지 않습니다.
2. IdP 쪽에서 ChatGPT Developer Mode가 제시하는 redirect URI와 client registration 절차를 승인합니다.
3. ChatGPT에서 Developer Mode를 켠 뒤 remote MCP app을 만들고 `https://<PUBLIC_HOST>/mcp`를 등록합니다.
4. connector details에서 refresh를 실행해 현재 tool catalog와 auth metadata를 다시 가져옵니다.
5. protected tool을 처음 호출할 때 ChatGPT가 login UI를 띄우면 OIDC 로그인을 완료합니다.

배포 전 빠른 점검:

```powershell
uv run python tools/mcp_connector_smoke.py --base-url https://<PUBLIC_HOST> --json
```

이 smoke는 `/app`, `/mcp`, tool catalog, protected-resource metadata, unauthorized auth challenge를 함께 확인합니다.

## 사용 방법

1. 백엔드를 띄웁니다.
2. 프런트를 띄웁니다.
3. `Home / New Run`에서 topic, queries, categories를 입력하고 run을 시작합니다.
4. `Run Detail`에서 SSE 기반 progress, ranked paper review, evidence/provenance, report workspace를 확인합니다.
5. `Compare` 화면 또는 Run Detail에서 두 run을 비교하고 child run rerun/branch를 시작합니다.
6. Compare 화면의 `Report` 탭에서 section/body/citation diff를 확인하고, `Rerun` 탭에서 compare-derived selection 또는 preset으로 child run을 시작합니다.
7. Compare 화면의 paper/theme/report 액션으로 `Run Detail`을 deep-link 열고, compare context 배너와 filter/section focus를 그대로 이어받아 review 또는 report 편집을 계속합니다.
8. `Report workspace`에서 compare-driven seed를 preview하고, explicit apply를 눌렀을 때만 outline draft를 변경합니다.
9. `Report workspace`에서 preview 결과를 staged merge candidate로 올리고, merge queue에서 순서를 바꾸거나 개별/일괄 apply 할 수 있습니다.
10. 현재 편집 상태를 editing session으로 저장하고, Home / Run List / Run Detail에서 다시 열어 compare context, focus section, staged merge queue를 복원할 수 있습니다.
11. merge queue에서 candidate-level patch preview를 보고, apply-all 전에는 preflight로 blocker/warning/stale 상태를 먼저 확인할 수 있습니다.
12. stale editing session은 refresh/rebase/discard recovery를 명시적으로 실행해 복구합니다.
13. outline action history에서 applied action별 before/after diff와 provenance를 다시 열어볼 수 있습니다.
14. queued candidate별 structured patch를 열어 anchor confidence, fallback reason, citation/paper linkage를 확인할 수 있습니다.
15. apply-all preflight는 safe/warn/blocked/recoverable/confirmation bucket으로 queue를 분류합니다.
16. stale session recovery는 preview 후 explicit apply로만 실행됩니다.
17. outline action history에서 action vs current draft, two-action diff, report focus, related paper focus를 다시 열 수 있습니다.
18. staged candidate conflict drilldown에서 target resolution, anchor weakness, recovery hint를 직접 inspect할 수 있습니다.
19. recovery preview에서 rebased candidate의 before/after patch와 conflict delta를 확인한 뒤에만 explicit apply를 실행합니다.
20. artifact는 `storage/runs/<run_id>/...`에서 확인합니다.

## 주요 API

- `GET /health`
- `POST /runs`
- `GET /runs`
- `GET /compare?left_run_id=&right_run_id=`
- `GET /compare/report?left_run_id=&right_run_id=`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/papers?page=&page_size=&q=&status=&theme=&score_band=&dedup=`
- `GET /runs/{run_id}/papers/{paper_id}`
- `GET /runs/{run_id}/papers/{paper_id}/review`
- `GET /runs/{run_id}/events`
- `GET /runs/{run_id}/report`
- `GET /runs/{run_id}/lineage`
- `GET /runs/{run_id}/compare-candidates`
- `GET /runs/{run_id}/compare-context`
- `GET /runs/{run_id}/workspace-suggestions`
- `POST /runs/{run_id}/workspace-actions/preview`
- `POST /runs/{run_id}/workspace-actions/apply`
- `GET /runs/{run_id}/workspace-actions/staged`
- `POST /runs/{run_id}/workspace-actions/stage`
- `POST /runs/{run_id}/workspace-actions/preflight-apply-staged`
- `GET /runs/{run_id}/workspace-actions/staged/{action_id}/patch`
- `PATCH /runs/{run_id}/workspace-actions/staged/reorder`
- `POST /runs/{run_id}/workspace-actions/apply-staged`
- `DELETE /runs/{run_id}/workspace-actions/staged/{action_id}`
- `GET /runs/{run_id}/outline`
- `PATCH /runs/{run_id}/outline`
- `GET /runs/{run_id}/preview`
- `POST /runs/{run_id}/export`
- `GET /runs/{run_id}/exports`
- `POST /runs/{run_id}/rerun`
- `GET /runs/{run_id}/rerun-presets`
- `POST /runs/{run_id}/rerun-presets`
- `DELETE /runs/{run_id}/rerun-presets/{preset_id}`
- `GET /runs/{run_id}/workspace-views`
- `POST /runs/{run_id}/workspace-views`
- `DELETE /runs/{run_id}/workspace-views/{view_id}`
- `GET /runs/{run_id}/editing-sessions`
- `POST /runs/{run_id}/editing-sessions`
- `GET /runs/{run_id}/editing-sessions/{session_id}`
- `GET /runs/{run_id}/editing-sessions/{session_id}/staleness`
- `POST /runs/{run_id}/editing-sessions/{session_id}/recover-preview`
- `POST /runs/{run_id}/editing-sessions/{session_id}/recover`
- `DELETE /runs/{run_id}/editing-sessions/{session_id}`
- `GET /runs/{run_id}/outline-action-history`
- `GET /runs/{run_id}/outline-action-history/diff?from_action_id=&to_action_id=&against_current_draft=`
- `GET /runs/{run_id}/outline-action-history/{action_id}/focus-context`

## 레거시 CLI

기존 명령은 계속 사용할 수 있습니다.

```powershell
python tools/pipeline.py --top 3 --weekly-top 15
python tools/arxiv_scout.py --config config/sources.json --keywords config/keywords.json
python tools/triage.py
python tools/summarize.py --top 3
python tools/synthesize_weekly.py --top 15
```

주의:

- `tools/pipeline.py`도 이제 내부적으로 run-scoped storage를 만듭니다.
- 레거시 출력은 계속 생성되지만, authoritative run data는 `storage/runs/`입니다.

## 문서

공개 소스 배포본에 포함되는 문서:

- [설치와 패키징](docs/PACKAGING_AND_INSTALLATION.md)
- [운영 가이드](docs/OPERATIONS.md)
- [환경 변수 레퍼런스](docs/ENVIRONMENT_REFERENCE.md)
- [배포 런북](docs/DEPLOYMENT_RUNBOOK.md)
- [프로덕션 환경 계약](docs/PRODUCTION_ENVIRONMENT.md)
- [프로덕션 활성화](docs/PRODUCTION_ACTIVATION.md)
- [오픈소스 준비 상태](docs/OPEN_SOURCE_READINESS.md)
- [라이선스 범위](docs/LICENSE_SCOPE.md)
- [기여 가이드](CONTRIBUTING.md) · [보안 정책](SECURITY.md) · [서드파티 고지](THIRD_PARTY_NOTICES.md)

M0-M17 마일스톤 기록과 `docs/audit/` 감사 기록은 저장소에 그대로 보존되지만,
개별 검토 전까지는 내부 문서로 분류되어 공개 소스 스냅샷에는 포함되지 않습니다.
분류는 `tools/public_content_policy.json`이 단일 기준입니다.

## 알려진 제한

- `make_report_pdf.py`, `make_journal_pdf.py`는 아직 레거시 renderer입니다.
- `run_all_to_journal_pdf.py`는 호환성 래퍼로만 유지됩니다.
- 글로벌 레거시 경로는 동시 실행에 안전하지 않습니다.
- report/journal PDF export는 기본 ReportLab 런타임과 OS font 설정에 영향을 받습니다.
- product auth/OIDC, ownership migration, ownership audit, MCP surface는 구현되어 있습니다. 다만 broad RBAC/team sharing, ChatGPT widget UI, Codex app-server는 아직 범위 밖입니다.
- branch mode에서 retrieval skip은 선택 paper를 child inbox로 시드하는 안전한 부분 구현만 제공하며, 더 정교한 partial retrieval 최적화는 아직 하지 않았습니다.
- staged merge queue와 editing session은 file-backed JSON이라 multi-user locking을 제공하지 않습니다.
- merge apply는 section-level deterministic patching이며, full body merge나 LLM auto-merge는 아직 하지 않았습니다.
- apply-all preflight는 non-mutating validation이며 atomic rollback을 제공하지 않습니다. 실제 apply-all은 queue 순서대로 진행하다 blocker/confirmation 문제에서 멈춥니다.
- stale recovery는 outline hash와 target/source resolution 기준의 deterministic recovery만 제공합니다. full rebase/merge assistant는 아직 없습니다.
- outline action history viewer는 per-action before/after 설명용입니다. full undo/version control은 아직 아닙니다.
- structured patch anchor는 section/paragraph/fallback heuristic 기준입니다. precise sentence-level anchoring은 아직 보장하지 않습니다.
- recovery preview는 non-mutating projection이며, actual recover/apply-all은 여전히 explicit mutation step입니다.

## CI merge gating

The repository contains [`.github/workflows/ci.yml`](./.github/workflows/ci.yml) for backend and frontend validation. GitHub Actions has run on the separate public history; exact commit results and unresolved failures are recorded in [current validation status](docs/OPEN_SOURCE_READINESS.md). Historical local automated command-contract proof at `d060383a` was `2991 collected / 2987 passed / 4 skipped`, frontend `27 files / 190 tests`, production build pass. Those historical counts are not current-HEAD results.

Merge-gated checks:
- backend contracts: `uv run pytest`, backend compile sanity, FastAPI startup/OpenAPI sanity
- frontend contracts: `npm run test`, `npm run build`
- tracked fixture immutability for `storage/runs`

The backend and frontend validation paths remain independent. Backend fixtures are synthetic by default and use temporary storage. Private real-run fixtures require an explicit local opt-in and are excluded from public CI. CI also checks that any tracked fixture files under `storage/runs` remain unchanged.

The exact workflow scope, merge gates, and local parity commands are recorded in the internal milestone note `docs/M9_CI_AND_MERGE_GATING.md`, which is retained in the repository but not part of the public source snapshot.

## M10 product auth and optional MCP

This repository now supports two product auth modes:

- `AUTH_MODE=disabled`: local/internal mode, no sign-in required
- `AUTH_MODE=oidc`: shared deployment mode with OIDC-backed product auth

Product auth details:
- browser login is handled through `GET /auth/login` and `GET /auth/callback`
- the web app uses signed httpOnly product session cookies
- provider/model credentials such as `OPENAI_API_KEY` remain server-side only

Optional MCP surface:
- endpoint: `/mcp`
- protected resource metadata:
  - `/.well-known/oauth-protected-resource`
  - `/.well-known/oauth-protected-resource/mcp`
- tools:
  - `search_papers`
  - `fetch_paper_details`
  - `start_research_run`
  - `get_run_status`
  - `get_report_preview`

Recommended configuration source:
- copy [`.env.example`](./.env.example)
- see the internal milestone note `docs/M10_AUTH_AND_OPTIONAL_MCP.md` (retained in the repository, not part of the public source snapshot)

Additional local validation commands for M10:

```powershell
uv run python -m py_compile app/schemas/workspace.py app/schemas/auth.py app/schemas/mcp.py app/services/outline_history.py app/services/workspace_actions.py app/auth/config.py app/auth/security.py app/api/auth.py app/api/mcp.py app/api/runs.py
```

## M11 legacy ownership migration

Shared OIDC deployments now include an admin-only legacy ownership migration surface for pre-auth runs that do not yet have owner_user_id.

- Admin API: /admin/legacy-runs, /admin/legacy-runs/preview-migration, /admin/legacy-runs/apply-migration`r
- Admin UI: /admin/legacy-runs`r
- Admin env allowlists: ADMIN_USER_IDS, ADMIN_USER_EMAILS`r
- Preview is non-mutating; apply writes only ownership metadata to storage/runs/<run_id>/run.json`r
- Lineage-aware migration can include the same-root family so compare/rerun descendants do not end up with split ownership accidentally

See docs/M11_LEGACY_OWNERSHIP_MIGRATION.md for the ownership model, API contract, and validation notes.


## M12 ownership bootstrap audit

Operators can now inspect shared-deployment ownership readiness before using the legacy migration UI.

- Admin API: `/admin/ownership-audit`
- Admin UI: audit summary at `/admin/legacy-runs`
- Optional CLI: `uv run python tools/ownership_audit.py --only-problems --limit 10`

See `docs/M12_OWNERSHIP_BOOTSTRAP_AUDIT.md` for the audit contract, lineage-state rules, and operator workflow.


## M13 shared deployment and connector readiness

Shared HTTPS deployment is now hardened for the existing web app plus the narrow `/mcp` companion surface.

- production serving model: build the frontend with `WEB_APP_BASE_PATH=/app`, then serve the built SPA from the FastAPI process at `/app`
- API, auth, admin, and MCP routes remain at the origin root
- public base URL and OIDC callback generation continue to use `APP_BASE_URL`
- cookies can now be hardened explicitly with `COOKIE_SECURE` and `COOKIE_SAMESITE`
- reverse-proxy deployments can opt into forwarded-header handling with `TRUST_PROXY_HEADERS=true`
- extra browser origins can be allowlisted with `ALLOWED_ORIGINS`
- connector smoke helper: `uv run python tools/mcp_connector_smoke.py --base-url https://your-public-origin`

Recommended production build:

```powershell
cd web
$env:WEB_APP_BASE_PATH='/app'
npm run build
```

Then start the backend normally and serve the built assets plus `/mcp` from the same public HTTPS origin.

See `docs/M13_SHARED_DEPLOYMENT_AND_CONNECTOR_READINESS.md` for the exact deployment path, public HTTPS expectations, reverse-proxy notes, and ChatGPT connector readiness checklist.


## M14 staging smoke and release verification

Post-deploy deployment verification now lives outside the PR merge gate.

- helper: `uv run python tools/mcp_connector_smoke.py --base-url https://your-public-origin --json`
- workflow: `.github/workflows/staging-smoke.yml`
- checks:
  - `/app` serves the production SPA shell
  - `/mcp` initialize works
  - `/mcp` tools/list returns the current 5-tool catalog exactly
  - protected-resource metadata endpoints are reachable and contain the expected connector-facing fields
  - unauthorized protected MCP calls challenge correctly
  - optional authenticated read-only MCP smoke when an explicit bearer token and run id are provided
  - OpenAI provider-backed research plan generation is a separate manual gate when that path is release-critical

This workflow is intentionally separate from `ci.yml`. It is for real deployed origins, not for PR merge gating.

If release readiness depends on the live OpenAI provider path, inject `OPENAI_API_KEY` and run:

```powershell
uv run python tools/provider_live_smoke.py
```

Without a key, the expected result is an explicit `skipped` status rather than a pass.

See `docs/M14_STAGING_SMOKE_AND_RELEASE_VERIFICATION.md` for the exact smoke contract, workflow inputs, and operator failure interpretation.


## M15 post-deploy verification and connector go-live

There is still no platform-specific deploy workflow checked into this repo. The release integration point remains the reusable post-deploy smoke workflow:

- GitHub Actions: `.github/workflows/staging-smoke.yml`
- local/manual helper: `uv run python tools/mcp_connector_smoke.py`

Operator-friendly additions:

- `--markdown-summary <path>` writes a reusable go-live report
- `--fail-on-warning true` can make warning-only states fail the run when desired
- the GitHub workflow now uploads both JSON and Markdown smoke artifacts

Go-live runbook:

- validate product web login separately from MCP connector behavior
- verify `/app`
- verify `/mcp`
- verify both protected-resource metadata endpoints
- verify unauthorized protected MCP tool calls challenge correctly
- optionally verify one authenticated read-only MCP call
- if provider-backed research-plan generation is part of the release gate, inject `OPENAI_API_KEY` and require one passing `uv run python tools/provider_live_smoke.py`
- only then connect ChatGPT to the public `/mcp` URL

See `docs/M15_PUBLIC_GO_LIVE_AND_CONNECTOR_CHECKLIST.md` for the full operator checklist and post-deploy invocation pattern.


## M16 Render staging deployment

This repository now includes one concrete no-Docker staging deployment path for Render.

- platform config: `render.yaml`
- deploy workflow: `.github/workflows/render-staging-deploy.yml`
- configured staging origin: `https://xrml-research-agent-staging.onrender.com`
- configured web URL: `https://xrml-research-agent-staging.onrender.com/app`
- configured MCP URL: `https://xrml-research-agent-staging.onrender.com/mcp`
- persistent staging data root: `/opt/render/project/src/storage`

The hostname above is the configured target. It is not proof that the Render service is currently live; treat `tools/mcp_connector_smoke.py` as the actual availability check.

The deploy workflow triggers the Render deploy hook for the selected GitHub commit and then calls the reusable `staging-smoke.yml` workflow with retries while the service comes up.

Required GitHub secret:

- `RENDER_DEPLOY_HOOK_URL`

Optional GitHub secret:

- `STAGING_SMOKE_BEARER_TOKEN`

Staging persistence:

- local/dev default stays repo-relative unless `XRML_DATA_DIR` is set
- Render staging explicitly sets `XRML_DATA_DIR=/opt/render/project/src/storage`
- Render staging must not stay on a free ephemeral web service while run artifacts remain file-backed

See `docs/M16_RENDER_DEPLOYMENT.md` for the one-time Render Blueprint setup, required env vars, deploy workflow inputs, and post-deploy operator flow.

## License

Apache License 2.0 (SPDX: `Apache-2.0`), `Copyright 2026 CAPTW`. Full text in
[`LICENSE`](LICENSE).

The license covers this project's own source. It does **not** cover dependencies
(none are bundled — each keeps its own terms), retrieved article PDFs / full text
/ JATS XML, datasets, runtime data under `storage/`, or generated reports and run
artifacts. The optional `fulltext` extra installs PyMuPDF, which is AGPL-3.0 or
Artifex-commercial; it is not installed by default and the application runs
without it.

Read [`docs/LICENSE_SCOPE.md`](docs/LICENSE_SCOPE.md) before redistributing
anything from this tree. Third-party terms are inventoried in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

# AI connections and evidence review

This guide describes the implemented local workflow without private execution
logs, user documents or machine-specific paths. Availability is per connection
and model; a configured adapter is not live-provider acceptance.

## Settings and execution

Settings exposes connection metadata and model configuration. Use an official
CLI/session or the supported API-key connection. Windows stores supported keys
in the existing DPAPI vault; do not put keys into source, chat, config examples,
exported reports or browser connection tickets. Endpoint/deployment options are
advanced connection details, not proof of model feature compatibility.

Provider, authentication method, model, supported operations and input/output
budgets are separate fields. OpenAI-compatible transport does not mean every
model supports the same structured output or budgets. New model selections apply
to subsequent runs; past execution metadata and results remain immutable.

In **모델 예산으로 전문·절별 분석 준비**, the **이번 분석에 사용할 AI** section
controls the next plan. **저장된 모델 설정 확인** rereads Settings without replacing
unsaved model or budget inputs. **선택 연결의 저장 모델 적용** explicitly fills that
connection's saved model and default input/output/safety budgets. Recheck them
before saving. A removed connection stays visible and cannot silently switch to
another service. Pending or unknown save/unit requests lock these changes until
the existing request is reconciled. Saved plans retain their own original models.
These controls prepare a plan; they do not make a paid call or add local inference.

The local Native Anthropic route requires an exact prepared input, stored key
revision and one-attempt approval bound to actor/topic/request/target/expiry.
Shared/OIDC/generic environments are not enabled by a local proof. Credential
availability alone does not authorize a paid request. Revoked, expired, consumed
or changed-key approval cannot be reused. An unknown response is queried before
any new attempt; there is no automatic external fallback after local failure.

The existing official Codex CLI direction remains. A subscription, CLI login,
API key and app OAuth are different interfaces and must be validated separately.
The historical bounded Codex/Anthropic proofs do not certify every provider,
subscription, account, paper or model. Small local-model task quality and actual
local inference still require a separate capability-specific evaluation.

## From a PDF to a prepared request

1. Open a paper in Papers. Under **본문 구간 검토 준비**, choose
   **PDF 전체 텍스트 추출·범위 확인 → 내 PDF 파일 연결** if no PDF is linked.
2. Select the occurrence and file, confirm the association, then save the copy.
   Selection alone does not upload, extract or call a model. Hash, size, actor and
   occurrence are retained. User association is not independent identity proof.
3. Explicitly extract the attached PDF. The app preserves text/page maps and
   no-text, unvisited/truncated-page limitations. Scanned images, formulae, figures
   and table meaning are not silently treated as read text.
4. **모델 예산으로 전문·절별 분석 준비** freezes the chosen model and input/output
   budget. Whole extracted text is used when it fits; otherwise ordered units
   retain outline, adjacent context and original coordinates. Old manual-range
   records preserve their earlier contract.
5. Inspect the actual input and missing ranges. Preparing a request is not
   permission to send it. An explicit execution remains bound to that input.
6. Query saved execution/results after interruption. A new analysis is a distinct
   run; it does not overwrite a prior response or hide incomplete units.

The existing limits of 25,000,000 PDF bytes and 2,000 physical pages are file
processing bounds, not model context limits. Empty, oversized, encrypted and
invalid input is rejected. File storage, database state and response receipt have
separate recovery contracts; they are not a global filesystem/database transaction.

## Saved analysis to Journal

Saved results preserve response, input range, target and evidence references.
Open the referenced original text to assess a claim. Citation identity/string
matching does not establish scientific entailment. The original analysis can
remain `needs_review` while a separate corrected Journal is accepted.

Create a Journal candidate from selected saved-analysis items. Assess each
sentence/question as supported, limited, unsupported or unassessed; limited or
unsupported judgments require an explanation. Unassessed/unsupported items block
adoption. Research questions are possibilities within the examined literature,
not proof of worldwide novelty or consensus. Explicit reviewing/accepted history
precedes accepted Markdown/JSON export and later readback.

If source bytes change or disappear, the app reports that condition rather than
silently substituting current content. Report export retains its lineage; native
Save As distinguishes completion, cancellation and errors. No stored result or
user-approved Journal automatically triggers a new external AI call.

See [browser acquisition](BROWSER_COLLECTION.md) for the separate file collection
path, and [open-source readiness](OPEN_SOURCE_READINESS.md) for verified scope and
remaining limitations.

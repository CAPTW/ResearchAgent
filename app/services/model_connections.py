"""Secret-free model selection. Configuration never confers execution authority.

Official CLIs own subscription login. Local encrypted storage or fixed environment
references supply API keys at trusted composition; preferences stay secret-free.
"""
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.constants import STORAGE_ROOT
from app.repositories.shared_state import SharedStateSchemaInvalid, load_json_state, mutate_json_state

STATE_PATH: Path = STORAGE_ROOT.parent / "model-connections.json"
SCHEMA = "MODEL_CONNECTIONS_V1"


@dataclass(frozen=True)
class Connector:
    connector_id: str
    display_name: str
    auth_mode: Literal["subscription_cli", "api_key"]
    provider_name: str
    adapter_available: bool
    credential_env: str | None
    cli_command: str | None
    login_instructions: str
    docs_url: str
    availability_reason: str


CONNECTORS = (
    Connector("codex_subscription", "Codex · ChatGPT 구독", "subscription_cli", "codex_cli", True, None,
        "codex login", "공식 Codex CLI에서 ChatGPT 계정으로 로그인합니다. XRML은 토큰을 읽거나 저장하지 않습니다.",
        "https://developers.openai.com/codex/auth", "논문 검토·Journal 어댑터 준비됨. 계정·실행 승인·실제 호출 검증은 별도입니다."),
    Connector("claude_subscription", "Claude Code · 구독", "subscription_cli", "claude_code", True, None,
        "claude auth login", "변경하지 않은 공식 Claude Code에서 직접 로그인합니다. XRML 자체 Claude.ai OAuth는 제공하지 않습니다.",
        "https://code.claude.com/docs/en/legal-and-compliance", "논문 검토·Journal CLI 어댑터 준비됨. 실제 호출·계정 과금 경로·관리 설정 격리 검증은 아직 미완료입니다."),
    Connector("antigravity_subscription", "Antigravity · Google 계정", "subscription_cli", "antigravity_cli", True, None,
        "agy", "공식 Antigravity CLI의 Google 로그인과 OS 보안 저장소를 사용합니다. 이용 범위는 계정 요금제를 확인해야 합니다.",
        "https://antigravity.google/docs/cli/install", "논문 검토·Journal CLI 어댑터 준비됨. 실제 호출·전역 설정 및 Windows 격리 검증은 아직 미완료입니다."),
    Connector("grok_subscription", "Grok Build · 구독 계정", "subscription_cli", "grok_cli", True, None,
        "grok login", "공식 Grok CLI에서 로그인합니다. 해당 구독의 CLI 이용 가능 여부·사용량은 계정에서 확인해야 합니다.",
        "https://docs.x.ai/build/cli/reference", "논문 검토·Journal ACP 어댑터 준비됨. CLI 내부 도구의 완전 차단은 보장되지 않습니다. 실제 호출·계정 과금 경로·Windows 격리 검증은 아직 미완료입니다."),
    Connector("openai_api", "OpenAI · API 키", "api_key", "openai", True, "OPENAI_API_KEY", None,
        "아래 API 키 관리에서 입력하고 암호화 저장합니다. ChatGPT 구독과 별도 API 과금이며 환경 변수도 지원합니다.",
        "https://platform.openai.com/docs/api-reference/authentication", "논문 검토·Journal 어댑터 준비됨. 저장만으로 전송·과금·실행을 승인하지 않습니다."),
    Connector("xai_api", "Grok · xAI API 키", "api_key", "xai", True, "XAI_API_KEY", None,
        "아래 API 키 관리에서 입력하고 암호화 저장합니다. Grok 구독 로그인과 분리된 API 과금 경로입니다.",
        "https://docs.x.ai/developers/quickstart", "논문 검토·Journal Responses 어댑터 준비됨. API 키 경로이며 실제 호출·요금·모델 검증은 별도입니다."),
    Connector("anthropic_api", "Claude · Anthropic API 키", "api_key", "anthropic", True, "ANTHROPIC_API_KEY", None,
        "아래 API 키 관리에서 Anthropic Console 키를 입력하고 암호화 저장합니다. Claude 구독과 별도 API 과금입니다.",
        "https://platform.claude.com/docs/en/api/messages/create", "논문 검토·Journal Messages 어댑터 준비됨. API 키 경로이며 실제 호출·요금·모델 검증은 별도입니다."),
)
BY_ID = {item.connector_id: item for item in CONNECTORS}
ALIASES = {item.provider_name: item.connector_id for item in CONNECTORS} | {"codex": "codex_subscription"}


def _valid_models(models: dict[str, str]) -> bool:
    return all(key in BY_ID and (value == "" or (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value) is not None
        and not value.lower().startswith(("sk-", "xai-", "ya29.")))) for key, value in models.items())


class Update(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    default_connector_id: str
    models: dict[str, str]

    @model_validator(mode="after")
    def check(self):
        if self.default_connector_id not in BY_ID or not _valid_models(self.models):
            raise ValueError("model_connections_invalid")
        return self


def _default() -> dict[str, Any]:
    return dict(schema_version=SCHEMA, revision=0, default_connector_id="codex_subscription", models={})


def _validate(value: dict[str, Any]) -> dict[str, Any]:
    try:
        if set(value) != set(_default()) or value["schema_version"] != SCHEMA:
            raise ValueError()
        Update(expected_revision=value["revision"], default_connector_id=value["default_connector_id"], models=value["models"])
        return value
    except (ValueError, TypeError, KeyError):
        raise SharedStateSchemaInvalid("model_connections") from None


def read() -> dict[str, Any]:
    return _validate(load_json_state(STATE_PATH, state_name="model_connections", expected_type=dict, default=_default))


def selection(state: dict[str, Any] | None = None) -> tuple[str, str | None]:
    override = (os.getenv("XRML_PROVIDER") or "").strip().lower()
    if override:
        return ALIASES.get(override, override), override
    selected = (state if state is not None else read())["default_connector_id"]
    return selected, None


def selected_provider_name() -> str:
    identity, _ = selection()
    return BY_ID[identity].provider_name if identity in BY_ID else identity


def status(state: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.services.model_credentials import CredentialUnavailable, status as credential_status
    def present(item: Connector) -> bool | None:
        if not item.credential_env:
            return None
        try:
            return credential_status(item.connector_id)["source"] != "none"
        except CredentialUnavailable:
            return None
    saved = read() if state is None else state
    effective, override = selection(saved)
    return dict(revision=saved["revision"], default_connector_id=saved["default_connector_id"],
        effective_connector_id=effective, environment_override=override, execution_allowed=False,
        connectors=[dict(asdict(item), model=saved["models"].get(item.connector_id, ""),
            credential_present=present(item))
            for item in CONNECTORS])


def save(body: Update) -> dict[str, Any]:
    # An exact replay is a no-op even after a lost response; distinct stale edits
    # conflict inside the cross-process lock instead of overwriting another tab.
    def mutate(raw):
        current = _validate(raw)
        same = current["default_connector_id"] == body.default_connector_id and current["models"] == body.models
        if current["revision"] != body.expected_revision and not same:
            raise ValueError("model_connections_changed")
        value = current if same else dict(schema_version=SCHEMA, revision=current["revision"] + 1,
            default_connector_id=body.default_connector_id, models=dict(body.models))
        return value, value
    saved = mutate_json_state(STATE_PATH, state_name="model_connections", expected_type=dict, default=_default, mutate=mutate)
    return status(saved)

export function oidcActivationLabel(status?: string | null, authMode?: string | null) {
  if (status === "configured") return "OIDC 구성 완료";
  if (status === "missing_env") return "운영 인증 설정 필요";
  if (status === "invalid_config") return "OIDC 설정 오류";
  if (status === "disabled_dev_mode") return "로컬 개발 모드";
  if (status === "unsafe_production_disabled") return "공유 배포에서는 인증 비활성화를 사용할 수 없습니다";
  if (authMode === "disabled") return "로컬 개발 모드";
  return "인증 상태 확인 필요";
}

export function oidcActivationGuidance(status?: string | null, configured?: boolean) {
  if (status === "unsafe_production_disabled") {
    return "공유 배포에서는 인증 비활성화를 사용할 수 없습니다";
  }
  if (configured) {
    return "운영 인증 설정이 유효합니다";
  }
  return "운영 인증 설정 필요";
}

export function smtpActivationLabel(state?: string | null) {
  if (state === "available") return "SMTP 사용 가능";
  if (state === "unavailable_missing_env") return "SMTP 환경 변수가 설정되지 않았습니다";
  if (state === "invalid_config" || state === "misconfigured") return "SMTP 설정 오류";
  if (state === "connection_failed") return "SMTP 연결 실패";
  if (state === "delivery_failed" || state === "failed_delivery") return "메일 전송 실패";
  return "SMTP 상태 확인 필요";
}

export function smtpActivationGuidance(state?: string | null, available?: boolean) {
  if (available || state === "available") {
    return "현재 설정은 메일 발송을 활성화할 수 있습니다";
  }
  return "현재 설정은 메일 발송을 활성화하지 않습니다";
}

export function secretSafeStatusCopy() {
  return "민감한 값은 표시되지 않습니다";
}

export function mcpActivationLabel(readiness?: string | null, enabled?: boolean, authRequired?: boolean) {
  if (readiness === "oauth_ready") return "MCP 범위 제한 활성";
  if (enabled === false) return "MCP 비활성";
  if (authRequired) return "MCP 범위 인증 필요";
  if (readiness === "local_cli_only") return "MCP 로컬 범위 제한";
  if (readiness === "oidc_missing_audience") return "MCP audience 설정 필요";
  if (readiness === "oidc_http_only") return "MCP HTTPS 설정 필요";
  return "MCP 상태 확인 필요";
}

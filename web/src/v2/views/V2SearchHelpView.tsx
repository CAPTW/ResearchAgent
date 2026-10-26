import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Info } from "@phosphor-icons/react";
import { Link } from "react-router-dom";

import { getSearchHelp, SEARCH_SYNTAX_CONTRACT, type SearchHelpResponse } from "../../lib/api";

export function V2SearchHelpView() {
  const [help, setHelp] = useState<SearchHelpResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    triggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const controller = new AbortController();
    void getSearchHelp({ signal: controller.signal }).then(setHelp).catch((nextError) => {
      if (!controller.signal.aborted) setError(nextError instanceof Error ? nextError.message : "검색 도움말을 불러오지 못했습니다.");
    });
    window.requestAnimationFrame(() => panelRef.current?.focus());
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      if (triggerRef.current && document.contains(triggerRef.current)) triggerRef.current.focus();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      controller.abort();
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, []);

  const syntax = help?.syntax ?? SEARCH_SYNTAX_CONTRACT;
  return (
    <section className="v2-search-help" aria-labelledby="v2-search-help-title" tabIndex={-1} ref={panelRef}>
      <span className="v2-compatibility-icon"><Info size={24} weight="duotone" /></span>
      <div>
        <p className="v2-eyebrow">Search Help · parser-derived</p>
        <h1 id="v2-search-help-title">검색 도움말</h1>
        <p>현재 검색기는 일반 텍스트를 NFKC 정규화·대소문자 접기·공백 정리 후 비교합니다. 꾸며낸 고급 문법은 지원하지 않습니다.</p>
        {error ? <p className="v2-form-error" role="alert">검색 파서 계약을 불러오지 못해 로컬 계약을 표시합니다.</p> : null}
        <dl className="v2-search-help-syntax">
          {Object.entries(syntax).map(([key, value]) => <div key={key}><dt>{key}</dt><dd><strong>{value.status}</strong> · {value.interpretation}</dd></div>)}
        </dl>
        <p className="v2-settings-note">Global Search는 주제·Paper Bank·evidence·run을, Paper Bank 검색은 선택한 주제의 저장 투영만 조회합니다. 검색은 Provider·네트워크·run 생성을 호출하지 않습니다.</p>
        <Link className="v2-button is-primary" to="/evidence?view=guide"><ArrowLeft size={15} />검색 화면으로 돌아가기</Link>
      </div>
    </section>
  );
}

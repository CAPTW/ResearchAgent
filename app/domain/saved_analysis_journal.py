"""Deterministic, private manuscript derivation from an unchanged analysis capture."""
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain import journal_exchange as exchange
from app.domain.local_journal import quote

SCHEMA = 'SAVED_ANALYSIS_JOURNAL_CANDIDATE_V1'
PACKET_SCHEMA = 'SAVED_ANALYSIS_JOURNAL_PACKET_V1'
FIELDS = (('contribution_summary', '기여 요약'), ('method_summary', '방법 요약'),
          ('evidence_summary', '근거 요약'), ('relevance_to_topic', '주제 관련성'),
          ('novelty_signal', '새로움에 대한 해석'), ('practical_implications', '활용에 대한 해석'),
          ('limitations', '분석의 한계'), ('unsupported_claims', '근거가 확인되지 않은 주장'),
          ('follow_up_questions', '후속 질문'))
NOTICE = ('새 모델 호출 없이 저장된 분석 문장을 선택해 만든 후보입니다. 분석은 원문 사실과 구분되는 모델 해석입니다. '
          '인용은 모델 응답 전체의 근거 집합을 연결했으며 문장별 뒷받침은 미확인입니다. '
          '발췌 범위만 포함하며 전체 논문 검토·과학적 검증·신규성 입증은 완료되지 않았습니다.')


def exact_digest(value: Any) -> str:
    return exchange.sha(exchange.preserved_json(value))


class Create(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_decision_id: str = Field(pattern=r'^decision_[a-f0-9]{64}$')
    component_ids: list[str] = Field(min_length=1, max_length=30)


def components(archive: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    draft = archive['receipt']['draft']
    for field, label in FIELDS:
        value = draft[field]
        values = enumerate(value) if isinstance(value, list) else [(None, value)]
        for index, text in values:
            result.append(dict(component_id='/' + field + (f'/{index}' if index is not None else ''),
                label=label + (f' {index + 1}' if index is not None else ''), text=text,
                kind='question' if field == 'follow_up_questions' else 'interpretation',
                section='questions' if field == 'follow_up_questions' else 'limitations' if field in {'limitations', 'unsupported_claims'} else 'overview',
                eligible=bool(text.strip()) and len(text) <= 3000,
                support='NOT_ESTABLISHED' if field == 'unsupported_claims' else 'NOT_ASSESSED'))
    return result


def selection(archive: dict[str, Any], body: Create) -> list[dict[str, Any]]:
    available = {item['component_id']: item for item in components(archive)}
    if (len(set(body.component_ids)) != len(body.component_ids)
            or any(key not in available or not available[key]['eligible'] for key in body.component_ids)):
        raise ValueError('saved_analysis_journal_selection_invalid')
    # Stable source order gives reordered repeated submissions one identity.
    return [item for key, item in available.items() if key in body.component_ids]


def identity(archive: dict[str, Any], body: Create) -> str:
    return 'ajcand_' + exact_digest([SCHEMA, archive['analysis_id'], body.expected_decision_id,
                                   [item['component_id'] for item in selection(archive, body)]])


def require_current(archive: dict[str, Any], decision_id: str) -> None:
    latest = archive['latest_decision']
    if not latest or latest['state'] != 'reviewed' or latest['decision_id'] != decision_id:
        raise ValueError('saved_analysis_journal_source_changed')


def locator(receipt: dict[str, Any], span: dict[str, Any]) -> dict[str, Any]:
    preparation = receipt['packet'].get('source_preparation') or {}
    return dict(schema_version='SAVED_ANALYSIS_LOCATOR_V1',
        note='근거 구간 좌표와 준비 당시 원문 선택 좌표는 서로 다른 범위입니다. 원본 경로는 반입 기록이며 현재 파일 접근을 보장하지 않습니다.',
        evidence_span={key: value for key, value in span.items() if key != 'text'},
        prepared_selection=preparation.get('selection'), prepared_source=preparation.get('source'),
        normalization=preparation.get('normalization'), decoding=preparation.get('decoding'))


def assemble(archive: dict[str, Any], body: Create, decision: dict[str, Any], created: str, *, render: bool = True) -> dict[str, Any]:
    selected = selection(archive, body)
    if decision['state'] != 'reviewed' or decision['decision_id'] != body.expected_decision_id:
        raise ValueError('saved_analysis_journal_source_changed')
    receipt = archive['receipt']
    source = dict(analysis_id=archive['analysis_id'], capture_digest=exact_digest(archive['bundle']),
        response_sha256=archive['bundle']['response_sha256'], request_body_sha256=archive['bundle']['request_body_sha256'],
        requested_model=archive['bundle']['plan']['contract']['model'], receipt=deepcopy(receipt), decision=deepcopy(decision))
    cited = set(receipt['draft']['cited_evidence_span_ids'])
    refs = [dict(citation_id='acite_' + exact_digest([archive['analysis_id'], span]),
        title=receipt['paper_id'], kind='saved_analysis_excerpt', text=span['text'], locator=locator(receipt, span),
        source_evidence_id=span['span_id'], text_sha256=exchange.sha(span['text']),
        source_identity=dict(imported_topic_id=receipt['topic_id'], imported_run_id=receipt['run_id'], imported_paper_id=receipt['paper_id']),
        input_origin=receipt['input_origin'], receipt_id=receipt['receipt_id'])
        for span in receipt['packet']['evidence_spans'] if span['span_id'] in cited]
    if not refs or len(refs) > 20:
        raise ValueError('saved_analysis_journal_selection_invalid')
    selected_ids = [item['component_id'] for item in selected]
    frozen: dict[str, Any] = dict(schema_version=PACKET_SCHEMA, analysis_id=archive['analysis_id'],
                  source=source, selected_component_ids=selected_ids, references=refs,
                  execution=dict(provider_invoked=False, source_collection=False, mode='SAVED_ANALYSIS_DERIVATION'))
    frozen = dict(packet_id='jpacket_' + exact_digest(frozen), **frozen)
    sections: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for section in ('overview', 'limitations', 'questions'):
        selected_section = [item for item in selected if item['section'] == section]
        entries = [dict(kind=item['kind'], text=item['text'], citation_ids=[ref['citation_id'] for ref in refs]) for item in selected_section]
        bindings.extend(dict(section=section, position=index, component_id=item['component_id'], label=item['label'], support=item['support']) for index, item in enumerate(selected_section))
        if entries:
            sections.append(dict(kind=section, entries=entries))
    proposal = exchange.Proposal.model_validate(dict(schema_version=exchange.OUTPUT_SCHEMA, packet_id=frozen['packet_id'],
        title='보존된 분석의 검토 원고', sections=sections))
    citations = exchange.validate(proposal, frozen)
    raw = exchange.preserved_json(proposal.model_dump())
    candidate_id = identity(archive, body)
    lines = ['# 저장 분석에서 만든 Journal 후보 · 미검토', '', NOTICE, '',
             quote(f"원본 보관 ID: {archive['analysis_id']} · 기준 판단: {decision['decision_id']}"), '',
             *exchange.manuscript_lines(proposal, citations)] if render else []
    markdown = '\n'.join(lines)
    return dict(schema_version=SCHEMA, origin='SAVED_ANALYSIS_DERIVATION', candidate_id=candidate_id,
        topic_id=archive['topic_id'], analysis_id=archive['analysis_id'], owner_id=archive['owner_id'],
        created_at=created, source=source, selected_component_ids=selected_ids, input_digest=exact_digest(frozen),
        packet=frozen, proposal=proposal.model_dump(), component_bindings=bindings, citations=citations, raw_json=raw, raw_sha256=exchange.sha(raw),
        markdown=markdown, content_sha256=exchange.sha(markdown), review_state='UNREVIEWED',
        coverage=dict(selected_analyses=1, selected_components=len(selected), excerpt_citations=len(citations)),
        validation=dict(binding='VERIFIED', sentence_support='NOT_ASSESSED', scientific_assessment='NOT_ASSESSED',
                        provider_execution='NOT_VERIFIED', full_paper_review='NOT_ESTABLISHED'))


def provenance_lines(candidate: dict[str, Any]) -> list[str]:
    source = candidate['source']
    receipt = source['receipt']
    return ['## 보존된 분석 출처와 범위', '', NOTICE, '',
        quote(f"원본 보관 ID: {source['analysis_id']} · 원본 입력: {receipt['receipt_id']}"),
        quote(f"분석 검토 기준: {source['decision']['decision_id']} · {source['decision']['note']}"),
        quote(f"요청 모델 ({'반입 기록' if receipt['input_origin'] == 'MANUAL_IMPORT_UNVERIFIED' else '앱 보존 기록'}): {source['requested_model']} · 원래 검증 상태: {receipt['status']}"),
        *([] if receipt['input_origin'] == 'MANUAL_IMPORT_UNVERIFIED' else [quote('보존 출처: ' + receipt['input_origin'] + ' · 합성 출처는 실제 AI 실행의 증거가 아닙니다.')]),
        quote(f"요청 SHA-256: {source['request_body_sha256']}"), quote(f"응답 SHA-256: {source['response_sha256']}"),
        quote('선택 항목: ' + ', '.join(candidate['selected_component_ids'])),
        quote('원 입력 범위: ' + exchange.preserved_json(receipt['packet']['input_scope'])), '',
        '전체 보존 입력과 원 응답은 원본 분석 보관함에서 확인하세요. 원문 근거·분석·검토 판단은 서로 구분됩니다.', '']

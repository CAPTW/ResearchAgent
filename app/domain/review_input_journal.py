"""Local manuscript selection from a frozen receipt, without new execution.

The full original receipt remains distinct from the compact Journal packet.
Citation identity does not establish support for an individual analysis sentence.
"""
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain import journal_exchange as exchange, journal_review_input
from app.domain.local_journal import quote
from app.domain.saved_analysis_journal import FIELDS

SCHEMA = 'REVIEW_INPUT_JOURNAL_CANDIDATE_V1'
ORIGIN = 'PRESERVED_REVIEW_DERIVATION'
ERROR = 'review_input_journal_selection_invalid'
NOTICE = ('새 Provider 호출 없이 고정된 저장 분석의 문장을 선택한 로컬 후보입니다. 분석은 원문 사실과 구분되는 해석입니다. '
          '원 분석의 인용 집합과 이미 선택한 입력 발췌의 교집합을 연결했으며, 문장별 뒷받침은 미평가입니다. '
          '원래 검증 상태·근거가 확인되지 않은 주장도 보존됩니다. 전체 논문 검토 NOT_ESTABLISHED · 과학적 타당성 NOT_ASSESSED.')


class Create(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_packet_id: str = Field(pattern=r'^jpacket_[a-f0-9]{64}$')
    receipt_id: str = Field(pattern=r'^aireceipt_[a-f0-9]{64}$')
    component_ids: list[str] = Field(min_length=1, max_length=30)


def _exact(value: Any) -> str:
    return exchange.sha(exchange.preserved_json(value))


def _papers(job: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for paper in job['inputs']['papers']:
        if paper.get('review_input') is not None:
            journal_review_input.validate_frozen(paper['review_input'])
            result.append(paper)
    return result


def _source(paper: dict[str, Any]) -> dict[str, Any]:
    frozen = paper['review_input']
    receipt = frozen['receipt']
    proof = receipt['validation'].get('prepared_execution')
    # These values are the validated prepared-result record, not guessed from
    # generic legacy fields, a current model selection or the active runtime.
    return dict(paper_version_id=paper['paper_version_id'], receipt_id=receipt['receipt_id'],
        input_origin=receipt['input_origin'], validation_status=receipt['status'],
        model=proof['contract']['model'] if proof is not None else None,
        request_sha256=proof['contract']['request_sha256'] if proof is not None else None,
        raw_text_sha256=proof['response']['raw_text_sha256'] if proof is not None else None,
        receipt=deepcopy(receipt), receipt_sha256=_exact(receipt),
        selected_input_component_ids=list(frozen['selected_component_ids']))


def _citations(paper: dict[str, Any], frozen: dict[str, Any]) -> list[dict[str, Any]]:
    receipt = paper['review_input']['receipt']
    cited = receipt['draft'].get('cited_evidence_span_ids', [])
    if (not isinstance(cited, list) or not cited or any(not isinstance(value, str) or not value.strip() for value in cited)
            or len(cited) != len(set(cited))):
        return []
    span_ids = {value for span in receipt['packet'].get('evidence_spans', [])
        if (value := next((span.get(key) for key in ('span_id', 'id', 'label') if isinstance(span.get(key), str) and span[key].strip()), None)) is not None}
    if not set(cited) <= span_ids:
        return []
    return [ref for ref in frozen['references'] if ref['kind'] == 'review_input_excerpt'
        and ref['paper_version_id'] == paper['paper_version_id'] and ref['review_receipt_id'] == receipt['receipt_id']
        and ref['source_evidence_id'] in cited]


def components(paper: dict[str, Any], frozen: dict[str, Any]) -> list[dict[str, Any]]:
    draft = paper['review_input']['receipt']['draft']
    citations = _citations(paper, frozen)
    result = []
    for field, label in FIELDS:
        value = draft.get(field)
        values = enumerate(value) if isinstance(value, list) else [(None, value)]
        for index, text in values:
            # Old free-form receipt drafts need not contain this schema. Never
            # stringify their unknown values into apparent analysis prose.
            if not isinstance(text, str):
                continue
            reason = ('BLANK_TEXT' if not text.strip() else 'TEXT_TOO_LONG' if len(text) > 3000 else
                      'NO_SELECTED_CITED_EVIDENCE' if not citations else 'CITATION_LIMIT_EXCEEDED' if len(citations) > 20 else None)
            result.append(dict(component_id='/' + field + (f'/{index}' if index is not None else ''),
                label=label + (f' {index + 1}' if index is not None else ''), text=text,
                kind='question' if field == 'follow_up_questions' else 'interpretation',
                section='questions' if field == 'follow_up_questions' else 'limitations' if field in {'limitations', 'unsupported_claims'} else 'overview',
                eligible=reason is None, ineligible_reason=reason,
                support='NOT_ESTABLISHED' if field == 'unsupported_claims' else 'NOT_ASSESSED'))
    return result


def inputs(job: dict[str, Any]) -> dict[str, Any]:
    frozen = exchange.packet(job)
    values = []
    for paper in _papers(job):
        source = _source(paper)
        values.append({key: source[key] for key in ('receipt_id', 'paper_version_id', 'input_origin', 'model', 'validation_status')}
            | dict(components=components(paper, frozen)))
    return dict(packet_id=frozen['packet_id'], inputs=values)


def _selection(job: dict[str, Any], body: Create) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    body = Create.model_validate(body.model_dump())
    frozen = exchange.packet(job)
    if body.expected_packet_id != frozen['packet_id']:
        raise ValueError('journal_packet_mismatch')
    matches = [paper for paper in _papers(job) if paper['review_input']['receipt']['receipt_id'] == body.receipt_id]
    if len(matches) != 1:
        raise ValueError(ERROR)
    paper = matches[0]
    available = {item['component_id']: item for item in components(paper, frozen)}
    if (len(set(body.component_ids)) != len(body.component_ids)
            or any(key not in available or not available[key]['eligible'] for key in body.component_ids)):
        raise ValueError(ERROR)
    return frozen, paper, [item for key, item in available.items() if key in body.component_ids]


def identity(job: dict[str, Any], body: Create) -> str:
    frozen, paper, selected = _selection(job, body)
    return 'jcand_' + _exact([SCHEMA, job['job_id'], frozen['packet_id'], paper['paper_version_id'], body.receipt_id,
                             [item['component_id'] for item in selected]])


def assemble(job: dict[str, Any], body: Create, actor: str, created: str, *, render: bool = True) -> dict[str, Any]:
    if not isinstance(actor, str) or not actor.strip() or not isinstance(created, str) or not created.strip():
        raise ValueError(ERROR)
    frozen, paper, selected = _selection(job, body)
    refs = _citations(paper, frozen)
    sections: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for section in ('overview', 'limitations', 'questions'):
        selected_section = [item for item in selected if item['section'] == section]
        if selected_section:
            sections.append(dict(kind=section, entries=[dict(kind=item['kind'], text=item['text'],
                citation_ids=[ref['citation_id'] for ref in refs]) for item in selected_section]))
            bindings.extend(dict(section=section, position=index, component_id=item['component_id'], label=item['label'], support=item['support'])
                for index, item in enumerate(selected_section))
    proposal = exchange.Proposal.model_validate(dict(schema_version=exchange.OUTPUT_SCHEMA, packet_id=frozen['packet_id'],
        title='보존된 논문 분석의 검토 원고', sections=sections))
    citations = exchange.validate(proposal, frozen)
    raw = exchange.preserved_json(proposal.model_dump())
    value = dict(schema_version=SCHEMA, origin=ORIGIN, candidate_id=identity(job, body),
        topic_id=job['topic_id'], job_id=job['job_id'], input_digest=job['input_digest'],
        preserved_by=actor, preserved_at=created, review_state='UNREVIEWED',
        source=_source(paper), selected_component_ids=[item['component_id'] for item in selected], component_bindings=bindings,
        packet=frozen, proposal=proposal.model_dump(), citations=citations,
        coverage=exchange.citation_coverage(frozen, citations) | dict(selected_components=len(selected)),
        validation=dict(method='PRESERVED_REVIEW_DERIVATION_V1', binding='VERIFIED', semantic_support='NOT_ASSESSED',
            scientific_assessment='NOT_ASSESSED', contradictions='NOT_ASSESSED', provider_execution='NOT_VERIFIED',
            full_document_review='NOT_ESTABLISHED'), raw_json=raw, raw_sha256=exchange.sha(raw))
    lines = ['# 저장된 논문 분석에서 만든 Journal 후보 · 미검토', '', NOTICE, '',
        *exchange.manuscript_lines(proposal, citations), *provenance_lines(value)] if render else []
    markdown = '\n'.join(lines)
    return value | dict(markdown=markdown, content_sha256=exchange.sha(markdown))


def validate(value: dict[str, Any], job: dict[str, Any]) -> None:
    try:
        body = Create(expected_packet_id=value['packet']['packet_id'], receipt_id=value['source']['receipt_id'],
            component_ids=value['selected_component_ids'])
        expected = assemble(job, body, value['preserved_by'], value['preserved_at'], render=False)
        preserved = {'markdown', 'content_sha256'}
        # Verify frozen semantics, without rewriting an earlier rendered cover.
        if (exchange.preserved_json({k: v for k, v in value.items() if k not in preserved})
                != exchange.preserved_json({k: v for k, v in expected.items() if k not in preserved})
                or exchange.sha(value['markdown']) != value['content_sha256']):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ValueError('journal_candidate_integrity_invalid') from None


def provenance_lines(candidate: dict[str, Any]) -> list[str]:
    source = candidate['source']
    return ['## 선택한 저장 분석의 출처와 한계', '', NOTICE, '',
        quote(f"보존 영수증: {source['receipt_id']} · 논문 버전: {source['paper_version_id']}"),
        quote(f"입력 출처: {source['input_origin']} · 원래 검증 상태: {source['validation_status']}"),
        quote('원래 검증 사유: ' + exchange.preserved_json(source['receipt']['validation'].get('reason_codes', []))),
        quote('요청 모델(보존 기록): ' + (source['model'] or '기록 없음')),
        quote('영수증 SHA-256: ' + source['receipt_sha256']),
        quote('원 요청 SHA-256: ' + (source['request_sha256'] or '기록 없음')),
        quote('원 assistant 응답 텍스트 SHA-256: ' + (source['raw_text_sha256'] or '기록 없음')),
        quote('선택한 분석 항목: ' + ', '.join(candidate['selected_component_ids'])),
        quote('고정 입력 발췌: ' + ', '.join(source['selected_input_component_ids'])), '',
        '전체 보존 영수증과 원 분석의 입력·결과·검증 기록은 후보 및 채택본 JSON의 source.receipt에서 확인할 수 있습니다.', '']

"""Isolated Messages composition: a fixed mock target, never a live issuer.

Reuses the ordinary paper dispatcher, authority ledger and atomic receipt save.
The mock surface is bound into the manifest; it cannot authorize native HTTP.
"""
from datetime import timedelta
import os
from threading import RLock
from typing import Any

import httpx
from pydantic import Field

from app.domain import prepared_review, review_authority, review_execution as domain, review_transport_execution as transport
from app.domain.review_offline import ADAPTER
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.repositories import review_authorities, review_executions, review_preparations, review_transport_history
from app.repositories.files import iso_now_utc
from app.services.ai_provider import ProviderRequest as SyntheticRequest
from app.services.offline_environment import require_isolated_data_root
from app.services.provider_output_class_proof import ProviderProofIdentity, ProviderProofLevel, ProviderProofRecord, compute_provider_proof_id
from app.services.providers import anthropic_prepared_review as provider
from app.services.review_dispatch import PaperDispatchRuntime
from app.services.review_offline import SyntheticReviewProvider
from app.services.review_transport_offline import project

MODEL = 'synthetic-anthropic-structured-model'
ISSUER = 'xrml-anthropic-mock-issuer-v1'
POLICY = 'PAPER_ANTHROPIC_MOCK_ONLY_V1'
MODE = 'ANTHROPIC_STRUCTURED_OFFLINE'


class Prepare(domain.Prepare):
    expected_preview_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class Approve(domain.Dispatch):
    expected_contract_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def manifest(prepared: dict[str, Any]) -> dict[str, Any]:
    return provider.build_plan(prepared, timeout_seconds=60, profile=provider.MANIFEST_SCHEMA_V2,
                               execution_surface='MOCK_TRANSPORT')[2]


def eligible(saved: dict[str, Any]) -> bool:
    value, authority = saved['contract'], saved.get('authority')
    return (value['mode'] == 'TRUSTED_PAPER_HTTP' and value['input_origin'] == 'FIXTURE_SYNTHETIC'
        and value['prepared']['provider_name'] == 'anthropic' and value['prepared']['model'] == MODEL
        and value['prepared']['provider_request']['max_output_tokens'] == 2200
        and encoded(value['transport']) == encoded(manifest(value['prepared']))
        and (authority is None or (authority['issuer_id'] == ISSUER and authority['policy_version'] == POLICY)))


def _preview(prepared: dict[str, Any], digest: str) -> dict[str, Any]:
    value = dict(schema_version='PAPER_ANTHROPIC_OFFLINE_PREVIEW_V1', preparation_digest=digest,
        prepared=prepared, transport=manifest(prepared), input_origin='FIXTURE_SYNTHETIC', execution='NOT_STARTED',
        specification=dict(provider_name='anthropic', model=MODEL, max_output_tokens=2200))
    return dict(value, preview_sha256=sha(encoded(value)))


def preview(runtime: 'AnthropicOfflineRuntime | None', scope: domain.Scope) -> dict[str, Any]:
    enabled = runtime is not None and runtime.enabled
    value = None
    if enabled:
        preparation = review_preparations.detail(scope.topic, scope.version, scope.preparation, list(scope.occurrences))
        value = _preview(prepared_review.prepare(build_packet(preparation), provider_name='anthropic', model=MODEL), sha(encoded(preparation)))
    return dict(enabled=enabled, mode=MODE, reason=None if enabled else 'paper_anthropic_offline_disabled', preview=value)


def _issue(contract: dict[str, Any], body: domain.Control) -> dict[str, Any]:
    if not eligible(dict(contract=contract)):
        raise ValueError('review_execution_transport_mismatch')
    now = domain.timestamp(iso_now_utc())
    frozen, binding = contract['prepared'], contract['transport']
    identity = ProviderProofIdentity('1', POLICY, 'anthropic', 'api', 'full_text_review_assist',
        'research_output', frozen['request_sha256'], MODEL, sha(encoded(binding)), binding['adapter_id'], binding['adapter_version'])
    proof = ProviderProofRecord(compute_provider_proof_id(identity), identity,
        ProviderProofLevel.EXACT_OUTPUT_CLASS_VERIFIED, sha(encoded([POLICY, contract])), now,
        now + timedelta(minutes=2), None, ('MOCK_TRANSPORT_ONLY', 'NO_NETWORK_OR_CREDENTIAL', 'NO_SCIENTIFIC_REVIEW'), True)
    grant = review_authority.Grant(authorization_id='amock_' + sha(encoded([contract['execution_id'], body.request_id])),
        actor_id=contract['actor_id'], contract_sha256=sha(encoded(contract)), input_origin='FIXTURE_SYNTHETIC',
        policy_version=POLICY, granted_at=now, expires_at=now + timedelta(minutes=2), reserved_output_tokens=2200, proof=proof)
    return review_authority.Record(grant=grant, issuer_id=ISSUER, decision_ref=body.request_id,
        registered_at=now, expected_sequence=body.expected_sequence).model_dump(mode='json')


def _synthetic_response(prepared: dict[str, Any], expected: bytes, request: httpx.Request) -> httpx.Response:
    if request.method != 'POST' or str(request.url) != 'https://api.anthropic.com/v1/messages' or request.content != expected:
        raise ValueError('review_execution_transport_mismatch')
    packet = prepared['packet']
    result = SyntheticReviewProvider().generate(SyntheticRequest(provider_name=ADAPTER, packet=packet,
        mode='stub', run_id=packet['source_preparation']['source']['run_id']))
    return httpx.Response(200, json=dict(type='message', role='assistant', model=MODEL,
        id='synthetic-' + prepared['request_sha256'], content=[dict(type='text', text=encoded(result.parsed_draft))],
        stop_reason='end_turn', stop_sequence=None, usage={}))


def _provider(prepared: dict[str, Any]) -> provider.AnthropicPreparedReviewProvider:
    _, body, _ = provider.build_plan(prepared, timeout_seconds=60, profile=provider.MANIFEST_SCHEMA_V2, execution_surface='MOCK_TRANSPORT')
    return provider.AnthropicPreparedReviewProvider(prepared, api_key='synthetic-only-not-a-credential', timeout_seconds=60,
        profile=provider.MANIFEST_SCHEMA_V2, execution_surface='MOCK_TRANSPORT',
        transport=httpx.MockTransport(lambda request: _synthetic_response(prepared, body, request)))


class AnthropicOfflineRuntime:
    def __init__(self) -> None:
        self._lock = RLock()
        self._closed = False
        self._workers: dict[str, PaperDispatchRuntime] = {}

    @property
    def enabled(self) -> bool:
        with self._lock:
            return not self._closed

    def prepare(self, scope: domain.Scope, body: Prepare, value: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise ValueError('paper_anthropic_offline_disabled')
            spec = transport.Prepare(**body.model_dump(exclude={'expected_preview_sha256'}), **value['specification'])
            return review_executions.prepare_transport(scope, spec, value['transport'], input_origin='FIXTURE_SYNTHETIC')

    def act(self, scope: domain.Scope, saved: dict[str, Any], action: str, body: domain.Dispatch) -> None:
        with self._lock:
            if self._closed:
                raise ValueError('paper_anthropic_offline_disabled')
            identity = saved['contract']['execution_id']
            for key, finished in list(self._workers.items()):
                try:
                    finished.wait_idle(0)
                except TimeoutError:
                    continue
                del self._workers[key]
            command = domain.Dispatch(request_id=body.request_id, expected_sequence=body.expected_sequence)
            if action == 'revoke':
                review_authorities.revoke(scope, identity, domain.Control(**command.model_dump(), action='revoke'), reason=POLICY)
                return
            worker = self._workers.get(identity) or PaperDispatchRuntime(resolver=_issue, provider=_provider(saved['contract']['prepared']))
            if action == 'approve':
                control = domain.Control(**command.model_dump(), action='approve')
                try:
                    worker.approve(scope, identity, control)
                except ValueError as error:
                    if str(error) != 'paper_review_authority_conflict':
                        raise
                    winner = review_transport_history.detail(scope, identity)
                    if not eligible(winner) or not review_executions._replay(winner, dict(kind='control', body=control.model_dump())):
                        raise
            elif action == 'dispatch':
                self._workers[identity] = worker
                worker.dispatch(scope, identity, command)
            else:
                raise ValueError('review_execution_transition_invalid')

    def wait_idle(self, timeout: float) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.wait_idle(timeout)

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            workers = list(self._workers.values())
        failed = False
        for worker in workers:
            try:
                worker.stop()
            except RuntimeError:
                failed = True
        if failed:
            raise RuntimeError('review_execution_stop_state_unavailable')


def build_runtime() -> AnthropicOfflineRuntime | None:
    if os.environ.get('XRML_PAPER_ANTHROPIC_OFFLINE_ENABLED', '0') != '1':
        return None
    require_isolated_data_root('XRML_PAPER_ANTHROPIC_OFFLINE', 'paper_anthropic_offline_configuration_invalid')
    return AnthropicOfflineRuntime()


def prepare(runtime: AnthropicOfflineRuntime | None, scope: domain.Scope, body: Prepare) -> dict[str, Any]:
    identity = transport.identity(scope, body.request_id)
    try:
        saved = review_transport_history.detail(scope, identity)
    except KeyError:
        saved = None
    if saved is not None:
        value = saved['contract']
        expected = _preview(value['prepared'], value['specification']['expected_preparation_digest'])
        specification = dict(body.model_dump(exclude={'expected_preview_sha256'}), **expected['specification'])
        if (not eligible(saved) or encoded(specification) != encoded(value['specification'])
                or body.expected_preview_sha256 != expected['preview_sha256']):
            raise ValueError('review_execution_request_conflict')
        return project(saved)
    selected = preview(runtime, scope)['preview']
    if selected is None or runtime is None:
        raise ValueError('paper_anthropic_offline_disabled')
    if body.expected_preview_sha256 != selected['preview_sha256'] or body.expected_preparation_digest != selected['preparation_digest']:
        raise ValueError('review_execution_preparation_changed')
    runtime.prepare(scope, body, selected)
    return project(review_transport_history.detail(scope, identity))


def act(runtime: AnthropicOfflineRuntime | None, scope: domain.Scope, identity: str, action: str, body: domain.Dispatch) -> dict[str, Any]:
    saved = review_transport_history.detail(scope, identity)
    if not eligible(saved):
        raise ValueError('review_execution_transport_mismatch')
    if isinstance(body, Approve) and body.expected_contract_sha256 != sha(encoded(saved['contract'])):
        raise ValueError('review_execution_changed')
    command: dict[str, Any] = dict(request_id=body.request_id, expected_sequence=body.expected_sequence)
    if action != 'dispatch':
        command['action'] = action
    if review_executions._replay(saved, dict(kind='dispatch' if action == 'dispatch' else 'control', body=command)):
        return project(saved)
    if action == 'revoke' and review_authorities.replay_revocation(scope, identity, domain.Control(**command), reason=POLICY):
        return project(review_transport_history.detail(scope, identity))
    if runtime is None or not runtime.enabled:
        raise ValueError('paper_anthropic_offline_disabled')
    runtime.act(scope, saved, action, body)
    return project(review_transport_history.detail(scope, identity))

"""Disposable Native composition test; HTTP is intercepted only in this module.

This deliberately exercises NATIVE_HTTP records with fabricated evidence. Nothing
here qualifies a live Provider. Offers are seeded separately, never by app reads.
The product adapter's prohibition on injected native transports stays intact.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

import httpx

from app.auth.resource_access import local_resource_actor
from app.domain import review_authority
from app.domain.constants import STORAGE_ROOT
from app.domain.review_preparation import encoded
from app.main import create_app as product_app
from app.repositories import review_transport_history
from app.services import model_credentials, review_execution
from app.services.providers import anthropic_prepared_review as adapter
from app.services.review_anthropic_native import AnthropicNativeRuntime
from tests.test_anthropic_messages import envelope
from tests.test_prepared_review import response
from tests.test_review_http_dispatch import http_grant

MODEL = 'fixture-native-anthropic-no-network'
TRUST = frozenset({('fixture-native-issuer', 'synthetic-policy-e\u0301')})
KEY = 'synthetic-native-browser-key-not-a-real-credential'


def fixture_root():
    root = Path(os.environ['XRML_DATA_DIR']).resolve(strict=True)
    scratch = Path(tempfile.gettempdir()).resolve(strict=True)
    if (root == scratch or not root.is_relative_to(scratch) or root != STORAGE_ROOT.parent
            or root.drive.upper() != 'D:' or os.environ.get('AUTH_MODE') != 'disabled'
            or os.environ.get('TOPIC_SCHEDULER_ENABLED') != '0'
            or os.environ.get('XRML_RUN_EXECUTOR_MODE') != 'disabled'
            or not model_credentials.path().resolve().is_relative_to(root)):
        raise ValueError('Native mock qualification requires isolated D-drive fixture data')
    return root


def lookup(contract):
    location = fixture_root() / 'native-approval-offers.json'
    if not location.exists():
        return None
    return json.loads(location.read_text(encoding='utf-8')).get(contract['execution_id'])


def create_app():
    root = fixture_root()
    if model_credentials.status('anthropic_api')['revision'] == 0:
        model_credentials.save('anthropic_api', 0, KEY)
    native_send = adapter.send

    def intercepted(body, *, api_key, timeout_seconds, transport=None, max_output_bytes, before_send=None):
        if (transport is not None or api_key != KEY
                or body != (root / 'native-request.bin').read_bytes()):
            raise AssertionError('Native fixture request or stored key mismatch')

        def handler(request):
            assert str(request.url) == 'https://api.anthropic.com/v1/messages'
            assert request.content == body
            with (root / 'native-sends.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(encoded(dict(kind='MOCK_HTTP_ONLY', live_calls=0)) + '\n')
            return httpx.Response(200, json=json.loads((root / 'native-response.json').read_text(encoding='utf-8')))

        return native_send(body, api_key=api_key, timeout_seconds=timeout_seconds,
                           transport=httpx.MockTransport(handler), max_output_bytes=max_output_bytes, before_send=before_send)

    adapter.send = intercepted
    runtime = AnthropicNativeRuntime(model=MODEL, credential_revision=1, trusted_issuers=TRUST, source=lookup)
    return product_app(paper_anthropic_native_runtime=runtime)


def seed_offer(topic, version, preparation, execution):
    root = fixture_root()
    scope = review_execution.scope(topic, version, preparation, local_resource_actor())
    saved = review_transport_history.detail(scope, execution)
    assert saved['current']['state'] == 'READY'
    contract = saved['contract']
    assert contract['prepared']['model'] == MODEL
    assert contract['transport']['credential_binding']['revision'] == 1
    now = datetime.now(timezone.utc)
    record = review_authority.Record(grant=http_grant(contract, now), issuer_id='fixture-native-issuer',
        decision_ref='fixture-only-never-live-authority', registered_at=now,
        expected_sequence=saved['current']['sequence']).model_dump(mode='json')
    path = root / 'native-approval-offers.json'
    offers = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if execution in offers:
        raise ValueError('Do not renew or replace an existing fixture offer')
    offers[execution] = record
    path.write_text(encoded(offers), encoding='utf-8')
    body = adapter.build_plan(contract['prepared'], timeout_seconds=60,
        profile=adapter.MANIFEST_SCHEMA_V2, credential_revision=1)[1]
    (root / 'native-request.bin').write_bytes(body)
    (root / 'native-response.json').write_text(encoded(envelope(
        response(contract['prepared']).raw_text, model=MODEL)), encoding='utf-8')
    return dict(execution_id=execution, fixture_only=True, live_calls=0, registered=False)

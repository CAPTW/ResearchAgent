"""Disposable browser composition: one pre-captured synthetic response, no sender."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

import httpx

from app.domain import prepared_review
from app.domain.constants import STORAGE_ROOT
from app.main import create_app as product_app
from app.services.local_anthropic_trial import LocalAnthropicTrial, SCHEMA_COMPILER_V2
from tests.test_anthropic_messages import envelope
from tests.test_prepared_review import packet, response


def forbidden(*args, **kwargs):
    raise AssertionError('Archiving a saved capture must never execute or resolve credentials')


def create_app():
    root = Path(os.environ['XRML_DATA_DIR']).resolve(strict=True)
    scratch = Path(tempfile.gettempdir()).resolve(strict=True)
    if (root == scratch or not root.is_relative_to(scratch) or root != STORAGE_ROOT.parent
            or root.drive.upper() != 'D:' or os.environ.get('AUTH_MODE') != 'disabled'
            or os.environ.get('TOPIC_SCHEDULER_ENABLED') != '0'
            or os.environ.get('XRML_RUN_EXECUTOR_MODE') != 'disabled'):
        raise ValueError('Synthetic browser runtime requires the isolated D-drive fixture')
    contract = prepared_review.prepare(packet(), provider_name='anthropic', model='claude-haiku-5-5')
    selection = contract['packet']['source_preparation']['selection']
    issued = datetime(2026, 10, 8, tzinfo=timezone.utc)
    grant = dict(trial_id='captured-analysis-browser', actor_id='local-internal-user', credential_revision=1,
        issued_at=issued.isoformat(), expires_at=(issued + timedelta(minutes=5)).isoformat(), origin='FIXTURE_SYNTHETIC',
        source=dict(title='합성 보존 발췌 · 외부 호출 없음', pdf_sha256='a' * 64, page=2,
            start=selection['start'], end=selection['end'], excerpt_sha256=selection['sha256'],
            excerpt=contract['packet']['evidence_spans'][0]['text']))
    trial_path = root / 'captured-trial.json'
    options = dict(path=trial_path, contract=contract, grant=grant, read_timeout_seconds=300, schema_compiler=SCHEMA_COMPILER_V2)
    if not trial_path.exists():
        calls = []
        def synthetic(request):
            calls.append(request.method)
            return httpx.Response(200, json=envelope(response(contract, unsupported=True).raw_text, model='claude-haiku-5-5'))
        seed = LocalAnthropicTrial(**options, resolve_key=lambda _: 'synthetic-only-not-a-key',
            transport=httpx.MockTransport(synthetic), clock=lambda: issued)
        seed.execute(seed.inspect()['trial']['binding_sha256'])
        if calls != ['POST']:
            raise AssertionError('Expected exactly one synthetic seed response')
        (root / 'capture-seed.json').write_text(json.dumps(dict(synthetic_calls=len(calls), live_calls=0)), encoding='utf-8')
    runtime = LocalAnthropicTrial(**options, resolve_key=forbidden, transport=httpx.MockTransport(forbidden))
    return product_app(local_anthropic_trial=runtime)

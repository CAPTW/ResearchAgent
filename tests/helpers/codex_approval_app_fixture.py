"""Explicit signed synthetic session, stored proof source and fake Python only.

The HTTP source reads prewritten offers. Only separately invoked test helpers
fabricate offers; neither app requests nor restart issue/renew authority.
"""
from dataclasses import replace
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from app.auth.config import get_auth_settings
from app.auth.resource_access import ResourceActor
from app.auth.security import ProductUser, encode_product_session_cookie
from app.domain import journal_authority, review_authority
from app.domain.constants import STORAGE_ROOT
from app.db.bootstrap import connect_metadata_index
from app.domain.review_preparation import encoded
from app.repositories import journal_executions, review_transport_history
from app.services import review_execution
from app.services.codex_app_composition import CodexAppConfig, create_codex_app
from app.services.providers.codex_journal_process import CliRuntime
from app.services.providers.codex_journal_v2 import CodexJournalV2Provider
from tests import test_codex_journal as journal_cli
from tests.test_prepared_review import response
from tests.test_review_authority import grant as paper_grant

USER = "local-internal-user"  # Existing disposable seed owner; never an actual account.
PAPER_TRUST = frozenset({("fixture-paper-issuer", "synthetic-policy-e\u0301")})
JOURNAL_TRUST = frozenset({("fixture-only-issuer", "synthetic-policy")})


def fixture_root():
    root = Path(os.environ["XRML_DATA_DIR"]).resolve(strict=True)
    scratch = Path(tempfile.gettempdir()).resolve(strict=True)
    if (root == scratch or not root.is_relative_to(scratch) or root != STORAGE_ROOT.parent
            or root.drive.upper() != "D:" or os.environ.get("AUTH_MODE") != "oidc"
            or os.environ.get("XRML_CODEX_APPROVAL_FIXTURE") != "1"
            or os.environ.get("XRML_ENVIRONMENT") != "development"
            or os.environ.get("XRML_SHARED_DEPLOYMENT") != "0"
            or os.environ.get("APP_BASE_URL") != "http://127.0.0.1:8002"
            or os.environ.get("TOPIC_SCHEDULER_ENABLED") != "0"
            or os.environ.get("XRML_RUN_EXECUTOR_MODE") != "disabled"):
        raise ValueError("Synthetic approval browser runtime requires the isolated D-drive fixture")
    return root


def shared_runtime(root):
    fake = root / "fake-composed-codex"
    fake.mkdir(exist_ok=True)
    script = fake / "fake_cli.py"
    # Stable executable bytes survive restart. All source offers remain separate
    # immutable fixture records; this process only emits synthetic output.
    source = '''import json,sys
from pathlib import Path
root=Path(__file__).parent.parent
request=json.loads(sys.stdin.read())
args=sys.argv[1:]
if request['schema_name']=='JOURNAL_PROPOSAL_V1':
 packet=json.loads(request['prompt'])
 target=root/'fake-codex-app'
 proposal=dict(schema_version='JOURNAL_PROPOSAL_V1',packet_id=packet['packet_id'],title='Offline CLI e\\u0301',sections=[dict(kind='limitations',entries=[dict(kind='observation',text='Synthetic process test only.',citation_ids=[packet['references'][0]['citation_id']])])])
 text=json.dumps(proposal,ensure_ascii=False)
else:
 target=root/'fake-paper-codex'
 text=(target/'output.json').read_text(encoding='utf-8')
with (target/'launches.log').open('a') as marker: marker.write('launch\\n')
Path(args[args.index('--output-last-message')+1]).write_text(text,encoding='utf-8',newline='')
for event in [dict(type='thread.started',thread_id='synthetic-composed-thread'),dict(type='turn.started'),dict(type='item.completed',item=dict(type='agent_message',text=text)),dict(type='turn.completed',usage=dict(input_tokens=10,output_tokens=5))]:
 print(json.dumps(event,ensure_ascii=False))
'''
    if script.exists():
        if script.read_text(encoding="utf-8") != source:
            raise ValueError("Fixture launcher changed; preserve it for diagnosis")
    else:
        script.write_text(source, encoding="utf-8")
    for directory in (fake / "empty-home", root / "fake-paper-codex", root / "fake-codex-app"):
        directory.mkdir(exist_ok=True)
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    environment.update(CODEX_HOME=str(fake / "empty-home"), TEMP=str(fake), TMP=str(fake), PYTHONIOENCODING="utf-8")
    return CliRuntime((sys.executable, str(script)), environment, fake / "attempts")


def lookup(domain, contract):
    path = fixture_root() / f"approval-source-{domain}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get(contract["execution_id"])


def create_app():
    root = fixture_root()
    runtime = shared_runtime(root)
    configuration = CodexAppConfig.model_validate(dict(schema_version="CODEX_V2_APP_CONFIG_V1",
        runtime=dict(schema_version="CODEX_JOURNAL_PREVIEW_CONFIG_V2", launcher=list(runtime.launcher),
            environment=dict(runtime.environment), workspace=str(runtime.workspace), reasoning_effort="low", timeout_seconds=30),
        paper_model="fixture-paper-model", paper_max_output_tokens=2200, approvers=[USER],
        trusted_issuers=[dict(issuer_id=issuer, policy_version=policy) for issuer, policy in sorted(PAPER_TRUST | JOURNAL_TRUST)],
        input_origin="FIXTURE_SYNTHETIC"))
    return create_codex_app(configuration, paper_source=lambda contract: lookup("paper", contract),
        journal_source=lambda contract: lookup("journal", contract))


def session_cookie():
    fixture_root()
    settings = get_auth_settings()
    cookie = encode_product_session_cookie(ProductUser(user_id=USER, display_name="Synthetic approval owner",
        email=None, auth_source="oidc", is_admin=False), settings)
    return dict(name=settings.session_cookie_name, value=cookie)


def metadata_digest():
    fixture_root()
    with closing(connect_metadata_index(read_only=True)) as connection:
        return hashlib.sha256("\n".join(connection.iterdump()).encode("utf-8")).hexdigest()


def write_offer(domain, execution, value):
    path = fixture_root() / f"approval-source-{domain}.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if execution in saved:
        raise ValueError("Fixture offer must not be renewed or silently replaced")
    saved[execution] = value
    path.write_text(encoded(saved), encoding="utf-8")


def seed_paper_offer(topic, version, preparation, execution):
    root = fixture_root()
    scope = review_execution.scope(topic, version, preparation, ResourceActor.oidc_user(USER))
    saved = review_transport_history.detail(scope, execution)
    assert saved["current"]["state"] == "READY" and saved["contract"]["actor_id"] == USER
    now = datetime.now(timezone.utc)
    original = paper_grant(saved["contract"])
    grant = original.model_copy(update=dict(granted_at=now, expires_at=now + timedelta(minutes=5),
        proof=replace(original.proof, accepted_at=now, expires_at=now + timedelta(minutes=6))))
    record = review_authority.Record(grant=grant, issuer_id="fixture-paper-issuer", decision_ref="synthetic-source-e\u0301",
        registered_at=now, expected_sequence=saved["current"]["sequence"]).model_dump(mode="json")
    write_offer("paper", execution, record)
    (root / "fake-paper-codex" / "output.json").write_text(encoded(response(saved["contract"]["prepared"]).payload), encoding="utf-8")
    return dict(execution_id=execution, source="FIXTURE_SYNTHETIC", registered=False)


def seed_journal_offer(topic, job, execution):
    root = fixture_root()
    saved = journal_executions.detail(topic, job, execution)
    assert saved["current"]["state"] == "READY" and saved["contract"]["actor_id"] == USER
    provider = CodexJournalV2Provider(saved["contract"], runtime=shared_runtime(root),
        reasoning_effort="low", timeout_seconds=30)
    now = datetime.now(timezone.utc)
    original = journal_cli.approval(saved, provider)
    grant = original.model_copy(update=dict(actor_id=USER, granted_at=now, expires_at=now + timedelta(minutes=5),
        proof=replace(original.proof, accepted_at=now, expires_at=now + timedelta(minutes=6))))
    record = journal_authority.Record(topic_id=topic, job_id=job, execution_id=execution, grant=grant,
        binding=provider.binding, registered_by="fixture-only-issuer", decision_ref="synthetic-source-e\u0301",
        expected_sequence=saved["current"]["sequence"], registered_at=now).model_dump(mode="json")
    write_offer("journal", execution, dict(record=record, manifest=provider.manifest()))
    return dict(execution_id=execution, source="FIXTURE_SYNTHETIC", registered=False)

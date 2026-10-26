from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth.security import ProductUser, encode_product_session_cookie
from app.repositories.runs import load_run_record, save_run_record


def _oidc_env(**extra: str) -> dict[str, str]:
    env = {
        "AUTH_MODE": "oidc",
        "SESSION_SECRET": "synthetic-test-session-secret-0123456789abcdef",
        "APP_BASE_URL": "http://testserver",
        "OIDC_ISSUER": "https://issuer.example.com",
        "OIDC_CLIENT_ID": "client-id",
        "OIDC_CLIENT_SECRET": "client-secret",
    }
    env.update(extra)
    return env


def _set_product_session_cookie(client: TestClient, *, user_id: str, email: str | None = None) -> None:
    cookie = encode_product_session_cookie(
        ProductUser(
            user_id=user_id,
            display_name=user_id,
            email=email,
            auth_source="oidc",
        )
    )
    client.cookies.set("xrml_session", cookie)


def test_non_admin_oidc_user_cannot_access_legacy_run_admin_endpoints(isolated_run_repo, app_factory) -> None:
    with TestClient(app_factory(**_oidc_env(ADMIN_USER_IDS="admin-user"))) as client:
        _set_product_session_cookie(client, user_id="regular-user", email="regular@example.com")
        response = client.get("/admin/legacy-runs")

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required."


def test_non_admin_oidc_user_cannot_access_ownership_audit_endpoint(isolated_run_repo, app_factory) -> None:
    with TestClient(app_factory(**_oidc_env(ADMIN_USER_IDS="admin-user"))) as client:
        _set_product_session_cookie(client, user_id="regular-user", email="regular@example.com")
        response = client.get("/admin/ownership-audit")

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required."


def test_admin_can_list_unowned_legacy_runs(isolated_run_repo, app_factory) -> None:
    with TestClient(app_factory(**_oidc_env(ADMIN_USER_IDS="admin-user"))) as client:
        _set_product_session_cookie(client, user_id="admin-user", email="admin@example.com")
        response = client.get("/admin/legacy-runs", params={"status": "unowned"})

    assert response.status_code == 200
    payload = response.json()
    returned_run_ids = {item["run_id"] for item in payload["items"]}
    assert isolated_run_repo["run_id"] in returned_run_ids
    assert isolated_run_repo["compare_run_id"] in returned_run_ids
    assert payload["unowned_count"] >= 2


def test_preview_migration_is_non_mutating_and_lineage_aware(isolated_run_repo, app_factory) -> None:
    with TestClient(app_factory(**_oidc_env(ADMIN_USER_IDS="admin-user"))) as client:
        _set_product_session_cookie(client, user_id="admin-user")
        response = client.post(
            "/admin/legacy-runs/preview-migration",
            json={
                "run_ids": [isolated_run_repo["run_id"]],
                "mode": "claim_to_self",
                "include_lineage_family": True,
                "skip_already_owned": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["would_change_count"] == 2
    assert payload["changed_count"] == 0
    assert isolated_run_repo["compare_run_id"] in payload["included_lineage_run_ids"]
    assert load_run_record(isolated_run_repo["run_id"]).get("owner_user_id") is None
    assert load_run_record(isolated_run_repo["compare_run_id"]).get("owner_user_id") is None


def test_apply_claim_to_self_sets_ownership_metadata_and_restores_access(isolated_run_repo, app_factory) -> None:
    with TestClient(app_factory(**_oidc_env(ADMIN_USER_IDS="admin-user"))) as client:
        _set_product_session_cookie(client, user_id="admin-user")
        migration = client.post(
            "/admin/legacy-runs/apply-migration",
            json={
                "run_ids": [isolated_run_repo["run_id"]],
                "mode": "claim_to_self",
                "include_lineage_family": True,
                "skip_already_owned": True,
            },
        )

        compare = client.get(
            "/compare",
            params={
                "left_run_id": isolated_run_repo["run_id"],
                "right_run_id": isolated_run_repo["compare_run_id"],
            },
        )
        report = client.get(
            "/compare/report",
            params={
                "left_run_id": isolated_run_repo["run_id"],
                "right_run_id": isolated_run_repo["compare_run_id"],
            },
        )
        workspace = client.get(f"/runs/{isolated_run_repo['run_id']}/editing-sessions")
        history = client.get(f"/runs/{isolated_run_repo['run_id']}/outline-action-history")

    assert migration.status_code == 200
    assert migration.json()["changed_count"] == 2

    primary_run = load_run_record(isolated_run_repo["run_id"])
    compare_run = load_run_record(isolated_run_repo["compare_run_id"])
    assert primary_run["owner_user_id"] == "admin-user"
    assert primary_run["ownership_source"] == "admin_claim"
    assert primary_run["owner_assigned_by"] == "admin-user"
    assert compare_run["owner_user_id"] == "admin-user"
    assert compare_run["ownership_source"] == "lineage_claim"

    assert compare.status_code == 200
    assert report.status_code == 200
    assert workspace.status_code == 200
    assert history.status_code == 200


def test_assign_to_user_reassigns_when_explicitly_requested(isolated_run_repo, app_factory) -> None:
    with TestClient(app_factory(**_oidc_env(ADMIN_USER_IDS="admin-user"))) as client:
        _set_product_session_cookie(client, user_id="admin-user")
        claim_response = client.post(
            "/admin/legacy-runs/apply-migration",
            json={
                "run_ids": [isolated_run_repo["run_id"]],
                "mode": "claim_to_self",
                "include_lineage_family": False,
                "skip_already_owned": True,
            },
        )
        assign_response = client.post(
            "/admin/legacy-runs/apply-migration",
            json={
                "run_ids": [isolated_run_repo["run_id"]],
                "mode": "assign_to_user",
                "target_user_id": "analyst-user",
                "include_lineage_family": False,
                "skip_already_owned": False,
            },
        )

    assert claim_response.status_code == 200
    assert assign_response.status_code == 200
    run = load_run_record(isolated_run_repo["run_id"])
    assert run["owner_user_id"] == "analyst-user"
    assert run["ownership_source"] == "admin_assign"
    assert run["owner_assigned_by"] == "admin-user"


def test_assign_preview_honors_skip_already_owned(isolated_run_repo, app_factory) -> None:
    with TestClient(app_factory(**_oidc_env(ADMIN_USER_IDS="admin-user"))) as client:
        _set_product_session_cookie(client, user_id="admin-user")
        claim_response = client.post(
            "/admin/legacy-runs/apply-migration",
            json={
                "run_ids": [isolated_run_repo["run_id"]],
                "mode": "claim_to_self",
                "include_lineage_family": False,
                "skip_already_owned": True,
            },
        )
        preview = client.post(
            "/admin/legacy-runs/preview-migration",
            json={
                "run_ids": [isolated_run_repo["run_id"]],
                "mode": "assign_to_user",
                "target_user_id": "another-user",
                "include_lineage_family": False,
                "skip_already_owned": True,
            },
        )

    assert claim_response.status_code == 200
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["would_change_count"] == 0
    assert isolated_run_repo["run_id"] in payload["already_owned_run_ids"]
    assert payload["ownership_preview"][0]["action"] == "skip_already_owned"


def test_auth_disabled_mode_still_allows_ownership_audit(isolated_run_repo, app_factory) -> None:
    with TestClient(app_factory()) as client:
        response = client.get("/admin/ownership-audit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["auth_mode"] == "disabled"
    assert payload["total_runs"] == 2
    assert payload["unowned_run_count"] == 2


def test_admin_oidc_user_can_access_ownership_audit_for_unowned_fixture_data(isolated_run_repo, app_factory) -> None:
    with TestClient(app_factory(**_oidc_env(ADMIN_USER_IDS="admin-user"))) as client:
        _set_product_session_cookie(client, user_id="admin-user")
        response = client.get("/admin/ownership-audit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_runs"] == 2
    assert payload["unowned_run_count"] == 2
    assert payload["mixed_ownership_lineage_family_count"] == 0
    assert payload["owned_runs_missing_ownership_source_count"] == 0
    assert payload["lineage_family_state_counts"]["fully_unowned_family_count"] == 1


def test_audit_detects_mixed_ownership_and_missing_source(isolated_run_repo, app_factory) -> None:
    primary_run = load_run_record(isolated_run_repo["run_id"])
    primary_run["owner_user_id"] = "owner-a"
    primary_run["owner_assigned_at"] = "2026-04-09T09:00:00+00:00"
    primary_run["owner_assigned_by"] = "admin-user"
    primary_run["ownership_source"] = None
    save_run_record(primary_run)

    with TestClient(app_factory(**_oidc_env(ADMIN_USER_IDS="admin-user"))) as client:
        _set_product_session_cookie(client, user_id="admin-user")
        response = client.get("/admin/ownership-audit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["unowned_run_count"] == 1
    assert payload["mixed_ownership_lineage_family_count"] == 1
    assert payload["owned_runs_missing_ownership_source_count"] == 1
    bucket_types = {bucket["bucket_type"] for bucket in payload["problem_buckets"]}
    assert "mixed_ownership_lineage_families" in bucket_types
    assert "owned_runs_missing_ownership_source" in bucket_types


def test_audit_reports_incomplete_lineage_without_mutating_runs(isolated_run_repo, app_factory) -> None:
    compare_run_before = load_run_record(isolated_run_repo["compare_run_id"])
    compare_run = dict(compare_run_before)
    compare_run["lineage"] = dict(compare_run["lineage"])
    compare_run["lineage"]["parent_run_id"] = "missing-parent-run"
    save_run_record(compare_run)

    before_snapshot = load_run_record(isolated_run_repo["compare_run_id"])

    with TestClient(app_factory(**_oidc_env(ADMIN_USER_IDS="admin-user"))) as client:
        _set_product_session_cookie(client, user_id="admin-user")
        response = client.get("/admin/ownership-audit")

    after_snapshot = load_run_record(isolated_run_repo["compare_run_id"])

    assert response.status_code == 200
    payload = response.json()
    assert payload["lineage_incomplete_family_count"] == 1
    assert any("missing parent" in warning.lower() for warning in payload["warnings"])
    assert before_snapshot == after_snapshot

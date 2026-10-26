"""HTTP status and redaction contracts for registered shared-state errors."""
from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.repositories.shared_state import SharedStateBusy, SharedStateCorrupt


@pytest.mark.parametrize("error,status", [(SharedStateCorrupt, 503), (SharedStateBusy, 409)])
def test_shared_state_handler_preserves_status_and_redacted_body(error, status):
    app = create_app()

    @app.get("/_test/shared-state-error")
    def raise_state_error():
        try:
            raise OSError("private filesystem path and contents must remain hidden")
        except OSError as cause:
            raise error("settings") from cause

    response = TestClient(app).get("/_test/shared-state-error")
    assert response.status_code == status
    assert response.json() == {"detail": {"code": error.code, "state": "settings"}}

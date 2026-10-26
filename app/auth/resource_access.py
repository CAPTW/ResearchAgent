from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterator

from app.auth.config import AuthSettings, get_auth_settings
from app.auth.security_types import ProductUser


class ResourceKind(StrEnum):
    USER_OWNED = "user_owned"
    INHERITED_OWNER = "inherited_owner"
    SYSTEM_TEMPLATE = "system_template"
    APPLICATION_GLOBAL_ADMIN = "application_global_admin"


class ResourceAction(StrEnum):
    LIST = "list"
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    TRIGGER = "trigger"
    TEST_DELIVER = "test_deliver"
    GLOBAL_CONFIG_MUTATION = "global_config_mutation"


@dataclass(frozen=True)
class ResourceActor:
    """Server-derived application actor.

    ``subject`` is the stable opaque product-session user id. A system actor is
    deliberately scoped to exactly one stored owner and is never an admin.
    """

    subject: str | None
    auth_mode: str
    actor_type: str
    is_admin: bool = False
    scoped_owner_user_id: str | None = None

    @classmethod
    def oidc_user(cls, subject: str, *, is_admin: bool = False) -> ResourceActor:
        stable_subject = str(subject or "").strip()
        if not stable_subject:
            raise ResourceAccessDenied()
        return cls(
            subject=stable_subject,
            auth_mode="oidc",
            actor_type="user",
            is_admin=bool(is_admin),
        )


class ResourceAccessDenied(FileNotFoundError):
    code = "resource_access_denied"

    def __init__(self) -> None:
        # A masked 404 does not disclose whether the identifier exists.
        super().__init__("Resource not found.")


class ResourceOwnerMissing(ResourceAccessDenied):
    code = "resource_owner_missing"


class ResourceAdminRequired(PermissionError):
    code = "resource_admin_required"

    def __init__(self) -> None:
        super().__init__("Admin access required.")


_CURRENT_RESOURCE_ACTOR: ContextVar[ResourceActor | None] = ContextVar(
    "current_resource_actor",
    default=None,
)


def local_resource_actor() -> ResourceActor:
    return ResourceActor(
        subject="local-internal-user",
        auth_mode="disabled",
        actor_type="local",
        is_admin=True,
    )


def resource_actor_for_user(
    user: ProductUser,
    *,
    settings: AuthSettings | None = None,
) -> ResourceActor:
    resolved_settings = settings or get_auth_settings()
    if not resolved_settings.auth_enabled:
        return local_resource_actor()
    return ResourceActor.oidc_user(user.user_id, is_admin=user.is_admin)


def scoped_system_actor(
    owner_user_id: str | None,
    *,
    settings: AuthSettings | None = None,
) -> ResourceActor:
    """Create a worker actor limited to one already-persisted owner."""

    resolved_settings = settings or get_auth_settings()
    if not resolved_settings.auth_enabled:
        return local_resource_actor()
    stable_owner = str(owner_user_id or "").strip()
    if not stable_owner:
        raise ResourceOwnerMissing()
    return ResourceActor(
        subject=None,
        auth_mode="oidc",
        actor_type="system",
        is_admin=False,
        scoped_owner_user_id=stable_owner,
    )


def resolve_resource_actor(
    actor: ResourceActor | None = None,
    *,
    settings: AuthSettings | None = None,
) -> ResourceActor:
    if actor is not None:
        return actor
    contextual_actor = _CURRENT_RESOURCE_ACTOR.get()
    if contextual_actor is not None:
        return contextual_actor
    resolved_settings = settings or get_auth_settings()
    if not resolved_settings.auth_enabled:
        return local_resource_actor()
    raise ResourceAccessDenied()


@contextmanager
def resource_actor_context(actor: ResourceActor) -> Iterator[ResourceActor]:
    token = _CURRENT_RESOURCE_ACTOR.set(actor)
    try:
        yield actor
    finally:
        _CURRENT_RESOURCE_ACTOR.reset(token)


def owner_user_id_for_actor(actor: ResourceActor | None = None) -> str | None:
    resolved_actor = resolve_resource_actor(actor)
    if resolved_actor.auth_mode == "disabled":
        return None
    if resolved_actor.actor_type == "system":
        return resolved_actor.scoped_owner_user_id
    return resolved_actor.subject


def _actor_matches_owner(actor: ResourceActor, owner_user_id: str) -> bool:
    if actor.actor_type == "system":
        return actor.scoped_owner_user_id == owner_user_id
    return actor.subject == owner_user_id


def authorize_resource_access(
    *,
    actor: ResourceActor | None,
    resource_kind: ResourceKind | str,
    action: ResourceAction | str,
    owner_user_id: str | None = None,
) -> ResourceActor:
    resolved_actor = resolve_resource_actor(actor)
    resolved_kind = ResourceKind(resource_kind)
    resolved_action = ResourceAction(action)

    if resolved_actor.auth_mode == "disabled":
        return resolved_actor

    if resolved_kind == ResourceKind.APPLICATION_GLOBAL_ADMIN:
        if resolved_action == ResourceAction.READ:
            return resolved_actor
        if not resolved_actor.is_admin or resolved_actor.actor_type != "user":
            raise ResourceAdminRequired()
        return resolved_actor

    if resolved_kind == ResourceKind.SYSTEM_TEMPLATE:
        if resolved_action in {ResourceAction.LIST, ResourceAction.READ}:
            return resolved_actor
        if resolved_actor.actor_type == "user" and resolved_actor.is_admin:
            return resolved_actor
        raise ResourceAccessDenied()

    stable_owner = str(owner_user_id or "").strip()
    if not stable_owner:
        raise ResourceOwnerMissing()
    if not _actor_matches_owner(resolved_actor, stable_owner):
        raise ResourceAccessDenied()
    return resolved_actor


def resource_visible_to_actor(
    *,
    actor: ResourceActor | None,
    resource_kind: ResourceKind | str,
    owner_user_id: str | None = None,
) -> bool:
    try:
        authorize_resource_access(
            actor=actor,
            resource_kind=resource_kind,
            action=ResourceAction.LIST,
            owner_user_id=owner_user_id,
        )
    except (ResourceAccessDenied, ResourceAdminRequired):
        return False
    return True


_CLIENT_AUTHORITY_FIELDS = {
    "admin",
    "isadmin",
    "is_admin",
    "owner",
    "ownerid",
    "owner_id",
    "owneruserid",
    "owner_user_id",
    "principal",
    "role",
    "roles",
    "scope",
    "scopes",
    "subject",
    "user",
    "userid",
    "user_id",
}


def reject_client_authority_fields(payload: dict[str, Any]) -> None:
    """Reject authority-shaped fields instead of silently ignoring them."""

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).strip().replace("-", "_").lower()
                compact = normalized.replace("_", "")
                if (
                    normalized in _CLIENT_AUTHORITY_FIELDS
                    or compact in _CLIENT_AUTHORITY_FIELDS
                ):
                    raise ValueError(
                        "Client-supplied authorization fields are not allowed."
                    )
                _walk(nested)
        elif isinstance(value, list):
            for nested in value:
                _walk(nested)

    _walk(payload)


def require_global_admin(actor: ResourceActor | None = None) -> ResourceActor:
    return authorize_resource_access(
        actor=actor,
        resource_kind=ResourceKind.APPLICATION_GLOBAL_ADMIN,
        action=ResourceAction.GLOBAL_CONFIG_MUTATION,
    )

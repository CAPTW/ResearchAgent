from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductUser:
    user_id: str
    display_name: str
    email: str | None
    auth_source: str
    is_admin: bool = False

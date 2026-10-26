"""Run membership only; callers retain their resource authorization boundary."""
from typing import Any, Mapping


def run_matches_topic(run: Mapping[str, Any], topic: str, profile_id: str | None = None) -> bool:
    # Unscoped legacy name queries retain their existing exact-name semantics.
    if profile_id is None:
        return run.get("topic") == topic
    if not profile_id:
        return False
    recorded = run.get("topic_profile_id")
    if recorded is not None and not isinstance(recorded, str):
        return False  # A malformed binding must not fall back to a display name.
    if recorded and recorded.strip():
        return recorded.strip() == profile_id
    # No historical aliases or inferred migration for old unbound runs.
    return bool(topic) and run.get("topic") == topic

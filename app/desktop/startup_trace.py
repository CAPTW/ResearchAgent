"""Opt-in, fixed-vocabulary startup timing; no paths, environment or error text.

Keep this module limited to interpreter-provided modules so the first marker is
available before the sidecar imports its application or server dependencies.
The clock starts at the sidecar's Python entrypoint, not process creation or the
native host timer. A packaged interpreter's runtime hooks run before this marker.
"""
from __future__ import annotations

import os
import sys
import time


PHASES = frozenset({
    "python_entry", "sidecar_imports_complete", "configuration_started", "configuration_complete",
    "listener_bind_started", "listener_bound", "runtime_configured",
    "application_import_started", "application_import_complete",
    "application_factory_started", "application_factory_complete",
    "server_import_started", "server_import_complete", "server_starting",
    "lifespan_started", "lifespan_enter_complete", "lifespan_failed", "server_started", "readiness_confirmed",
})
_ENABLED = os.environ.get("XRML_STARTUP_TRACE") == "1"
_STARTED_NS = time.perf_counter_ns()


def startup_phase(phase: str) -> None:
    if not _ENABLED or phase not in PHASES:
        return
    stamp = time.perf_counter_ns()
    # phase is from the fixed ASCII vocabulary; everything else is numeric.
    # Avoid importing json before measuring the rest of the import phase.
    line = (
        '{"event":"startup_phase","schema_version":1,"phase":"' + phase + '",'
        f'"monotonic_ns":{stamp},"elapsed_ms":{(stamp - _STARTED_NS) / 1_000_000:.3f},'
        f'"pid":{os.getpid()}' + '}\n'
    )
    try:
        if sys.stderr is not None:
            sys.stderr.write(line)
            sys.stderr.flush()
    except (OSError, ValueError):
        # Diagnostics cannot change readiness or prevent application startup.
        pass

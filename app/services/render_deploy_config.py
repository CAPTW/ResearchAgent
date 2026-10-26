from __future__ import annotations

from pathlib import Path


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_inline_mapping(text: str) -> tuple[str, str]:
    key, _, value = text.partition(":")
    return key.strip(), _strip_quotes(value.strip())


def _parse_first_service(path: Path) -> dict[str, object] | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    in_services = False
    service_indent: int | None = None
    service: dict[str, object] = {}
    disk: dict[str, str] = {}
    env_vars: list[dict[str, str]] = []
    current_section: str | None = None
    current_env_var: dict[str, str] | None = None

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))

        if not in_services:
            if stripped == "services:":
                in_services = True
            continue

        if service_indent is None:
            if stripped.startswith("- "):
                service_indent = indent
                content = stripped[2:].strip()
                if ":" in content:
                    key, value = _parse_inline_mapping(content)
                    service[key] = value
                continue
            if indent <= 0:
                break
            continue

        if indent <= service_indent:
            break

        if stripped == "disk:":
            current_section = "disk"
            current_env_var = None
            continue

        if stripped == "envVars:":
            current_section = "envVars"
            current_env_var = None
            continue

        if current_section == "disk":
            if indent <= service_indent + 2:
                current_section = None
            elif ":" in stripped and not stripped.startswith("- "):
                key, value = _parse_inline_mapping(stripped)
                disk[key] = value
                continue

        if current_section == "envVars":
            if indent <= service_indent + 2:
                current_section = None
                current_env_var = None
            elif stripped.startswith("- "):
                current_env_var = {}
                env_vars.append(current_env_var)
                content = stripped[2:].strip()
                if ":" in content:
                    key, value = _parse_inline_mapping(content)
                    current_env_var[key] = value
                continue
            elif current_env_var is not None and ":" in stripped:
                key, value = _parse_inline_mapping(stripped)
                current_env_var[key] = value
                continue

        if current_section is None and ":" in stripped and not stripped.startswith("- "):
            key, value = _parse_inline_mapping(stripped)
            service[key] = value

    if not in_services or service_indent is None:
        return None

    if disk:
        service["disk"] = disk
    if env_vars:
        service["envVars"] = env_vars
    return service


def validate_render_blueprint(path: Path) -> list[str]:
    service = _parse_first_service(path)
    if service is None:
        return ["render.yaml must define at least one service."]

    errors: list[str] = []

    plan = str(service.get("plan") or "").strip().lower()
    if plan == "free":
        errors.append("Render staging must not use the free plan when file-backed storage remains authoritative.")

    if str(service.get("healthCheckPath") or "").strip() != "/health":
        errors.append("Render staging service must define healthCheckPath: /health.")

    disk = service.get("disk")
    if not isinstance(disk, dict):
        errors.append("Render staging service must define a persistent disk.")
    else:
        disk_name = str(disk.get("name") or "").strip()
        mount_path = str(disk.get("mountPath") or "").strip()
        size_gb_raw = str(disk.get("sizeGB") or "").strip()
        try:
            size_gb = int(size_gb_raw)
        except ValueError:
            size_gb = 0
        if not disk_name:
            errors.append("Render disk config must include a disk name.")
        if mount_path != "/opt/render/project/src/storage":
            errors.append("Render disk mountPath must be /opt/render/project/src/storage.")
        if size_gb < 1:
            errors.append("Render disk sizeGB must be an integer >= 1.")

    env_vars = service.get("envVars")
    if not isinstance(env_vars, list):
        return errors + ["Render staging service must define envVars."]

    env_map: dict[str, dict[str, str]] = {}
    for entry in env_vars:
        if isinstance(entry, dict) and entry.get("key"):
            env_map[str(entry["key"])] = entry

    expected_values = {
        "APP_BASE_URL": "https://xrml-research-agent-staging.onrender.com",
        "WEB_APP_BASE_PATH": "/app",
        "XRML_DATA_DIR": "/opt/render/project/src/storage",
        "XRML_ENVIRONMENT": "staging",
        "XRML_DEPLOYMENT_PROFILE": "render-single-service",
        "XRML_STRICT_ENV_VALIDATION": "true",
        "LOG_FORMAT": "json",
        "TOPIC_SCHEDULER_MODE": "embedded",
        "SCHEDULER_MODE": "single_node_file_backed",
    }
    for key, expected in expected_values.items():
        actual = str(env_map.get(key, {}).get("value") or "").strip()
        if actual != expected:
            errors.append(f"Render staging env {key} must be set to {expected}.")

    return errors

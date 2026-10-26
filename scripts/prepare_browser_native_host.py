"""Prepare reviewable native-host registration files; never install or register."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

REGISTRY = {
    "whale": r"Software\Naver\Naver Whale\NativeMessagingHosts",
    "chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
}
HOST = "org.xrml.paper_collector"


def safe_cmd_path(path: Path) -> str:
    value = str(path.resolve())
    if any(char in value for char in '\r\n"%!&|<>^'):
        raise ValueError("Path cannot be represented safely in the launcher")
    return value


def prepare(output: Path, extension_id: str, download_root: Path, browser: str,
            python: Path = Path(sys.executable)) -> dict[str, str]:
    if re.fullmatch(r"[a-p]{32}", extension_id) is None:
        raise ValueError("Use the exact extension ID shown by the selected browser")
    if browser not in REGISTRY or not download_root.is_absolute() or download_root.name != "XRML":
        raise ValueError("Select a browser and an absolute Downloads/XRML root")
    # Keep this preparation script standalone; the host independently validates
    # the selected browser and exact persisted origin before reading a message.
    scheme = "whale-extension" if browser == "whale" else "chrome-extension"
    origin = f"{scheme}://{extension_id}/"
    repo = Path(__file__).resolve().parents[1]
    executable, directory = safe_cmd_path(python), safe_cmd_path(repo)
    config_path = output.resolve() / "host-config.json"
    safe_cmd_path(config_path)
    output.mkdir(parents=True, exist_ok=False)
    config_path.write_text(json.dumps({"browser": browser, "extension_id": extension_id,
        "extension_origin": origin, "download_root": str(download_root)}, indent=2), encoding="utf-8")
    launch = output.resolve() / "launch.cmd"
    launch.write_text(f'@echo off\ncd /d "{directory}"\n"{executable}" -m app.desktop.browser_native_host --config "{config_path}" %*\n', encoding="utf-8")
    manifest_path = output.resolve() / (HOST + ".json")
    manifest_path.write_text(json.dumps({"name": HOST, "description": "XRML bounded paper collector",
        "path": str(launch), "type": "stdio", "allowed_origins": [origin]}, indent=2), encoding="utf-8")
    registry_value = str(manifest_path).replace("\\", "\\\\")
    registry_file = output.resolve() / "register-current-user.reg"
    registry_file.write_text('Windows Registry Editor Version 5.00\n\n'
        + f'[HKEY_CURRENT_USER\\{REGISTRY[browser]}\\{HOST}]\n@="{registry_value}"\n', encoding="utf-16")
    result = {"state": "PREPARED_NOT_REGISTERED", "browser": browser,
        "native_browser_validation": "UNRUN", "host": HOST,
        "registration_file": str(registry_file), "extension_id": extension_id}
    (output / "preparation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extension-id", required=True)
    parser.add_argument("--download-root", type=Path, required=True)
    parser.add_argument("--browser", choices=sorted(REGISTRY), required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output, args.extension_id, args.download_root, args.browser), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

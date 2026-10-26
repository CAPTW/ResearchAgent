"""Request-bound ACP v1 conversation; no timing-based completion inference.

https://agentclientprotocol.com/protocol/v1/prompt-turn
https://docs.x.ai/build/cli/headless-scripting
Only a narrow text/config subset is supported. Unsupported extensions fail closed.
"""
import json
import math
from pathlib import Path
import re
from typing import Any

from app.domain import prepared_review
from app.domain.journal_exchange import preserved_json
from app.services.providers.base import ProviderError, ProviderOutputError, ProviderRefusalError, ProviderReportedFailure, ProviderResult
from app.services.providers.codex_journal_plan import MAX_INPUT_BYTES, MAX_STREAM_BYTES

MAX_EVENTS = 4096


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def parse_line(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.endswith("\n") or "\n" in raw[:-1] or "\r" in raw:
        raise ValueError()
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    preserved_json(value).encode("utf-8", errors="strict")
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise ValueError()
    return value


def line(value: dict[str, Any]) -> str:
    return preserved_json(value) + "\n"


def _text(value: Any, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError()
    return value


def _identity(value: Any) -> str:
    value = _text(value, 200)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,199}", value):
        raise ValueError()
    return value


def _metadata(value: Any) -> None:
    if not isinstance(value, dict) or len(preserved_json(value).encode("utf-8")) > 64_000:
        raise ValueError()


def _models(value: Any, expected: str) -> None:
    if (not isinstance(value, dict) or set(value) != {"currentModelId", "availableModels"}
            or value["currentModelId"] != expected or not isinstance(value["availableModels"], list)
            or not 1 <= len(value["availableModels"]) <= 1000):
        raise ValueError()
    ids: set[str] = set()
    for model in value["availableModels"]:
        if (not isinstance(model, dict) or not {"modelId", "name"} <= set(model)
                or set(model) - {"modelId", "name", "description", "_meta"}):
            raise ValueError()
        identity = _identity(model["modelId"])
        if identity in ids:
            raise ValueError()
        ids.add(identity)
        _text(model["name"])
        if "description" in model:
            _text(model["description"], 4000)
        if "_meta" in model:
            _metadata(model["_meta"])
    if expected not in ids:
        raise ValueError()


def model_option(options: Any) -> str:
    if not isinstance(options, list) or not 1 <= len(options) <= 100:
        raise ValueError()
    seen: set[str] = set()
    current = None
    for option in options:
        if not isinstance(option, dict):
            raise ValueError()
        required = {"id", "name", "type", "currentValue", "options"}
        if not required <= set(option) or set(option) - required - {"description", "category", "_meta"}:
            raise ValueError()
        key = _text(option["id"])
        if key in seen or option["type"] != "select":
            raise ValueError()
        seen.add(key)
        if "_meta" in option:
            _metadata(option["_meta"])
        _text(option["name"])
        for extra in ("description", "category"):
            if extra in option:
                _text(option[extra], 4000)
        values = option["options"]
        if not isinstance(values, list) or not 1 <= len(values) <= 1000:
            raise ValueError()
        available: set[str] = set()
        for item in values:
            if (not isinstance(item, dict) or not {"value", "name"} <= set(item)
                    or set(item) - {"value", "name", "description", "_meta"}):
                raise ValueError()  # Grouped/custom selectors require another explicit profile.
            selected = _text(item["value"])
            _text(item["name"])
            if "_meta" in item:
                _metadata(item["_meta"])
            if "description" in item:
                _text(item["description"], 4000)
            if selected in available:
                raise ValueError()
            available.add(selected)
        if not isinstance(option["currentValue"], str) or option["currentValue"] not in available:
            raise ValueError()
        if key == "model":
            if option.get("category", "model") != "model":
                raise ValueError()
            current = _identity(option["currentValue"])
    if current is None:
        raise ValueError()
    return current


class DeniedRequest(ValueError):
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        super().__init__("paper_grok_client_request_denied")


class Conversation:
    def __init__(self, request_text: str, model: str, cwd: str) -> None:
        self.request_text, self.model, self.cwd = request_text, model, cwd
        self.session: str | None = None
        self.awaiting: int | None = None
        self.next_request: dict[str, Any] | None = self._request(1, "initialize", dict(
            protocolVersion=1, clientCapabilities={}, clientInfo=dict(name="xrml-prepared-paper", version="1")))
        self.transcript: list[dict[str, str]] = []
        self.text_parts: list[str] = []
        self.text_bytes = 0
        self.message_id: str | None = None
        self.message_id_seen = False
        self.configured = False
        self.terminal: dict[str, Any] | None = None
        self.deferred: list[dict[str, Any]] = []
        self.wire_bytes = 0
        self.server_bytes = 0

    @staticmethod
    def _request(number: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return dict(jsonrpc="2.0", id=number, method=method, params=params)

    def _append(self, direction: str, raw: str) -> None:
        count = len(raw.encode("utf-8"))
        self.wire_bytes += count
        if direction == "server":
            self.server_bytes += count
        if (len(self.transcript) >= MAX_EVENTS or self.server_bytes > MAX_STREAM_BYTES
                or self.wire_bytes > MAX_INPUT_BYTES + MAX_STREAM_BYTES + 64_000):
            raise ValueError()
        self.transcript.append(dict(direction=direction, raw_line=raw))

    def sent(self, raw: str) -> None:
        if self.next_request is None or raw != line(self.next_request) or self.awaiting is not None or self.terminal is not None:
            raise ValueError()
        self.awaiting = self.next_request["id"]
        self.next_request = None
        self._append("client", raw)

    def received(self, raw: str) -> None:
        value = parse_line(raw)
        if self.terminal is not None:
            raise ValueError()
        self._append("server", raw)
        if "method" in value:
            if "id" in value:
                request_id = value["id"]
                if type(request_id) not in {str, int} or isinstance(request_id, str) and not request_id:
                    raise ValueError()
                if value["method"] == "session/request_permission":
                    reply = dict(jsonrpc="2.0", id=request_id, result=dict(outcome=dict(outcome="cancelled")))
                else:
                    reply = dict(jsonrpc="2.0", id=request_id, error=dict(code=-32601, message="Unsupported client method"))
                raise DeniedRequest(reply)
            if self.session is None and self.awaiting == 3:
                if len(self.deferred) >= 32:
                    raise ValueError()
                self.deferred.append(value)
            else:
                self._notification(value)
            return
        if (set(value) not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"})
                or type(value["id"]) is not int or value["id"] != self.awaiting):
            raise ValueError()
        number = self.awaiting
        if "error" in value:
            error = value["error"]
            if (not isinstance(error, dict) or set(error) != {"code", "message"}
                    or type(error["code"]) is not int or not isinstance(error["message"], str)):
                raise ValueError()
            if number != 5:
                raise ValueError()
            self.terminal = dict(error=error)
            self.awaiting = None
            return
        result = value["result"]
        if not isinstance(result, dict):
            raise ValueError()
        if number == 1:
            self._initialize(result)
            self.next_request = self._request(2, "authenticate", dict(methodId="cached_token", _meta=dict(headless=True)))
        elif number == 2:
            if result != {}:
                raise ValueError()
            self.next_request = self._request(3, "session/new", dict(cwd=self.cwd, mcpServers=[]))
        elif number == 3:
            if not {"sessionId", "configOptions"} <= set(result) or set(result) - {"sessionId", "configOptions", "models", "_meta"}:
                raise ValueError()
            self.session = _identity(result["sessionId"])
            selected = model_option(result["configOptions"])
            if "models" in result:
                _models(result["models"], selected)
            if "_meta" in result:
                _metadata(result["_meta"])
                reported_cwd = result["_meta"].get("currentWorkingDirectory", self.cwd)
                if not isinstance(reported_cwd, str) or Path(reported_cwd) != Path(self.cwd):
                    raise ValueError()
            for notification in self.deferred:
                self._notification(notification)
            self.deferred.clear()
            options = next(item["options"] for item in result["configOptions"] if item["id"] == "model")
            if self.model not in {item["value"] for item in options}:
                raise ValueError()
            self.next_request = self._request(4, "session/set_config_option", dict(sessionId=self.session, configId="model", value=self.model))
        elif number == 4:
            if set(result) != {"configOptions"} or model_option(result["configOptions"]) != self.model:
                raise ValueError()
            self.configured = True
            self.next_request = self._request(5, "session/prompt", dict(sessionId=self.session,
                prompt=[dict(type="text", text=self.request_text)]))
        elif number == 5:
            if set(result) != {"stopReason"} or result["stopReason"] not in {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}:
                raise ValueError()
            self.terminal = result
        else:
            raise ValueError()
        self.awaiting = None

    @staticmethod
    def _initialize(result: dict[str, Any]) -> None:
        required = {"protocolVersion", "agentCapabilities", "authMethods"}
        if (not required <= set(result) or set(result) - required - {"agentInfo"}
                or type(result["protocolVersion"]) is not int or result["protocolVersion"] != 1
                or not isinstance(result["agentCapabilities"], dict)):
            raise ValueError()
        # Capabilities are declarative; this client advertises no fs/terminal/auth UI.
        if "agentInfo" in result:
            info = result["agentInfo"]
            if not isinstance(info, dict) or not {"name", "version"} <= set(info) or set(info) - {"name", "version", "title"}:
                raise ValueError()
            for item in info.values():
                _text(item)
        methods = result["authMethods"]
        if not isinstance(methods, list) or not 1 <= len(methods) <= 32:
            raise ValueError()
        ids: set[str] = set()
        for item in methods:
            if not isinstance(item, dict) or not {"id", "name"} <= set(item) or set(item) - {"id", "name", "description"}:
                raise ValueError()
            identity = _text(item["id"])
            if identity in ids:
                raise ValueError()
            ids.add(identity)
            for field in item.values():
                _text(field, 4000)
        if "cached_token" not in ids:
            raise ValueError()

    def _notification(self, value: dict[str, Any]) -> None:
        # Grok's ExtNotification("x.ai/mcp_initialized") is prefixed by the
        # pinned ACP 0.10.4 sender, not by the ExtNotification constructor:
        # github.com/agentclientprotocol/rust-sdk/blob/v0.10.4/src/agent-client-protocol/src/lib.rs#L536-L541
        if value.get("method") == "_x.ai/mcp_initialized":
            params = value.get("params")
            if (set(value) != {"jsonrpc", "method", "params"} or not isinstance(params, dict)
                    or set(params) != {"sessionId", "mcpToolCount", "elapsedMs"}
                    or self.session is None or params["sessionId"] != self.session
                    or type(params["mcpToolCount"]) is not int or params["mcpToolCount"] != 0
                    or type(params["elapsedMs"]) not in {int, float}
                    or not math.isfinite(params["elapsedMs"]) or params["elapsedMs"] < 0):
                raise ValueError()
            return
        if (set(value) != {"jsonrpc", "method", "params"} or value["method"] != "session/update"
                or not isinstance(value["params"], dict) or not {"sessionId", "update"} <= set(value["params"])
                or set(value["params"]) - {"sessionId", "update", "_meta"}
                or self.session is None or value["params"]["sessionId"] != self.session):
            raise ValueError()
        if "_meta" in value["params"]:
            _metadata(value["params"]["_meta"])
        update = value["params"]["update"]
        if not isinstance(update, dict):
            raise ValueError()
        kind = update.get("sessionUpdate")
        if kind == "available_commands_update":
            if (not {"sessionUpdate", "availableCommands"} <= set(update)
                    or set(update) - {"sessionUpdate", "availableCommands", "_meta"}
                    or not isinstance(update["availableCommands"], list) or len(update["availableCommands"]) > 200):
                raise ValueError()
            if "_meta" in update:
                _metadata(update["_meta"])
            for command in update["availableCommands"]:
                if (not isinstance(command, dict) or not {"name", "description"} <= set(command)
                        or set(command) - {"name", "description", "input", "_meta"}):
                    raise ValueError()
                _text(command["name"])
                _text(command["description"], 4000)
                if "input" in command and command["input"] is not None:
                    if not isinstance(command["input"], dict) or set(command["input"]) != {"hint"}:
                        raise ValueError()
                    _text(command["input"]["hint"], 4000)
                if "_meta" in command:
                    _metadata(command["_meta"])
        elif kind == "config_option_update":
            if set(update) != {"sessionUpdate", "configOptions"}:
                raise ValueError()
            selected = model_option(update["configOptions"])
            if self.configured and selected != self.model:
                raise ValueError()
        elif kind in {"agent_message_chunk", "agent_thought_chunk"}:
            if (self.awaiting != 5 or not self.configured or not {"sessionUpdate", "content"} <= set(update)
                    or set(update) - {"sessionUpdate", "content", "messageId"}):
                raise ValueError()
            content = update["content"]
            if not isinstance(content, dict) or set(content) != {"type", "text"} or content["type"] != "text" or not isinstance(content["text"], str):
                raise ValueError()
            if "messageId" in update:
                _identity(update["messageId"])
            if kind == "agent_message_chunk":
                identifier = update.get("messageId")
                if self.message_id_seen and identifier != self.message_id:
                    raise ValueError()  # Multiple assistant messages are not one JSON review.
                self.message_id_seen = True
                self.message_id = identifier
                self.text_bytes += len(content["text"].encode("utf-8"))
                if self.text_bytes > prepared_review.MAX_OUTPUT_BYTES:
                    raise ValueError()
                self.text_parts.append(content["text"])
        elif kind == "usage_update":
            if (self.awaiting != 5 or not {"sessionUpdate", "used", "size"} <= set(update)
                    or set(update) - {"sessionUpdate", "used", "size", "cost"}
                    or any(type(update[key]) is not int or update[key] < 0 for key in ("used", "size"))):
                raise ValueError()
            if "cost" in update:
                cost = update["cost"]
                if (not isinstance(cost, dict) or set(cost) != {"amount", "currency"}
                        or type(cost["amount"]) not in {int, float} or not math.isfinite(cost["amount"]) or cost["amount"] < 0
                        or not isinstance(cost["currency"], str) or not re.fullmatch("[A-Z]{3}", cost["currency"])):
                    raise ValueError()
        else:
            raise ValueError()  # Includes tool, subagent, permission and extension activity.

    def response(self) -> ProviderResult:
        if self.terminal is None or self.awaiting is not None or self.next_request is not None or self.session is None:
            raise ProviderError("paper_grok_completion_outcome_unknown")
        if "error" in self.terminal:
            # Internal JSON-RPC errors do not establish the outcome of a model
            # request that may already have been sent. Do not unlock a retry.
            raise ProviderError("paper_grok_rpc_outcome_unknown")
        reason = self.terminal["stopReason"]
        if reason == "refusal":
            raise ProviderRefusalError("paper_grok_provider_refused")
        if reason != "end_turn":
            raise ProviderReportedFailure("cancelled" if reason == "cancelled" else "incomplete")
        raw = "".join(self.text_parts)
        try:
            payload = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(payload, dict):
                raise ValueError()
            preserved_json(payload).encode("utf-8", errors="strict")
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise ProviderOutputError("paper_grok_output_invalid") from None
        # ACP usage_update is context/cumulative cost, not input/output token usage.
        return ProviderResult("grok_cli", "not_reported", self.session, payload, usage={}, raw_text=raw)


def replay(transcript: Any, *, request_text: str, model: str, cwd: str) -> ProviderResult:
    try:
        if not isinstance(transcript, list) or not 1 <= len(transcript) <= MAX_EVENTS:
            raise ValueError()
        conversation = Conversation(request_text, model, cwd)
        for entry in transcript:
            if not isinstance(entry, dict) or set(entry) != {"direction", "raw_line"}:
                raise ValueError()
            if entry["direction"] == "client":
                conversation.sent(entry["raw_line"])
            elif entry["direction"] == "server":
                conversation.received(entry["raw_line"])
            else:
                raise ValueError()
        return conversation.response()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise ProviderError("paper_grok_completion_outcome_unknown") from None

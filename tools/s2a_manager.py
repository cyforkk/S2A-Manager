#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
import json
import os
import re
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


def get_runtime_base_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[1]


def load_app_version(default: str = "v0.1.0") -> str:
    candidates = [
        get_runtime_base_dir() / "VERSION",
        Path(sys.executable).resolve().parent / "VERSION" if getattr(sys, "frozen", False) else None,
        Path(__file__).resolve().parents[1] / "VERSION",
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return default


DEFAULT_BASE_URL = os.environ.get("SUB2API_BASE_URL", "http://127.0.0.1:8080")
APP_NAME = "S2A Manager"
APP_VERSION = load_app_version("v0.1.0")
APP_GITHUB_REPO = "GALIAIS/S2A-Manager"
APP_RELEASES_URL = f"https://github.com/{APP_GITHUB_REPO}/releases"
APP_LATEST_RELEASE_API = f"https://api.github.com/repos/{APP_GITHUB_REPO}/releases/latest"
GUI_CONFIG_DIRNAME = "S2A Manager"
GUI_CONFIG_FILENAME = "gui-config.json"
ENV_ADMIN_API_KEY = "SUB2API_ADMIN_API_KEY"
ENV_BEARER_TOKEN = "SUB2API_BEARER_TOKEN"
ENV_LOGIN_EMAIL = "SUB2API_LOGIN_EMAIL"
ENV_LOGIN_PASSWORD = "SUB2API_LOGIN_PASSWORD"
ENV_TURNSTILE_TOKEN = "SUB2API_TURNSTILE_TOKEN"
ENV_TOTP_CODE = "SUB2API_TOTP_CODE"
ENV_REFRESH_TOKEN = "SUB2API_REFRESH_TOKEN"
ENV_USER_AGENT = "SUB2API_USER_AGENT"
DEFAULT_USER_AGENT = os.environ.get("SUB2API_USER_AGENT", "S2A-Manager/1.0")
DATA_TYPE = "sub2api-data"
LEGACY_DATA_TYPE = "sub2api-bundle"
DATA_VERSION = 1
VALID_PROXY_PROTOCOLS = {"http", "https", "socks5", "socks5h"}
VALID_PROXY_STATUSES = {"active", "inactive"}
VALID_ACCOUNT_IMPORT_TYPES = {"oauth", "setup-token", "apikey", "upstream"}
KNOWN_ACCOUNT_PLATFORMS = {"anthropic", "openai", "gemini", "antigravity", "sora"}
EXPORT_IDS_BATCH_SIZE = 200
ADMIN_LIST_PAGE_SIZE_CAP = 100
DEFAULT_SYNC_CONCURRENCY = 4
DEFAULT_DETECTION_CONCURRENCY = 20
DEFAULT_DELETE_CONCURRENCY = 8
REQUEST_RETRY_LIMIT = 3
ACCOUNT_LIST_GROUP_UNGROUPED = "ungrouped"
CREDENTIAL_FALLBACK_KEYS = (
    "token",
    "access_token",
    "refresh_token",
    "session_token",
    "id_token",
    "api_key",
    "apikey",
    "setup_token",
    "cookie",
    "cookies",
    "email",
    "password",
    "chatgpt_account_id",
    "chatgpt_user_id",
    "organization_id",
    "project_id",
    "headers",
    "endpoint",
    "base_url",
    "region",
)
REAUTH_TEXT_MARKERS = (
    "401",
    "unauth",
    "unauthorized",
    "unauthenticated",
    "authentication_error",
    "invalid token",
    "token invalid",
    "token invalidated",
    "token expired",
    "token revoked",
    "invalid_grant",
    "invalid_session",
    "missing_access_token",
    "missing access token",
    "no access token available",
    "access token has expired",
    "session expired",
    "login required",
    "reauth",
    "re-auth",
    "please reauthorize",
    "please re-auth",
)
FORBIDDEN_TEXT_MARKERS = (
    "403",
    "forbidden",
    "permission_error",
    "access forbidden",
    "validation required",
    "needs verify",
    "need verify",
    "account violation",
    "lack permissions",
    "suspended",
    "blocked by cloudflare challenge",
)
QUOTA_TEXT_MARKERS = (
    "usage_limit_reached",
    "insufficient_quota",
    "quota",
    "quota_exhausted",
    "rate limit",
    "rate_limited",
    "rate limited",
    "rate_limit_exceeded",
    "resource_exhausted",
    "resource has been exhausted",
    "credits exhausted",
    "429",
)


class CLIError(RuntimeError):
    pass


class TaskCancelled(CLIError):
    pass


class APIError(CLIError):
    def __init__(
        self,
        status: int,
        code: int | str | None,
        message: str,
        details: Any = None,
    ) -> None:
        self.status = status
        self.code = code
        self.details = details
        super().__init__(f"HTTP {status} | code={code} | {message}")


@dataclass
class CommandResult:
    payload: Any
    failed: bool = False


@dataclass
class LoginOptions:
    email: str | None
    password: str | None
    turnstile_token: str | None
    totp_code: str | None


@dataclass
class ResolvedAuth:
    admin_api_key: str | None
    bearer_token: str | None
    refresh_token: str | None
    login: LoginOptions


@dataclass
class GUIConfig:
    base_url: str
    admin_api_key: str
    sync_concurrency: int = DEFAULT_SYNC_CONCURRENCY
    detection_concurrency: int = DEFAULT_DETECTION_CONCURRENCY
    delete_concurrency: int = DEFAULT_DELETE_CONCURRENCY
    bulk_page_size: int = 100
    bulk_batch_size: int = 100
    window_width: int | None = None
    window_height: int | None = None


def normalize_api_base(url: str) -> str:
    value = url.strip().rstrip("/")
    if not value:
        raise CLIError("`--base-url` 不能为空")
    if value.endswith("/api/v1"):
        return value
    if "/api/v1/" in value:
        return value.split("/api/v1/", 1)[0] + "/api/v1"
    return value + "/api/v1"


def normalize_version_tag(value: str) -> tuple[int, ...]:
    text = value.strip().lower()
    if text.startswith("v"):
        text = text[1:]
    parts: list[int] = []
    for token in re.split(r"[.\-_]", text):
        if not token:
            continue
        if token.isdigit():
            parts.append(int(token))
            continue
        match = re.match(r"(\d+)", token)
        if match:
            parts.append(int(match.group(1)))
        else:
            break
    return tuple(parts or [0])


def non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def get_gui_config_path() -> Path:
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / GUI_CONFIG_DIRNAME / GUI_CONFIG_FILENAME
    return Path.home() / f".{GUI_CONFIG_DIRNAME}.json"


def coerce_optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def load_gui_config(*, default_base_url: str, default_admin_api_key: str) -> GUIConfig:
    config = GUIConfig(base_url=default_base_url, admin_api_key=default_admin_api_key)
    path = get_gui_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError):
        return config

    if not isinstance(raw, dict):
        return config

    if "base_url" in raw and isinstance(raw.get("base_url"), str):
        config.base_url = raw["base_url"].strip()
    if "admin_api_key" in raw and isinstance(raw.get("admin_api_key"), str):
        config.admin_api_key = raw["admin_api_key"].strip()
    saved_sync_concurrency = coerce_optional_positive_int(raw.get("sync_concurrency"))
    if saved_sync_concurrency is not None:
        config.sync_concurrency = saved_sync_concurrency
    saved_detection_concurrency = coerce_optional_positive_int(raw.get("detection_concurrency"))
    if saved_detection_concurrency is not None:
        config.detection_concurrency = saved_detection_concurrency
    saved_delete_concurrency = coerce_optional_positive_int(raw.get("delete_concurrency"))
    if saved_delete_concurrency is not None:
        config.delete_concurrency = saved_delete_concurrency
    saved_bulk_page_size = coerce_optional_positive_int(raw.get("bulk_page_size"))
    if saved_bulk_page_size is not None:
        config.bulk_page_size = saved_bulk_page_size
    saved_bulk_batch_size = coerce_optional_positive_int(raw.get("bulk_batch_size"))
    if saved_bulk_batch_size is not None:
        config.bulk_batch_size = saved_bulk_batch_size
    config.window_width = coerce_optional_positive_int(raw.get("window_width"))
    config.window_height = coerce_optional_positive_int(raw.get("window_height"))
    return config


def save_gui_config(config: GUIConfig) -> Path:
    path = get_gui_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise CLIError(f"保存本地配置失败: {path}: {exc}") from exc
    return path


def contains_any_marker(value: str, markers: tuple[str, ...]) -> bool:
    normalized = " ".join(str(value or "").strip().lower().split())
    if not normalized:
        return False
    return any(marker in normalized for marker in markers)


def collect_text_fragments(value: Any) -> list[str]:
    fragments: list[str] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if current is None:
            continue
        if isinstance(current, str):
            text = current.strip()
            if text:
                fragments.append(text)
            continue
        if isinstance(current, dict):
            for nested in reversed(list(current.values())):
                stack.append(nested)
            continue
        if isinstance(current, list):
            for nested in reversed(current):
                stack.append(nested)
    return fragments


def summarize_text_fragments(texts: list[str], *, fallback: str = "", max_items: int = 3, max_length: int = 240) -> str:
    unique_texts: list[str] = []
    seen: set[str] = set()
    for text in texts:
        normalized = " ".join(text.split())
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_texts.append(normalized)
        if len(unique_texts) >= max_items:
            break
    summary = " | ".join(unique_texts)
    if not summary:
        summary = " ".join(fallback.split())
    if len(summary) > max_length:
        summary = summary[: max_length - 3].rstrip() + "..."
    return summary


def iter_sse_json_events(raw_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal data_lines
        if not data_lines:
            return
        payload = "\n".join(data_lines).strip()
        data_lines = []
        if not payload or payload == "[DONE]":
            return
        try:
            parsed = json.loads(payload)
        except JSONDecodeError:
            return
        if isinstance(parsed, dict):
            events.append(parsed)

    for raw_line in raw_text.splitlines():
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith("data:"):
            data_lines.append(stripped[5:].strip())
            continue
        if stripped.startswith("event:") or stripped.startswith(":"):
            continue
        if stripped.startswith("{") or stripped.startswith("["):
            data_lines.append(stripped)
    flush()
    return events


def analyze_account_test_result(*, raw_text: str | None = None, error_text: str | None = None) -> dict[str, Any]:
    events = iter_sse_json_events(raw_text or "")
    source_texts: list[str] = []
    error_texts: list[str] = []
    saw_success = False
    saw_error = False

    for event in events:
        event_type = str(event.get("type") or "").strip().lower()
        event_texts: list[str] = []
        for key in ("text", "message", "error", "detail", "data", "response"):
            event_texts.extend(collect_text_fragments(event.get(key)))

        if event_type == "test_complete":
            if event.get("success") is True:
                saw_success = True
            else:
                saw_error = True
                error_texts.extend(event_texts)
            continue

        if event_type == "error":
            saw_error = True
            error_texts.extend(event_texts)
            continue

        if event_type in {"sora_test_result", "test_result"}:
            status_text = str(event.get("status") or "").strip().lower()
            if status_text in {"failed", "error"}:
                saw_error = True
                error_texts.extend(event_texts)
            else:
                source_texts.extend(event_texts)
            continue

        if any(flag in event_type for flag in ("error", "failed", "incomplete", "cancelled", "canceled")):
            saw_error = True
            error_texts.extend(event_texts)
            continue

        source_texts.extend(event_texts)

    if raw_text:
        normalized_raw = raw_text.lower()
        source_texts.append(raw_text)
        if '"type":"error"' in normalized_raw or '"type": "error"' in normalized_raw or '"success":false' in normalized_raw:
            saw_error = True
        if '"success":true' in normalized_raw or '"success": true' in normalized_raw:
            saw_success = True

    if error_text:
        saw_error = True
        error_texts.extend(collect_text_fragments(error_text))
        source_texts.extend(collect_text_fragments(error_text))

    combined_text = " | ".join([*error_texts, *source_texts]).lower()
    summary = summarize_text_fragments([*error_texts, *source_texts], fallback=raw_text or error_text or "")
    return {
        "ok": saw_success and not saw_error,
        "has_error": saw_error,
        "has_success": saw_success,
        "event_count": len(events),
        "is_401": contains_any_marker(combined_text, REAUTH_TEXT_MARKERS),
        "is_403": contains_any_marker(combined_text, FORBIDDEN_TEXT_MARKERS),
        "is_quota": contains_any_marker(combined_text, QUOTA_TEXT_MARKERS),
        "summary": summary,
    }


def configure_tk_runtime() -> None:
    if not getattr(sys, "frozen", False):
        return
    meipass = getattr(sys, "_MEIPASS", "")
    if not meipass:
        return
    base = Path(meipass)
    tcl_dir = base / "_tcl_data"
    tk_dir = base / "_tk_data"
    if not tcl_dir.is_dir():
        tcl_dir = base / "tcl" / "tcl8.6"
    if not tk_dir.is_dir():
        tk_dir = base / "tcl" / "tk8.6"
    if tcl_dir.is_dir():
        os.environ.setdefault("TCL_LIBRARY", str(tcl_dir))
    if tk_dir.is_dir():
        os.environ.setdefault("TK_LIBRARY", str(tk_dir))


def read_json(path_str: str) -> Any:
    try:
        if path_str == "-":
            raw = sys.stdin.read()
            source = "<stdin>"
        else:
            raw = Path(path_str).read_text(encoding="utf-8")
            source = path_str
    except OSError as exc:
        raise CLIError(f"读取文件失败: {path_str}: {exc}") from exc

    try:
        return json.loads(raw)
    except JSONDecodeError as exc:
        raise CLIError(f"JSON 解析失败: {source}: {exc}") from exc


def write_json(payload: Any, stream: Any = None) -> None:
    if stream is None:
        stream = sys.stdout
    stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def unique_ids(values: list[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        if value <= 0:
            raise CLIError(f"ID 必须为正整数: {value}")
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def ensure_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CLIError(f"{label} 必须是 JSON 对象")
    return dict(value)


def ensure_list_payload(value: Any, key: str, label: str) -> dict[str, Any]:
    if isinstance(value, list):
        return {key: value}
    if isinstance(value, dict):
        payload = dict(value)
        items = payload.get(key)
        if not isinstance(items, list):
            raise CLIError(f"{label} 必须包含数组字段 `{key}`")
        return payload
    raise CLIError(f"{label} 必须是 JSON 数组，或包含 `{key}` 的 JSON 对象")


def json_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def json_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def utc_now_rfc3339() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_proxy_key(protocol: str, host: str, port: int, username: str = "", password: str = "") -> str:
    return f"{protocol.strip()}|{host.strip()}|{port}|{username.strip()}|{password.strip()}"


def normalize_proxy_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized == "disabled":
        return "inactive"
    return normalized


def format_validation_message(result: dict[str, Any], *, source_label: str | None = None) -> str:
    errors = [str(item) for item in result.get("errors") or []]
    if not errors:
        warnings = result.get("warnings") or []
        if warnings:
            prefix = f"{source_label}：" if source_label else ""
            return prefix + f"发现 {len(warnings)} 条提醒，请先查看检查结果。"
        return f"{source_label or '当前文件'} 格式检查通过"

    preview = "；".join(errors[:3])
    if len(errors) > 3:
        preview += f"；其余 {len(errors) - 3} 条请点“先检查文件格式”查看"
    prefix = f"{source_label}：" if source_label else ""
    return prefix + preview


def normalize_optional_string_field(
    value: Any,
    *,
    location: str,
    field: str,
    errors: list[str],
    allow_none: bool = True,
) -> str | None:
    if value is None:
        if allow_none:
            return None
        errors.append(f"{location}.{field} 必须是字符串")
        return None
    if not isinstance(value, str):
        errors.append(f"{location}.{field} 必须是字符串")
        return None
    return value.strip()


def derive_credentials_from_raw_account(item: dict[str, Any], location: str, warnings: list[str]) -> dict[str, Any] | None:
    if "credentials" in item:
        raw_credentials = item.get("credentials")
        if not isinstance(raw_credentials, dict) or not raw_credentials:
            return None
        return dict(raw_credentials)

    if "credential" in item:
        raw_credentials = item.get("credential")
        if isinstance(raw_credentials, dict) and raw_credentials:
            warnings.append(f"{location}: 已自动把 `credential` 改写成 `credentials`")
            return dict(raw_credentials)
        return None

    credentials: dict[str, Any] = {}
    for key in CREDENTIAL_FALLBACK_KEYS:
        if key not in item:
            continue
        value = item.get(key)
        if value is None:
            continue
        target_key = "api_key" if key == "apikey" else key
        credentials[target_key] = value

    if credentials:
        warnings.append(f"{location}: 未找到 `credentials`，已自动从顶层常见字段生成")
        return credentials
    return None


def validate_data_proxy_item(item: Any, index: int) -> dict[str, Any]:
    location = f"proxies[{index}]"
    errors: list[str] = []
    warnings: list[str] = []
    normalized: dict[str, Any] = {}

    if not isinstance(item, dict):
        return {"errors": [f"{location} 必须是 JSON 对象"], "warnings": [], "normalized": None, "effective_proxy_key": None}

    protocol = normalize_optional_string_field(item.get("protocol"), location=location, field="protocol", errors=errors, allow_none=False)
    host = normalize_optional_string_field(item.get("host"), location=location, field="host", errors=errors, allow_none=False)
    port = json_int(item.get("port"))
    if port is None:
        errors.append(f"{location}.port 必须是整数")
    elif port <= 0 or port > 65535:
        errors.append(f"{location}.port 必须在 1 到 65535 之间")

    normalized_protocol = ""
    if protocol is not None:
        normalized_protocol = protocol.lower()
        if normalized_protocol not in VALID_PROXY_PROTOCOLS:
            errors.append(f"{location}.protocol 只支持 {', '.join(sorted(VALID_PROXY_PROTOCOLS))}")
        normalized["protocol"] = normalized_protocol

    if host is not None:
        if not host:
            errors.append(f"{location}.host 不能为空")
        normalized["host"] = host

    if port is not None:
        normalized["port"] = port

    for field in ("name", "username", "password"):
        if field not in item:
            continue
        text = normalize_optional_string_field(item.get(field), location=location, field=field, errors=errors, allow_none=True)
        if text is not None:
            normalized[field] = text

    effective_proxy_key: str | None = None
    raw_proxy_key = item.get("proxy_key")
    if raw_proxy_key is not None:
        proxy_key = normalize_optional_string_field(raw_proxy_key, location=location, field="proxy_key", errors=errors, allow_none=True)
        if proxy_key:
            effective_proxy_key = proxy_key
            normalized["proxy_key"] = proxy_key

    raw_status = item.get("status")
    if raw_status is not None:
        status = normalize_optional_string_field(raw_status, location=location, field="status", errors=errors, allow_none=True)
        if status:
            normalized_status = normalize_proxy_status(status)
            if normalized_status not in VALID_PROXY_STATUSES:
                errors.append(f"{location}.status 只支持 active / inactive")
            else:
                if normalized_status != status.strip().lower():
                    warnings.append(f"{location}.status 已规范化为 `{normalized_status}`")
                normalized["status"] = normalized_status

    if effective_proxy_key is None and protocol and host and port:
        effective_proxy_key = build_proxy_key(
            normalized_protocol or protocol.strip().lower(),
            host,
            port,
            str(normalized.get("username") or ""),
            str(normalized.get("password") or ""),
        )
        normalized["proxy_key"] = effective_proxy_key
        warnings.append(f"{location}: 未提供 `proxy_key`，已按协议/地址/端口自动生成")

    return {
        "errors": errors,
        "warnings": warnings,
        "normalized": normalized if not errors else None,
        "effective_proxy_key": effective_proxy_key,
    }


def validate_data_account_item(item: Any, index: int, file_proxy_keys: set[str]) -> dict[str, Any]:
    location = f"accounts[{index}]"
    errors: list[str] = []
    warnings: list[str] = []
    normalized: dict[str, Any] = {}

    if not isinstance(item, dict):
        return {"errors": [f"{location} 必须是 JSON 对象"], "warnings": [], "normalized": None}

    name = normalize_optional_string_field(item.get("name"), location=location, field="name", errors=errors, allow_none=False)
    if name is not None:
        if not name:
            errors.append(f"{location}.name 不能为空")
        normalized["name"] = name

    platform = normalize_optional_string_field(item.get("platform"), location=location, field="platform", errors=errors, allow_none=False)
    if platform is not None:
        normalized_platform = platform.lower()
        if not normalized_platform:
            errors.append(f"{location}.platform 不能为空")
        else:
            if normalized_platform not in KNOWN_ACCOUNT_PLATFORMS:
                warnings.append(f"{location}.platform=`{normalized_platform}` 不是当前程序已知平台，导入时将交由服务端最终判断")
            normalized["platform"] = normalized_platform

    account_type = normalize_optional_string_field(item.get("type"), location=location, field="type", errors=errors, allow_none=False)
    if account_type is not None:
        normalized_type = account_type.lower()
        if not normalized_type:
            errors.append(f"{location}.type 不能为空")
        elif normalized_type not in VALID_ACCOUNT_IMPORT_TYPES:
            allowed = ", ".join(sorted(VALID_ACCOUNT_IMPORT_TYPES))
            errors.append(f"{location}.type 只支持 {allowed}；当前导入接口不接受 bedrock")
        else:
            normalized["type"] = normalized_type

    credentials = derive_credentials_from_raw_account(item, location, warnings)
    if not isinstance(credentials, dict) or not credentials:
        errors.append(f"{location}.credentials 必须是非空 JSON 对象")
    else:
        normalized["credentials"] = credentials

    if "notes" in item:
        notes = normalize_optional_string_field(item.get("notes"), location=location, field="notes", errors=errors, allow_none=True)
        normalized["notes"] = notes

    if "extra" in item:
        extra = item.get("extra")
        if extra is None:
            normalized["extra"] = None
        elif not isinstance(extra, dict):
            errors.append(f"{location}.extra 必须是 JSON 对象")
        else:
            normalized["extra"] = dict(extra)

    raw_proxy_key = item.get("proxy_key")
    if raw_proxy_key is not None:
        proxy_key = normalize_optional_string_field(raw_proxy_key, location=location, field="proxy_key", errors=errors, allow_none=True)
        if proxy_key:
            normalized["proxy_key"] = proxy_key
            if proxy_key not in file_proxy_keys:
                warnings.append(f"{location}.proxy_key 在当前文件的代理列表里找不到；只有站点中已存在同 key 代理时导入才会成功")

    concurrency = item.get("concurrency", 0)
    concurrency_value = json_int(concurrency)
    if concurrency_value is None:
        errors.append(f"{location}.concurrency 必须是整数")
    elif concurrency_value < 0:
        errors.append(f"{location}.concurrency 不能为负数")
    else:
        normalized["concurrency"] = concurrency_value

    priority = item.get("priority", 0)
    priority_value = json_int(priority)
    if priority_value is None:
        errors.append(f"{location}.priority 必须是整数")
    elif priority_value < 0:
        errors.append(f"{location}.priority 不能为负数")
    else:
        normalized["priority"] = priority_value

    if "rate_multiplier" in item:
        rate_multiplier = item.get("rate_multiplier")
        if rate_multiplier is None:
            normalized["rate_multiplier"] = None
        else:
            rate_value = json_number(rate_multiplier)
            if rate_value is None:
                errors.append(f"{location}.rate_multiplier 必须是数字")
            elif rate_value < 0:
                errors.append(f"{location}.rate_multiplier 不能为负数")
            else:
                normalized["rate_multiplier"] = rate_value

    if "expires_at" in item:
        expires_at = item.get("expires_at")
        if expires_at is None:
            normalized["expires_at"] = None
        else:
            expires_value = json_int(expires_at)
            if expires_value is None:
                errors.append(f"{location}.expires_at 必须是整数时间戳")
            else:
                normalized["expires_at"] = expires_value

    if "auto_pause_on_expired" in item:
        auto_pause = item.get("auto_pause_on_expired")
        if auto_pause is not None and not isinstance(auto_pause, bool):
            errors.append(f"{location}.auto_pause_on_expired 必须是布尔值")
        else:
            normalized["auto_pause_on_expired"] = auto_pause

    return {"errors": errors, "warnings": warnings, "normalized": normalized if not errors else None}


def validate_accounts_data_payload(raw_payload: Any) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    proxy_keys_in_file: set[str] = set()

    if not isinstance(raw_payload, dict):
        return {
            "ok": False,
            "errors": ["`data` 必须是 JSON 对象"],
            "warnings": [],
            "normalized": None,
            "proxy_count": 0,
            "account_count": 0,
            "proxy_keys_in_file": set(),
        }

    payload = dict(raw_payload)
    normalized: dict[str, Any] = dict(payload)

    if "type" in payload:
        payload_type = payload.get("type")
        if not isinstance(payload_type, str):
            errors.append("`data.type` 必须是字符串")
        else:
            normalized_type = payload_type.strip()
            if normalized_type and normalized_type not in {DATA_TYPE, LEGACY_DATA_TYPE}:
                errors.append(f"`data.type` 只支持 `{DATA_TYPE}` 或 `{LEGACY_DATA_TYPE}`")
            else:
                normalized["type"] = normalized_type
    else:
        warnings.append("未提供 `data.type`，后端仍可导入，但这不属于标准导出文件")

    if "version" in payload:
        version = json_int(payload.get("version"))
        if version is None:
            errors.append("`data.version` 必须是整数")
        elif version != DATA_VERSION:
            errors.append(f"`data.version` 目前只支持 {DATA_VERSION}")
        else:
            normalized["version"] = version
    else:
        warnings.append("未提供 `data.version`，后端仍可导入，但这不属于标准导出文件")

    if "exported_at" in payload:
        exported_at = payload.get("exported_at")
        if not isinstance(exported_at, str):
            errors.append("`data.exported_at` 必须是字符串")
        else:
            normalized["exported_at"] = exported_at.strip()
    else:
        warnings.append("未提供 `data.exported_at`，建议先转成标准导入格式")

    proxies = payload.get("proxies")
    if proxies is None:
        errors.append("缺少 `data.proxies` 数组")
        proxies = []
    elif not isinstance(proxies, list):
        errors.append("`data.proxies` 必须是数组")
        proxies = []

    accounts = payload.get("accounts")
    if accounts is None:
        errors.append("缺少 `data.accounts` 数组")
        accounts = []
    elif not isinstance(accounts, list):
        errors.append("`data.accounts` 必须是数组")
        accounts = []

    normalized_proxies: list[dict[str, Any]] = []
    for index, proxy_item in enumerate(proxies):
        proxy_result = validate_data_proxy_item(proxy_item, index)
        errors.extend(proxy_result["errors"])
        warnings.extend(proxy_result["warnings"])
        normalized_proxy = proxy_result.get("normalized")
        effective_proxy_key = proxy_result.get("effective_proxy_key")
        if normalized_proxy is not None:
            normalized_proxies.append(normalized_proxy)
        if isinstance(effective_proxy_key, str) and effective_proxy_key:
            proxy_keys_in_file.add(effective_proxy_key)

    normalized_accounts: list[dict[str, Any]] = []
    for index, account_item in enumerate(accounts):
        account_result = validate_data_account_item(account_item, index, proxy_keys_in_file)
        errors.extend(account_result["errors"])
        warnings.extend(account_result["warnings"])
        normalized_account = account_result.get("normalized")
        if normalized_account is not None:
            normalized_accounts.append(normalized_account)

    normalized["proxies"] = normalized_proxies
    normalized["accounts"] = normalized_accounts

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "normalized": normalized if not errors else None,
        "proxy_count": len(normalized_proxies),
        "account_count": len(normalized_accounts),
        "proxy_keys_in_file": proxy_keys_in_file,
    }


def validate_accounts_import_payload(raw: Any, *, skip_default_group_bind: bool) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    request_payload: dict[str, Any] | None = None
    raw_data: Any = None
    wrapped = False
    effective_skip = skip_default_group_bind

    if not isinstance(raw, dict):
        errors.append("账号导入文件必须是 JSON 对象；支持直接放 DataPayload，或放到 `data` 字段里")
    else:
        wrapped = "data" in raw
        if wrapped:
            raw_data = raw.get("data")
            skip_value = raw.get("skip_default_group_bind")
            if "skip_default_group_bind" in raw:
                if skip_value is None:
                    pass
                elif not isinstance(skip_value, bool):
                    errors.append("`skip_default_group_bind` 必须是布尔值")
                else:
                    effective_skip = skip_value
        else:
            raw_data = raw

    data_result = validate_accounts_data_payload(raw_data)
    errors.extend(data_result["errors"])
    warnings.extend(data_result["warnings"])

    normalized_data = data_result.get("normalized")
    if normalized_data is not None:
        request_payload = dict(raw) if isinstance(raw, dict) else {}
        request_payload["data"] = normalized_data
        if "skip_default_group_bind" not in request_payload or request_payload.get("skip_default_group_bind") is None:
            request_payload["skip_default_group_bind"] = effective_skip

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "wrapped": wrapped,
        "request_payload": request_payload,
        "data_payload": normalized_data,
        "account_count": data_result["account_count"],
        "proxy_count": data_result["proxy_count"],
        "skip_default_group_bind": effective_skip,
    }


def build_standard_accounts_data_payload(data_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data_payload)
    payload["type"] = DATA_TYPE
    payload["version"] = DATA_VERSION
    if not isinstance(payload.get("exported_at"), str) or not payload.get("exported_at", "").strip():
        payload["exported_at"] = utc_now_rfc3339()
    payload["proxies"] = [dict(item) for item in payload.get("proxies") or [] if isinstance(item, dict)]
    payload["accounts"] = [dict(item) for item in payload.get("accounts") or [] if isinstance(item, dict)]

    for proxy in payload["proxies"]:
        if "name" not in proxy or not str(proxy.get("name") or "").strip():
            proxy["name"] = "imported-proxy"
        if "status" not in proxy or not str(proxy.get("status") or "").strip():
            proxy["status"] = "active"

    for account in payload["accounts"]:
        if "concurrency" not in account or json_int(account.get("concurrency")) is None:
            account["concurrency"] = 10
        if "priority" not in account or json_int(account.get("priority")) is None:
            account["priority"] = 1

    return payload


def try_convert_auth_snapshot_account(raw: Any, *, source_name: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    access_token = non_empty(str(raw.get("access_token") or ""))
    refresh_token = non_empty(str(raw.get("refresh_token") or ""))
    email = non_empty(str(raw.get("email") or ""))
    source_type = non_empty(str(raw.get("type") or ""))
    account_id = non_empty(str(raw.get("account_id") or ""))
    id_token = non_empty(str(raw.get("id_token") or ""))
    if not access_token or not refresh_token or not email:
        return None

    credentials: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "email": email,
    }
    if id_token:
        credentials["id_token"] = id_token
    if account_id:
        credentials["chatgpt_account_id"] = account_id
    expired = non_empty(str(raw.get("expired") or ""))
    if expired:
        credentials["expires_at"] = expired

    extra: dict[str, Any] = {
        "source_format": "auth_snapshot",
        "source_name": source_name,
    }
    if source_type:
        extra["source_type"] = source_type
    if "disabled" in raw:
        extra["disabled"] = bool(raw.get("disabled"))
    last_refresh = non_empty(str(raw.get("last_refresh") or ""))
    if last_refresh:
        extra["last_refresh"] = last_refresh

    normalized_account = {
        "name": email,
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": extra,
        "concurrency": 10,
        "priority": 1,
    }
    standardized = build_standard_accounts_data_payload(
        {
            "exported_at": utc_now_rfc3339(),
            "proxies": [],
            "accounts": [normalized_account],
        }
    )
    return {
        "ok": True,
        "errors": [],
        "warnings": [f"{source_name}: 已识别为 auths 单账号快照，自动转换为 openai/oauth 账号"],
        "data_payload": standardized,
        "account_count": 1,
        "proxy_count": 0,
        "mode": "auth-snapshot",
    }


def convert_simple_accounts_json(raw: Any, *, source_name: str = "当前 JSON") -> dict[str, Any]:
    standard_validation = validate_accounts_import_payload(raw, skip_default_group_bind=True)
    if standard_validation["ok"] and isinstance(standard_validation.get("data_payload"), dict):
        standardized = build_standard_accounts_data_payload(standard_validation["data_payload"])
        return {
            "ok": True,
            "errors": [],
            "warnings": list(standard_validation["warnings"]),
            "data_payload": standardized,
            "account_count": len(standardized.get("accounts") or []),
            "proxy_count": len(standardized.get("proxies") or []),
            "mode": "standardized-existing",
        }

    auth_snapshot_conversion = try_convert_auth_snapshot_account(raw, source_name=source_name)
    if auth_snapshot_conversion is not None:
        return auth_snapshot_conversion

    warnings: list[str] = []
    errors: list[str] = []
    raw_accounts: Any = None
    raw_proxies: Any = []
    source_candidate = raw
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        source_candidate = raw.get("data")

    if isinstance(source_candidate, list):
        raw_accounts = source_candidate
    elif isinstance(source_candidate, dict):
        if "accounts" in source_candidate:
            raw_accounts = source_candidate.get("accounts")
            raw_proxies = source_candidate.get("proxies", [])
        elif {"name", "platform", "type"} & set(source_candidate.keys()):
            raw_accounts = [source_candidate]
            raw_proxies = []
        else:
            errors.append(f"{source_name}: 无法识别账号列表。请提供数组、`accounts` 数组，或单个账号对象")
    else:
        errors.append(f"{source_name}: 只能转换 JSON 对象或 JSON 数组")

    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "data_payload": None, "account_count": 0, "proxy_count": 0, "mode": "simple"}

    if not isinstance(raw_accounts, list):
        return {"ok": False, "errors": [f"{source_name}: `accounts` 必须是数组"], "warnings": warnings, "data_payload": None, "account_count": 0, "proxy_count": 0, "mode": "simple"}

    if raw_proxies is None:
        raw_proxies = []
    if not isinstance(raw_proxies, list):
        return {"ok": False, "errors": [f"{source_name}: `proxies` 必须是数组"], "warnings": warnings, "data_payload": None, "account_count": 0, "proxy_count": 0, "mode": "simple"}

    proxy_keys_in_file: set[str] = set()
    normalized_proxies: list[dict[str, Any]] = []
    for index, proxy_item in enumerate(raw_proxies):
        proxy_result = validate_data_proxy_item(proxy_item, index)
        errors.extend(proxy_result["errors"])
        warnings.extend(proxy_result["warnings"])
        normalized_proxy = proxy_result.get("normalized")
        effective_proxy_key = proxy_result.get("effective_proxy_key")
        if normalized_proxy is not None:
            normalized_proxies.append(normalized_proxy)
        if isinstance(effective_proxy_key, str) and effective_proxy_key:
            proxy_keys_in_file.add(effective_proxy_key)

    normalized_accounts: list[dict[str, Any]] = []
    for index, account_item in enumerate(raw_accounts):
        account_result = validate_data_account_item(account_item, index, proxy_keys_in_file)
        errors.extend(account_result["errors"])
        warnings.extend(account_result["warnings"])
        normalized_account = account_result.get("normalized")
        if normalized_account is not None:
            normalized_accounts.append(normalized_account)

    if errors:
        return {
            "ok": False,
            "errors": errors,
            "warnings": warnings,
            "data_payload": None,
            "account_count": len(normalized_accounts),
            "proxy_count": len(normalized_proxies),
            "mode": "simple",
        }

    standardized = build_standard_accounts_data_payload(
        {
            "exported_at": utc_now_rfc3339(),
            "proxies": normalized_proxies,
            "accounts": normalized_accounts,
        }
    )
    return {
        "ok": True,
        "errors": [],
        "warnings": warnings,
        "data_payload": standardized,
        "account_count": len(normalized_accounts),
        "proxy_count": len(normalized_proxies),
        "mode": "simple",
    }


def build_accounts_import_payload(raw: Any, skip_default_group_bind: bool) -> dict[str, Any]:
    validation = validate_accounts_import_payload(raw, skip_default_group_bind=skip_default_group_bind)
    if not validation["ok"] or not isinstance(validation.get("request_payload"), dict):
        raise CLIError(format_validation_message(validation, source_label="账号导入文件格式不正确"))
    return validation["request_payload"]


def build_proxies_import_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and "data" in raw:
        return dict(raw)
    if isinstance(raw, dict):
        return {"data": raw}
    raise CLIError("代理导入文件必须是 DataPayload JSON 对象，或包含 `data` 的包装对象")


def extract_error(status: int, raw_body: str) -> APIError:
    try:
        payload = json.loads(raw_body) if raw_body else {}
    except JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        if "code" in payload:
            return APIError(
                status=status,
                code=payload.get("code"),
                message=str(payload.get("message") or "Unknown error"),
                details=payload,
            )
        return APIError(status=status, code=status, message=raw_body or "Request failed", details=payload)

    return APIError(status=status, code=status, message=raw_body or "Request failed")


def is_transient_ssl_eof_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "unexpected_eof_while_reading" in message or "eof occurred in violation of protocol" in message


def resolve_auth(args: argparse.Namespace) -> ResolvedAuth:
    admin_api_key = non_empty(args.admin_api_key)
    bearer_token = non_empty(args.bearer_token)
    email = non_empty(args.email)
    password = non_empty(args.password)
    turnstile_token = non_empty(args.turnstile_token)
    totp_code = non_empty(args.totp_code)
    refresh_token = non_empty(args.refresh_token) or non_empty(os.environ.get(ENV_REFRESH_TOKEN))
    return ResolvedAuth(
        admin_api_key=admin_api_key,
        bearer_token=bearer_token,
        refresh_token=refresh_token,
        login=LoginOptions(
            email=email,
            password=password,
            turnstile_token=turnstile_token,
            totp_code=totp_code,
        ),
    )


class AdminAPIClient:
    def __init__(
        self,
        api_base: str,
        *,
        admin_api_key: str | None,
        bearer_token: str | None,
        login: LoginOptions,
        timeout: float,
        insecure: bool,
        user_agent: str,
    ) -> None:
        self.api_base = normalize_api_base(api_base)
        self.timeout = timeout
        self.insecure = insecure
        self.user_agent = user_agent.strip() or DEFAULT_USER_AGENT
        self.admin_api_key = admin_api_key
        self.bearer_token = bearer_token
        self.login = login
        self.refresh_token: str | None = None
        self._public_settings: dict[str, Any] | None = None

        has_key = bool(admin_api_key)
        has_bearer = bool(bearer_token)
        has_login = bool(login.email and login.password)

        if login.email and not login.password:
            raise CLIError("提供了 `--email` 但缺少 `--password`")
        if login.password and not login.email:
            raise CLIError("提供了 `--password` 但缺少 `--email`")

        if has_key and (has_bearer or has_login):
            raise CLIError(
                "使用 `--admin-api-key` 时不能再叠加 token 或邮箱密码登录"
            )

    def request(self, method: str, path: str, payload: Any = None, *, auth_required: bool = True) -> Any:
        retried_with_password = False
        request_attempt = 0
        while True:
            request_attempt += 1
            if auth_required:
                self.ensure_authenticated()

            headers = {
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            }
            if self.admin_api_key:
                headers["x-api-key"] = self.admin_api_key
            if self.bearer_token:
                headers["Authorization"] = f"Bearer {self.bearer_token}"

            data: bytes | None = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            request = Request(
                url=urljoin(self.api_base + "/", path.lstrip("/")),
                data=data,
                headers=headers,
                method=method.upper(),
            )

            context = None
            if self.insecure and request.full_url.startswith("https://"):
                context = ssl._create_unverified_context()

            try:
                with urlopen(request, timeout=self.timeout, context=context) as response:
                    raw_body = response.read().decode("utf-8")
                    return self._unwrap_success(raw_body, response.status)
            except HTTPError as exc:
                raw_body = exc.read().decode("utf-8", errors="replace")
                api_error = extract_error(exc.code, raw_body)
                if (
                    auth_required
                    and not retried_with_password
                    and api_error.status == 401
                    and self.bearer_token
                    and self.login.email
                    and self.login.password
                    and not self.admin_api_key
                ):
                    self.login_with_password(force=True)
                    retried_with_password = True
                    continue
                raise api_error from exc
            except URLError as exc:
                if request_attempt < REQUEST_RETRY_LIMIT and is_transient_ssl_eof_error(exc):
                    time.sleep(0.35 * request_attempt)
                    continue
                if is_transient_ssl_eof_error(exc):
                    raise CLIError(
                        "请求失败: 与网站建立 HTTPS 连接时被对端提前断开。"
                        f"已自动重试 {request_attempt} 次仍失败。原始错误: {exc}"
                    ) from exc
                raise CLIError(f"请求失败: {exc}") from exc

    def request_text(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        auth_required: bool = True,
        accept: str = "text/event-stream",
    ) -> tuple[int, str]:
        retried_with_password = False
        request_attempt = 0
        while True:
            request_attempt += 1
            if auth_required:
                self.ensure_authenticated()

            headers = {
                "Accept": accept,
                "User-Agent": self.user_agent,
            }
            if self.admin_api_key:
                headers["x-api-key"] = self.admin_api_key
            if self.bearer_token:
                headers["Authorization"] = f"Bearer {self.bearer_token}"

            data: bytes | None = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            request = Request(
                url=urljoin(self.api_base + "/", path.lstrip("/")),
                data=data,
                headers=headers,
                method=method.upper(),
            )

            context = None
            if self.insecure and request.full_url.startswith("https://"):
                context = ssl._create_unverified_context()

            try:
                with urlopen(request, timeout=self.timeout, context=context) as response:
                    raw_body = response.read().decode("utf-8", errors="replace")
                    return response.status, raw_body
            except HTTPError as exc:
                raw_body = exc.read().decode("utf-8", errors="replace")
                api_error = extract_error(exc.code, raw_body)
                if (
                    auth_required
                    and not retried_with_password
                    and api_error.status == 401
                    and self.bearer_token
                    and self.login.email
                    and self.login.password
                    and not self.admin_api_key
                ):
                    self.login_with_password(force=True)
                    retried_with_password = True
                    continue
                raise api_error from exc
            except URLError as exc:
                if request_attempt < REQUEST_RETRY_LIMIT and is_transient_ssl_eof_error(exc):
                    time.sleep(0.35 * request_attempt)
                    continue
                if is_transient_ssl_eof_error(exc):
                    raise CLIError(
                        "请求失败: 与网站建立 HTTPS 连接时被对端提前断开。"
                        f"已自动重试 {request_attempt} 次仍失败。原始错误: {exc}"
                    ) from exc
                raise CLIError(f"请求失败: {exc}") from exc
            except ssl.SSLError as exc:
                if request_attempt < REQUEST_RETRY_LIMIT and is_transient_ssl_eof_error(exc):
                    time.sleep(0.35 * request_attempt)
                    continue
                if is_transient_ssl_eof_error(exc):
                    raise CLIError(
                        "请求失败: 与网站建立 HTTPS 连接时被对端提前断开。"
                        f"已自动重试 {request_attempt} 次仍失败。原始错误: {exc}"
                    ) from exc
                raise CLIError(f"请求失败: {exc}") from exc
            except ssl.SSLError as exc:
                if request_attempt < REQUEST_RETRY_LIMIT and is_transient_ssl_eof_error(exc):
                    time.sleep(0.35 * request_attempt)
                    continue
                if is_transient_ssl_eof_error(exc):
                    raise CLIError(
                        "请求失败: 与网站建立 HTTPS 连接时被对端提前断开。"
                        f"已自动重试 {request_attempt} 次仍失败。原始错误: {exc}"
                    ) from exc
                raise CLIError(f"请求失败: {exc}") from exc

    @staticmethod
    def _unwrap_success(raw_body: str, status: int) -> Any:
        if not raw_body.strip():
            return {"status": status}

        try:
            payload = json.loads(raw_body)
        except JSONDecodeError as exc:
            raise CLIError(f"服务端返回了非 JSON 响应: {raw_body[:200]}") from exc

        if isinstance(payload, dict) and "code" in payload:
            if payload.get("code") != 0:
                raise APIError(
                    status=status,
                    code=payload.get("code"),
                    message=str(payload.get("message") or "Unknown error"),
                    details=payload,
                )
            return payload.get("data")
        return payload

    def ensure_authenticated(self) -> None:
        if self.admin_api_key or self.bearer_token:
            return
        if not self.login.email or not self.login.password:
            raise CLIError("当前请求需要认证，但未提供可用的认证信息")
        self.login_with_password()

    def get_public_settings(self) -> dict[str, Any]:
        if self._public_settings is None:
            payload = self.request("GET", "/settings/public", auth_required=False)
            if not isinstance(payload, dict):
                raise CLIError("`/settings/public` 返回格式异常")
            self._public_settings = payload
        return self._public_settings

    def login_with_password(self, *, force: bool = False) -> dict[str, Any]:
        if self.bearer_token and not force:
            return {
                "access_token": self.bearer_token,
                "refresh_token": self.refresh_token,
                "token_type": "Bearer",
            }

        if not self.login.email or not self.login.password:
            raise CLIError("邮箱/密码登录需要同时提供 `--email` 和 `--password`")

        turnstile_enabled = False
        try:
            settings = self.get_public_settings()
            turnstile_enabled = bool(settings.get("turnstile_enabled"))
        except CLIError:
            # 公开设置探测失败时，仍允许直接尝试登录。
            pass

        if turnstile_enabled and not self.login.turnstile_token:
            raise CLIError(
                "站点已启用 Turnstile，脚本不会自动破解验证码。"
                f"请通过 `--turnstile-token` 或环境变量 `{ENV_TURNSTILE_TOKEN}` 提供 token。"
            )

        login_payload: dict[str, Any] = {
            "email": self.login.email,
            "password": self.login.password,
        }
        if self.login.turnstile_token:
            login_payload["turnstile_token"] = self.login.turnstile_token

        result = self.request("POST", "/auth/login", login_payload, auth_required=False)
        if not isinstance(result, dict):
            raise CLIError("`/auth/login` 返回格式异常")

        if result.get("requires_2fa") is True:
            temp_token = str(result.get("temp_token") or "")
            if not temp_token:
                raise CLIError("登录返回要求 2FA，但缺少 `temp_token`")
            if not self.login.totp_code:
                raise CLIError(
                    "该账号启用了 2FA。请通过 `--totp-code` 或环境变量 "
                    f"`{ENV_TOTP_CODE}` 提供 6 位验证码。"
                )
            result = self.request(
                "POST",
                "/auth/login/2fa",
                {
                    "temp_token": temp_token,
                    "totp_code": self.login.totp_code,
                },
                auth_required=False,
            )
            if not isinstance(result, dict):
                raise CLIError("`/auth/login/2fa` 返回格式异常")

        access_token = str(result.get("access_token") or "")
        if not access_token:
            raise CLIError("登录成功响应中缺少 `access_token`")

        self.bearer_token = access_token
        refresh_token = result.get("refresh_token")
        self.refresh_token = str(refresh_token) if isinstance(refresh_token, str) and refresh_token else None
        return result


def handle_delete_accounts(client: AdminAPIClient, args: argparse.Namespace) -> CommandResult:
    deleted_ids: list[int] = []
    failed: list[dict[str, Any]] = []

    # 后端没有批量删账号接口，这里显式循环单删。
    for account_id in unique_ids(args.ids):
        try:
            response = client.request("DELETE", f"/admin/accounts/{account_id}")
            deleted_ids.append(account_id)
            if args.verbose:
                write_json({"account_id": account_id, "response": response}, sys.stderr)
        except APIError as exc:
            failed.append(
                {
                    "id": account_id,
                    "status": exc.status,
                    "code": exc.code,
                    "message": str(exc),
                }
            )

    return CommandResult(
        payload={
            "deleted_ids": deleted_ids,
            "failed": failed,
            "success": len(deleted_ids),
            "failed_count": len(failed),
        },
        failed=bool(failed),
    )


def handle_delete_proxies(client: AdminAPIClient, args: argparse.Namespace) -> CommandResult:
    result = client.request("POST", "/admin/proxies/batch-delete", {"ids": unique_ids(args.ids)})
    failed = bool(result.get("skipped"))
    return CommandResult(payload=result, failed=failed)


def handle_batch_create_accounts(client: AdminAPIClient, args: argparse.Namespace) -> CommandResult:
    payload = ensure_list_payload(read_json(args.file), "accounts", "账号批量创建输入")
    result = client.request("POST", "/admin/accounts/batch", payload)
    failed = int(result.get("failed", 0)) > 0
    return CommandResult(payload=result, failed=failed)


def handle_batch_create_proxies(client: AdminAPIClient, args: argparse.Namespace) -> CommandResult:
    payload = ensure_list_payload(read_json(args.file), "proxies", "代理批量创建输入")
    result = client.request("POST", "/admin/proxies/batch", payload)
    failed = int(result.get("skipped", 0)) > 0
    return CommandResult(payload=result, failed=failed)


def handle_import_accounts_data(client: AdminAPIClient, args: argparse.Namespace) -> CommandResult:
    payload = build_accounts_import_payload(read_json(args.file), args.skip_default_group_bind)
    result = client.request("POST", "/admin/accounts/data", payload)
    failed = int(result.get("account_failed", 0)) > 0 or int(result.get("proxy_failed", 0)) > 0
    return CommandResult(payload=result, failed=failed)


def handle_import_proxies_data(client: AdminAPIClient, args: argparse.Namespace) -> CommandResult:
    payload = build_proxies_import_payload(read_json(args.file))
    result = client.request("POST", "/admin/proxies/data", payload)
    failed = int(result.get("proxy_failed", 0)) > 0
    return CommandResult(payload=result, failed=failed)


def handle_bulk_update_accounts(client: AdminAPIClient, args: argparse.Namespace) -> CommandResult:
    payload = ensure_dict(read_json(args.file), "账号批量编辑输入")
    account_ids = payload.get("account_ids")
    if not isinstance(account_ids, list) or not account_ids:
        raise CLIError("账号批量编辑输入必须包含非空数组字段 `account_ids`")

    result = client.request("POST", "/admin/accounts/bulk-update", payload)
    failed = int(result.get("failed", 0)) > 0
    return CommandResult(payload=result, failed=failed)


def handle_login(client: AdminAPIClient, args: argparse.Namespace) -> CommandResult:
    result = client.login_with_password(force=True)
    return CommandResult(payload=result)


def run_admin_gui(
    *,
    default_base_url: str,
    default_admin_api_key: str,
    default_timeout: float,
    default_insecure: bool,
) -> None:
    configure_tk_runtime()
    try:
        import threading
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:
        raise CLIError("当前环境不支持 GUI（Tkinter 不可用），请改用命令行模式。") from exc

    saved_config = load_gui_config(
        default_base_url=default_base_url,
        default_admin_api_key=default_admin_api_key,
    )
    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION}")
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    initial_width = min(saved_config.window_width or 1160, max(screen_width - 80, 820))
    initial_height = min(saved_config.window_height or 760, max(screen_height - 120, 620))
    root.geometry(f"{max(initial_width, 820)}x{max(initial_height, 620)}")
    root.minsize(820, 620)

    root.columnconfigure(0, weight=1)
    root.rowconfigure(2, weight=1)

    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
    style.configure("Hint.TLabel", foreground="#5b6470")

    class ScrolledText(ttk.Frame):
        def __init__(self, master: Any, **kwargs: Any) -> None:
            super().__init__(master)
            self.columnconfigure(0, weight=1)
            self.rowconfigure(0, weight=1)
            self.text = tk.Text(self, **kwargs)
            self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
            self.text.configure(yscrollcommand=self.scrollbar.set)
            self.text.grid(row=0, column=0, sticky="nsew")
            self.scrollbar.grid(row=0, column=1, sticky="ns")

        def __getattr__(self, name: str) -> Any:
            return getattr(self.text, name)

    top = ttk.LabelFrame(root, text="连接配置", padding=12)
    top.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
    top.columnconfigure(1, weight=1)
    top.columnconfigure(3, weight=0)
    top.columnconfigure(4, weight=0)

    ttk.Label(top, text="网站地址").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
    base_url_var = tk.StringVar(value=saved_config.base_url)
    ttk.Entry(top, textvariable=base_url_var).grid(row=0, column=1, sticky="ew", pady=2)

    ttk.Label(top, text="管理员 API Key").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=2)
    admin_key_var = tk.StringVar(value=saved_config.admin_api_key)
    show_admin_key_var = tk.BooleanVar(value=False)
    admin_key_entry = ttk.Entry(top, textvariable=admin_key_var, show="*")
    admin_key_entry.grid(row=1, column=1, sticky="ew", pady=2)
    ttk.Checkbutton(
        top,
        text="显示",
        variable=show_admin_key_var,
        command=lambda: admin_key_entry.configure(show="" if show_admin_key_var.get() else "*"),
    ).grid(row=1, column=2, sticky="w", padx=(8, 0), pady=2)

    sync_concurrency_var = tk.StringVar(value=str(clamp_sync_concurrency(saved_config.sync_concurrency)))
    detection_concurrency_var = tk.StringVar(value=str(clamp_detection_concurrency(saved_config.detection_concurrency)))
    delete_concurrency_var = tk.StringVar(value=str(clamp_delete_concurrency(saved_config.delete_concurrency)))
    sync_concurrency_frame = ttk.Frame(top)
    sync_concurrency_frame.grid(row=0, column=2, columnspan=2, sticky="w", padx=(12, 0), pady=2)
    ttk.Label(sync_concurrency_frame, text="同步并发数").grid(row=0, column=0, sticky="w")
    ttk.Entry(sync_concurrency_frame, textvariable=sync_concurrency_var, width=8).grid(row=0, column=1, sticky="w", padx=(6, 0))

    status_var = tk.StringVar(value="已就绪。先填写网站地址和管理员 API Key，再到“开始使用”里检查连接。")
    ttk.Label(top, textvariable=status_var).grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

    progress_text_var = tk.StringVar(value="等待执行")
    progress_frame = ttk.Frame(root, padding=(12, 0, 12, 6))
    progress_frame.grid(row=1, column=0, sticky="ew")
    progress_frame.columnconfigure(0, weight=1)
    progress_bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=1, value=0)
    progress_bar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
    ttk.Label(progress_frame, textvariable=progress_text_var).grid(row=0, column=1, sticky="w")
    current_cancel_event: Any = None
    stop_task_btn: Any = None
    latest_release_notice_shown = False

    notebook_container = ttk.Frame(root)
    notebook_container.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 8))
    notebook_container.columnconfigure(0, weight=1)
    notebook_container.rowconfigure(0, weight=1)
    notebook = ttk.Notebook(notebook_container)
    notebook.grid(row=0, column=0, sticky="nsew")

    output_frame = ttk.LabelFrame(root, text="执行结果", padding=8)
    output_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
    output_frame.columnconfigure(0, weight=1)
    output_frame.rowconfigure(0, weight=1)
    output = ScrolledText(output_frame, wrap="word", height=6)
    output.grid(row=0, column=0, sticky="nsew")

    action_buttons: list[ttk.Button] = []
    group_filter_combos: list[Any] = []
    group_update_combos: list[Any] = []
    account_picker_combos: list[Any] = []
    proxy_picker_combos: list[Any] = []
    proxy_update_combos: list[Any] = []
    group_label_to_id: dict[str, int] = {}
    account_label_to_id: dict[str, int] = {}
    account_id_to_label: dict[int, str] = {}
    detection_label_to_account_id: dict[str, int] = {}
    invalid_401_account_ids_cache: list[int] = []
    invalid_quota_account_ids_cache: list[int] = []
    proxy_label_to_id: dict[str, int] = {}
    scheduled_after_ids: list[str] = []
    groups_cache: list[dict[str, Any]] = []
    accounts_cache: list[dict[str, Any]] = []
    proxies_cache: list[dict[str, Any]] = []
    GROUP_FILTER_ALL = "(不限)"
    GROUP_UPDATE_KEEP = "(保持不变)"
    GROUP_UPDATE_CLEAR = "(清空分组)"
    KEEP_OPTION = "(保持不变)"
    CLEAR_OPTION = "(清空)"
    ENABLE_OPTION = "(启用)"
    DISABLE_OPTION = "(禁用)"
    ACCOUNT_PICKER_HINT = "(请先同步账号列表)"
    PROXY_PICKER_HINT = "(请先同步代理列表)"
    PROXY_UPDATE_KEEP = "(保持当前代理)"
    PROXY_UPDATE_CLEAR = "(清空代理)"

    def pick_json(target_var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(
            title="选择 JSON 文件",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
        )
        if path:
            target_var.set(path)

    def pick_directory(target_var: tk.StringVar) -> None:
        path = filedialog.askdirectory(title="选择包含 JSON 文件的目录")
        if path:
            target_var.set(path)

    def collect_json_input_files(mode: str, source_path: str | None, *, recursive: bool, purpose_label: str) -> tuple[Path, list[Path]]:
        if not source_path:
            if mode == "folder":
                raise CLIError(f"请选择{purpose_label}文件夹")
            raise CLIError(f"请选择{purpose_label}文件")

        source = Path(source_path)
        if mode == "folder":
            if not source.exists() or not source.is_dir():
                raise CLIError(f"选择的{purpose_label}文件夹不存在或不可访问")
            files = sorted(source.rglob("*.json") if recursive else source.glob("*.json"))
            if not files:
                raise CLIError(f"所选{purpose_label}文件夹中没有找到 .json 文件")
            return source, files

        if not source.exists() or not source.is_file():
            raise CLIError(f"选择的{purpose_label}文件不存在或不可访问")
        return source.parent, [source]

    def summarize_invalid_file_checks(scan_result: dict[str, Any], *, action_label: str) -> str:
        invalid_items = [item for item in scan_result.get("details", []) if not item.get("ok")]
        if not invalid_items:
            return f"{action_label}格式检查通过"
        if len(scan_result.get("details", [])) == 1:
            first = invalid_items[0]
            return format_validation_message({"errors": first.get("errors", []), "warnings": first.get("warnings", [])}, source_label=action_label)

        preview: list[str] = []
        for item in invalid_items[:3]:
            name = Path(str(item.get("file") or "")).name or str(item.get("file") or "未知文件")
            message = "；".join(str(part) for part in (item.get("errors") or [])[:2]) or "格式不正确"
            preview.append(f"{name}: {message}")
        if len(invalid_items) > 3:
            preview.append(f"其余 {len(invalid_items) - 3} 个请先点“先检查文件格式”查看")
        return f"{action_label}发现 {len(invalid_items)} 个文件不符合要求：{'；'.join(preview)}"

    def inspect_accounts_import_files(
        files: list[Path],
        *,
        skip_default_group_bind: bool,
        progress_callback: Callable[[int, int, str], None],
        keep_payloads: bool,
        include_single_preview: bool,
    ) -> dict[str, Any]:
        details: list[dict[str, Any]] = []
        prepared_payloads: list[dict[str, Any]] = []
        warning_count = 0
        invalid_files = 0

        for index, file in enumerate(files, start=1):
            progress_callback(index - 1, len(files), f"检查 {index}/{len(files)}: {file.name}")
            try:
                raw = read_json(str(file))
                validation = validate_accounts_import_payload(raw, skip_default_group_bind=skip_default_group_bind)
                file_detail = {
                    "file": str(file),
                    "ok": bool(validation["ok"]),
                    "account_count": validation["account_count"],
                    "proxy_count": validation["proxy_count"],
                    "warning_count": len(validation["warnings"]),
                    "warnings": validation["warnings"],
                }
                if validation["ok"]:
                    if keep_payloads and isinstance(validation.get("request_payload"), dict):
                        prepared_payloads.append({"file": str(file), "request_payload": validation["request_payload"]})
                    if include_single_preview and len(files) == 1 and isinstance(validation.get("request_payload"), dict):
                        file_detail["normalized_request"] = validation["request_payload"]
                else:
                    invalid_files += 1
                    file_detail["errors"] = validation["errors"]
                warning_count += len(validation["warnings"])
            except Exception as exc:
                invalid_files += 1
                file_detail = {
                    "file": str(file),
                    "ok": False,
                    "errors": [str(exc)],
                    "warnings": [],
                    "warning_count": 0,
                    "account_count": 0,
                    "proxy_count": 0,
                }
            details.append(file_detail)

        progress_callback(len(files), len(files), f"检查完成，共 {len(files)} 个文件")
        result = {
            "ok": invalid_files == 0,
            "checked_files": len(files),
            "valid_files": len(files) - invalid_files,
            "invalid_files": invalid_files,
            "warning_count": warning_count,
            "details": details,
        }
        if keep_payloads:
            result["prepared_payloads"] = prepared_payloads
        if include_single_preview and len(files) == 1 and details:
            result["preview"] = details[0].get("normalized_request")
        return result

    def inspect_convertible_json_files(
        files: list[Path],
        *,
        progress_callback: Callable[[int, int, str], None],
        keep_payloads: bool,
        include_preview: bool,
    ) -> dict[str, Any]:
        details: list[dict[str, Any]] = []
        converted_payloads: list[dict[str, Any]] = []
        warning_count = 0
        invalid_files = 0

        for index, file in enumerate(files, start=1):
            progress_callback(index - 1, len(files), f"转换检查 {index}/{len(files)}")
            try:
                raw = read_json(str(file))
                conversion = convert_simple_accounts_json(raw, source_name=file.name)
                detail = {
                    "file": str(file),
                    "ok": bool(conversion["ok"]),
                    "account_count": conversion["account_count"],
                    "proxy_count": conversion["proxy_count"],
                    "warning_count": len(conversion["warnings"]),
                    "warnings": conversion["warnings"],
                    "mode": conversion.get("mode"),
                }
                if conversion["ok"] and isinstance(conversion.get("data_payload"), dict):
                    data_payload = conversion["data_payload"]
                    if keep_payloads:
                        converted_payloads.append({"file": str(file), "data_payload": data_payload})
                    if include_preview and len(files) == 1:
                        detail["converted_data"] = data_payload
                else:
                    invalid_files += 1
                    detail["errors"] = conversion["errors"]
                warning_count += len(conversion["warnings"])
            except Exception as exc:
                invalid_files += 1
                detail = {
                    "file": str(file),
                    "ok": False,
                    "errors": [str(exc)],
                    "warnings": [],
                    "warning_count": 0,
                    "account_count": 0,
                    "proxy_count": 0,
                    "mode": "failed",
                }
            details.append(detail)

        progress_callback(len(files), len(files), f"转换检查完成，共 {len(files)} 个文件")
        result = {
            "ok": invalid_files == 0,
            "checked_files": len(files),
            "valid_files": len(files) - invalid_files,
            "invalid_files": invalid_files,
            "warning_count": warning_count,
            "details": details,
        }
        if keep_payloads:
            result["converted_payloads"] = converted_payloads
        if include_preview and len(files) == 1 and details:
            result["preview"] = details[0].get("converted_data")
        return result

    def resolve_conversion_output_dir(mode: str, source_path: str | None, output_path: str | None) -> Path:
        if output_path:
            return Path(output_path)
        if not source_path:
            raise CLIError("请先选择待转换的文件或文件夹")
        source = Path(source_path)
        base = source.parent if mode == "file" else source
        return base / "s2a-manager-converted"

    def build_conversion_output_path(source_root: Path, file: Path, output_dir: Path, *, mode: str) -> Path:
        relative_parent = Path()
        if mode == "folder":
            try:
                relative_parent = file.relative_to(source_root).parent
            except ValueError:
                relative_parent = Path()

        base_name = f"{file.stem}.s2a-manager-import"
        candidate = output_dir / relative_parent / f"{base_name}.json"
        if not candidate.exists():
            return candidate

        index = 1
        while True:
            candidate = output_dir / relative_parent / f"{base_name}-{index}.json"
            if not candidate.exists():
                return candidate
            index += 1

    def render_payload(payload: Any) -> None:
        output.delete("1.0", tk.END)
        output.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))

    def open_release_page() -> None:
        import webbrowser

        webbrowser.open(APP_RELEASES_URL)

    def fetch_latest_release_info() -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        request = Request(APP_LATEST_RELEASE_API, headers=headers, method="GET")
        with urlopen(request, timeout=8) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_body)
        except JSONDecodeError as exc:
            raise CLIError(f"GitHub Release 返回了非 JSON 数据: {raw_body[:200]}") from exc
        if not isinstance(payload, dict):
            raise CLIError("GitHub Release 返回格式异常")
        tag_name = str(payload.get("tag_name") or "").strip()
        html_url = str(payload.get("html_url") or APP_RELEASES_URL).strip() or APP_RELEASES_URL
        name = str(payload.get("name") or tag_name or "未命名版本").strip()
        body = str(payload.get("body") or "").strip()
        published_at = str(payload.get("published_at") or "").strip()
        return {
            "tag_name": tag_name,
            "html_url": html_url,
            "name": name,
            "body": body,
            "published_at": published_at,
        }

    def handle_update_check_error(
        exc: Exception,
        *,
        interactive: bool,
        previous_status: str,
    ) -> None:
        status_var.set(previous_status)
        if interactive:
            messagebox.showerror("检查更新失败", str(exc))

    def check_for_updates(
        info: dict[str, Any],
        *,
        interactive: bool,
        silent_when_latest: bool = False,
        previous_status: str,
    ) -> bool:
        nonlocal latest_release_notice_shown
        latest_tag = str(info.get("tag_name") or "").strip()
        if not latest_tag:
            status_var.set(previous_status)
            if interactive:
                messagebox.showinfo("检查更新", "当前 GitHub Release 里还没有可识别的 tag。")
            return False

        current_version = normalize_version_tag(APP_VERSION)
        latest_version = normalize_version_tag(latest_tag)
        info["current_version"] = APP_VERSION
        info["update_available"] = latest_version > current_version

        if info["update_available"]:
            status_var.set(f"发现新版本 {latest_tag}，当前版本 {APP_VERSION}")
            body_preview = str(info.get("body") or "").strip()
            if len(body_preview) > 300:
                body_preview = body_preview[:300] + "..."
            message = (
                f"发现新版本：{latest_tag}\n"
                f"当前版本：{APP_VERSION}\n\n"
                f"发布标题：{info.get('name') or latest_tag}\n"
                f"发布时间：{info.get('published_at') or '未知'}"
            )
            if body_preview:
                message += f"\n\n更新说明：\n{body_preview}"
            if interactive or not latest_release_notice_shown:
                latest_release_notice_shown = True
                if messagebox.askyesno("发现新版本", message + "\n\n是否打开 Release 页面？"):
                    open_release_page()
            return True

        status_var.set(previous_status)
        if interactive and not silent_when_latest:
            messagebox.showinfo("检查更新", f"当前已是最新版本。\n当前版本：{APP_VERSION}\n最新版本：{latest_tag}")
        return False

    def trigger_update_check(*, interactive: bool, silent_when_latest: bool = False) -> None:
        previous_status = status_var.get()
        status_var.set("正在检查 GitHub 最新版本...")

        def worker() -> None:
            try:
                info = fetch_latest_release_info()
            except Exception as exc:
                safe_after(
                    0,
                    lambda err=exc: handle_update_check_error(
                        err,
                        interactive=interactive,
                        previous_status=previous_status,
                    ),
                )
                return
            safe_after(
                0,
                lambda payload=info: check_for_updates(
                    payload,
                    interactive=interactive,
                    silent_when_latest=silent_when_latest,
                    previous_status=previous_status,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def show_about_dialog() -> None:
        messagebox.showinfo(
            "关于",
            (
                f"{APP_NAME}\n"
                f"版本：{APP_VERSION}\n\n"
                "一个面向 sub2api 管理接口的桌面管理工具。\n"
                "支持账号同步、批量导入、批量调整、账号检测、导出和 JSON 转换。\n\n"
                f"Release 页面：\n{APP_RELEASES_URL}"
            ),
        )

    def safe_after(delay_ms: int, callback: Callable[..., Any], *args: Any) -> str | None:
        try:
            after_id = root.after(delay_ms, callback, *args)
        except tk.TclError:
            return None
        scheduled_after_ids.append(after_id)
        return after_id

    def cancel_scheduled_afters() -> None:
        while scheduled_after_ids:
            after_id = scheduled_after_ids.pop()
            try:
                root.after_cancel(after_id)
            except tk.TclError:
                continue

    def safe_ui_action(action: Callable[[], None]) -> Callable[[], None]:
        def wrapped() -> None:
            try:
                action()
            except Exception as exc:
                messagebox.showerror("操作失败", str(exc))

        return wrapped

    def set_busy(is_busy: bool) -> None:
        state = "disabled" if is_busy else "normal"
        for btn in action_buttons:
            btn.configure(state=state)
        if stop_task_btn is not None:
            stop_task_btn.configure(state="normal" if is_busy else "disabled")

    def request_cancel_current_task() -> None:
        if current_cancel_event is None:
            return
        current_cancel_event.set()
        status_var.set("正在请求停止当前任务...")
        set_progress(determinate=False, message="正在停止当前任务...")

    def set_progress(*, determinate: bool, current: int = 0, total: int = 1, message: str = "") -> None:
        if determinate:
            progress_bar.stop()
            safe_total = max(total, 1)
            safe_current = max(0, min(current, safe_total))
            progress_bar.configure(mode="determinate", maximum=safe_total, value=safe_current)
        else:
            progress_bar.configure(mode="indeterminate", maximum=100, value=0)
            progress_bar.start(12)
        progress_text_var.set(message or "处理中...")

    def parse_ids(raw: str, label: str, *, required: bool) -> list[int] | None:
        tokens = [t for t in re.split(r"[\s,，]+", raw.strip()) if t]
        if not tokens:
            if required:
                raise CLIError(f"{label} 不能为空")
            return None
        values: list[int] = []
        for token in tokens:
            try:
                values.append(int(token))
            except ValueError as exc:
                raise CLIError(f"{label} 包含非法整数: {token}") from exc
        return unique_ids(values)

    def parse_optional_positive_int(raw: str, label: str) -> int | None:
        value = raw.strip()
        if not value:
            return None
        try:
            parsed = int(value)
        except ValueError as exc:
            raise CLIError(f"{label} 必须是整数") from exc
        if parsed <= 0:
            raise CLIError(f"{label} 必须为正整数")
        return parsed

    def parse_optional_nonnegative_int(raw: str, label: str) -> int | None:
        value = raw.strip()
        if not value:
            return None
        try:
            parsed = int(value)
        except ValueError as exc:
            raise CLIError(f"{label} 必须是整数") from exc
        if parsed < 0:
            raise CLIError(f"{label} 不能为负数")
        return parsed

    def parse_optional_float(raw: str, label: str, *, min_value: float | None = None) -> float | None:
        value = raw.strip()
        if not value:
            return None
        try:
            parsed = float(value)
        except ValueError as exc:
            raise CLIError(f"{label} 必须是数字") from exc
        if min_value is not None and parsed < min_value:
            raise CLIError(f"{label} 不能小于 {min_value}")
        return parsed

    def parse_optional_int(raw: str, label: str) -> int | None:
        value = raw.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise CLIError(f"{label} 必须是整数") from exc

    def parse_optional_bool_choice(value: str, label: str) -> tuple[bool, bool | None]:
        selected = value.strip()
        if not selected or selected == KEEP_OPTION:
            return False, None
        if selected in {ENABLE_OPTION, "true"}:
            return True, True
        if selected in {DISABLE_OPTION, "false"}:
            return True, False
        if selected == CLEAR_OPTION:
            return True, False
        raise CLIError(f"{label} 选项无效")

    def parse_optional_timestamp(raw: str, label: str) -> int | None:
        value = raw.strip()
        if not value:
            return None
        if re.fullmatch(r"\d+", value):
            return int(value)
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise CLIError(f"{label} 必须是 Unix 秒时间戳或 ISO 时间，例如 2026-03-20T18:30:00+08:00") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())

    def ensure_not_cancelled() -> None:
        if current_cancel_event is not None and current_cancel_event.is_set():
            raise TaskCancelled("任务已停止")

    def parse_json_text(raw: str, label: str, *, require_dict: bool = False) -> Any:
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except JSONDecodeError as exc:
            raise CLIError(f"{label} 不是合法 JSON: {exc}") from exc
        if require_dict and not isinstance(parsed, dict):
            raise CLIError(f"{label} 必须是 JSON 对象")
        return parsed

    def format_detection_label(account: dict[str, Any], issue: str) -> str:
        account_id = int(account.get("id") or 0)
        name = str(account.get("name") or f"account-{account_id}")
        platform = str(account.get("platform") or "unknown")
        account_type = str(account.get("type") or "unknown")
        return f"[检测:{issue}] [{account_id}] {name} ({platform}/{account_type})"

    def extract_usage_utilization(usage: dict[str, Any], key: str) -> float | None:
        window = usage.get(key)
        if not isinstance(window, dict):
            return None
        value = window.get("utilization")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def text_contains_any(value: str, markers: tuple[str, ...]) -> bool:
        normalized = value.strip().lower()
        if not normalized:
            return False
        return any(marker in normalized for marker in markers)

    def usage_error_text(usage: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("error", "forbidden_reason", "message"):
            raw = usage.get(key)
            if raw is None:
                continue
            text = str(raw).strip()
            if text:
                parts.append(text.lower())
        return " | ".join(parts)

    def usage_window_utilization_above_threshold(usage: dict[str, Any], threshold: float, window_keys: tuple[str, ...]) -> bool:
        for key in window_keys:
            utilization = extract_usage_utilization(usage, key)
            if utilization is not None and utilization >= threshold:
                return True

        antigravity_quota = usage.get("antigravity_quota")
        if isinstance(antigravity_quota, dict):
            for quota in antigravity_quota.values():
                if not isinstance(quota, dict):
                    continue
                try:
                    utilization = float(quota.get("utilization"))
                except (TypeError, ValueError):
                    continue
                if utilization >= threshold:
                    return True
        return False

    def usage_ai_credits_exhausted(usage: dict[str, Any]) -> bool:
        raw_credits = usage.get("ai_credits")
        if not isinstance(raw_credits, list) or not raw_credits:
            return False

        comparable_credit_found = False
        all_credits_exhausted = True
        for item in raw_credits:
            if not isinstance(item, dict):
                continue
            try:
                amount = float(item.get("amount"))
            except (TypeError, ValueError):
                continue
            minimum_raw = item.get("minimum_balance")
            try:
                minimum_balance = float(minimum_raw) if minimum_raw is not None else 0.0
            except (TypeError, ValueError):
                minimum_balance = 0.0
            comparable_credit_found = True
            if amount > max(minimum_balance, 0.0):
                all_credits_exhausted = False
                break
        return comparable_credit_found and all_credits_exhausted

    def account_extra_utilization(account: dict[str, Any], key: str) -> float | None:
        extra = account.get("extra")
        if not isinstance(extra, dict):
            return None
        value = extra.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def usage_indicates_reauth(usage: dict[str, Any]) -> bool:
        if usage.get("needs_reauth") is True:
            return True
        error_code = str(usage.get("error_code") or "").strip().lower()
        if error_code in {"unauthenticated", "unauthorized"}:
            return True
        error_text = usage_error_text(usage)
        reauth_markers = (
            "401",
            "unauth",
            "unauthorized",
            "invalid token",
            "token expired",
            "token invalid",
            "invalid_grant",
            "missing_access_token",
            "no access token available",
            "session expired",
            "login required",
            "reauth",
            "re-auth",
        )
        return text_contains_any(error_text, reauth_markers)

    def usage_indicates_forbidden(usage: dict[str, Any]) -> bool:
        if usage.get("is_forbidden") is True:
            return True
        error_code = str(usage.get("error_code") or "").strip().lower()
        if error_code in {"forbidden", "permission_error"}:
            return True
        return text_contains_any(usage_error_text(usage), FORBIDDEN_TEXT_MARKERS)

    def account_error_text(account: dict[str, Any]) -> str:
        return str(account.get("error_message") or account.get("error") or "").strip().lower()

    def account_indicates_reauth(account: dict[str, Any]) -> bool:
        error_text = account_error_text(account)
        reauth_markers = (
            "401",
            "unauthorized",
            "unauthenticated",
            "invalid token",
            "token_invalidated",
            "token expired",
            "token invalid",
            "access token",
            "refresh token",
            "invalid_grant",
            "missing_access_token",
            "no access token available",
            "session expired",
            "invalid_session",
            "token revoked",
            "login required",
            "reauth",
            "re-auth",
        )
        if text_contains_any(error_text, reauth_markers):
            return True
        return False

    def account_indicates_forbidden(account: dict[str, Any]) -> bool:
        return text_contains_any(account_error_text(account), FORBIDDEN_TEXT_MARKERS)

    def account_indicates_quota_exhausted(account: dict[str, Any], *, five_hour_threshold: float, seven_day_threshold: float) -> bool:
        error_text = account_error_text(account)
        quota_markers = (
            "rate limit",
            "rate_limited",
            "rate limited",
            "rate_limit_exceeded",
            "usage_limit_reached",
            "insufficient_quota",
            "quota",
            "429",
        )
        if any(marker in error_text for marker in quota_markers):
            return True
        codex_5h = account_extra_utilization(account, "codex_5h_used_percent")
        codex_7d = account_extra_utilization(account, "codex_7d_used_percent")
        if (codex_5h is not None and codex_5h >= five_hour_threshold) or (codex_7d is not None and codex_7d >= seven_day_threshold):
            return True
        return bool(account.get("rate_limited_at") or account.get("rate_limit_reset_at"))

    def usage_indicates_quota_exhausted(usage: dict[str, Any], *, five_hour_threshold: float, seven_day_threshold: float) -> bool:
        error_code = str(usage.get("error_code") or "").strip().lower()
        if error_code == "rate_limited":
            return True
        error_text = usage_error_text(usage)
        quota_markers = (
            "usage_limit_reached",
            "insufficient_quota",
            "quota",
            "rate limit",
            "rate_limited",
            "rate limited",
            "429",
        )
        if text_contains_any(error_text, quota_markers):
            return True
        if usage_ai_credits_exhausted(usage):
            return True
        short_window_keys = ("five_hour", "gemini_shared_minute", "gemini_pro_minute", "gemini_flash_minute")
        long_window_keys = ("seven_day", "seven_day_sonnet", "gemini_shared_daily", "gemini_pro_daily", "gemini_flash_daily")
        return bool(
            usage_window_utilization_above_threshold(usage, five_hour_threshold, short_window_keys)
            or usage_window_utilization_above_threshold(usage, seven_day_threshold, long_window_keys)
        )

    def account_prefers_passive_usage(account: dict[str, Any]) -> bool:
        platform = str(account.get("platform") or "").strip().lower()
        account_type = str(account.get("type") or "").strip().lower()
        return platform == "anthropic" and account_type in {"oauth", "setup-token"}

    def resolve_account_usage_source(account: dict[str, Any], source_mode: str) -> str | None:
        normalized = source_mode.strip().lower()
        if not account_prefers_passive_usage(account):
            return None
        if normalized in {"passive", "被动", "被动采样"}:
            return "passive"
        if normalized in {"active", "主动", "主动最新", "主动查询最新"}:
            return "active"
        return "passive"

    def build_account_usage_path(account_id: int, source: str | None) -> str:
        if source in {"passive", "active"}:
            return f"/admin/accounts/{account_id}/usage?source={source}"
        return f"/admin/accounts/{account_id}/usage"

    def bulk_mark_accounts_error(
        account_ids: list[int],
        *,
        progress_callback: Callable[[int, int, str], None],
    ) -> dict[str, Any]:
        ids = unique_ids([account_id for account_id in account_ids if isinstance(account_id, int) and account_id > 0])
        if not ids:
            return {"updated_ids": [], "failed": [], "updated_count": 0, "failed_count": 0}

        updated_ids: list[int] = []
        failed: list[dict[str, Any]] = []
        batch_size = 100
        total_batches = max((len(ids) + batch_size - 1) // batch_size, 1)

        for index in range(0, len(ids), batch_size):
            ensure_not_cancelled()
            batch_ids = ids[index : index + batch_size]
            batch_no = index // batch_size + 1
            try:
                response = get_client().request(
                    "POST",
                    "/admin/accounts/bulk-update",
                    {
                        "account_ids": batch_ids,
                        "status": "error",
                    },
                )
                success_ids = response.get("success_ids") if isinstance(response, dict) else None
                if isinstance(success_ids, list):
                    updated_ids.extend(int(item) for item in success_ids if isinstance(item, int) and item > 0)
                else:
                    updated_ids.extend(batch_ids)
                if isinstance(response, dict):
                    for item in response.get("results") or []:
                        if not isinstance(item, dict) or item.get("success") is True:
                            continue
                        failed.append(
                            {
                                "account_id": item.get("account_id"),
                                "error": str(item.get("error") or "unknown error"),
                            }
                        )
            except Exception as exc:
                error_message = str(exc)
                for account_id in batch_ids:
                    failed.append({"account_id": account_id, "error": error_message})
            progress_callback(
                batch_no,
                total_batches,
                f"正在回写鉴权异常状态：批次 {batch_no}/{total_batches}，成功 {len(updated_ids)} 个，失败 {len(failed)} 个",
            )

        updated_ids = unique_ids(updated_ids)
        failed.sort(key=lambda item: int(item.get("account_id") or 0))
        return {
            "updated_ids": updated_ids,
            "failed": failed,
            "updated_count": len(updated_ids),
            "failed_count": len(failed),
        }

    def sync_account_cache_after_detection(
        *,
        progress_callback: Callable[[int, int, str], None],
        prefix: str,
    ) -> dict[str, Any]:
        target_total = max(len(accounts_cache), 1)

        def nested_progress(current: int, total: int, message: str) -> None:
            safe_total = max(total, 1)
            adjusted_total = target_total + safe_total
            adjusted_current = min(target_total + max(current, 0), adjusted_total)
            progress_callback(adjusted_current, adjusted_total, f"{prefix}，正在同步最新账号状态... {message}")

        return fetch_accounts(progress_callback=nested_progress)

    def iter_detection_target_accounts() -> list[dict[str, Any]]:
        if not accounts_cache:
            raise CLIError("请先点击“同步账号列表”")
        group_id = single_group_id_from_label(delete_group_var.get(), allow_all=True)
        if group_id is None:
            return [dict(account) for account in accounts_cache]
        return [dict(account) for account in accounts_cache if group_id in get_account_group_ids(account)]

    def run_usage_detection(
        *,
        title: str,
        detect_kind: str,
        progress_callback: Callable[[int, int, str], None],
        five_hour_threshold: float,
        seven_day_threshold: float,
    ) -> dict[str, Any]:
        nonlocal detection_label_to_account_id, invalid_401_account_ids_cache, invalid_quota_account_ids_cache
        target_accounts = iter_detection_target_accounts()
        if not target_accounts:
            raise CLIError("当前范围下没有可检测账号")

        detection_concurrency = clamp_detection_concurrency(parse_optional_positive_int(detection_concurrency_var.get(), "检测并发数") or DEFAULT_DETECTION_CONCURRENCY)
        source_mode = detection_usage_source_mode_var.get().strip().lower()
        completed = 0
        problem_accounts: list[dict[str, Any]] = []
        failed_accounts: list[dict[str, Any]] = []
        detected_auth_error_ids: list[int] = []
        detected_quota_ids: list[int] = []

        def detect_one(account: dict[str, Any]) -> dict[str, Any]:
            ensure_not_cancelled()
            account_id = account.get("id")
            if not isinstance(account_id, int) or account_id <= 0:
                return {"account": account, "ok": False, "problem": False, "error": "账号 ID 无效"}
            test_summary = ""
            usage_source = resolve_account_usage_source(account, source_mode)
            usage: dict[str, Any] | None = None
            usage_error: str | None = None
            try:
                _, raw_text = get_client().request_text(
                    "POST",
                    f"/admin/accounts/{account_id}/test",
                    payload={},
                )
                test_result = analyze_account_test_result(raw_text=raw_text)
                test_summary = str(test_result.get("summary") or "")
            except Exception as exc:
                test_result = analyze_account_test_result(error_text=str(exc))

            try:
                usage_result = get_client().request("GET", build_account_usage_path(account_id, usage_source))
                if isinstance(usage_result, dict):
                    usage = usage_result
                else:
                    usage_error = "`/usage` 返回格式异常"
            except Exception as exc:
                usage_error = str(exc)

            is_401 = account_indicates_reauth(account)
            is_403 = account_indicates_forbidden(account)
            is_quota = account_indicates_quota_exhausted(
                account,
                five_hour_threshold=five_hour_threshold,
                seven_day_threshold=seven_day_threshold,
            )
            is_401 = is_401 or bool(test_result.get("is_401"))
            is_403 = is_403 or bool(test_result.get("is_403"))
            is_quota = is_quota or bool(test_result.get("is_quota"))
            if isinstance(usage, dict):
                is_401 = is_401 or usage_indicates_reauth(usage)
                is_403 = is_403 or usage_indicates_forbidden(usage)
                is_quota = is_quota or usage_indicates_quota_exhausted(
                    usage,
                    five_hour_threshold=five_hour_threshold,
                    seven_day_threshold=seven_day_threshold,
                )
            elif usage_error:
                lowered_error = usage_error.lower()
                if "401" in lowered_error or "unauth" in lowered_error or "unauthorized" in lowered_error:
                    is_401 = True
                if "403" in lowered_error or "forbidden" in lowered_error:
                    is_403 = True
                if "429" in lowered_error or "quota" in lowered_error or "rate limit" in lowered_error:
                    is_quota = True
            is_auth_error = is_401 or is_403
            if detect_kind == "auth":
                problem = is_auth_error
            elif detect_kind == "quota":
                problem = is_quota
            else:
                problem = is_auth_error or is_quota
            has_observation = bool(test_result.get("has_success") or test_result.get("has_error") or isinstance(usage, dict))
            if not problem and not has_observation:
                return {
                    "account": account,
                    "ok": False,
                    "problem": False,
                    "error": usage_error or test_summary or "test/usage unavailable",
                    "is_401": is_401,
                    "is_403": is_403,
                    "is_auth_error": is_auth_error,
                    "is_quota": is_quota,
                }
            return {
                "account": account,
                "ok": True,
                "problem": problem,
                "is_401": is_401,
                "is_403": is_403,
                "is_auth_error": is_auth_error,
                "is_quota": is_quota,
                "usage_error": usage_error,
                "usage_source": usage_source or "default",
                "test_summary": test_summary,
            }

        with ThreadPoolExecutor(max_workers=min(detection_concurrency, len(target_accounts))) as executor:
            future_map = {executor.submit(detect_one, account): account for account in target_accounts}
            total = len(future_map)
            pending = set(future_map.keys())
            while pending:
                ensure_not_cancelled()
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    account = future_map[future]
                    completed += 1
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"account": account, "ok": False, "problem": False, "error": str(exc)}
                    if result.get("problem"):
                        problem_accounts.append(dict(account))
                    account_id = int(account.get("id") or 0)
                    if account_id > 0:
                        if result.get("is_auth_error"):
                            detected_auth_error_ids.append(account_id)
                        if result.get("is_quota"):
                            detected_quota_ids.append(account_id)
                    if not result.get("ok"):
                        failed_accounts.append(
                            {
                                "account_id": account.get("id"),
                                "name": account.get("name"),
                                "error": result.get("error") or "unknown error",
                            }
                        )
                    progress_callback(
                        completed,
                        total,
                        f"{title}：已检测 {completed}/{total} 个账号，命中 {len(problem_accounts)} 个，失败 {len(failed_accounts)} 个",
                    )
            executor.shutdown(wait=False, cancel_futures=True)

        synced_auth_errors_result: dict[str, Any] | None = None
        synced_accounts_result: dict[str, Any] | None = None
        if detected_auth_error_ids:
            synced_auth_errors_result = bulk_mark_accounts_error(
                detected_auth_error_ids,
                progress_callback=lambda current, total, message: progress_callback(
                    len(target_accounts) + current,
                    len(target_accounts) + max(total, 1),
                    message,
                ),
            )

        if detected_auth_error_ids or detected_quota_ids:
            sync_prefix = "检测完成"
            if detected_auth_error_ids and detected_quota_ids:
                sync_prefix = "鉴权异常 / 无额度状态更新完成"
            elif detected_auth_error_ids:
                sync_prefix = "鉴权异常状态回写完成"
            elif detected_quota_ids:
                sync_prefix = "无额度检测完成"
            synced_accounts_result = sync_account_cache_after_detection(
                progress_callback=progress_callback,
                prefix=sync_prefix,
            )

        problem_accounts.sort(key=lambda item: int(item.get("id") or 0))
        invalid_401_account_ids_cache = unique_ids(detected_auth_error_ids)
        invalid_quota_account_ids_cache = unique_ids(detected_quota_ids)
        detection_label_to_account_id = {}
        labels: list[str] = []
        for account in problem_accounts:
            account_id = int(account.get("id") or 0)
            issues: list[str] = []
            if account_id in invalid_401_account_ids_cache:
                issues.append("401/403")
            if account_id in invalid_quota_account_ids_cache:
                issues.append("无额度")
            issue_text = " / ".join(issues) if issues else ("401/403" if detect_kind == "auth" else ("无额度" if detect_kind == "quota" else "问题"))
            label = format_detection_label(account, issue_text)
            labels.append(label)
            if account_id > 0:
                detection_label_to_account_id[label] = account_id

        return {
            "scope_account_count": len(target_accounts),
            "detection_concurrency": detection_concurrency,
            "usage_source_mode": source_mode or "auto",
            "detected_auth_error_count": len(invalid_401_account_ids_cache),
            "detected_quota_count": len(invalid_quota_account_ids_cache),
            "problem_count": len(problem_accounts),
            "problem_account_ids": [int(item.get("id") or 0) for item in problem_accounts if int(item.get("id") or 0) > 0],
            "problem_labels_preview": labels[:20],
            "failed_count": len(failed_accounts),
            "failed_preview": failed_accounts[:20],
            "status_synced_auth_count": parse_int_field((synced_auth_errors_result or {}).get("updated_count"), 0),
            "status_sync_failed_count": parse_int_field((synced_auth_errors_result or {}).get("failed_count"), 0),
            "synced_account_count": parse_int_field((synced_accounts_result or {}).get("account_count"), 0),
        }

    def refresh_detection_account_states(progress_callback: Callable[[int, int, str], None]) -> dict[str, Any]:
        target_accounts = iter_detection_target_accounts()
        if not target_accounts:
            raise CLIError("当前范围下没有可刷新的账号")

        refresh_concurrency = clamp_detection_concurrency(
            parse_optional_positive_int(detection_concurrency_var.get(), "检测并发数") or DEFAULT_DETECTION_CONCURRENCY
        )
        completed = 0
        refreshed_count = 0
        failed_refreshes: list[dict[str, Any]] = []
        detected_auth_error_ids: list[int] = []
        detected_quota_ids: list[int] = []
        progress_callback(0, len(target_accounts), f"开始手动刷新状态，共 {len(target_accounts)} 个账号，并发 {refresh_concurrency}")

        def refresh_one(account: dict[str, Any]) -> dict[str, Any]:
            ensure_not_cancelled()
            account_id = int(account.get("id") or 0)
            if account_id <= 0:
                return {
                    "account_id": account.get("id"),
                    "name": account.get("name"),
                    "ok": False,
                    "message": "账号 ID 无效",
                }
            try:
                status, raw_text = get_client().request_text(
                    "POST",
                    f"/admin/accounts/{account_id}/test",
                    payload={},
                )
                test_result = analyze_account_test_result(raw_text=raw_text)
                is_auth_error = bool(test_result.get("is_401") or test_result.get("is_403"))
                is_quota = bool(test_result.get("is_quota"))
                if test_result.get("has_error") and not test_result.get("ok"):
                    return {
                        "account_id": account_id,
                        "name": account.get("name"),
                        "ok": False,
                        "message": str(test_result.get("summary") or raw_text[:300].strip() or f"HTTP {status}"),
                        "is_auth_error": is_auth_error,
                        "is_quota": is_quota,
                    }
                return {
                    "account_id": account_id,
                    "name": account.get("name"),
                    "ok": True,
                    "message": str(test_result.get("summary") or raw_text[:160].strip() or f"HTTP {status}"),
                    "is_auth_error": is_auth_error,
                    "is_quota": is_quota,
                }
            except Exception as exc:
                test_result = analyze_account_test_result(error_text=str(exc))
                return {
                    "account_id": account_id,
                    "name": account.get("name"),
                    "ok": False,
                    "message": str(test_result.get("summary") or exc),
                    "is_auth_error": bool(test_result.get("is_401") or test_result.get("is_403")),
                    "is_quota": bool(test_result.get("is_quota")),
                }

        with ThreadPoolExecutor(max_workers=min(refresh_concurrency, len(target_accounts))) as executor:
            future_map = {executor.submit(refresh_one, account): account for account in target_accounts}
            total = len(future_map)
            pending = set(future_map.keys())
            while pending:
                ensure_not_cancelled()
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    result = future.result()
                    completed += 1
                    account_id = int(result.get("account_id") or 0)
                    if account_id > 0:
                        if result.get("is_auth_error"):
                            detected_auth_error_ids.append(account_id)
                        if result.get("is_quota"):
                            detected_quota_ids.append(account_id)
                    if result.get("ok"):
                        refreshed_count += 1
                    else:
                        failed_refreshes.append(
                            {
                                "account_id": result.get("account_id"),
                                "name": result.get("name"),
                                "error": result.get("message") or "unknown error",
                            }
                        )
                    progress_callback(
                        completed,
                        total,
                        f"手动刷新状态：已完成 {completed}/{total} 个，成功 {refreshed_count} 个，失败 {len(failed_refreshes)} 个",
                    )
            executor.shutdown(wait=False, cancel_futures=True)

        auth_status_sync_result: dict[str, Any] | None = None
        if detected_auth_error_ids:
            auth_status_sync_result = bulk_mark_accounts_error(
                detected_auth_error_ids,
                progress_callback=lambda current, total, message: progress_callback(
                    len(target_accounts) + current,
                    len(target_accounts) + max(total, 1),
                    message,
                ),
            )

        account_sync_progress_base = max(len(target_accounts), 1)

        def sync_progress(current: int, total: int, message: str) -> None:
            adjusted_total = account_sync_progress_base + max(total, 1)
            adjusted_current = min(account_sync_progress_base + max(current, 0), adjusted_total)
            progress_callback(
                adjusted_current,
                adjusted_total,
                f"手动刷新状态完成，正在同步最新账号状态... {message}",
            )

        accounts_result = fetch_accounts(progress_callback=sync_progress)
        failed_refreshes.sort(key=lambda item: int(item.get("account_id") or 0))
        return {
            "scope_account_count": len(target_accounts),
            "refresh_concurrency": refresh_concurrency,
            "refreshed_count": refreshed_count,
            "detected_auth_error_count": len(unique_ids(detected_auth_error_ids)),
            "detected_quota_count": len(unique_ids(detected_quota_ids)),
            "status_synced_auth_count": parse_int_field((auth_status_sync_result or {}).get("updated_count"), 0),
            "failed_refresh_count": len(failed_refreshes),
            "failed_refresh_preview": failed_refreshes[:20],
            "synced_account_count": accounts_result.get("account_count", 0),
        }

    def build_gui_config() -> GUIConfig:
        width = coerce_optional_positive_int(root.winfo_width())
        height = coerce_optional_positive_int(root.winfo_height())
        if width is not None and width < 200:
            width = None
        if height is not None and height < 200:
            height = None
        return GUIConfig(
            base_url=base_url_var.get().strip(),
            admin_api_key=admin_key_var.get().strip(),
            sync_concurrency=clamp_sync_concurrency(parse_optional_positive_int(sync_concurrency_var.get(), "同步并发数") or DEFAULT_SYNC_CONCURRENCY),
            detection_concurrency=clamp_detection_concurrency(parse_optional_positive_int(detection_concurrency_var.get(), "检测并发数") or DEFAULT_DETECTION_CONCURRENCY),
            delete_concurrency=clamp_delete_concurrency(parse_optional_positive_int(delete_concurrency_var.get(), "删除并发数") or DEFAULT_DELETE_CONCURRENCY),
            bulk_page_size=clamp_admin_list_page_size(parse_optional_positive_int(page_size_var.get(), "每次读取数量") or 100),
            bulk_batch_size=parse_optional_positive_int(batch_size_var.get(), "每次提交数量") or 100,
            window_width=width,
            window_height=height,
        )

    def save_gui_config_action(*, show_message: bool) -> Path:
        path = save_gui_config(build_gui_config())
        status_var.set("本地配置已保存，下次打开会自动载入。")
        if show_message:
            messagebox.showinfo("保存成功", f"配置已保存到：\n{path}")
        return path

    def get_client(*, require_admin_key: bool = True) -> AdminAPIClient:
        base_url = non_empty(base_url_var.get())
        admin_key = non_empty(admin_key_var.get())
        if not base_url:
            raise CLIError("网站地址不能为空")
        if require_admin_key and not admin_key:
            raise CLIError("管理员 API Key 不能为空")

        return AdminAPIClient(
            base_url,
            admin_api_key=admin_key,
            bearer_token=None,
            login=LoginOptions(email=None, password=None, turnstile_token=None, totp_code=None),
            timeout=default_timeout,
            insecure=default_insecure,
            user_agent=DEFAULT_USER_AGENT,
        )

    def format_account_label(account: dict[str, Any]) -> str:
        account_id = int(account.get("id") or 0)
        name = str(account.get("name") or f"account-{account_id}")
        platform = str(account.get("platform") or "unknown")
        account_type = str(account.get("type") or "unknown")
        status = str(account.get("status") or "unknown")
        return f"[{account_id}] {name} ({platform}/{account_type}/{status})"

    def format_proxy_label(proxy: dict[str, Any]) -> str:
        proxy_id = int(proxy.get("id") or 0)
        protocol = str(proxy.get("protocol") or "unknown")
        host = str(proxy.get("host") or "unknown")
        port = str(proxy.get("port") or "?")
        status = str(proxy.get("status") or "unknown")
        account_count = parse_int_field(proxy.get("account_count"), 0)
        suffix = f"，已关联 {account_count} 个账号" if account_count > 0 else ""
        return f"[{proxy_id}] {protocol}://{host}:{port} ({status}{suffix})"

    def get_account_group_ids(account: dict[str, Any]) -> list[int]:
        raw = account.get("group_ids")
        if not isinstance(raw, list):
            return []
        return [group_id for group_id in raw if isinstance(group_id, int) and group_id > 0]

    def listbox_items(listbox: Any) -> list[str]:
        return [str(item) for item in listbox.get(0, tk.END)]

    def set_listbox_items(listbox: Any, labels: list[str]) -> None:
        listbox.delete(0, tk.END)
        for label in labels:
            listbox.insert(tk.END, label)

    def add_listbox_item(listbox: Any, label: str) -> None:
        existing = set(listbox_items(listbox))
        if label not in existing:
            listbox.insert(tk.END, label)

    def remove_selected_listbox_items(listbox: Any) -> None:
        for index in reversed(tuple(listbox.curselection())):
            listbox.delete(index)

    def ids_from_listbox(listbox: Any, mapping: dict[str, int], label: str) -> list[int]:
        labels = listbox_items(listbox)
        if not labels:
            raise CLIError(f"请先选择{label}")
        ids: list[int] = []
        for item_label in labels:
            item_id = mapping.get(item_label)
            if item_id is None:
                raise CLIError(f"{label}下拉值已过期，请重新同步列表")
            ids.append(item_id)
        return unique_ids(ids)

    def single_group_id_from_label(value: str, *, allow_all: bool = True) -> int | None:
        selected = value.strip()
        if not selected:
            return None
        if allow_all and selected == GROUP_FILTER_ALL:
            return None
        group_id = group_label_to_id.get(selected)
        if group_id is None:
            raise CLIError("分组下拉值无效，请先点击“同步分组”")
        return group_id

    def format_group_combo_label(group: dict[str, Any]) -> str:
        group_id = int(group.get("id") or 0)
        name = str(group.get("name") or f"group-{group_id}")
        platform = str(group.get("platform") or "unknown")
        status = str(group.get("status") or "unknown")
        visibility = "专属" if bool(group.get("is_exclusive")) else "公开"
        return f"[{group_id}] {name} ({platform}/{status}/{visibility})"

    def apply_groups_to_combos(groups: list[dict[str, Any]]) -> None:
        nonlocal groups_cache, group_label_to_id
        groups_cache = groups
        group_label_to_id = {}
        labels: list[str] = []
        for group in groups:
            group_id = group["id"]
            label = format_group_combo_label(group)
            labels.append(label)
            group_label_to_id[label] = group_id

        filter_values = [GROUP_FILTER_ALL, *labels]
        update_values = [GROUP_UPDATE_KEEP, GROUP_UPDATE_CLEAR, *labels]
        for combo in group_filter_combos:
            combo.configure(values=filter_values)
            if combo.get() not in filter_values:
                combo.set(GROUP_FILTER_ALL)
        for combo in group_update_combos:
            combo.configure(values=update_values)
            if combo.get() not in update_values:
                combo.set(GROUP_UPDATE_KEEP)

    def apply_accounts_to_combos(accounts: list[dict[str, Any]]) -> None:
        nonlocal accounts_cache, account_label_to_id, account_id_to_label, detection_label_to_account_id, invalid_401_account_ids_cache, invalid_quota_account_ids_cache
        accounts_cache = accounts
        account_label_to_id = {}
        account_id_to_label = {}
        detection_label_to_account_id = {}
        invalid_401_account_ids_cache = []
        invalid_quota_account_ids_cache = []
        labels: list[str] = []
        for account in accounts:
            account_id = account.get("id")
            if not isinstance(account_id, int) or account_id <= 0:
                continue
            label = format_account_label(account)
            labels.append(label)
            account_label_to_id[label] = account_id
            account_id_to_label[account_id] = label

        values = [ACCOUNT_PICKER_HINT, *labels]
        for combo in account_picker_combos:
            combo.configure(values=values)
            if combo.get() not in values:
                combo.set(ACCOUNT_PICKER_HINT)
        try:
            root.after(0, update_account_selection_summary)
        except NameError:
            pass

    def apply_proxies_to_widgets(proxies: list[dict[str, Any]]) -> None:
        nonlocal proxies_cache, proxy_label_to_id
        proxies_cache = proxies
        proxy_label_to_id = {}
        labels: list[str] = []
        for proxy in proxies:
            proxy_id = proxy.get("id")
            if not isinstance(proxy_id, int) or proxy_id <= 0:
                continue
            label = format_proxy_label(proxy)
            labels.append(label)
            proxy_label_to_id[label] = proxy_id

        picker_values = [PROXY_PICKER_HINT, *labels]
        update_values = [PROXY_UPDATE_KEEP, PROXY_UPDATE_CLEAR, *labels]
        for combo in proxy_picker_combos:
            combo.configure(values=picker_values)
            if combo.get() not in picker_values:
                combo.set(PROXY_PICKER_HINT)
        for combo in proxy_update_combos:
            combo.configure(values=update_values)
            if combo.get() not in update_values:
                combo.set(PROXY_UPDATE_KEEP)

    def fetch_groups(progress_callback: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
        if progress_callback:
            progress_callback(0, 1, "正在同步分组，准备拉取分组列表...")
        payload = get_client().request("GET", "/admin/groups/all")
        if not isinstance(payload, list):
            raise CLIError("`/admin/groups/all` 返回格式异常，期望数组")
        parsed: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            group_id = item.get("id")
            if not isinstance(group_id, int) or group_id <= 0:
                continue
            parsed.append(
                {
                    "id": group_id,
                    "name": str(item.get("name") or f"group-{group_id}"),
                    "platform": str(item.get("platform") or "unknown"),
                    "status": str(item.get("status") or "unknown"),
                    "is_exclusive": bool(item.get("is_exclusive")),
                }
            )
        parsed.sort(key=lambda x: (x["platform"], x["name"], x["id"]))
        root.after(0, lambda: apply_groups_to_combos(parsed))
        if progress_callback:
            progress_callback(1, 1, f"分组同步完成，共 {len(parsed)} 个分组")
        return {"group_count": len(parsed), "groups": parsed}

    def fetch_accounts(progress_callback: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
        sync_concurrency = clamp_sync_concurrency(parse_optional_positive_int(sync_concurrency_var.get(), "同步并发数") or DEFAULT_SYNC_CONCURRENCY)
        parsed = list_all_accounts(
            get_client(),
            page_size=ADMIN_LIST_PAGE_SIZE_CAP,
            concurrency=sync_concurrency,
            progress_callback=progress_callback,
            cancel_callback=ensure_not_cancelled,
        )
        parsed.sort(key=lambda item: (str(item.get("platform") or ""), str(item.get("name") or ""), int(item.get("id") or 0)))
        root.after(0, lambda: apply_accounts_to_combos(parsed))
        return {"account_count": len(parsed), "sync_concurrency": sync_concurrency, "accounts": parsed}

    def fetch_proxies(progress_callback: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
        if progress_callback:
            progress_callback(0, 1, "正在同步代理，准备拉取代理列表...")
        parsed = list_all_proxies(get_client())
        parsed.sort(key=lambda item: (str(item.get("host") or ""), parse_int_field(item.get("port"), 0), int(item.get("id") or 0)))
        root.after(0, lambda: apply_proxies_to_widgets(parsed))
        if progress_callback:
            progress_callback(1, 1, f"代理同步完成，共 {len(parsed)} 个代理")
        return {"proxy_count": len(parsed), "proxies": parsed}

    def sync_reference_data(progress_callback: Callable[[int, int, str], None]) -> dict[str, Any]:
        progress_callback(0, 3, "开始同步数据：1/3 正在同步分组...")
        groups_result = fetch_groups(
            progress_callback=lambda current, total, message: progress_callback(current, total, message),
        )
        progress_callback(1, 3, f"开始同步数据：1/3 分组完成，共 {groups_result.get('group_count', 0)} 个；准备同步代理...")
        proxies_result = fetch_proxies(
            progress_callback=lambda current, total, message: progress_callback(current, total, message),
        )
        progress_callback(2, 3, f"开始同步数据：2/3 代理完成，共 {proxies_result.get('proxy_count', 0)} 个；准备同步账号...")
        accounts_result = fetch_accounts(
            progress_callback=lambda current, total, message: progress_callback(current, total, message),
        )
        progress_callback(
            3,
            3,
            "数据同步完成："
            f"分组 {groups_result.get('group_count', 0)} 个，"
            f"代理 {proxies_result.get('proxy_count', 0)} 个，"
            f"账号 {accounts_result.get('account_count', 0)} 个",
        )
        return {
            "groups": groups_result.get("group_count", 0),
            "proxies": proxies_result.get("proxy_count", 0),
            "accounts": accounts_result.get("account_count", 0),
        }

    def run_action(
        title: str,
        action: Callable[[Callable[[int, int, str], None]], Any],
        *,
        determinate: bool = False,
    ) -> None:
        nonlocal current_cancel_event
        current_cancel_event = threading.Event()
        set_busy(True)
        status_var.set(f"{title} 执行中...")
        if determinate:
            set_progress(determinate=True, current=0, total=1, message=f"{title} 准备中...")
        else:
            set_progress(determinate=False, message=f"{title} 执行中...")

        def progress_callback(current: int, total: int, message: str) -> None:
            if current_cancel_event is not None and current_cancel_event.is_set():
                raise TaskCancelled(f"{title} 已停止")
            safe_after(0, lambda: set_progress(determinate=True, current=current, total=total, message=message))

        def worker() -> None:
            try:
                payload = action(progress_callback)
            except TaskCancelled as exc:
                message = str(exc)

                def on_cancel() -> None:
                    nonlocal current_cancel_event
                    current_cancel_event = None
                    set_progress(determinate=True, current=0, total=1, message=f"{title} 已停止")
                    status_var.set(f"{title} 已停止")
                    output.insert("1.0", json.dumps({"cancelled": True, "message": message}, ensure_ascii=False, indent=2))
                    set_busy(False)

                safe_after(0, on_cancel)
                return
            except Exception as exc:
                message = str(exc)

                def on_error() -> None:
                    nonlocal current_cancel_event
                    current_cancel_event = None
                    set_progress(determinate=True, current=0, total=1, message=f"{title} 失败")
                    status_var.set(f"{title} 失败")
                    messagebox.showerror("执行失败", message)
                    set_busy(False)

                safe_after(0, on_error)
                return

            def on_success() -> None:
                nonlocal current_cancel_event
                current_cancel_event = None
                render_payload(payload)
                set_progress(determinate=True, current=1, total=1, message=f"{title} 完成")
                status_var.set(f"{title} 完成")
                set_busy(False)

            safe_after(0, on_success)

        threading.Thread(target=worker, daemon=True).start()

    def add_tab(name: str, *, scrollable: bool = True) -> ttk.Frame:
        if not scrollable:
            frame = ttk.Frame(notebook, padding=10)
            frame.columnconfigure(1, weight=1)
            notebook.add(frame, text=name)
            return frame

        outer = ttk.Frame(notebook)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        frame = ttk.Frame(canvas, padding=10)
        frame.columnconfigure(1, weight=1)
        window_id = canvas.create_window((0, 0), window=frame, anchor="nw")
        pending_scrollregion = False
        pending_fit_width = False
        last_canvas_width = 0

        def update_scrollregion() -> None:
            nonlocal pending_scrollregion
            pending_scrollregion = False
            if not canvas.winfo_exists():
                return
            bbox = canvas.bbox("all")
            if bbox:
                canvas.configure(scrollregion=bbox)

        def schedule_scrollregion(_event: Any = None) -> None:
            nonlocal pending_scrollregion
            if pending_scrollregion:
                return
            pending_scrollregion = True
            canvas.after_idle(update_scrollregion)

        def fit_frame_width() -> None:
            nonlocal pending_fit_width, last_canvas_width
            pending_fit_width = False
            if not canvas.winfo_exists():
                return
            width = max(int(canvas.winfo_width()), 1)
            if width == last_canvas_width:
                return
            last_canvas_width = width
            canvas.itemconfigure(window_id, width=width)

        def schedule_fit_frame_width(_event: Any = None) -> None:
            nonlocal pending_fit_width
            if pending_fit_width:
                return
            pending_fit_width = True
            canvas.after_idle(fit_frame_width)

        def handle_mousewheel(event: Any) -> None:
            delta = getattr(event, "delta", 0)
            if delta:
                canvas.yview_scroll(int(-delta / 120), "units")

        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<Configure>", schedule_fit_frame_width)
        frame.bind("<Configure>", schedule_scrollregion)
        outer.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", handle_mousewheel))
        outer.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))

        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        notebook.add(outer, text=name)
        return frame

    def add_file_row(frame: ttk.Frame, row: int, label: str, var: tk.StringVar) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(frame, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(frame, text="选择文件", command=lambda: pick_json(var)).grid(row=row, column=2, padx=(8, 0), pady=4)

    top_button_frame = ttk.Frame(top)
    top_button_frame.grid(row=0, column=4, rowspan=2, sticky="ns", padx=(10, 0))
    top_verify_btn = ttk.Button(
        top_button_frame,
        text="检查连接",
        command=lambda: run_action("检查连接", lambda _progress: get_client().request("GET", "/admin/settings/admin-api-key")),
    )
    top_verify_btn.grid(row=0, column=0, sticky="ew", pady=(0, 4))
    action_buttons.append(top_verify_btn)
    top_save_btn = ttk.Button(
        top_button_frame,
        text="保存配置",
        command=safe_ui_action(lambda: save_gui_config_action(show_message=True)),
    )
    top_save_btn.grid(row=1, column=0, sticky="ew", pady=(0, 4))
    action_buttons.append(top_save_btn)
    top_sync_btn = ttk.Button(
        top_button_frame,
        text="同步数据",
        command=lambda: run_action("同步数据", sync_reference_data, determinate=True),
    )
    top_sync_btn.grid(row=2, column=0, sticky="ew", pady=(0, 4))
    action_buttons.append(top_sync_btn)
    top_clear_btn = ttk.Button(top_button_frame, text="清空结果", command=lambda: output.delete("1.0", tk.END))
    top_clear_btn.grid(row=3, column=0, sticky="ew")
    action_buttons.append(top_clear_btn)
    stop_task_btn = ttk.Button(top_button_frame, text="停止任务", command=request_cancel_current_task, state="disabled")
    stop_task_btn.grid(row=4, column=0, sticky="ew", pady=(4, 0))

    # 开始使用
    tab_test = add_tab("开始使用", scrollable=False)
    tab_test.columnconfigure(0, weight=1)
    ttk.Label(tab_test, text="第一次使用建议先完成下面三步。", style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
    ttk.Label(
        tab_test,
        text="1. 检查连接：确认网站地址和管理员 API Key 可用。\n2. 保存配置：以后再次打开时会自动带出网站地址和管理员 API Key。\n3. 同步数据：把分组、账号、代理都拉下来，后面各页就能直接下拉选择。",
        style="Hint.TLabel",
        justify="left",
        wraplength=980,
    ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 10))
    verify_btn = ttk.Button(
        tab_test,
        text="1. 检查连接",
        command=lambda: run_action(
            "检查连接",
            lambda _progress: get_client().request("GET", "/admin/settings/admin-api-key"),
        ),
    )
    verify_btn.grid(row=2, column=0, sticky="w")
    action_buttons.append(verify_btn)
    save_btn = ttk.Button(tab_test, text="2. 保存配置", command=safe_ui_action(lambda: save_gui_config_action(show_message=True)))
    save_btn.grid(row=2, column=1, sticky="w", padx=(8, 0))
    action_buttons.append(save_btn)
    public_btn = ttk.Button(
        tab_test,
        text="查看站点公开信息",
        command=lambda: run_action(
            "查看站点公开信息",
            lambda _progress: get_client(require_admin_key=False).request("GET", "/settings/public", auth_required=False),
        ),
    )
    public_btn.grid(row=2, column=2, sticky="w", padx=(8, 0))
    action_buttons.append(public_btn)
    sync_btn = ttk.Button(tab_test, text="3. 同步数据", command=lambda: run_action("同步数据", sync_reference_data, determinate=True))
    sync_btn.grid(row=2, column=3, sticky="w", padx=(8, 0))
    action_buttons.append(sync_btn)

    # 运行设置
    tab_runtime_settings = add_tab("运行设置", scrollable=False)
    tab_runtime_settings.columnconfigure(1, weight=1)
    ttk.Label(
        tab_runtime_settings,
        text="这里仅保留 0.1.104 和当前桌面工具直接相关的少量设置：Claude Code 最低/最高版本限制，以及是否允许未分组 Key 调度。",
        style="Title.TLabel",
        justify="left",
        wraplength=980,
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
    ttk.Label(
        tab_runtime_settings,
        text="版本号留空表示不限制，格式示例：2.1.63。保存后会直接写回网站的 /admin/settings。",
        style="Hint.TLabel",
        justify="left",
        wraplength=980,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

    min_claude_code_version_var = tk.StringVar()
    max_claude_code_version_var = tk.StringVar()
    allow_ungrouped_key_scheduling_var = tk.BooleanVar(value=False)

    ttk.Label(tab_runtime_settings, text="最低 Claude Code 版本").grid(row=2, column=0, sticky="w", pady=3)
    ttk.Entry(tab_runtime_settings, textvariable=min_claude_code_version_var).grid(row=2, column=1, sticky="ew", pady=3)
    ttk.Label(tab_runtime_settings, text="最高 Claude Code 版本").grid(row=3, column=0, sticky="w", pady=3)
    ttk.Entry(tab_runtime_settings, textvariable=max_claude_code_version_var).grid(row=3, column=1, sticky="ew", pady=3)
    ttk.Checkbutton(
        tab_runtime_settings,
        text="允许未分组 Key 调度（关闭时未分组 Key 默认返回 403）",
        variable=allow_ungrouped_key_scheduling_var,
    ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 3))

    def normalize_optional_semver_input(value: str, label: str) -> str:
        text = value.strip()
        if not text:
            return ""
        if not re.fullmatch(r"\d+\.\d+\.\d+", text):
            raise CLIError(f"{label} 必须是 semver 版本号，例如 2.1.63；留空表示不限制")
        return text

    def apply_runtime_settings(payload: dict[str, Any]) -> None:
        min_claude_code_version_var.set(str(payload.get("min_claude_code_version") or "").strip())
        max_claude_code_version_var.set(str(payload.get("max_claude_code_version") or "").strip())
        allow_ungrouped_key_scheduling_var.set(bool(payload.get("allow_ungrouped_key_scheduling")))

    def load_runtime_settings_action(_progress_callback: Callable[[int, int, str], None]) -> Any:
        payload = get_client().request("GET", "/admin/settings")
        if not isinstance(payload, dict):
            raise CLIError("`/admin/settings` 返回格式异常：期望对象")
        safe_after(0, lambda data=dict(payload): apply_runtime_settings(data))
        return {
            "min_claude_code_version": str(payload.get("min_claude_code_version") or "").strip(),
            "max_claude_code_version": str(payload.get("max_claude_code_version") or "").strip(),
            "allow_ungrouped_key_scheduling": bool(payload.get("allow_ungrouped_key_scheduling")),
        }

    def save_runtime_settings_action(_progress_callback: Callable[[int, int, str], None]) -> Any:
        min_version = normalize_optional_semver_input(min_claude_code_version_var.get(), "最低 Claude Code 版本")
        max_version = normalize_optional_semver_input(max_claude_code_version_var.get(), "最高 Claude Code 版本")
        if min_version and max_version and normalize_version_tag(max_version) < normalize_version_tag(min_version):
            raise CLIError("最高 Claude Code 版本不能小于最低 Claude Code 版本")
        payload = {
            "min_claude_code_version": min_version,
            "max_claude_code_version": max_version,
            "allow_ungrouped_key_scheduling": bool(allow_ungrouped_key_scheduling_var.get()),
        }
        return get_client().request("PUT", "/admin/settings", payload)

    runtime_settings_load_btn = ttk.Button(tab_runtime_settings, text="读取当前设置", command=lambda: run_action("读取运行设置", load_runtime_settings_action))
    runtime_settings_load_btn.grid(row=5, column=0, sticky="w", pady=(8, 0))
    action_buttons.append(runtime_settings_load_btn)
    runtime_settings_save_btn = ttk.Button(tab_runtime_settings, text="保存到网站", command=lambda: run_action("保存运行设置", save_runtime_settings_action))
    runtime_settings_save_btn.grid(row=5, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
    action_buttons.append(runtime_settings_save_btn)

    # 账号检测
    tab_delete_accounts = add_tab("账号检测", scrollable=False)
    tab_delete_accounts.columnconfigure(0, weight=1)
    tab_delete_accounts.rowconfigure(8, weight=1)
    ttk.Label(
        tab_delete_accounts,
        text="先同步账号列表，再按当前分组范围检测 401/403 鉴权异常和无额度账号。检测会优先执行 /test 刷新运行时状态，再按所选用量来源补充读取 /usage；Anthropic OAuth/SetupToken 默认走被动采样，命中后会同步状态并可直接载入列表。",
        style="Title.TLabel",
        justify="left",
        wraplength=980,
    ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))

    delete_group_var = tk.StringVar(value=GROUP_FILTER_ALL)
    detect_five_hour_threshold_var = tk.StringVar(value="99")
    detect_seven_day_threshold_var = tk.StringVar(value="99")
    detection_usage_source_mode_var = tk.StringVar(value="自动（Anthropic 被动，其它主动）")
    ttk.Label(tab_delete_accounts, text="当前检测分组").grid(row=1, column=0, sticky="w", pady=3)
    delete_group_combo = ttk.Combobox(tab_delete_accounts, textvariable=delete_group_var, values=[GROUP_FILTER_ALL], state="readonly")
    delete_group_combo.grid(row=1, column=1, sticky="ew", pady=3)
    group_filter_combos.append(delete_group_combo)

    def load_group_accounts_to_delete() -> None:
        if not accounts_cache:
            raise CLIError("请先点击“同步账号列表”")
        group_id = single_group_id_from_label(delete_group_var.get(), allow_all=True)
        if group_id is None:
            labels = [format_account_label(account) for account in accounts_cache]
        else:
            labels = [format_account_label(account) for account in accounts_cache if group_id in get_account_group_ids(account)]
        if not labels:
            raise CLIError("当前范围下没有账号")
        set_listbox_items(delete_accounts_listbox, labels)

    delete_sync_accounts_btn = ttk.Button(tab_delete_accounts, text="同步账号列表", command=lambda: run_action("同步账号列表", fetch_accounts, determinate=True))
    delete_sync_accounts_btn.grid(row=1, column=2, sticky="w", padx=(8, 0), pady=3)
    action_buttons.append(delete_sync_accounts_btn)
    delete_group_load_btn = ttk.Button(tab_delete_accounts, text="载入当前范围账号", command=safe_ui_action(load_group_accounts_to_delete))
    delete_group_load_btn.grid(row=1, column=3, sticky="w", padx=(8, 0), pady=3)
    action_buttons.append(delete_group_load_btn)

    ttk.Label(tab_delete_accounts, text="检测并发数").grid(row=2, column=0, sticky="w", pady=3)
    ttk.Entry(tab_delete_accounts, textvariable=detection_concurrency_var, width=10).grid(row=2, column=1, sticky="w", pady=3)
    ttk.Label(tab_delete_accounts, text="删除并发数").grid(row=2, column=2, sticky="w", pady=3)
    ttk.Entry(tab_delete_accounts, textvariable=delete_concurrency_var, width=10).grid(row=2, column=3, sticky="w", pady=3)

    detection_options_frame = ttk.Frame(tab_delete_accounts)
    detection_options_frame.grid(row=3, column=0, columnspan=4, sticky="ew", pady=3)
    detection_options_frame.columnconfigure(1, weight=1)
    detection_options_frame.columnconfigure(3, weight=1)
    detection_options_frame.columnconfigure(5, weight=1)
    ttk.Label(detection_options_frame, text="用量来源").grid(row=0, column=0, sticky="w")
    ttk.Combobox(
        detection_options_frame,
        textvariable=detection_usage_source_mode_var,
        values=["自动（Anthropic 被动，其它主动）", "被动采样", "主动查询最新"],
        state="readonly",
        width=24,
    ).grid(row=0, column=1, sticky="ew", padx=(0, 10))
    ttk.Label(detection_options_frame, text="5小时阈值(%)").grid(row=0, column=2, sticky="w")
    ttk.Entry(detection_options_frame, textvariable=detect_five_hour_threshold_var, width=10).grid(row=0, column=3, sticky="ew", padx=(0, 10))
    ttk.Label(detection_options_frame, text="7天阈值(%)").grid(row=0, column=4, sticky="w")
    ttk.Entry(detection_options_frame, textvariable=detect_seven_day_threshold_var, width=10).grid(row=0, column=5, sticky="ew")

    def detect_401_accounts_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        return run_usage_detection(
            title="401/403 检测",
            detect_kind="auth",
            progress_callback=progress_callback,
            five_hour_threshold=parse_optional_float(detect_five_hour_threshold_var.get(), "5小时阈值", min_value=0.0) or 99.0,
            seven_day_threshold=parse_optional_float(detect_seven_day_threshold_var.get(), "7天阈值", min_value=0.0) or 99.0,
        )

    def detect_quota_accounts_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        return run_usage_detection(
            title="无额度检测",
            detect_kind="quota",
            progress_callback=progress_callback,
            five_hour_threshold=parse_optional_float(detect_five_hour_threshold_var.get(), "5小时阈值", min_value=0.0) or 99.0,
            seven_day_threshold=parse_optional_float(detect_seven_day_threshold_var.get(), "7天阈值", min_value=0.0) or 99.0,
        )

    def detect_all_accounts_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        return run_usage_detection(
            title="完整检测",
            detect_kind="all",
            progress_callback=progress_callback,
            five_hour_threshold=parse_optional_float(detect_five_hour_threshold_var.get(), "5小时阈值", min_value=0.0) or 99.0,
            seven_day_threshold=parse_optional_float(detect_seven_day_threshold_var.get(), "7天阈值", min_value=0.0) or 99.0,
        )

    def refresh_account_states_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        return refresh_detection_account_states(progress_callback)

    def load_detected_accounts(account_ids: list[int], issue_label: str) -> None:
        if not account_ids:
            raise CLIError(f"当前没有检测到{issue_label}账号")
        labels: list[str] = []
        for account_id in account_ids:
            account = next((item for item in accounts_cache if int(item.get("id") or 0) == account_id), None)
            if not account:
                continue
            label = format_detection_label(account, issue_label)
            detection_label_to_account_id[label] = account_id
            labels.append(label)
        if not labels:
            raise CLIError("检测结果对应的账号已失效，请重新同步账号列表后再检测")
        set_listbox_items(delete_accounts_listbox, labels)

    def load_all_detected_accounts() -> None:
        combined_ids = unique_ids([*invalid_401_account_ids_cache, *invalid_quota_account_ids_cache])
        if not combined_ids:
            raise CLIError("当前没有检测到问题账号")
        labels: list[str] = []
        detection_label_to_account_id.clear()
        for account_id in combined_ids:
            account = next((item for item in accounts_cache if int(item.get("id") or 0) == account_id), None)
            if not account:
                continue
            issues: list[str] = []
            if account_id in invalid_401_account_ids_cache:
                issues.append("401/403")
            if account_id in invalid_quota_account_ids_cache:
                issues.append("无额度")
            label = format_detection_label(account, " / ".join(issues) if issues else "问题")
            detection_label_to_account_id[label] = account_id
            labels.append(label)
        if not labels:
            raise CLIError("检测结果对应的账号已失效，请重新同步账号列表后再检测")
        set_listbox_items(delete_accounts_listbox, labels)

    def delete_accounts_with_progress(
        account_ids: list[int],
        *,
        progress_callback: Callable[[int, int, str], None],
    ) -> dict[str, Any]:
        ids = unique_ids([account_id for account_id in account_ids if isinstance(account_id, int) and account_id > 0])
        if not ids:
            raise CLIError("当前没有可删除的账号")
        delete_concurrency = clamp_delete_concurrency(parse_optional_positive_int(delete_concurrency_var.get(), "删除并发数") or DEFAULT_DELETE_CONCURRENCY)
        completed = 0
        deleted_ids: list[int] = []
        failed: list[dict[str, Any]] = []
        progress_callback(0, len(ids), f"开始删除账号，共 {len(ids)} 个，并发 {delete_concurrency}")

        def delete_one(account_id: int) -> tuple[int, bool, Any]:
            ensure_not_cancelled()
            try:
                response = get_client().request("DELETE", f"/admin/accounts/{account_id}")
                return (account_id, True, response)
            except APIError as exc:
                return (
                    account_id,
                    False,
                    {
                        "id": account_id,
                        "status": exc.status,
                        "code": exc.code,
                        "message": str(exc),
                    },
                )

        with ThreadPoolExecutor(max_workers=min(delete_concurrency, len(ids))) as executor:
            future_map = {executor.submit(delete_one, account_id): account_id for account_id in ids}
            pending = set(future_map.keys())
            while pending:
                ensure_not_cancelled()
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    completed += 1
                    account_id = future_map[future]
                    try:
                        result_id, ok, payload = future.result()
                    except Exception as exc:
                        result_id, ok, payload = (
                            account_id,
                            False,
                            {"id": account_id, "status": "client_error", "code": "client_error", "message": str(exc)},
                        )
                    if ok:
                        deleted_ids.append(result_id)
                    else:
                        failed.append(dict(payload) if isinstance(payload, dict) else {"id": result_id, "message": str(payload)})
                    progress_callback(
                        completed,
                        len(ids),
                        f"删除账号 {completed}/{len(ids)}，成功 {len(deleted_ids)}，失败 {len(failed)}",
                    )
            executor.shutdown(wait=False, cancel_futures=True)

        deleted_ids.sort()
        failed.sort(key=lambda item: int(item.get("id") or 0))
        return {
            "deleted_ids": deleted_ids,
            "failed": failed,
            "success": len(deleted_ids),
            "failed_count": len(failed),
            "delete_concurrency": delete_concurrency,
        }

    def delete_account_ids_with_confirm(account_ids: list[int], confirm_label: str) -> None:
        ids = unique_ids([account_id for account_id in account_ids if isinstance(account_id, int) and account_id > 0])
        if not ids:
            raise CLIError(f"当前没有可删除的{confirm_label}账号")
        if not messagebox.askyesno("确认删除", f"即将删除 {len(ids)} 个{confirm_label}账号，此操作不可撤销。\n是否继续？"):
            return
        run_action(
            f"删除{confirm_label}账号",
            lambda progress: delete_accounts_with_progress(ids, progress_callback=progress),
            determinate=True,
        )

    def delete_detected_401_accounts() -> None:
        delete_account_ids_with_confirm(invalid_401_account_ids_cache, "401/403")

    def delete_detected_quota_accounts() -> None:
        delete_account_ids_with_confirm(invalid_quota_account_ids_cache, "无额度")

    def delete_all_detected_accounts() -> None:
        delete_account_ids_with_confirm(
            unique_ids([*invalid_401_account_ids_cache, *invalid_quota_account_ids_cache]),
            "问题",
        )

    detect_401_btn = ttk.Button(tab_delete_accounts, text="检测 401/403", command=lambda: run_action("检测 401/403", detect_401_accounts_action, determinate=True))
    detect_401_btn.grid(row=4, column=0, sticky="w", pady=4)
    action_buttons.append(detect_401_btn)
    detect_quota_btn = ttk.Button(tab_delete_accounts, text="检测无额度", command=lambda: run_action("检测无额度", detect_quota_accounts_action, determinate=True))
    detect_quota_btn.grid(row=4, column=1, sticky="w", pady=4)
    action_buttons.append(detect_quota_btn)
    detect_all_btn = ttk.Button(tab_delete_accounts, text="完整检测", command=lambda: run_action("完整检测", detect_all_accounts_action, determinate=True))
    detect_all_btn.grid(row=4, column=2, sticky="w", pady=4)
    action_buttons.append(detect_all_btn)
    refresh_states_btn = ttk.Button(tab_delete_accounts, text="手动刷新状态", command=lambda: run_action("手动刷新状态", refresh_account_states_action, determinate=True))
    refresh_states_btn.grid(row=4, column=3, sticky="w", pady=4)
    action_buttons.append(refresh_states_btn)
    load_401_btn = ttk.Button(tab_delete_accounts, text="载入 401/403 账号", command=safe_ui_action(lambda: load_detected_accounts(invalid_401_account_ids_cache, "401/403")))
    load_401_btn.grid(row=5, column=0, sticky="w", pady=4)
    action_buttons.append(load_401_btn)
    load_quota_btn = ttk.Button(tab_delete_accounts, text="载入无额度账号", command=safe_ui_action(lambda: load_detected_accounts(invalid_quota_account_ids_cache, "无额度")))
    load_quota_btn.grid(row=5, column=1, sticky="w", pady=4)
    action_buttons.append(load_quota_btn)
    load_all_detected_btn = ttk.Button(tab_delete_accounts, text="载入全部问题账号", command=safe_ui_action(load_all_detected_accounts))
    load_all_detected_btn.grid(row=5, column=2, sticky="w", pady=4)
    action_buttons.append(load_all_detected_btn)
    delete_detected_401_btn = ttk.Button(tab_delete_accounts, text="直接删除 401/403", command=safe_ui_action(delete_detected_401_accounts))
    delete_detected_401_btn.grid(row=5, column=3, sticky="w", pady=4)
    action_buttons.append(delete_detected_401_btn)

    delete_detected_quota_btn = ttk.Button(tab_delete_accounts, text="直接删除无额度", command=safe_ui_action(delete_detected_quota_accounts))
    delete_detected_quota_btn.grid(row=6, column=0, sticky="w", pady=3)
    action_buttons.append(delete_detected_quota_btn)
    delete_all_detected_btn = ttk.Button(tab_delete_accounts, text="直接删除全部问题账号", command=safe_ui_action(delete_all_detected_accounts))
    delete_all_detected_btn.grid(row=6, column=1, sticky="w", pady=3)
    action_buttons.append(delete_all_detected_btn)

    delete_accounts_list_frame = ttk.Frame(tab_delete_accounts)
    delete_accounts_list_frame.grid(row=8, column=0, columnspan=4, sticky="nsew", pady=(4, 0))
    delete_accounts_list_frame.columnconfigure(0, weight=1)
    delete_accounts_list_frame.rowconfigure(0, weight=1)
    delete_accounts_listbox = tk.Listbox(delete_accounts_list_frame, selectmode=tk.EXTENDED, exportselection=False, height=10)
    delete_accounts_listbox.grid(row=0, column=0, sticky="nsew")
    delete_accounts_scrollbar = ttk.Scrollbar(delete_accounts_list_frame, orient="vertical", command=delete_accounts_listbox.yview)
    delete_accounts_scrollbar.grid(row=0, column=1, sticky="ns")
    delete_accounts_listbox.configure(yscrollcommand=delete_accounts_scrollbar.set)

    ttk.Label(tab_delete_accounts, text="待删除账号列表").grid(row=7, column=0, sticky="w", pady=(8, 2))
    delete_account_clear_btn = ttk.Button(tab_delete_accounts, text="清空列表", command=lambda: set_listbox_items(delete_accounts_listbox, []))
    delete_account_clear_btn.grid(row=7, column=1, sticky="w", pady=(8, 2))
    action_buttons.append(delete_account_clear_btn)

    def run_delete_accounts_selected() -> None:
        labels = listbox_items(delete_accounts_listbox)
        if not labels:
            raise CLIError("请先选择账号")
        ids: list[int] = []
        for label in labels:
            account_id = account_label_to_id.get(label)
            if account_id is None:
                account_id = detection_label_to_account_id.get(label)
            if account_id is None:
                raise CLIError("待删除列表中存在过期项，请重新载入")
            ids.append(account_id)
        ids = unique_ids(ids)
        if not messagebox.askyesno("确认删除", f"即将删除 {len(ids)} 个账号，此操作不可撤销。\n是否继续？"):
            return
        run_action(
            "删除账号",
            lambda progress: delete_accounts_with_progress(ids, progress_callback=progress),
            determinate=True,
        )

    delete_accounts_btn = ttk.Button(tab_delete_accounts, text="删除当前列表中的账号", command=safe_ui_action(run_delete_accounts_selected))
    delete_accounts_btn.grid(row=9, column=0, sticky="w", pady=8)
    action_buttons.append(delete_accounts_btn)

    # 删除代理
    tab_delete_proxies = add_tab("删除代理", scrollable=False)
    tab_delete_proxies.columnconfigure(0, weight=1)
    tab_delete_proxies.rowconfigure(3, weight=1)
    ttk.Label(tab_delete_proxies, text="同步代理后，可直接下拉选择并批量删除。", style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))

    delete_proxy_picker_var = tk.StringVar(value=PROXY_PICKER_HINT)
    ttk.Label(tab_delete_proxies, text="代理下拉选择").grid(row=1, column=0, sticky="w", pady=3)
    delete_proxy_picker_combo = ttk.Combobox(tab_delete_proxies, textvariable=delete_proxy_picker_var, values=[PROXY_PICKER_HINT], state="readonly")
    delete_proxy_picker_combo.grid(row=1, column=1, sticky="ew", pady=3)
    proxy_picker_combos.append(delete_proxy_picker_combo)
    delete_proxies_sync_btn = ttk.Button(tab_delete_proxies, text="同步代理列表", command=lambda: run_action("同步代理列表", fetch_proxies, determinate=True))
    delete_proxies_sync_btn.grid(row=1, column=2, sticky="w", padx=(8, 0), pady=3)
    action_buttons.append(delete_proxies_sync_btn)

    delete_proxies_list_frame = ttk.Frame(tab_delete_proxies)
    delete_proxies_list_frame.grid(row=3, column=0, columnspan=4, sticky="nsew", pady=(4, 0))
    delete_proxies_list_frame.columnconfigure(0, weight=1)
    delete_proxies_list_frame.rowconfigure(0, weight=1)
    delete_proxies_listbox = tk.Listbox(delete_proxies_list_frame, selectmode=tk.EXTENDED, exportselection=False, height=10)
    delete_proxies_listbox.grid(row=0, column=0, sticky="nsew")
    delete_proxies_scrollbar = ttk.Scrollbar(delete_proxies_list_frame, orient="vertical", command=delete_proxies_listbox.yview)
    delete_proxies_scrollbar.grid(row=0, column=1, sticky="ns")
    delete_proxies_listbox.configure(yscrollcommand=delete_proxies_scrollbar.set)

    def add_delete_proxy_from_picker() -> None:
        label = delete_proxy_picker_var.get().strip()
        if not label or label == PROXY_PICKER_HINT:
            raise CLIError("请先从下拉框里选择一个代理")
        if label not in proxy_label_to_id:
            raise CLIError("代理下拉值已过期，请重新同步代理列表")
        add_listbox_item(delete_proxies_listbox, label)

    delete_proxy_add_btn = ttk.Button(tab_delete_proxies, text="添加到待删除列表", command=safe_ui_action(add_delete_proxy_from_picker))
    delete_proxy_add_btn.grid(row=1, column=3, sticky="w", padx=(8, 0), pady=3)
    action_buttons.append(delete_proxy_add_btn)
    ttk.Label(tab_delete_proxies, text="待删除代理列表").grid(row=2, column=0, sticky="w", pady=(8, 2))
    delete_proxy_remove_btn = ttk.Button(tab_delete_proxies, text="移除选中项", command=lambda: remove_selected_listbox_items(delete_proxies_listbox))
    delete_proxy_remove_btn.grid(row=2, column=1, sticky="w", pady=(8, 2))
    action_buttons.append(delete_proxy_remove_btn)
    delete_proxy_clear_btn = ttk.Button(tab_delete_proxies, text="清空列表", command=lambda: set_listbox_items(delete_proxies_listbox, []))
    delete_proxy_clear_btn.grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(8, 2))
    action_buttons.append(delete_proxy_clear_btn)

    def run_delete_proxies_selected() -> None:
        ids = ids_from_listbox(delete_proxies_listbox, proxy_label_to_id, "代理")
        if not messagebox.askyesno("确认删除", f"即将删除 {len(ids)} 个代理，此操作不可撤销。\n是否继续？"):
            return
        run_action(
            "删除代理",
            lambda progress: (
                progress(0, 1, f"准备删除代理，共 {len(ids)} 个"),
                handle_delete_proxies(
                    get_client(),
                    argparse.Namespace(ids=ids),
                ).payload,
            )[1],
            determinate=True,
        )

    delete_proxies_btn = ttk.Button(tab_delete_proxies, text="删除当前列表中的代理", command=safe_ui_action(run_delete_proxies_selected))
    delete_proxies_btn.grid(row=4, column=0, sticky="w", pady=8)
    action_buttons.append(delete_proxies_btn)

    # 批量创建账号
    tab_batch_accounts = add_tab("批量创建账号", scrollable=False)
    batch_accounts_file = tk.StringVar()
    add_file_row(tab_batch_accounts, 0, "账号 JSON 文件", batch_accounts_file)
    batch_accounts_btn = ttk.Button(
        tab_batch_accounts,
        text="执行批量创建账号",
        command=lambda: run_action(
            "批量创建账号",
            lambda _progress: handle_batch_create_accounts(
                get_client(),
                argparse.Namespace(file=non_empty(batch_accounts_file.get()) or ""),
            ).payload,
        ),
    )
    batch_accounts_btn.grid(row=1, column=0, sticky="w", pady=4)
    action_buttons.append(batch_accounts_btn)

    # 批量创建代理
    tab_batch_proxies = add_tab("批量创建代理", scrollable=False)
    batch_proxies_file = tk.StringVar()
    add_file_row(tab_batch_proxies, 0, "代理 JSON 文件", batch_proxies_file)
    batch_proxies_btn = ttk.Button(
        tab_batch_proxies,
        text="执行批量创建代理",
        command=lambda: run_action(
            "批量创建代理",
            lambda _progress: handle_batch_create_proxies(
                get_client(),
                argparse.Namespace(file=non_empty(batch_proxies_file.get()) or ""),
            ).payload,
        ),
    )
    batch_proxies_btn.grid(row=1, column=0, sticky="w", pady=4)
    action_buttons.append(batch_proxies_btn)

    # 导出现有账号
    tab_export_accounts = add_tab("导出现有账号")
    tab_export_accounts.columnconfigure(1, weight=1)
    export_group_var = tk.StringVar(value=GROUP_FILTER_ALL)
    export_platform_var = tk.StringVar(value="(不限)")
    export_account_type_var = tk.StringVar(value="(不限)")
    export_account_status_var = tk.StringVar(value="(不限)")
    export_search_var = tk.StringVar()
    export_include_proxies_var = tk.BooleanVar(value=True)
    export_accounts_per_file_var = tk.StringVar(value="1")
    export_output_dir_var = tk.StringVar(value=str(get_default_download_output_dir("s2a-manager-account-exports")))
    export_output_hint_var = tk.StringVar()

    ttk.Label(tab_export_accounts, text="把网站现有账号下载到本地，并按你指定的数量自动拆成多个 JSON 文件。", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
    ttk.Label(
        tab_export_accounts,
        text="每个文件填 1 时，会按账号名输出一个文件一个账号；填 20 时，就会每 20 个账号打成一个 JSON，最后一个文件自动放剩余账号。分组选“不限”时，会把未分组账号一并包含进去。",
        style="Hint.TLabel",
        justify="left",
        wraplength=980,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

    ttk.Label(tab_export_accounts, text="按分组").grid(row=2, column=0, sticky="w", pady=3)
    export_group_combo = ttk.Combobox(tab_export_accounts, textvariable=export_group_var, values=[GROUP_FILTER_ALL], state="readonly")
    export_group_combo.grid(row=2, column=1, sticky="ew", pady=3)
    group_filter_combos.append(export_group_combo)
    export_sync_groups_btn = ttk.Button(tab_export_accounts, text="同步分组选项", command=lambda: run_action("同步分组", fetch_groups, determinate=True))
    export_sync_groups_btn.grid(row=2, column=2, sticky="w", padx=(8, 0), pady=3)
    action_buttons.append(export_sync_groups_btn)

    export_row3 = ttk.Frame(tab_export_accounts)
    export_row3.grid(row=3, column=0, columnspan=3, sticky="ew", pady=3)
    export_row3.columnconfigure(1, weight=1)
    export_row3.columnconfigure(3, weight=1)
    ttk.Label(export_row3, text="平台").grid(row=0, column=0, sticky="w")
    ttk.Combobox(export_row3, textvariable=export_platform_var, values=["(不限)", "openai", "anthropic", "gemini", "antigravity", "sora"], state="readonly").grid(row=0, column=1, sticky="ew", padx=(0, 10))
    ttk.Label(export_row3, text="账号类型").grid(row=0, column=2, sticky="w")
    export_type_combo = ttk.Combobox(export_row3, textvariable=export_account_type_var, values=["(不限)", "oauth", "apikey", "setup-token", "upstream", "bedrock"], state="readonly")
    export_type_combo.grid(row=0, column=3, sticky="ew")

    export_row4 = ttk.Frame(tab_export_accounts)
    export_row4.grid(row=4, column=0, columnspan=3, sticky="ew", pady=3)
    export_row4.columnconfigure(1, weight=1)
    export_row4.columnconfigure(3, weight=1)
    ttk.Label(export_row4, text="账号状态").grid(row=0, column=0, sticky="w")
    ttk.Combobox(export_row4, textvariable=export_account_status_var, values=["(不限)", "active", "inactive", "error"], state="readonly").grid(row=0, column=1, sticky="ew", padx=(0, 10))
    ttk.Label(export_row4, text="搜索关键词").grid(row=0, column=2, sticky="w")
    ttk.Entry(export_row4, textvariable=export_search_var).grid(row=0, column=3, sticky="ew")

    export_row5 = ttk.Frame(tab_export_accounts)
    export_row5.grid(row=5, column=0, columnspan=3, sticky="ew", pady=3)
    export_row5.columnconfigure(1, weight=1)
    ttk.Label(export_row5, text="输出文件夹").grid(row=0, column=0, sticky="w")
    ttk.Entry(export_row5, textvariable=export_output_dir_var).grid(row=0, column=1, sticky="ew", padx=(0, 8))
    export_output_btn = ttk.Button(export_row5, text="选择文件夹", command=lambda: pick_directory(export_output_dir_var))
    export_output_btn.grid(row=0, column=2, sticky="w")
    action_buttons.append(export_output_btn)

    export_row6 = ttk.Frame(tab_export_accounts)
    export_row6.grid(row=6, column=0, columnspan=3, sticky="ew", pady=3)
    ttk.Label(export_row6, text="每个文件包含账号数").grid(row=0, column=0, sticky="w")
    ttk.Entry(export_row6, textvariable=export_accounts_per_file_var, width=10).grid(row=0, column=1, sticky="w", padx=(8, 16))
    ttk.Checkbutton(export_row6, text="导出账号关联代理", variable=export_include_proxies_var).grid(row=0, column=2, sticky="w")
    ttk.Label(tab_export_accounts, textvariable=export_output_hint_var, style="Hint.TLabel", justify="left", wraplength=980).grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 4))

    def refresh_export_output_hint(*_args: Any) -> None:
        output_dir = non_empty(export_output_dir_var.get())
        if output_dir:
            export_output_hint_var.set(f"文件会写到：{output_dir}")
        else:
            export_output_hint_var.set(f"未填写时默认写到：{get_default_download_output_dir('s2a-manager-account-exports')}")

    export_output_dir_var.trace_add("write", refresh_export_output_hint)
    refresh_export_output_hint()

    def normalize_export_select(value: str) -> str | None:
        stripped = value.strip()
        if not stripped or stripped.startswith("("):
            return None
        return stripped

    def resolve_export_output_dir() -> Path:
        output_dir = non_empty(export_output_dir_var.get())
        if output_dir:
            return Path(output_dir)
        return get_default_download_output_dir("s2a-manager-account-exports")

    def build_export_selection(progress_callback: Callable[[int, int, str], None]) -> dict[str, Any]:
        platform = normalize_export_select(export_platform_var.get())
        account_type = normalize_export_select(export_account_type_var.get())
        account_status = normalize_export_select(export_account_status_var.get())
        search = non_empty(export_search_var.get())
        group_id = single_group_id_from_label(export_group_var.get(), allow_all=True)
        include_ungrouped = group_id is None

        progress_callback(0, 1, "正在扫描符合条件的账号...")
        selection_args = argparse.Namespace(
            account_ids=None,
            platform=platform,
            account_type=account_type,
            account_status=account_status,
            search=search,
            name_contains=None,
            group_ids=[group_id] if group_id is not None else None,
            ungrouped_only=False,
            max_accounts=None,
            page_size=ADMIN_LIST_PAGE_SIZE_CAP,
        )
        ids = collect_target_account_ids(get_client(), selection_args, progress_callback=progress_callback)
        if not ids:
            raise CLIError("当前筛选条件下没有匹配到任何账号")

        return {
            "ids": ids,
            "matched_id_count": len(ids),
            "group_id": group_id,
            "group_scope": "all_including_ungrouped" if include_ungrouped else "single_group_only",
            "include_ungrouped": include_ungrouped,
            "path_mode": "ids_export",
            "platform": platform,
            "account_type": account_type,
            "account_status": account_status,
            "search": search,
        }

    def build_export_reimport_warnings(payload: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        unsupported_types: dict[str, int] = {}
        for account in payload.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            account_type = str(account.get("type") or "").strip().lower()
            if account_type and account_type not in VALID_ACCOUNT_IMPORT_TYPES:
                unsupported_types[account_type] = unsupported_types.get(account_type, 0) + 1
        for account_type, count in sorted(unsupported_types.items()):
            warnings.append(f"检测到 {count} 个 `{account_type}` 账号；当前 /admin/accounts/data 导入接口不一定支持这种类型重新导入")
        return warnings

    def preview_export_accounts_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        selection = build_export_selection(progress_callback)
        accounts_per_file = parse_optional_positive_int(export_accounts_per_file_var.get(), "每个文件包含账号数") or 1
        payload = fetch_accounts_export_data(
            get_client(),
            ids=selection["ids"],
            platform=selection["platform"],
            account_type=selection["account_type"],
            status=selection["account_status"],
            search=selection["search"],
            include_proxies=bool(export_include_proxies_var.get()),
            progress_callback=progress_callback,
        )
        file_plans = build_accounts_export_file_plans(payload, accounts_per_file=accounts_per_file)
        preview_files = [
            {
                "file_name": f"{plan['name_base']}.json",
                "account_count": plan["account_count"],
                "proxy_count": plan["proxy_count"],
                "account_names": plan["account_names"][:5],
            }
            for plan in file_plans[:10]
        ]
        return {
            "output_dir": str(resolve_export_output_dir()),
            "accounts_per_file": accounts_per_file,
            "include_proxies": bool(export_include_proxies_var.get()),
            "matched_account_ids": selection["matched_id_count"],
            "matched_accounts": len(payload.get("accounts") or []),
            "matched_proxies": len(payload.get("proxies") or []),
            "planned_files": len(file_plans),
            "filters": selection,
            "warnings": build_export_reimport_warnings(payload),
            "preview_files": preview_files,
            "first_file_preview": file_plans[0]["payload"] if file_plans else None,
        }

    def export_accounts_to_local_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        selection = build_export_selection(progress_callback)
        accounts_per_file = parse_optional_positive_int(export_accounts_per_file_var.get(), "每个文件包含账号数") or 1
        output_dir = resolve_export_output_dir()
        payload = fetch_accounts_export_data(
            get_client(),
            ids=selection["ids"],
            platform=selection["platform"],
            account_type=selection["account_type"],
            status=selection["account_status"],
            search=selection["search"],
            include_proxies=bool(export_include_proxies_var.get()),
            progress_callback=progress_callback,
        )
        file_plans = build_accounts_export_file_plans(payload, accounts_per_file=accounts_per_file)
        written_files = write_accounts_export_files(file_plans, output_dir=output_dir, progress_callback=progress_callback)
        return {
            "output_dir": str(output_dir),
            "accounts_per_file": accounts_per_file,
            "include_proxies": bool(export_include_proxies_var.get()),
            "matched_account_ids": selection["matched_id_count"],
            "matched_accounts": len(payload.get("accounts") or []),
            "matched_proxies": len(payload.get("proxies") or []),
            "written_files": len(written_files),
            "warnings": build_export_reimport_warnings(payload),
            "files": written_files,
        }

    export_preview_btn = ttk.Button(
        tab_export_accounts,
        text="预估导出结果",
        command=lambda: run_action("预估导出结果", preview_export_accounts_action, determinate=True),
    )
    export_preview_btn.grid(row=8, column=0, sticky="w", pady=6)
    action_buttons.append(export_preview_btn)
    export_start_btn = ttk.Button(
        tab_export_accounts,
        text="开始下载到本地",
        command=lambda: run_action("下载账号到本地", export_accounts_to_local_action, determinate=True),
    )
    export_start_btn.grid(row=8, column=1, sticky="w", pady=6)
    action_buttons.append(export_start_btn)

    # 导入账号数据
    tab_import_accounts = add_tab("导入账号数据")
    tab_import_accounts.columnconfigure(1, weight=1)
    import_mode_var = tk.StringVar(value="file")
    import_source_var = tk.StringVar()
    import_mode_hint_var = tk.StringVar()
    import_recursive_var = tk.BooleanVar(value=True)
    skip_default_group_bind_var = tk.BooleanVar(value=True)
    ttk.Label(tab_import_accounts, text="把导出的账号 JSON 导入到当前网站。", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
    ttk.Label(
        tab_import_accounts,
        text="先选择导入方式，再选择一个文件或一个文件夹即可。建议先点“先检查文件格式”；如果检查不通过，再去旁边的“转换账号 JSON”页处理。",
        style="Hint.TLabel",
        justify="left",
        wraplength=980,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))
    ttk.Label(tab_import_accounts, text="导入方式").grid(row=2, column=0, sticky="w")
    ttk.Radiobutton(tab_import_accounts, text="单个 JSON 文件", variable=import_mode_var, value="file").grid(row=2, column=1, sticky="w")
    ttk.Radiobutton(tab_import_accounts, text="整个文件夹批量导入", variable=import_mode_var, value="folder").grid(row=2, column=2, sticky="w")
    import_source_label = ttk.Label(tab_import_accounts, text="导入来源文件")
    import_source_label.grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(tab_import_accounts, textvariable=import_source_var).grid(row=3, column=1, sticky="ew", pady=4)
    import_source_btn = ttk.Button(tab_import_accounts, text="选择文件")
    import_source_btn.grid(row=3, column=2, padx=(8, 0), pady=4)
    ttk.Label(
        tab_import_accounts,
        textvariable=import_mode_hint_var,
        style="Hint.TLabel",
        justify="left",
        wraplength=980,
    ).grid(row=4, column=1, columnspan=2, sticky="w", pady=(0, 2))
    import_recursive_check = ttk.Checkbutton(tab_import_accounts, text="同时扫描子文件夹里的 JSON 文件", variable=import_recursive_var)
    import_recursive_check.grid(row=5, column=1, sticky="w", pady=2)
    ttk.Checkbutton(
        tab_import_accounts,
        text="保留导入文件里的分组设置，不额外补默认分组",
        variable=skip_default_group_bind_var,
    ).grid(row=6, column=1, columnspan=2, sticky="w", pady=2)

    def refresh_import_mode_ui(*_args: Any) -> None:
        mode = import_mode_var.get().strip() or "file"
        current_path = non_empty(import_source_var.get())
        if current_path:
            current = Path(current_path)
            if mode == "file" and current.is_dir():
                import_source_var.set("")
            elif mode == "folder" and current.is_file():
                import_source_var.set("")

        if mode == "folder":
            import_source_label.configure(text="导入来源文件夹")
            import_source_btn.configure(text="选择文件夹", command=lambda: pick_directory(import_source_var))
            import_mode_hint_var.set("适合一个目录里放了多个账号导出 JSON，程序会自动逐个导入。")
            import_recursive_check.grid()
            return

        import_source_label.configure(text="导入来源文件")
        import_source_btn.configure(text="选择文件", command=lambda: pick_json(import_source_var))
        import_mode_hint_var.set("适合一次导入一个账号导出 JSON 文件。")
        import_recursive_check.grid_remove()

    import_mode_var.trace_add("write", refresh_import_mode_ui)
    refresh_import_mode_ui()

    def check_import_accounts_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        mode = import_mode_var.get().strip() or "file"
        source_path = non_empty(import_source_var.get())
        source_root, files = collect_json_input_files(
            mode,
            source_path,
            recursive=bool(import_recursive_var.get()),
            purpose_label="账号导入 JSON ",
        )
        result = inspect_accounts_import_files(
            files,
            skip_default_group_bind=bool(skip_default_group_bind_var.get()),
            progress_callback=progress_callback,
            keep_payloads=False,
            include_single_preview=True,
        )
        result["mode"] = mode
        result["source_root"] = str(source_root)
        result["source"] = source_path
        if len(files) == 1 and result.get("preview") is not None:
            result["normalized_request"] = result["preview"]
        return result

    def import_accounts_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        mode = import_mode_var.get().strip()
        skip_default_group_bind = bool(skip_default_group_bind_var.get())
        client = get_client()
        source_path = non_empty(import_source_var.get())
        source_root, files = collect_json_input_files(
            mode,
            source_path,
            recursive=bool(import_recursive_var.get()),
            purpose_label="账号导入 JSON ",
        )
        validation_result = inspect_accounts_import_files(
            files,
            skip_default_group_bind=skip_default_group_bind,
            progress_callback=progress_callback,
            keep_payloads=True,
            include_single_preview=False,
        )
        if not validation_result["ok"]:
            raise CLIError(summarize_invalid_file_checks(validation_result, action_label="账号导入"))

        prepared_payloads = validation_result.get("prepared_payloads") or []
        if mode == "file":
            if not prepared_payloads or not isinstance(prepared_payloads[0].get("request_payload"), dict):
                raise CLIError("文件检查通过，但未能生成可导入请求体")
            progress_callback(0, 1, f"准备导入文件: {Path(source_path or '').name}")
            result = client.request("POST", "/admin/accounts/data", prepared_payloads[0]["request_payload"])
            progress_callback(1, 1, "文件导入完成")
            return {
                "mode": "file",
                "file": source_path,
                "source_root": str(source_root),
                "validation_warnings": validation_result["warning_count"],
                "result": result,
            }

        summary: dict[str, int] = {
            "proxy_created": 0,
            "proxy_reused": 0,
            "proxy_failed": 0,
            "account_created": 0,
            "account_failed": 0,
        }
        details: list[dict[str, Any]] = []
        failed_files = 0
        for index, prepared in enumerate(prepared_payloads, start=1):
            file = Path(str(prepared.get("file") or ""))
            request_payload = prepared.get("request_payload")
            if not isinstance(request_payload, dict):
                failed_files += 1
                details.append({"file": str(file), "ok": False, "error": "未生成可导入请求体"})
                continue
            progress_callback(index - 1, len(prepared_payloads), f"导入 {index}/{len(prepared_payloads)}: {file.name}")
            try:
                result = client.request("POST", "/admin/accounts/data", request_payload)
                if not isinstance(result, dict):
                    raise CLIError("`/admin/accounts/data` 返回格式异常")
                for key in summary:
                    summary[key] += parse_int_field(result.get(key), 0)
                details.append({"file": str(file), "ok": True, "result": result})
            except Exception as exc:
                failed_files += 1
                details.append({"file": str(file), "ok": False, "error": str(exc)})

        progress_callback(len(prepared_payloads), len(prepared_payloads), f"批量导入完成，共 {len(prepared_payloads)} 个文件")
        return {
            "mode": "folder",
            "folder": str(source_path),
            "source_root": str(source_root),
            "total_files": len(prepared_payloads),
            "failed_files": failed_files,
            "success_files": len(prepared_payloads) - failed_files,
            "validation_warnings": validation_result["warning_count"],
            "summary": summary,
            "details": details,
        }

    import_accounts_check_btn = ttk.Button(
        tab_import_accounts,
        text="先检查文件格式",
        command=lambda: run_action("检查账号导入 JSON", check_import_accounts_action, determinate=True),
    )
    import_accounts_check_btn.grid(row=7, column=0, sticky="w", pady=6)
    action_buttons.append(import_accounts_check_btn)
    import_accounts_btn = ttk.Button(
        tab_import_accounts,
        text="开始导入账号数据",
        command=lambda: run_action("导入账号数据", import_accounts_action, determinate=True),
    )
    import_accounts_btn.grid(row=7, column=1, sticky="w", pady=6)
    action_buttons.append(import_accounts_btn)

    # 转换账号 JSON
    tab_convert_accounts = add_tab("转换账号 JSON")
    tab_convert_accounts.columnconfigure(1, weight=1)
    convert_mode_var = tk.StringVar(value="file")
    convert_source_var = tk.StringVar()
    convert_output_var = tk.StringVar()
    convert_accounts_per_file_var = tk.StringVar(value="1")
    convert_mode_hint_var = tk.StringVar()
    convert_output_hint_var = tk.StringVar(value="未填写输出文件夹时，默认写到源目录下的 `s2a-manager-converted` 文件夹。")
    convert_recursive_var = tk.BooleanVar(value=True)

    ttk.Label(tab_convert_accounts, text="把简化账号 JSON 批量转成 S2A Manager 可直接导入的标准 JSON。", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
    ttk.Label(
        tab_convert_accounts,
        text="支持单文件和整个文件夹。只要原始 JSON 至少能提供 name、platform、type，以及 credentials 或可识别的常见凭证字段，就能自动转换。",
        style="Hint.TLabel",
        justify="left",
        wraplength=980,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))
    ttk.Label(tab_convert_accounts, text="转换方式").grid(row=2, column=0, sticky="w")
    ttk.Radiobutton(tab_convert_accounts, text="单个 JSON 文件", variable=convert_mode_var, value="file").grid(row=2, column=1, sticky="w")
    ttk.Radiobutton(tab_convert_accounts, text="整个文件夹批量转换", variable=convert_mode_var, value="folder").grid(row=2, column=2, sticky="w")

    convert_source_label = ttk.Label(tab_convert_accounts, text="待转换文件")
    convert_source_label.grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(tab_convert_accounts, textvariable=convert_source_var).grid(row=3, column=1, sticky="ew", pady=4)
    convert_source_btn = ttk.Button(tab_convert_accounts, text="选择文件")
    convert_source_btn.grid(row=3, column=2, padx=(8, 0), pady=4)
    ttk.Label(tab_convert_accounts, textvariable=convert_mode_hint_var, style="Hint.TLabel", justify="left", wraplength=980).grid(row=4, column=1, columnspan=2, sticky="w", pady=(0, 2))
    convert_recursive_check = ttk.Checkbutton(tab_convert_accounts, text="同时扫描子文件夹里的 JSON 文件", variable=convert_recursive_var)
    convert_recursive_check.grid(row=5, column=1, sticky="w", pady=2)

    ttk.Label(tab_convert_accounts, text="输出文件夹").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(tab_convert_accounts, textvariable=convert_output_var).grid(row=6, column=1, sticky="ew", pady=4)
    convert_output_btn = ttk.Button(tab_convert_accounts, text="选择文件夹", command=lambda: pick_directory(convert_output_var))
    convert_output_btn.grid(row=6, column=2, padx=(8, 0), pady=4)
    action_buttons.append(convert_output_btn)
    ttk.Label(tab_convert_accounts, textvariable=convert_output_hint_var, style="Hint.TLabel", justify="left", wraplength=980).grid(row=7, column=1, columnspan=2, sticky="w", pady=(0, 4))
    ttk.Label(tab_convert_accounts, text="每个输出文件包含账号数").grid(row=8, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(tab_convert_accounts, textvariable=convert_accounts_per_file_var, width=10).grid(row=8, column=1, sticky="w", pady=4)

    def refresh_convert_mode_ui(*_args: Any) -> None:
        mode = convert_mode_var.get().strip() or "file"
        current_path = non_empty(convert_source_var.get())
        if current_path:
            current = Path(current_path)
            if mode == "file" and current.is_dir():
                convert_source_var.set("")
            elif mode == "folder" and current.is_file():
                convert_source_var.set("")

        if mode == "folder":
            convert_source_label.configure(text="待转换文件夹")
            convert_source_btn.configure(text="选择文件夹", command=lambda: pick_directory(convert_source_var))
            convert_mode_hint_var.set("适合一个目录里放了多个账号 JSON，程序会自动逐个检查、预览并输出标准导入文件。")
            convert_recursive_check.grid()
        else:
            convert_source_label.configure(text="待转换文件")
            convert_source_btn.configure(text="选择文件", command=lambda: pick_json(convert_source_var))
            convert_mode_hint_var.set("适合先拿一个文件试转换结果，确认没问题后再批量处理。")
            convert_recursive_check.grid_remove()

        suggested_dir = None
        source_value = non_empty(convert_source_var.get())
        if source_value:
            try:
                suggested_dir = resolve_conversion_output_dir(mode, source_value, None)
            except CLIError:
                suggested_dir = None
        if suggested_dir is not None:
            convert_output_hint_var.set(f"未填写输出文件夹时，默认写到：{suggested_dir}")
        else:
            convert_output_hint_var.set("未填写输出文件夹时，默认写到源目录下的 `s2a-manager-converted` 文件夹。")

    convert_mode_var.trace_add("write", refresh_convert_mode_ui)
    convert_source_var.trace_add("write", refresh_convert_mode_ui)
    refresh_convert_mode_ui()

    def check_convert_accounts_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        mode = convert_mode_var.get().strip() or "file"
        source_path = non_empty(convert_source_var.get())
        accounts_per_file = parse_optional_positive_int(convert_accounts_per_file_var.get(), "每个输出文件包含账号数") or 1
        source_root, files = collect_json_input_files(
            mode,
            source_path,
            recursive=bool(convert_recursive_var.get()),
            purpose_label="待转换 JSON ",
        )
        result = inspect_convertible_json_files(
            files,
            progress_callback=progress_callback,
            keep_payloads=False,
            include_preview=True,
        )
        result["mode"] = mode
        result["source_root"] = str(source_root)
        result["source"] = source_path
        result["accounts_per_file"] = accounts_per_file
        result["output_dir"] = str(resolve_conversion_output_dir(mode, source_path, non_empty(convert_output_var.get())))
        if result["ok"]:
            merged_payload = merge_exported_accounts_payloads(
                [item["data_payload"] for item in result.get("converted_payloads", []) if isinstance(item.get("data_payload"), dict)]
            ) if result.get("converted_payloads") else None
            if merged_payload is None:
                inspection_preview = inspect_convertible_json_files(
                    files,
                    progress_callback=lambda *_args: None,
                    keep_payloads=True,
                    include_preview=False,
                )
                merged_payload = merge_exported_accounts_payloads(
                    [item["data_payload"] for item in inspection_preview.get("converted_payloads", []) if isinstance(item.get("data_payload"), dict)]
                )
            file_plans = build_accounts_export_file_plans(merged_payload, accounts_per_file=accounts_per_file)
            result["total_accounts"] = len(merged_payload.get("accounts") or [])
            result["total_proxies"] = len(merged_payload.get("proxies") or [])
            result["planned_files"] = len(file_plans)
        if len(files) == 1 and result.get("preview") is not None:
            result["converted_data"] = result["preview"]
        return result

    def preview_convert_accounts_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        mode = convert_mode_var.get().strip() or "file"
        source_path = non_empty(convert_source_var.get())
        accounts_per_file = parse_optional_positive_int(convert_accounts_per_file_var.get(), "每个输出文件包含账号数") or 1
        source_root, files = collect_json_input_files(
            mode,
            source_path,
            recursive=bool(convert_recursive_var.get()),
            purpose_label="待转换 JSON ",
        )
        result = inspect_convertible_json_files(
            files,
            progress_callback=progress_callback,
            keep_payloads=True,
            include_preview=True,
        )
        preview_payload = result.get("preview")
        converted_payloads = result.pop("converted_payloads", [])
        if preview_payload is None and converted_payloads:
            preview_payload = converted_payloads[0].get("data_payload")
            result["preview_file"] = converted_payloads[0].get("file")
        merged_payload = merge_exported_accounts_payloads(
            [item["data_payload"] for item in converted_payloads if isinstance(item.get("data_payload"), dict)]
        )
        file_plans = build_accounts_export_file_plans(merged_payload, accounts_per_file=accounts_per_file)
        result["mode"] = mode
        result["source_root"] = str(source_root)
        result["source"] = source_path
        result["accounts_per_file"] = accounts_per_file
        result["output_dir"] = str(resolve_conversion_output_dir(mode, source_path, non_empty(convert_output_var.get())))
        result["total_accounts"] = len(merged_payload.get("accounts") or [])
        result["total_proxies"] = len(merged_payload.get("proxies") or [])
        result["planned_files"] = len(file_plans)
        if file_plans:
            result["first_output_preview"] = file_plans[0]["payload"]
        if preview_payload is not None:
            result["converted_data"] = preview_payload
        return result

    def convert_accounts_json_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        mode = convert_mode_var.get().strip() or "file"
        source_path = non_empty(convert_source_var.get())
        source_root, files = collect_json_input_files(
            mode,
            source_path,
            recursive=bool(convert_recursive_var.get()),
            purpose_label="待转换 JSON ",
        )
        accounts_per_file = parse_optional_positive_int(convert_accounts_per_file_var.get(), "每个输出文件包含账号数") or 1
        inspection = inspect_convertible_json_files(
            files,
            progress_callback=progress_callback,
            keep_payloads=True,
            include_preview=False,
        )
        if not inspection["ok"]:
            raise CLIError(summarize_invalid_file_checks(inspection, action_label="JSON 转换"))

        output_dir = resolve_conversion_output_dir(mode, source_path, non_empty(convert_output_var.get()))
        output_dir.mkdir(parents=True, exist_ok=True)

        converted_payloads = inspection.get("converted_payloads") or []
        merged_payload = merge_exported_accounts_payloads(
            [item["data_payload"] for item in converted_payloads if isinstance(item.get("data_payload"), dict)]
        )
        file_plans = build_accounts_export_file_plans(merged_payload, accounts_per_file=accounts_per_file)
        written_files: list[dict[str, Any]] = []
        used_names: dict[str, int] = {}
        for index, plan in enumerate(file_plans, start=1):
            data_payload = plan.get("payload")
            if not isinstance(data_payload, dict):
                raise CLIError("转换分片结果缺少标准导入 JSON")
            target = make_unique_json_output_path(output_dir, str(plan.get("name_base") or f"accounts-{index}"), used_names)
            progress_callback(index, len(file_plans), f"写出文件 {index}/{len(file_plans)}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(data_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            written_files.append(
                {
                    "target": str(target),
                    "account_count": len(data_payload.get("accounts") or []),
                    "proxy_count": len(data_payload.get("proxies") or []),
                    "account_names_preview": list(plan.get("account_names") or [])[:10],
                }
            )

        progress_callback(len(file_plans), len(file_plans), f"转换完成，共输出 {len(written_files)} 个文件")
        return {
            "mode": mode,
            "source": source_path,
            "source_root": str(source_root),
            "output_dir": str(output_dir),
            "accounts_per_file": accounts_per_file,
            "total_accounts": len(merged_payload.get("accounts") or []),
            "total_proxies": len(merged_payload.get("proxies") or []),
            "written_files": len(written_files),
            "warning_count": inspection["warning_count"],
            "files": written_files,
        }

    convert_check_btn = ttk.Button(
        tab_convert_accounts,
        text="检查原文件",
        command=lambda: run_action("检查待转换 JSON", check_convert_accounts_action, determinate=True),
    )
    convert_check_btn.grid(row=9, column=0, sticky="w", pady=6)
    action_buttons.append(convert_check_btn)
    convert_preview_btn = ttk.Button(
        tab_convert_accounts,
        text="预览转换结果",
        command=lambda: run_action("预览转换结果", preview_convert_accounts_action, determinate=True),
    )
    convert_preview_btn.grid(row=9, column=1, sticky="w", pady=6)
    action_buttons.append(convert_preview_btn)
    convert_start_btn = ttk.Button(
        tab_convert_accounts,
        text="开始批量转换",
        command=lambda: run_action("批量转换账号 JSON", convert_accounts_json_action, determinate=True),
    )
    convert_start_btn.grid(row=9, column=2, sticky="w", pady=6)
    action_buttons.append(convert_start_btn)

    # 导入代理数据
    tab_import_proxies = add_tab("导入代理数据", scrollable=False)
    import_proxies_file = tk.StringVar()
    add_file_row(tab_import_proxies, 0, "代理导入 JSON 文件", import_proxies_file)
    import_proxies_btn = ttk.Button(
        tab_import_proxies,
        text="执行导入代理数据",
        command=lambda: run_action(
            "导入代理数据",
            lambda _progress: handle_import_proxies_data(
                get_client(),
                argparse.Namespace(file=non_empty(import_proxies_file.get()) or ""),
            ).payload,
        ),
    )
    import_proxies_btn.grid(row=1, column=0, sticky="w", pady=4)
    action_buttons.append(import_proxies_btn)

    # 批量调整账号
    tab_bulk_all = add_tab("批量调整账号")
    tab_bulk_all.columnconfigure(0, weight=1)
    tab_bulk_all.rowconfigure(5, weight=1)

    ttk.Label(tab_bulk_all, text="按条件筛选后，批量调整账号设置。", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
    ttk.Label(
        tab_bulk_all,
        text="常见用法：先同步分组，再按平台、状态或当前分组筛选，最后选择要改成的分组、代理、状态等字段。预览和真正修改已分开，直接点开始批量调整就会实际提交。",
        style="Hint.TLabel",
        justify="left",
        wraplength=980,
    ).grid(row=1, column=0, sticky="w", pady=(0, 8))

    filter_frame = ttk.LabelFrame(tab_bulk_all, text="筛选条件", padding=8)
    filter_frame.grid(row=2, column=0, sticky="ew")
    for c in range(6):
        filter_frame.columnconfigure(c, weight=1 if c in (1, 3, 5) else 0)

    account_ids_raw_var = tk.StringVar()
    account_picker_var = tk.StringVar(value=ACCOUNT_PICKER_HINT)
    account_selection_summary_var = tk.StringVar(value="未指定账号时，会按下面的筛选条件自动匹配。")
    platform_var = tk.StringVar(value="(不限)")
    account_type_var = tk.StringVar(value="(不限)")
    account_status_var = tk.StringVar(value="(不限)")
    search_var = tk.StringVar()
    name_contains_var = tk.StringVar()
    group_filter_var = tk.StringVar(value=GROUP_FILTER_ALL)
    max_accounts_var = tk.StringVar()
    page_size_var = tk.StringVar(value=str(saved_config.bulk_page_size))
    batch_size_var = tk.StringVar(value=str(saved_config.bulk_batch_size))
    ungrouped_only_var = tk.BooleanVar(value=False)
    dry_run_var = tk.BooleanVar(value=False)

    ttk.Label(filter_frame, text="指定账号").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
    account_picker_frame = ttk.Frame(filter_frame)
    account_picker_frame.grid(row=0, column=1, sticky="ew", pady=3)
    account_picker_frame.columnconfigure(0, weight=1)
    account_picker_combo = ttk.Combobox(account_picker_frame, textvariable=account_picker_var, values=[ACCOUNT_PICKER_HINT], state="readonly")
    account_picker_combo.grid(row=0, column=0, sticky="ew")
    account_picker_combos.append(account_picker_combo)
    ttk.Label(filter_frame, text="平台").grid(row=0, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Combobox(filter_frame, textvariable=platform_var, values=["(不限)", "openai", "anthropic", "gemini", "antigravity"], state="readonly").grid(row=0, column=3, sticky="ew", pady=3)
    ttk.Label(filter_frame, text="账号类型").grid(row=0, column=4, sticky="w", padx=(8, 6), pady=3)
    ttk.Combobox(filter_frame, textvariable=account_type_var, values=["(不限)", "oauth", "apikey", "setup-token", "upstream", "bedrock"], state="readonly").grid(row=0, column=5, sticky="ew", pady=3)
    ttk.Label(filter_frame, text="账号状态").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Combobox(filter_frame, textvariable=account_status_var, values=["(不限)", "active", "inactive", "error"], state="readonly").grid(row=1, column=1, sticky="ew", pady=3)
    ttk.Label(filter_frame, text="搜索关键词").grid(row=1, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(filter_frame, textvariable=search_var).grid(row=1, column=3, sticky="ew", pady=3)
    ttk.Label(filter_frame, text="名称包含").grid(row=1, column=4, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(filter_frame, textvariable=name_contains_var).grid(row=1, column=5, sticky="ew", pady=3)
    ttk.Label(filter_frame, text="当前分组").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
    group_filter_combo = ttk.Combobox(filter_frame, textvariable=group_filter_var, values=[GROUP_FILTER_ALL], state="readonly")
    group_filter_combo.grid(row=2, column=1, sticky="ew", pady=3)
    group_filter_combos.append(group_filter_combo)
    ttk.Label(filter_frame, text="最多处理数量").grid(row=2, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(filter_frame, textvariable=max_accounts_var).grid(row=2, column=3, sticky="ew", pady=3)
    ttk.Label(filter_frame, text="每次读取数量").grid(row=2, column=4, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(filter_frame, textvariable=page_size_var).grid(row=2, column=5, sticky="ew", pady=3)
    ttk.Label(filter_frame, text="每次提交数量").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(filter_frame, textvariable=batch_size_var).grid(row=3, column=1, sticky="ew", pady=3)
    ttk.Checkbutton(filter_frame, text="仅未分组账号", variable=ungrouped_only_var).grid(row=3, column=2, sticky="w", pady=3)
    ttk.Checkbutton(filter_frame, text="开始调整前先做预览", variable=dry_run_var).grid(row=3, column=3, sticky="w", pady=3)
    smart_refresh_btn = ttk.Button(filter_frame, text="同步分组选项", command=lambda: run_action("同步分组", fetch_groups, determinate=True))
    smart_refresh_btn.grid(row=3, column=5, sticky="e", pady=3)
    action_buttons.append(smart_refresh_btn)
    smart_sync_accounts_btn = ttk.Button(filter_frame, text="同步账号列表", command=lambda: run_action("同步账号列表", fetch_accounts, determinate=True))
    smart_sync_accounts_btn.grid(row=4, column=4, sticky="e", pady=3)
    action_buttons.append(smart_sync_accounts_btn)
    smart_sync_proxies_btn = ttk.Button(filter_frame, text="同步代理列表", command=lambda: run_action("同步代理列表", fetch_proxies, determinate=True))
    smart_sync_proxies_btn.grid(row=4, column=5, sticky="e", pady=3)
    action_buttons.append(smart_sync_proxies_btn)
    ttk.Label(filter_frame, textvariable=account_selection_summary_var, style="Hint.TLabel", justify="left", wraplength=620).grid(row=4, column=0, columnspan=4, sticky="w", pady=3)
    ttk.Label(filter_frame, text="当前分组选“不限”时，会匹配全部账号，包含未分组；如果勾选“仅未分组账号”，则只处理当前未分组账号。", style="Hint.TLabel", justify="left", wraplength=980).grid(row=5, column=0, columnspan=6, sticky="w", pady=(0, 3))

    update_frame = ttk.LabelFrame(tab_bulk_all, text="要修改成什么", padding=8)
    update_frame.grid(row=3, column=0, sticky="ew", pady=(8, 0))
    for c in range(4):
        update_frame.columnconfigure(c, weight=1 if c in (1, 3) else 0)

    update_group_var = tk.StringVar(value=GROUP_UPDATE_KEEP)
    update_proxy_var = tk.StringVar(value=PROXY_UPDATE_KEEP)
    update_status_var = tk.StringVar(value=KEEP_OPTION)
    update_schedulable_var = tk.StringVar(value=KEEP_OPTION)
    update_name_var = tk.StringVar()
    update_notes_var = tk.StringVar()
    update_notes_mode_var = tk.StringVar(value=KEEP_OPTION)
    update_type_var = tk.StringVar(value=KEEP_OPTION)
    update_concurrency_var = tk.StringVar()
    update_load_factor_mode_var = tk.StringVar(value=KEEP_OPTION)
    update_load_factor_var = tk.StringVar()
    update_priority_var = tk.StringVar()
    update_rate_multiplier_var = tk.StringVar()
    update_expires_at_mode_var = tk.StringVar(value=KEEP_OPTION)
    update_expires_at_var = tk.StringVar()
    update_auto_pause_var = tk.StringVar(value=KEEP_OPTION)
    confirm_mixed_channel_var = tk.BooleanVar(value=False)
    advanced_visible_var = tk.BooleanVar(value=False)
    ttk.Label(update_frame, text="改到分组").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
    group_update_combo = ttk.Combobox(update_frame, textvariable=update_group_var, values=[GROUP_UPDATE_KEEP, GROUP_UPDATE_CLEAR], state="readonly")
    group_update_combo.grid(row=0, column=1, sticky="ew", pady=3)
    group_update_combos.append(group_update_combo)
    ttk.Label(update_frame, text="代理").grid(row=0, column=2, sticky="w", padx=(8, 6), pady=3)
    update_proxy_combo = ttk.Combobox(update_frame, textvariable=update_proxy_var, values=[PROXY_UPDATE_KEEP, PROXY_UPDATE_CLEAR], state="readonly")
    update_proxy_combo.grid(row=0, column=3, sticky="ew", pady=3)
    proxy_update_combos.append(update_proxy_combo)
    ttk.Label(update_frame, text="账号状态").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Combobox(update_frame, textvariable=update_status_var, values=[KEEP_OPTION, "active", "inactive", "error"], state="readonly").grid(row=1, column=1, sticky="ew", pady=3)
    ttk.Label(update_frame, text="允许调度").grid(row=1, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Combobox(update_frame, textvariable=update_schedulable_var, values=[KEEP_OPTION, ENABLE_OPTION, DISABLE_OPTION], state="readonly").grid(row=1, column=3, sticky="ew", pady=3)
    ttk.Label(update_frame, text="名称改为").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(update_frame, textvariable=update_name_var).grid(row=2, column=1, sticky="ew", pady=3)
    ttk.Label(update_frame, text="备注").grid(row=2, column=2, sticky="w", padx=(8, 6), pady=3)
    notes_frame = ttk.Frame(update_frame)
    notes_frame.grid(row=2, column=3, sticky="ew", pady=3)
    notes_frame.columnconfigure(1, weight=1)
    ttk.Combobox(notes_frame, textvariable=update_notes_mode_var, values=[KEEP_OPTION, CLEAR_OPTION], state="readonly", width=10).grid(row=0, column=0, sticky="w", padx=(0, 6))
    ttk.Entry(notes_frame, textvariable=update_notes_var).grid(row=0, column=1, sticky="ew")
    ttk.Label(update_frame, text="账号类型").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Combobox(update_frame, textvariable=update_type_var, values=[KEEP_OPTION, "oauth", "apikey", "setup-token", "upstream", "bedrock"], state="readonly").grid(row=3, column=1, sticky="ew", pady=3)
    ttk.Label(update_frame, text="并发数").grid(row=3, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(update_frame, textvariable=update_concurrency_var).grid(row=3, column=3, sticky="ew", pady=3)
    ttk.Label(update_frame, text="负载因子").grid(row=4, column=0, sticky="w", padx=(0, 6), pady=3)
    load_factor_frame = ttk.Frame(update_frame)
    load_factor_frame.grid(row=4, column=1, sticky="ew", pady=3)
    load_factor_frame.columnconfigure(1, weight=1)
    ttk.Combobox(load_factor_frame, textvariable=update_load_factor_mode_var, values=[KEEP_OPTION, CLEAR_OPTION], state="readonly", width=10).grid(row=0, column=0, sticky="w", padx=(0, 6))
    ttk.Entry(load_factor_frame, textvariable=update_load_factor_var).grid(row=0, column=1, sticky="ew")
    ttk.Label(update_frame, text="优先级").grid(row=4, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(update_frame, textvariable=update_priority_var).grid(row=4, column=3, sticky="ew", pady=3)
    ttk.Label(update_frame, text="速率倍率").grid(row=5, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(update_frame, textvariable=update_rate_multiplier_var).grid(row=5, column=1, sticky="ew", pady=3)
    ttk.Label(update_frame, text="过期时间").grid(row=5, column=2, sticky="w", padx=(8, 6), pady=3)
    expires_at_frame = ttk.Frame(update_frame)
    expires_at_frame.grid(row=5, column=3, sticky="ew", pady=3)
    expires_at_frame.columnconfigure(1, weight=1)
    ttk.Combobox(expires_at_frame, textvariable=update_expires_at_mode_var, values=[KEEP_OPTION, CLEAR_OPTION], state="readonly", width=10).grid(row=0, column=0, sticky="w", padx=(0, 6))
    ttk.Entry(expires_at_frame, textvariable=update_expires_at_var).grid(row=0, column=1, sticky="ew")
    ttk.Label(update_frame, text="到期自动暂停").grid(row=6, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Combobox(update_frame, textvariable=update_auto_pause_var, values=[KEEP_OPTION, ENABLE_OPTION, DISABLE_OPTION], state="readonly").grid(row=6, column=1, sticky="ew", pady=3)
    ttk.Checkbutton(update_frame, text="确认混合渠道风险（绑定分组时跳过风险拦截）", variable=confirm_mixed_channel_var).grid(row=6, column=2, columnspan=2, sticky="w", pady=3)

    action_frame = ttk.Frame(tab_bulk_all)
    action_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
    action_frame.columnconfigure(3, weight=1)

    advanced_frame = ttk.LabelFrame(tab_bulk_all, text="高级设置（一般不用填）", padding=8)
    advanced_frame.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
    advanced_frame.columnconfigure(0, weight=1)
    advanced_frame.columnconfigure(1, weight=1)
    advanced_frame.rowconfigure(1, weight=1)
    advanced_frame.rowconfigure(3, weight=1)
    ttk.Label(advanced_frame, text="AI 平台账号信息 JSON").grid(row=0, column=0, sticky="w")
    ttk.Label(advanced_frame, text="扩展信息 JSON").grid(row=0, column=1, sticky="w")
    credentials_text = ScrolledText(advanced_frame, height=6)
    extra_text = ScrolledText(advanced_frame, height=6)
    credentials_text.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(3, 6))
    extra_text.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(3, 6))
    ttk.Label(advanced_frame, text="底层补充更新 JSON（仅高级用户；会覆盖上面的同名字段）").grid(row=2, column=0, columnspan=2, sticky="w")
    manual_updates_text = ScrolledText(advanced_frame, height=6)
    manual_updates_text.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(3, 0))

    def refresh_bulk_advanced_ui() -> None:
        if advanced_visible_var.get():
            advanced_frame.grid()
            toggle_advanced_btn.configure(text="收起高级设置")
        else:
            advanced_frame.grid_remove()
            toggle_advanced_btn.configure(text="展开高级设置")

    bulk_all_btn = ttk.Button(action_frame, text="开始批量调整", command=lambda: run_action("批量调整账号", bulk_all_action, determinate=True))
    bulk_all_btn.grid(row=0, column=0, sticky="w")
    action_buttons.append(bulk_all_btn)
    preview_bulk_all_btn = ttk.Button(action_frame, text="预览匹配结果", command=lambda: run_action("预览批量调整", preview_bulk_all_action, determinate=True))
    preview_bulk_all_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))
    action_buttons.append(preview_bulk_all_btn)
    toggle_advanced_btn = ttk.Button(action_frame, text="展开高级设置", command=lambda: (advanced_visible_var.set(not advanced_visible_var.get()), refresh_bulk_advanced_ui()))
    toggle_advanced_btn.grid(row=0, column=2, sticky="w", padx=(8, 0))
    action_buttons.append(toggle_advanced_btn)
    ttk.Label(action_frame, text="常用修改只需要上面的筛选条件和修改目标，下面的 JSON 一般可以不碰。", style="Hint.TLabel").grid(row=0, column=3, sticky="e")
    refresh_bulk_advanced_ui()

    def normalize_select(value: str) -> str | None:
        stripped = value.strip()
        if not stripped or stripped.startswith("("):
            return None
        return stripped

    def update_account_selection_summary(*_args: Any) -> None:
        ids = parse_ids(account_ids_raw_var.get(), "指定账号", required=False) or []
        if not ids:
            account_selection_summary_var.set("未指定账号时，会按下面的筛选条件自动匹配。")
            return
        labels = [account_id_to_label.get(account_id, f"[{account_id}]") for account_id in ids[:3]]
        preview = "，".join(labels)
        if len(ids) > 3:
            preview += f" 等 {len(ids)} 个账号"
        account_selection_summary_var.set(f"当前已指定 {len(ids)} 个账号：{preview}")

    def add_bulk_account_from_picker() -> None:
        label = account_picker_var.get().strip()
        if not label or label == ACCOUNT_PICKER_HINT:
            raise CLIError("请先从下拉框里选择一个账号")
        account_id = account_label_to_id.get(label)
        if account_id is None:
            raise CLIError("账号下拉值已过期，请重新同步账号列表")
        ids = parse_ids(account_ids_raw_var.get(), "指定账号", required=False) or []
        if account_id not in ids:
            ids.append(account_id)
        account_ids_raw_var.set(" ".join(str(item) for item in ids))

    def clear_bulk_account_selection() -> None:
        account_ids_raw_var.set("")

    account_ids_raw_var.trace_add("write", update_account_selection_summary)
    account_picker_add_btn = ttk.Button(account_picker_frame, text="添加", command=safe_ui_action(add_bulk_account_from_picker))
    account_picker_add_btn.grid(row=0, column=1, padx=(6, 0))
    action_buttons.append(account_picker_add_btn)
    account_picker_clear_btn = ttk.Button(account_picker_frame, text="清空", command=clear_bulk_account_selection)
    account_picker_clear_btn.grid(row=0, column=2, padx=(6, 0))
    action_buttons.append(account_picker_clear_btn)
    update_account_selection_summary()

    def selected_group_ids_from_label(value: str, *, allow_none: bool, allow_clear: bool) -> list[int] | None:
        selected = value.strip()
        if not selected:
            return None if allow_none else []
        if allow_none and selected == GROUP_FILTER_ALL:
            return None
        if allow_none and selected == GROUP_UPDATE_KEEP:
            return None
        if allow_clear and selected == GROUP_UPDATE_CLEAR:
            return []
        group_id = group_label_to_id.get(selected)
        if group_id is None:
            raise CLIError("分组下拉值无效，请先点击“同步分组”")
        return [group_id]

    def selected_proxy_id_from_label(value: str) -> int | None:
        selected = value.strip()
        if not selected or selected == PROXY_UPDATE_KEEP:
            return None
        if selected == PROXY_UPDATE_CLEAR:
            return 0
        proxy_id = proxy_label_to_id.get(selected)
        if proxy_id is None:
            raise CLIError("代理下拉值无效，请先点击“同步代理列表”")
        return proxy_id

    def preview_bulk_all_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        original_value = bool(dry_run_var.get())
        dry_run_var.set(True)
        try:
            return bulk_all_action(progress_callback)
        finally:
            dry_run_var.set(original_value)

    def bulk_all_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        updates: dict[str, Any] = {}
        target_group_ids = selected_group_ids_from_label(update_group_var.get(), allow_none=True, allow_clear=True)
        if target_group_ids is not None:
            updates["group_ids"] = target_group_ids
        proxy_id = selected_proxy_id_from_label(update_proxy_var.get())
        if proxy_id is not None:
            updates["proxy_id"] = proxy_id
        update_status = normalize_select(update_status_var.get())
        if update_status:
            updates["status"] = update_status
        has_schedulable, schedulable_value = parse_optional_bool_choice(update_schedulable_var.get(), "允许调度")
        if has_schedulable:
            updates["schedulable"] = bool(schedulable_value)
        update_name = non_empty(update_name_var.get())
        if update_name is not None:
            updates["name"] = update_name
        notes_mode = update_notes_mode_var.get().strip()
        notes_text = update_notes_var.get().strip()
        if notes_mode == CLEAR_OPTION:
            updates["notes"] = None
        elif notes_text:
            updates["notes"] = notes_text
        update_type = normalize_select(update_type_var.get())
        if update_type:
            updates["type"] = update_type
        update_concurrency = parse_optional_positive_int(update_concurrency_var.get(), "并发数")
        if update_concurrency is not None:
            updates["concurrency"] = update_concurrency
        load_factor_mode = update_load_factor_mode_var.get().strip()
        load_factor_raw = update_load_factor_var.get().strip()
        if load_factor_mode == CLEAR_OPTION:
            updates["load_factor"] = None
        elif load_factor_raw:
            load_factor_value = parse_optional_int(load_factor_raw, "负载因子")
            if load_factor_value is None:
                raise CLIError("负载因子不能为空")
            updates["load_factor"] = load_factor_value
        update_priority = parse_optional_int(update_priority_var.get(), "优先级")
        if update_priority is not None:
            updates["priority"] = update_priority
        rate_multiplier = parse_optional_float(update_rate_multiplier_var.get(), "速率倍率", min_value=0.0)
        if rate_multiplier is not None:
            updates["rate_multiplier"] = rate_multiplier
        expires_at_mode = update_expires_at_mode_var.get().strip()
        expires_at_raw = update_expires_at_var.get().strip()
        if expires_at_mode == CLEAR_OPTION:
            updates["expires_at"] = None
        elif expires_at_raw:
            updates["expires_at"] = parse_optional_timestamp(expires_at_raw, "过期时间")
        has_auto_pause, auto_pause_value = parse_optional_bool_choice(update_auto_pause_var.get(), "到期自动暂停")
        if has_auto_pause:
            updates["auto_pause_on_expired"] = bool(auto_pause_value)
        if confirm_mixed_channel_var.get():
            updates["confirm_mixed_channel_risk"] = True

        credentials_payload = parse_json_text(credentials_text.get("1.0", tk.END), "AI 平台账号信息 JSON", require_dict=True)
        if credentials_payload is not None:
            updates["credentials"] = credentials_payload
        extra_payload = parse_json_text(extra_text.get("1.0", tk.END), "扩展信息 JSON", require_dict=True)
        if extra_payload is not None:
            updates["extra"] = extra_payload
        manual_updates = parse_json_text(manual_updates_text.get("1.0", tk.END), "底层补充更新 JSON", require_dict=True)
        if manual_updates is not None:
            updates.update(manual_updates)

        if "account_ids" in updates:
            raise CLIError("更新字段中不允许包含 account_ids")
        if not updates:
            raise CLIError("请至少填写一个更新字段")

        args = argparse.Namespace(
            updates_file="",
            updates_payload=updates,
            account_ids=parse_ids(account_ids_raw_var.get(), "指定账号", required=False),
            platform=normalize_select(platform_var.get()),
            account_type=normalize_select(account_type_var.get()),
            account_status=normalize_select(account_status_var.get()),
            search=non_empty(search_var.get()),
            name_contains=non_empty(name_contains_var.get()),
            group_ids=selected_group_ids_from_label(group_filter_var.get(), allow_none=True, allow_clear=False),
            ungrouped_only=bool(ungrouped_only_var.get()),
            max_accounts=parse_optional_positive_int(max_accounts_var.get(), "最多处理数量"),
            page_size=parse_optional_positive_int(page_size_var.get(), "每次读取数量") or 100,
            batch_size=parse_optional_positive_int(batch_size_var.get(), "每次提交数量") or 100,
            dry_run=bool(dry_run_var.get()),
            progress_callback=progress_callback,
        )
        return handle_bulk_update_all_accounts(get_client(), args).payload

    # 高级功能
    tab_api = add_tab("高级功能")
    method_var = tk.StringVar(value="GET")
    path_var = tk.StringVar(value="/admin/settings/admin-api-key")
    no_auth_var = tk.BooleanVar(value=False)
    ttk.Label(tab_api, text="这里是原始接口调试区，一般情况下不用打开。", style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
    ttk.Label(
        tab_api,
        text="只有当上面的可视化功能不够用时，再手动填写请求方式、接口路径和 JSON 内容。",
        style="Hint.TLabel",
        justify="left",
        wraplength=980,
    ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))
    ttk.Label(tab_api, text="请求方式").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Combobox(tab_api, textvariable=method_var, values=["GET", "POST", "PUT", "PATCH", "DELETE"], state="readonly", width=10).grid(row=2, column=1, sticky="w", pady=4)
    ttk.Label(tab_api, text="接口路径").grid(row=2, column=2, sticky="w", padx=(8, 8), pady=4)
    ttk.Entry(tab_api, textvariable=path_var).grid(row=2, column=3, sticky="ew", pady=4)
    tab_api.columnconfigure(3, weight=1)
    ttk.Checkbutton(tab_api, text="此请求不带鉴权", variable=no_auth_var).grid(row=3, column=3, sticky="w", pady=2)
    ttk.Label(tab_api, text="JSON 内容（可选）").grid(row=4, column=0, sticky="w", pady=4)
    request_body = ScrolledText(tab_api, height=10)
    request_body.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=4)
    tab_api.rowconfigure(5, weight=1)

    def api_request_action(_progress_callback: Callable[[int, int, str], None]) -> Any:
        method = method_var.get().strip().upper() or "GET"
        path = non_empty(path_var.get())
        if not path:
            raise CLIError("接口路径不能为空")
        payload = parse_json_text(request_body.get("1.0", tk.END), "JSON 内容", require_dict=False)
        client = get_client(require_admin_key=not bool(no_auth_var.get()))
        return client.request(method, path, payload, auth_required=not bool(no_auth_var.get()))

    api_btn = ttk.Button(tab_api, text="发送高级请求", command=lambda: run_action("高级接口请求", api_request_action))
    api_btn.grid(row=6, column=0, sticky="w", pady=4)
    action_buttons.append(api_btn)

    def on_close() -> None:
        request_cancel_current_task()
        cancel_scheduled_afters()
        try:
            save_gui_config_action(show_message=False)
        except CLIError:
            pass
        try:
            root.quit()
        except tk.TclError:
            pass
        try:
            root.destroy()
        except tk.TclError:
            pass
        try:
            os._exit(0)
        except Exception:
            raise SystemExit(0)

    menu_bar = tk.Menu(root)
    help_menu = tk.Menu(menu_bar, tearoff=0)
    help_menu.add_command(label="关于", command=show_about_dialog)
    help_menu.add_command(label="检查更新", command=lambda: trigger_update_check(interactive=True))
    help_menu.add_separator()
    help_menu.add_command(label="打开 Release 页面", command=open_release_page)
    menu_bar.add_cascade(label="帮助", menu=help_menu)
    root.configure(menu=menu_bar)

    root.protocol("WM_DELETE_WINDOW", on_close)
    safe_after(2500, lambda: trigger_update_check(interactive=False, silent_when_latest=True))

    root.mainloop()


def to_positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int):
        raise CLIError(f"{label} 必须是整数")
    if value <= 0:
        raise CLIError(f"{label} 必须为正整数")
    return value


def to_int_list(values: Any, label: str) -> list[int]:
    if not isinstance(values, list):
        raise CLIError(f"{label} 必须是整数数组")
    return [to_positive_int(v, label) for v in values]


def parse_int_field(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_admin_list_page_size(page_size: Any) -> int:
    parsed = parse_int_field(page_size, 0)
    if parsed <= 0:
        return ADMIN_LIST_PAGE_SIZE_CAP
    return min(parsed, ADMIN_LIST_PAGE_SIZE_CAP)


def clamp_sync_concurrency(value: Any) -> int:
    parsed = parse_int_field(value, 0)
    if parsed <= 0:
        return DEFAULT_SYNC_CONCURRENCY
    return parsed


def clamp_detection_concurrency(value: Any) -> int:
    parsed = parse_int_field(value, 0)
    if parsed <= 0:
        return DEFAULT_DETECTION_CONCURRENCY
    return parsed


def clamp_delete_concurrency(value: Any) -> int:
    parsed = parse_int_field(value, 0)
    if parsed <= 0:
        return DEFAULT_DELETE_CONCURRENCY
    return parsed


def list_accounts_page(
    client: AdminAPIClient,
    *,
    page: int,
    page_size: int,
    platform: str | None,
    account_type: str | None,
    status: str | None,
    search: str | None,
    group_id: int | str | None = None,
    lite: bool = False,
) -> dict[str, Any]:
    effective_page_size = clamp_admin_list_page_size(page_size)
    query: dict[str, Any] = {"page": page, "page_size": effective_page_size}
    if platform:
        query["platform"] = platform
    if account_type:
        query["type"] = account_type
    if status:
        query["status"] = status
    if search:
        query["search"] = search
    if isinstance(group_id, int) and group_id > 0:
        query["group"] = group_id
    elif isinstance(group_id, str) and group_id.strip().lower() == ACCOUNT_LIST_GROUP_UNGROUPED:
        query["group"] = ACCOUNT_LIST_GROUP_UNGROUPED
    if lite:
        query["lite"] = "true"

    payload = client.request("GET", f"/admin/accounts?{urlencode(query)}")
    if not isinstance(payload, dict):
        raise CLIError("`/admin/accounts` 返回格式异常：期望对象")
    if not isinstance(payload.get("items"), list):
        raise CLIError("`/admin/accounts` 返回格式异常：缺少数组字段 `items`")
    return payload


def list_all_accounts(
    client: AdminAPIClient,
    *,
    page_size: int = ADMIN_LIST_PAGE_SIZE_CAP,
    concurrency: int = 1,
    group_id: int | str | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_callback: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    seen: set[int] = set()
    effective_page_size = clamp_admin_list_page_size(page_size)
    effective_concurrency = clamp_sync_concurrency(concurrency)

    def merge_items(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            account_id = item.get("id")
            if not isinstance(account_id, int) or account_id <= 0 or account_id in seen:
                continue
            seen.add(account_id)
            accounts.append(item)

    def progress_message(prefix: str, *, completed_pages: int, total_pages: int, current_page: int | None = None) -> str:
        synced_accounts = len(accounts)
        if total > 0:
            count_text = f"已同步 {synced_accounts}/{total} 个账号"
        else:
            count_text = f"已同步 {synced_accounts} 个账号"
        page_text = f"页数 {completed_pages}/{total_pages}"
        if current_page is not None:
            page_text += f"，当前页 {current_page}"
        return f"{prefix}，{count_text}，{page_text}"

    first_page = list_accounts_page(
        client,
        page=1,
        page_size=effective_page_size,
        platform=None,
        account_type=None,
        status=None,
        search=None,
        group_id=group_id,
    )
    first_items = first_page.get("items")
    if not isinstance(first_items, list) or not first_items:
        if progress_callback:
            progress_callback(1, 1, "账号列表同步完成，共 0 个")
        return accounts

    total = parse_int_field(first_page.get("total"), 0)
    total_pages = max((total + effective_page_size - 1) // effective_page_size, 1) if total > 0 else 1
    merge_items(first_items)
    if progress_callback:
        progress_callback(1, total_pages, progress_message("正在读取账号列表", completed_pages=1, total_pages=total_pages, current_page=1))

    if total_pages > 1 and effective_concurrency > 1:
        remaining_pages = list(range(2, total_pages + 1))
        max_workers = min(effective_concurrency, len(remaining_pages))
        completed_pages = 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    list_accounts_page,
                    client,
                    page=page_num,
                    page_size=effective_page_size,
                    platform=None,
                    account_type=None,
                    status=None,
                    search=None,
                    group_id=group_id,
                ): page_num
                for page_num in remaining_pages
            }
            pending = set(future_map.keys())
            while pending:
                if callable(cancel_callback):
                    cancel_callback()
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    page_num = future_map[future]
                    page_data = future.result()
                    merge_items(page_data.get("items"))
                    completed_pages += 1
                    if progress_callback:
                        progress_callback(
                            completed_pages,
                            total_pages,
                            progress_message("正在并发读取账号列表", completed_pages=completed_pages, total_pages=total_pages, current_page=page_num),
                        )
            executor.shutdown(wait=False, cancel_futures=True)
    else:
        page = 2
        while page <= total_pages:
            if callable(cancel_callback):
                cancel_callback()
            page_data = list_accounts_page(
                client,
                page=page,
                page_size=effective_page_size,
                platform=None,
                account_type=None,
                status=None,
                search=None,
                group_id=group_id,
            )
            merge_items(page_data.get("items"))
            if progress_callback:
                progress_callback(page, total_pages, progress_message("正在读取账号列表", completed_pages=page, total_pages=total_pages, current_page=page))
            page += 1

    if progress_callback:
        progress_callback(1, 1, f"账号列表同步完成，共同步 {len(accounts)} 个账号")
    return accounts


def list_all_proxies(client: AdminAPIClient) -> list[dict[str, Any]]:
    payload = client.request("GET", "/admin/proxies/all?with_count=true")
    if not isinstance(payload, list):
        raise CLIError("`/admin/proxies/all` 返回格式异常：期望数组")
    return [item for item in payload if isinstance(item, dict)]


def chunked_items[T](items: list[T], size: int) -> list[list[T]]:
    if size <= 0:
        raise CLIError("分批大小必须为正整数")
    return [items[i : i + size] for i in range(0, len(items), size)]


def get_default_download_output_dir(dirname: str) -> Path:
    for folder_name in ("Downloads", "Desktop", "Documents"):
        candidate = Path.home() / folder_name
        if candidate.exists() and candidate.is_dir():
            return candidate / dirname
    return Path.cwd() / dirname


def sanitize_filename_component(name: str, *, fallback: str) -> str:
    value = re.sub(r"\s+", " ", name.strip())
    value = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]', "_", value)
    value = value.strip(" .")
    return value or fallback


def standardize_exported_accounts_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CLIError("导出账号数据返回格式异常：应为 JSON 对象")

    raw_accounts = payload.get("accounts")
    raw_proxies = payload.get("proxies")
    if not isinstance(raw_accounts, list):
        raise CLIError("导出账号数据返回格式异常：缺少 `accounts` 数组")
    if not isinstance(raw_proxies, list):
        raise CLIError("导出账号数据返回格式异常：缺少 `proxies` 数组")

    exported_at = payload.get("exported_at")
    standardized = build_standard_accounts_data_payload(
        {
            "exported_at": exported_at if isinstance(exported_at, str) and exported_at.strip() else utc_now_rfc3339(),
            "proxies": [dict(item) for item in raw_proxies if isinstance(item, dict)],
            "accounts": [dict(item) for item in raw_accounts if isinstance(item, dict)],
        }
    )
    return standardized


def merge_exported_accounts_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    if not payloads:
        return build_standard_accounts_data_payload({"exported_at": utc_now_rfc3339(), "proxies": [], "accounts": []})

    merged_accounts: list[dict[str, Any]] = []
    merged_proxies: list[dict[str, Any]] = []
    seen_proxy_keys: set[str] = set()
    exported_at = payloads[0].get("exported_at") if isinstance(payloads[0].get("exported_at"), str) else utc_now_rfc3339()

    for payload in payloads:
        standardized = standardize_exported_accounts_payload(payload)
        merged_accounts.extend([dict(item) for item in standardized.get("accounts") or [] if isinstance(item, dict)])
        for proxy in standardized.get("proxies") or []:
            if not isinstance(proxy, dict):
                continue
            proxy_key = str(proxy.get("proxy_key") or "").strip()
            if proxy_key:
                if proxy_key in seen_proxy_keys:
                    continue
                seen_proxy_keys.add(proxy_key)
            merged_proxies.append(dict(proxy))

    return build_standard_accounts_data_payload(
        {
            "exported_at": exported_at,
            "proxies": merged_proxies,
            "accounts": merged_accounts,
        }
    )


def build_accounts_export_path(
    *,
    ids: list[int] | None = None,
    platform: str | None = None,
    account_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    include_proxies: bool,
) -> str:
    params: dict[str, Any] = {}
    if ids:
        params["ids"] = ",".join(str(item) for item in ids)
    else:
        if platform:
            params["platform"] = platform
        if account_type:
            params["type"] = account_type
        if status:
            params["status"] = status
        if search:
            params["search"] = search
    if not include_proxies:
        params["include_proxies"] = "false"
    suffix = f"?{urlencode(params)}" if params else ""
    return f"/admin/accounts/data{suffix}"


def fetch_accounts_export_data(
    client: AdminAPIClient,
    *,
    ids: list[int] | None = None,
    platform: str | None = None,
    account_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    include_proxies: bool = True,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    if ids:
        resolved_ids = unique_ids(ids)
        batches = chunked_items(resolved_ids, EXPORT_IDS_BATCH_SIZE)
        payloads: list[dict[str, Any]] = []
        for index, batch in enumerate(batches, start=1):
            if progress_callback:
                progress_callback(index - 1, len(batches), f"正在导出账号数据，第 {index}/{len(batches)} 批，本批 {len(batch)} 个账号")
            payload = client.request(
                "GET",
                build_accounts_export_path(ids=batch, include_proxies=include_proxies),
            )
            if not isinstance(payload, dict):
                raise CLIError("`/admin/accounts/data` 返回格式异常")
            payloads.append(payload)
        if progress_callback:
            progress_callback(len(batches), len(batches), "账号数据导出完成")
        return merge_exported_accounts_payloads(payloads)

    if progress_callback:
        progress_callback(0, 1, "正在导出账号数据...")
    payload = client.request(
        "GET",
        build_accounts_export_path(
            platform=platform,
            account_type=account_type,
            status=status,
            search=search,
            include_proxies=include_proxies,
        ),
    )
    if not isinstance(payload, dict):
        raise CLIError("`/admin/accounts/data` 返回格式异常")
    standardized = standardize_exported_accounts_payload(payload)
    if progress_callback:
        progress_callback(1, 1, f"账号数据导出完成，共 {len(standardized.get('accounts') or [])} 个账号")
    return standardized


def subset_export_payload_for_accounts(
    payload: dict[str, Any],
    accounts: list[dict[str, Any]],
) -> dict[str, Any]:
    all_proxies = [dict(item) for item in payload.get("proxies") or [] if isinstance(item, dict)]
    needed_proxy_keys = {
        str(item.get("proxy_key") or "").strip()
        for item in accounts
        if isinstance(item, dict) and isinstance(item.get("proxy_key"), str) and str(item.get("proxy_key") or "").strip()
    }
    selected_proxies = [proxy for proxy in all_proxies if str(proxy.get("proxy_key") or "").strip() in needed_proxy_keys]
    return build_standard_accounts_data_payload(
        {
            "exported_at": str(payload.get("exported_at") or utc_now_rfc3339()),
            "proxies": selected_proxies,
            "accounts": [dict(item) for item in accounts if isinstance(item, dict)],
        }
    )


def build_accounts_export_file_plans(payload: dict[str, Any], *, accounts_per_file: int) -> list[dict[str, Any]]:
    standardized = standardize_exported_accounts_payload(payload)
    accounts = [dict(item) for item in standardized.get("accounts") or [] if isinstance(item, dict)]
    if not accounts:
        raise CLIError("当前条件下没有可导出的账号")
    if accounts_per_file <= 0:
        raise CLIError("每个文件包含账号数必须为正整数")

    chunks = chunked_items(accounts, accounts_per_file)
    total_files = len(chunks)
    digits = max(len(str(total_files)), 2)
    plans: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        if accounts_per_file == 1 and len(chunk) == 1:
            account_name = str(chunk[0].get("name") or "").strip()
            name_base = sanitize_filename_component(account_name, fallback=f"account-{index:0{digits}d}")
        else:
            name_base = f"accounts-{index:0{digits}d}-of-{total_files:0{digits}d}"
        chunk_payload = subset_export_payload_for_accounts(standardized, chunk)
        plans.append(
            {
                "name_base": name_base,
                "account_count": len(chunk),
                "proxy_count": len(chunk_payload.get("proxies") or []),
                "account_names": [str(item.get("name") or "") for item in chunk],
                "payload": chunk_payload,
            }
        )
    return plans


def make_unique_json_output_path(output_dir: Path, base_name: str, used_names: dict[str, int]) -> Path:
    safe_base = sanitize_filename_component(base_name, fallback="accounts")
    count = used_names.get(safe_base, 0) + 1
    used_names[safe_base] = count
    candidate_name = safe_base if count == 1 else f"{safe_base}-{count}"
    candidate = output_dir / f"{candidate_name}.json"
    while candidate.exists():
        count += 1
        used_names[safe_base] = count
        candidate = output_dir / f"{safe_base}-{count}.json"
    return candidate


def write_accounts_export_files(
    file_plans: list[dict[str, Any]],
    *,
    output_dir: Path,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    used_names: dict[str, int] = {}
    written_files: list[dict[str, Any]] = []
    for index, plan in enumerate(file_plans, start=1):
        target = make_unique_json_output_path(output_dir, str(plan.get("name_base") or f"accounts-{index}"), used_names)
        payload = plan.get("payload")
        if not isinstance(payload, dict):
            raise CLIError(f"文件计划 {index} 缺少可写出的 JSON 数据")
        if progress_callback:
            progress_callback(index, len(file_plans), f"写出 {index}/{len(file_plans)}: {target.name}")
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written_files.append(
            {
                "file": str(target),
                "account_count": int(plan.get("account_count") or 0),
                "proxy_count": int(plan.get("proxy_count") or 0),
                "account_names": plan.get("account_names") or [],
            }
        )
    return written_files


def account_matches_local_filters(account: dict[str, Any], args: argparse.Namespace) -> bool:
    account_id = account.get("id")
    if not isinstance(account_id, int) or account_id <= 0:
        return False

    group_ids_raw = account.get("group_ids")
    group_ids = group_ids_raw if isinstance(group_ids_raw, list) else []
    group_ids_int = [gid for gid in group_ids if isinstance(gid, int) and gid > 0]
    group_id_set = set(group_ids_int)

    if args.ungrouped_only and group_id_set:
        return False

    if args.group_ids and group_id_set.isdisjoint(args.group_ids):
        return False

    if args.name_contains:
        name = str(account.get("name") or "")
        if args.name_contains.lower() not in name.lower():
            return False

    return True


def collect_target_account_ids(
    client: AdminAPIClient,
    args: argparse.Namespace,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[int]:
    if args.account_ids:
        ids = unique_ids(args.account_ids)
        if progress_callback:
            progress_callback(1, 1, f"直接使用指定账号 ID，共 {len(ids)} 个")
        if args.max_accounts and args.max_accounts > 0:
            return ids[: args.max_accounts]
        return ids

    page = 1
    seen: set[int] = set()
    target_ids: list[int] = []
    effective_page_size = clamp_admin_list_page_size(getattr(args, "page_size", None))
    fetched_count = 0
    request_group_id: int | str | None = None
    raw_group_ids = getattr(args, "group_ids", None)
    if getattr(args, "ungrouped_only", False):
        request_group_id = ACCOUNT_LIST_GROUP_UNGROUPED
    elif raw_group_ids:
        if isinstance(raw_group_ids, set) and len(raw_group_ids) == 1:
            request_group_id = next(iter(raw_group_ids))
        elif isinstance(raw_group_ids, list) and len(raw_group_ids) == 1:
            request_group_id = raw_group_ids[0]
        elif isinstance(raw_group_ids, tuple) and len(raw_group_ids) == 1:
            request_group_id = raw_group_ids[0]
        if not isinstance(request_group_id, int) or request_group_id <= 0:
            request_group_id = None

    while True:
        page_data = list_accounts_page(
            client,
            page=page,
            page_size=effective_page_size,
            platform=args.platform,
            account_type=args.account_type,
            status=args.account_status,
            search=args.search,
            group_id=request_group_id,
        )
        items = page_data.get("items")
        if not isinstance(items, list) or not items:
            break

        fetched_count += len(items)
        total = parse_int_field(page_data.get("total"), 0)
        total_pages = max((total + effective_page_size - 1) // effective_page_size, 1) if total > 0 else max(page, 1)
        if progress_callback:
            progress_callback(page, total_pages, f"扫描账号第 {page}/{total_pages} 页，已匹配 {len(target_ids)} 个")

        for raw in items:
            if not isinstance(raw, dict):
                continue
            if not account_matches_local_filters(raw, args):
                continue
            account_id = raw.get("id")
            if not isinstance(account_id, int) or account_id in seen:
                continue
            seen.add(account_id)
            target_ids.append(account_id)
            if args.max_accounts and args.max_accounts > 0 and len(target_ids) >= args.max_accounts:
                if progress_callback:
                    progress_callback(page, total_pages, f"达到 max_accounts={args.max_accounts}，提前结束扫描")
                return target_ids

        if (total > 0 and fetched_count >= total) or len(items) < effective_page_size:
            break
        page += 1

    if progress_callback:
        progress_callback(1, 1, f"扫描完成，命中账号 {len(target_ids)} 个")
    return target_ids


def chunked_ids(ids: list[int], size: int) -> list[list[int]]:
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def handle_bulk_update_all_accounts(client: AdminAPIClient, args: argparse.Namespace) -> CommandResult:
    args.page_size = to_positive_int(args.page_size, "`--page-size`")
    args.batch_size = to_positive_int(args.batch_size, "`--batch-size`")
    if args.max_accounts is not None and args.max_accounts <= 0:
        raise CLIError("`--max-accounts` 必须为正整数")
    if args.group_ids:
        args.group_ids = set(unique_ids(args.group_ids))

    progress_callback = getattr(args, "progress_callback", None)

    def emit_progress(current: int, total: int, message: str) -> None:
        if callable(progress_callback):
            progress_callback(max(current, 0), max(total, 1), message)

    updates_payload = getattr(args, "updates_payload", None)
    updates_file = getattr(args, "updates_file", None)
    if updates_payload is not None:
        updates = ensure_dict(updates_payload, "账号全量批量编辑输入")
    else:
        if not isinstance(updates_file, str) or not updates_file.strip():
            raise CLIError("请提供 `--updates-file`，或在程序内构造 `updates_payload`")
        updates = ensure_dict(read_json(updates_file), "账号全量批量编辑输入")

    if "account_ids" in updates:
        raise CLIError("`updates-file` 中不允许包含 `account_ids`，脚本会自动填充")
    if not updates:
        raise CLIError("`updates-file` 不能为空对象")
    bulk_supported_keys = {
        "name",
        "proxy_id",
        "concurrency",
        "priority",
        "rate_multiplier",
        "load_factor",
        "status",
        "schedulable",
        "group_ids",
        "credentials",
        "extra",
        "confirm_mixed_channel_risk",
    }
    single_update_only_keys = sorted(key for key in updates.keys() if key not in bulk_supported_keys)

    emit_progress(0, 1, "开始扫描匹配账号")
    target_ids = collect_target_account_ids(client, args, progress_callback=emit_progress)
    if args.dry_run:
        emit_progress(1, 1, f"Dry Run 完成，命中账号 {len(target_ids)} 个")
        return CommandResult(
            payload={
                "matched": len(target_ids),
                "sample_ids": target_ids[:20],
                "updates": updates,
                "update_mode": "single" if single_update_only_keys else "bulk",
                "single_update_only_keys": single_update_only_keys,
            }
        )

    if not target_ids:
        return CommandResult(
            payload={
                "matched": 0,
                "updated_success": 0,
                "updated_failed": 0,
                "message": "没有匹配到任何账号",
            }
        )

    updated_success = 0
    updated_failed = 0
    success_ids: list[int] = []
    failed_ids: list[int] = []
    results: list[dict[str, Any]] = []
    if single_update_only_keys:
        batches = [[account_id] for account_id in target_ids]
        for index, account_id in enumerate(target_ids, start=1):
            emit_progress(index - 1, len(target_ids), f"正在逐个更新账号 {index}/{len(target_ids)}")
            try:
                client.request("PUT", f"/admin/accounts/{account_id}", updates)
                updated_success += 1
                success_ids.append(account_id)
                results.append({"account_id": account_id, "success": True})
            except Exception as exc:
                updated_failed += 1
                failed_ids.append(account_id)
                results.append({"account_id": account_id, "success": False, "error": str(exc)})
            emit_progress(index, len(target_ids), f"逐个更新账号 {index}/{len(target_ids)}，成功 {updated_success}，失败 {updated_failed}")
        update_mode = "single"
    else:
        batch_size = to_positive_int(args.batch_size, "`--batch-size`")
        batches = chunked_ids(target_ids, batch_size)
        for index, batch in enumerate(batches, start=1):
            emit_progress(index, len(batches), f"正在执行第 {index}/{len(batches)} 批更新，每批 {len(batch)} 个账号")
            payload = dict(updates)
            payload["account_ids"] = batch
            result = client.request("POST", "/admin/accounts/bulk-update", payload)
            if not isinstance(result, dict):
                raise CLIError("`/admin/accounts/bulk-update` 返回格式异常")

            updated_success += parse_int_field(result.get("success"), 0)
            updated_failed += parse_int_field(result.get("failed"), 0)
            success_ids.extend(
                to_int_list(result.get("success_ids") or [], "`success_ids`")
                if isinstance(result.get("success_ids"), list)
                else []
            )
            failed_ids.extend(
                to_int_list(result.get("failed_ids") or [], "`failed_ids`")
                if isinstance(result.get("failed_ids"), list)
                else []
            )
            if isinstance(result.get("results"), list):
                results.extend([row for row in result["results"] if isinstance(row, dict)])
        update_mode = "bulk"

    emit_progress(len(batches), len(batches), f"更新完成：成功 {updated_success}，失败 {updated_failed}")
    success_details = [row for row in results if bool(row.get("success"))]
    failed_details = [row for row in results if not bool(row.get("success"))]
    return CommandResult(
        payload={
            "matched": len(target_ids),
            "batches": len(batches),
            "updated_success": updated_success,
            "updated_failed": updated_failed,
            "update_mode": update_mode,
            "single_update_only_keys": single_update_only_keys,
            "success_ids": success_ids,
            "failed_ids": failed_ids,
            "success_details": success_details[:200],
            "failed_details": failed_details[:200],
            "results": results,
        },
        failed=updated_failed > 0,
    )


def handle_api_request(client: AdminAPIClient, args: argparse.Namespace) -> CommandResult:
    payload = read_json(args.file) if args.file else None
    result = client.request(
        method=args.method,
        path=args.path,
        payload=payload,
        auth_required=not args.no_auth,
    )
    return CommandResult(payload=result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sub2API 管理端 API CLI。支持删除、批量创建、数据导入、批量编辑账号与代理。",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "示例:\n"
            "  python tools/admin_api_cli.py                   # 无参数直接打开可视化控制台\n"
            "  python tools/admin_api_cli.py gui-login         # 显式打开可视化控制台\n"
            "  python tools/admin_api_cli.py --email admin@example.com --password \"pwd\" login\n"
            "  python tools/admin_api_cli.py --admin-api-key xxx delete-accounts --ids 101 102\n"
            "  python tools/admin_api_cli.py --base-url http://127.0.0.1:8080 --admin-api-key xxx \\\n"
            "    bulk-update-accounts --file bulk-update.json\n"
            "  python tools/admin_api_cli.py --bearer-token xxx bulk-update-all-accounts --updates-file updates.json --ungrouped-only\n"
            "  python tools/admin_api_cli.py --bearer-token xxx api-request --method POST --path /admin/accounts/bulk-update --file req.json\n"
            "  python tools/admin_api_cli.py --admin-api-key xxx import-accounts-data --file bundle.json\n"
            "  python tools/admin_api_cli.py --admin-api-key xxx batch-create-proxies --file proxies.json\n"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"站点根地址或 API Base，默认取环境变量 SUB2API_BASE_URL，否则用 {DEFAULT_BASE_URL}",
    )
    parser.add_argument("--admin-api-key", default=os.environ.get(ENV_ADMIN_API_KEY))
    parser.add_argument("--bearer-token", default=os.environ.get(ENV_BEARER_TOKEN))
    parser.add_argument("--refresh-token", default=os.environ.get(ENV_REFRESH_TOKEN), help=f"刷新 token，可用环境变量 {ENV_REFRESH_TOKEN}")
    parser.add_argument(
        "--email",
        default=os.environ.get(ENV_LOGIN_EMAIL),
        help=f"管理员邮箱，也可用环境变量 {ENV_LOGIN_EMAIL}",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get(ENV_LOGIN_PASSWORD),
        help=f"管理员密码，也可用环境变量 {ENV_LOGIN_PASSWORD}",
    )
    parser.add_argument(
        "--turnstile-token",
        default=os.environ.get(ENV_TURNSTILE_TOKEN),
        help=f"登录验证码 token。仅当站点启用 Turnstile 时需要，可用环境变量 {ENV_TURNSTILE_TOKEN}",
    )
    parser.add_argument(
        "--totp-code",
        default=os.environ.get(ENV_TOTP_CODE),
        help=f"账号开启 2FA 时使用的 6 位 TOTP 验证码，也可用环境变量 {ENV_TOTP_CODE}",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP 超时秒数，默认 30")
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help=f"请求头 User-Agent，默认 `{DEFAULT_USER_AGENT}`，也可用环境变量 {ENV_USER_AGENT}",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="跳过 HTTPS 证书校验，仅用于自签名环境",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    delete_accounts = subparsers.add_parser("delete-accounts", help="删除一个或多个账号（脚本内循环单删）")
    delete_accounts.add_argument("--ids", nargs="+", type=int, required=True, help="账号 ID 列表")
    delete_accounts.add_argument("--verbose", action="store_true", help="逐条输出每个账号的删除响应")
    delete_accounts.set_defaults(handler=handle_delete_accounts)

    delete_proxies = subparsers.add_parser("delete-proxies", help="批量删除代理")
    delete_proxies.add_argument("--ids", nargs="+", type=int, required=True, help="代理 ID 列表")
    delete_proxies.set_defaults(handler=handle_delete_proxies)

    batch_create_accounts = subparsers.add_parser("batch-create-accounts", help="批量创建账号")
    batch_create_accounts.add_argument(
        "--file",
        required=True,
        help="JSON 文件路径。内容可以是 accounts 数组，或 {\"accounts\": [...]}",
    )
    batch_create_accounts.set_defaults(handler=handle_batch_create_accounts)

    batch_create_proxies = subparsers.add_parser("batch-create-proxies", help="批量创建代理")
    batch_create_proxies.add_argument(
        "--file",
        required=True,
        help="JSON 文件路径。内容可以是 proxies 数组，或 {\"proxies\": [...]}",
    )
    batch_create_proxies.set_defaults(handler=handle_batch_create_proxies)

    import_accounts = subparsers.add_parser("import-accounts-data", help="导入账号/代理打包数据")
    import_accounts.add_argument(
        "--file",
        required=True,
        help="DataPayload JSON 文件路径，支持直接传 data 对象，或 {\"data\": {...}}",
    )
    import_accounts.add_argument(
        "--bind-default-group",
        dest="skip_default_group_bind",
        action="store_false",
        help="允许后端为未显式分组的账号绑定默认分组",
    )
    import_accounts.add_argument(
        "--skip-default-group-bind",
        dest="skip_default_group_bind",
        action="store_true",
        help="跳过默认分组绑定（默认行为）",
    )
    import_accounts.set_defaults(handler=handle_import_accounts_data, skip_default_group_bind=True)

    import_proxies = subparsers.add_parser("import-proxies-data", help="导入代理打包数据")
    import_proxies.add_argument(
        "--file",
        required=True,
        help="DataPayload JSON 文件路径，支持直接传 data 对象，或 {\"data\": {...}}",
    )
    import_proxies.set_defaults(handler=handle_import_proxies_data)

    bulk_update = subparsers.add_parser("bulk-update-accounts", help="批量编辑账号")
    bulk_update.add_argument(
        "--file",
        required=True,
        help="JSON 文件路径，内容应直接对应 /admin/accounts/bulk-update 请求体",
    )
    bulk_update.set_defaults(handler=handle_bulk_update_accounts)

    bulk_update_all = subparsers.add_parser("bulk-update-all-accounts", help="按筛选条件批量编辑账号（自动分页+分批调用 /admin/accounts/bulk-update）")
    bulk_update_all.add_argument(
        "--updates-file",
        required=True,
        help="JSON 文件路径，仅包含需要修改的字段（不要包含 account_ids）",
    )
    bulk_update_all.add_argument("--account-ids", nargs="+", type=int, help="直接指定账号 ID 列表；提供后将跳过筛选扫描")
    bulk_update_all.add_argument("--platform", help="平台筛选，如 openai/anthropic/gemini/antigravity")
    bulk_update_all.add_argument("--account-type", help="账号类型筛选，如 oauth/apikey/setup-token/upstream/bedrock")
    bulk_update_all.add_argument("--account-status", help="账号状态筛选，如 active/inactive/error")
    bulk_update_all.add_argument("--search", help="服务端搜索关键词（会透传到 /admin/accounts?search=）")
    bulk_update_all.add_argument("--name-contains", help="客户端二次筛选：账号名包含该关键词（不区分大小写）")
    bulk_update_all.add_argument("--group-ids", nargs="+", type=int, help="客户端二次筛选：命中任一 group_id")
    bulk_update_all.add_argument("--ungrouped-only", action="store_true", help="仅筛选当前未绑定任何分组的账号")
    bulk_update_all.add_argument("--max-accounts", type=int, help="最多处理前 N 个匹配账号")
    bulk_update_all.add_argument("--page-size", type=int, default=100, help="扫描账号分页大小，默认 100")
    bulk_update_all.add_argument("--batch-size", type=int, default=100, help="提交 bulk-update 的单批账号数，默认 100")
    bulk_update_all.add_argument("--dry-run", action="store_true", help="只展示匹配账号，不实际更新")
    bulk_update_all.set_defaults(handler=handle_bulk_update_all_accounts)

    api_request = subparsers.add_parser("api-request", help="通用 API 请求（可覆盖管理端可修改范围内的任意接口）")
    api_request.add_argument("--method", default="GET", choices=["GET", "POST", "PUT", "PATCH", "DELETE"], help="HTTP 方法")
    api_request.add_argument("--path", required=True, help="API 路径，如 /admin/accounts/1 或 /admin/accounts?page=1&page_size=50")
    api_request.add_argument("--file", help="请求体 JSON 文件（GET/DELETE 通常可不传）")
    api_request.add_argument("--no-auth", action="store_true", help="不附带认证头，适用于 /settings/public 等公开接口")
    api_request.set_defaults(handler=handle_api_request)

    gui_login = subparsers.add_parser("gui-login", help="打开可视化管理控制台（URL + Admin API Key）")
    gui_login.set_defaults(handler=None)

    login = subparsers.add_parser("login", help="用邮箱/密码登录并输出 token")
    login.set_defaults(handler=handle_login)

    return parser


def main() -> int:
    if len(sys.argv) == 1:
        try:
            run_admin_gui(
                default_base_url=DEFAULT_BASE_URL,
                default_admin_api_key=os.environ.get(ENV_ADMIN_API_KEY, ""),
                default_timeout=30.0,
                default_insecure=False,
            )
            return 0
        except KeyboardInterrupt:
            sys.stderr.write("已中断\n")
            return 130
        except CLIError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1

    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "gui-login":
            run_admin_gui(
                default_base_url=args.base_url,
                default_admin_api_key=os.environ.get(ENV_ADMIN_API_KEY, ""),
                default_timeout=args.timeout,
                default_insecure=args.insecure,
            )
            return 0

        auth = resolve_auth(args)
        client = AdminAPIClient(
            args.base_url,
            admin_api_key=auth.admin_api_key,
            bearer_token=auth.bearer_token,
            login=auth.login,
            timeout=args.timeout,
            insecure=args.insecure,
            user_agent=args.user_agent,
        )
        if auth.refresh_token:
            client.refresh_token = auth.refresh_token
        result: CommandResult = args.handler(client, args)
        write_json(result.payload)
        return 1 if result.failed else 0
    except KeyboardInterrupt:
        sys.stderr.write("已中断\n")
        return 130
    except CLIError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class ChatGPTRegisterAdapterError(RuntimeError):
    pass


class ChatGPTRegisterCancelled(ChatGPTRegisterAdapterError):
    pass


COMMON_CONFIG_KEYS = (
    "mail_provider",
    "blocked_domains",
    "mail_tm_base_url",
    "mail_tm_domain_index",
    "mail_tm_password",
    "temp_mail_base_url",
    "temp_mail_admin_auth",
    "temp_mail_custom_auth",
    "temp_mail_domain",
    "temp_mail_domains",
    "temp_mail_domain_index",
    "temp_mail_domain_state_file",
    "temp_mail_enable_prefix",
    "temp_mail_name_length",
    "temp_mail_name_prefix",
    "temp_mail_fetch_limit",
    "proxy",
    "proxy_list",
    "proxy_rotation",
    "register_strategy",
    "codex_entry_url",
    "protocol_trace",
    "protocol_trace_dir",
    "total_accounts",
    "max_workers",
    "max_retries",
    "retry_delay",
    "network_retry_count",
    "network_retry_delay",
    "output_file",
    "enable_oauth",
    "oauth_required",
    "oauth_issuer",
    "oauth_client_id",
    "oauth_redirect_uri",
    "ak_file",
    "rk_file",
    "token_json_dir",
    "sub2api_base_url",
    "sub2api_admin_api_key",
    "sub2api_group_name",
)


def find_default_chatgpt_register_root() -> Path | None:
    candidates = [
        Path(r"E:\WorkSpace\sub2api\chatgpt_register"),
        Path(__file__).resolve().parents[2] / "chatgpt_register",
        Path(sys_executable_parent()) / "chatgpt_register",
    ]
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if (candidate / "chatgpt_register.py").is_file() and (candidate / "config.json").is_file():
            return candidate
    return None


def sys_executable_parent() -> Path:
    import sys

    return Path(sys.executable).resolve().parent


def normalize_chatgpt_register_root(root: str | Path) -> Path:
    value = str(root or "").strip()
    if not value:
        default_root = find_default_chatgpt_register_root()
        if default_root is None:
            raise ChatGPTRegisterAdapterError("未找到 chatgpt_register 目录，请先在页面里指定“注册机目录”")
        return default_root
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    script_path = path / "chatgpt_register.py"
    config_path = path / "config.json"
    if not script_path.is_file():
        raise ChatGPTRegisterAdapterError(f"注册机目录无效，缺少脚本：{script_path}")
    if not config_path.is_file():
        raise ChatGPTRegisterAdapterError(f"注册机目录无效，缺少配置：{config_path}")
    return path


def load_chatgpt_register_config(root: str | Path) -> dict[str, Any]:
    register_root = normalize_chatgpt_register_root(root)
    config_path = register_root / "config.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ChatGPTRegisterAdapterError(f"注册机配置不是合法 JSON: {config_path}: {exc}") from exc
    except OSError as exc:
        raise ChatGPTRegisterAdapterError(f"读取注册机配置失败: {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ChatGPTRegisterAdapterError(f"注册机配置格式错误: {config_path}")
    return payload


def save_chatgpt_register_config(root: str | Path, updates: dict[str, Any]) -> Path:
    register_root = normalize_chatgpt_register_root(root)
    config_path = register_root / "config.json"
    current = load_chatgpt_register_config(register_root)
    for key, value in updates.items():
        if key not in COMMON_CONFIG_KEYS:
            continue
        current[key] = value
    config_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


def build_common_register_updates(values: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for key in COMMON_CONFIG_KEYS:
        if key in values:
            updates[key] = values[key]
    return updates


@dataclass
class _GuiRegisterStatus:
    total_override: int
    progress_callback: Callable[[int, int, str], None]
    cancel_event: threading.Event | None = None
    log_callback: Callable[[str], None] | None = None

    total: int = 0
    done: int = 0
    success: int = 0
    fail: int = 0
    current_idx: Any = None
    current_tag: Any = None
    current_attempt: Any = None
    current_stage: str = "初始化"
    current_msg: str = ""

    def _check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ChatGPTRegisterCancelled("注册任务已停止")

    def _safe_total(self) -> int:
        value = self.total or self.total_override or 1
        return max(int(value), 1)

    def _safe_current(self) -> int:
        return max(0, min(int(self.done or 0), self._safe_total()))

    def _emit(self) -> None:
        self._check_cancel()
        account_label = str(self.current_tag or self.current_idx or "-").strip()
        attempt_label = str(self.current_attempt or "-").strip()
        message = (
            f"注册机：{self._safe_current()}/{self._safe_total()} "
            f"成功 {self.success} 失败 {self.fail} | 当前 {account_label} | "
            f"{self.current_stage} | 尝试 {attempt_label} | {self.current_msg or '执行中'}"
        )
        self.progress_callback(self._safe_current(), self._safe_total(), message)

    def start(self, total: int, provider_name: str, oauth_enabled: bool) -> None:
        self.total = int(total or 0)
        self.current_stage = "初始化"
        self.current_msg = f"邮箱:{provider_name or '-'} | OAuth:{'开' if oauth_enabled else '关'}"
        self._emit()

    def update(self, *, idx=None, tag=None, attempt=None, stage=None, msg=None) -> None:
        if idx is not None:
            self.current_idx = idx
        if tag is not None:
            self.current_tag = tag
        if attempt is not None:
            self.current_attempt = attempt
        if stage is not None:
            self.current_stage = str(stage)
        if msg is not None:
            self.current_msg = str(msg)
        self._emit()

    def mark_done(self, ok: bool, err: str | None = None) -> None:
        self.done += 1
        if ok:
            self.success += 1
            self.current_stage = "完成"
            if not self.current_msg:
                self.current_msg = "注册成功"
        else:
            self.fail += 1
            self.current_stage = "失败"
            if err:
                self.current_msg = str(err)
        self._emit()

    def println(self, text: str) -> None:
        if self.log_callback:
            self.log_callback(str(text))
        self._check_cancel()

    def finish(self) -> None:
        self._emit()


def _collect_buffer_lines(buffer: io.StringIO, log_callback: Callable[[str], None] | None) -> list[str]:
    text = buffer.getvalue().replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if log_callback:
        for line in lines:
            log_callback(line)
    return lines


def _load_chatgpt_register_module(register_root: Path, log_callback: Callable[[str], None] | None = None):
    script_path = register_root / "chatgpt_register.py"
    module_name = f"chatgpt_register_runtime_{int(time.time() * 1000)}_{os.getpid()}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ChatGPTRegisterAdapterError(f"无法加载注册机脚本: {script_path}")

    module = importlib.util.module_from_spec(spec)
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            spec.loader.exec_module(module)
    except Exception as exc:
        _collect_buffer_lines(stdout_buffer, log_callback)
        _collect_buffer_lines(stderr_buffer, log_callback)
        raise ChatGPTRegisterAdapterError(f"加载注册机脚本失败: {exc}") from exc

    _collect_buffer_lines(stdout_buffer, log_callback)
    _collect_buffer_lines(stderr_buffer, log_callback)
    return module


def run_chatgpt_register_job(
    *,
    register_root: str | Path,
    total_accounts: int,
    max_workers: int,
    output_file: str,
    proxy: str = "",
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[int, int, str], None],
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    root = normalize_chatgpt_register_root(register_root)
    if int(total_accounts or 0) <= 0:
        raise ChatGPTRegisterAdapterError("注册账号数量必须大于 0")
    if int(max_workers or 0) <= 0:
        raise ChatGPTRegisterAdapterError("注册并发数必须大于 0")

    config = load_chatgpt_register_config(root)
    module = _load_chatgpt_register_module(root, log_callback=log_callback)
    status_bridge = _GuiRegisterStatus(
        total_override=int(total_accounts),
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        log_callback=log_callback,
    )

    original_status = getattr(module, "_STATUS", None)
    original_curl_request = getattr(module, "_curl_request", None)
    module._STATUS = status_bridge

    if callable(original_curl_request):
        def wrapped_curl_request(*args: Any, **kwargs: Any):
            status_bridge._check_cancel()
            response = original_curl_request(*args, **kwargs)
            status_bridge._check_cancel()
            return response

        module._curl_request = wrapped_curl_request

    output_path = Path(output_file.strip() if str(output_file or "").strip() else str(config.get("output_file") or "registered_accounts.txt"))
    if not output_path.is_absolute():
        output_path = root / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    start_time = time.time()
    try:
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            module.run_batch(
                total_accounts=int(total_accounts),
                output_file=str(output_path),
                max_workers=int(max_workers),
                proxy=str(proxy or "").strip() or None,
            )
    except ChatGPTRegisterCancelled:
        raise
    except Exception as exc:
        raise ChatGPTRegisterAdapterError(f"注册执行失败: {exc}") from exc
    finally:
        module._STATUS = original_status
        if callable(original_curl_request):
            module._curl_request = original_curl_request

    log_lines = []
    log_lines.extend(_collect_buffer_lines(stdout_buffer, log_callback))
    log_lines.extend(_collect_buffer_lines(stderr_buffer, log_callback))

    result_output_lines = 0
    if output_path.is_file():
        try:
            result_output_lines = len([line for line in output_path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()])
        except OSError:
            result_output_lines = 0

    token_dir = Path(str(config.get("token_json_dir") or "codex_tokens"))
    if not token_dir.is_absolute():
        token_dir = root / token_dir

    return {
        "register_root": str(root),
        "config_path": str(root / "config.json"),
        "total_accounts": int(total_accounts),
        "max_workers": int(max_workers),
        "proxy": str(proxy or "").strip(),
        "success": int(status_bridge.success),
        "failed": int(status_bridge.fail),
        "completed": int(status_bridge.done),
        "elapsed_seconds": round(time.time() - start_time, 2),
        "output_file": str(output_path),
        "output_lines": result_output_lines,
        "token_json_dir": str(token_dir),
        "mail_provider": str(config.get("mail_provider") or ""),
        "sub2api_base_url": str(config.get("sub2api_base_url") or ""),
        "sub2api_group_name": str(config.get("sub2api_group_name") or ""),
        "log_preview": log_lines[-40:],
    }

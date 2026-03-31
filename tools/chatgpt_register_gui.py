from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from chatgpt_register_adapter import (
    ChatGPTRegisterAdapterError,
    ChatGPTRegisterCancelled,
    build_common_register_updates,
    find_default_chatgpt_register_root,
    load_chatgpt_register_config,
    normalize_chatgpt_register_root,
    run_chatgpt_register_job,
    save_chatgpt_register_config,
)


def build_chatgpt_register_tab(
    *,
    tab_register: Any,
    tk: Any,
    ttk: Any,
    ScrolledText: Any,
    action_buttons: list[Any],
    register_root_var: Any,
    base_url_var: Any,
    admin_key_var: Any,
    messagebox: Any,
    pick_directory: Callable[[Any], None],
    open_local_path: Callable[[str | Path], None],
    safe_ui_action: Callable[[Callable[[], None]], Callable[[], None]],
    run_action: Callable[[str, Callable[[Callable[[int, int, str], None]], Any]], None],
    current_cancel_event_getter: Callable[[], Any],
    parse_optional_positive_int: Callable[[str, str], int | None],
    parse_optional_nonnegative_int: Callable[[str, str], int | None],
    parse_optional_float: Callable[..., float | None],
    non_empty: Callable[[str | None], str | None],
    cli_error_cls: type[Exception],
    task_cancelled_cls: type[Exception],
) -> None:
    tab_register.columnconfigure(0, weight=1)

    register_total_var = tk.StringVar(value="1")
    register_workers_var = tk.StringVar(value="1")
    register_output_var = tk.StringVar(value="registered_accounts.txt")
    register_token_dir_var = tk.StringVar(value="codex_tokens")
    register_proxy_var = tk.StringVar()
    register_mail_provider_var = tk.StringVar(value="tempmail")
    register_enable_oauth_var = tk.BooleanVar(value=True)
    register_oauth_required_var = tk.BooleanVar(value=True)

    register_mail_tm_base_url_var = tk.StringVar(value="https://api.mail.tm")
    register_mail_tm_domain_index_var = tk.StringVar(value="0")
    register_mail_tm_password_var = tk.StringVar()

    register_temp_mail_base_url_var = tk.StringVar()
    register_temp_mail_admin_auth_var = tk.StringVar()
    register_temp_mail_custom_auth_var = tk.StringVar()
    register_temp_mail_domain_var = tk.StringVar()
    register_temp_mail_domain_index_var = tk.StringVar(value="0")
    register_temp_mail_domain_state_file_var = tk.StringVar(value="temp_mail_domain_state.json")
    register_temp_mail_enable_prefix_var = tk.BooleanVar(value=True)
    register_temp_mail_name_length_var = tk.StringVar(value="9")
    register_temp_mail_name_prefix_var = tk.StringVar()
    register_temp_mail_fetch_limit_var = tk.StringVar(value="10")

    register_proxy_rotation_var = tk.BooleanVar(value=True)
    register_strategy_var = tk.StringVar(value="protocol_full")
    register_codex_entry_url_var = tk.StringVar(value="https://chatgpt.com/codex")
    register_protocol_trace_var = tk.BooleanVar(value=True)
    register_protocol_trace_dir_var = tk.StringVar(value="protocol_traces")
    register_max_retries_var = tk.StringVar(value="3")
    register_retry_delay_var = tk.StringVar(value="2")
    register_network_retry_count_var = tk.StringVar(value="2")
    register_network_retry_delay_var = tk.StringVar(value="1.0")

    register_oauth_issuer_var = tk.StringVar(value="https://auth.openai.com")
    register_oauth_client_id_var = tk.StringVar(value="app_EMoamEEZ73f0CkXaXp7hrann")
    register_oauth_redirect_uri_var = tk.StringVar(value="http://localhost:1455/auth/callback")
    register_ak_file_var = tk.StringVar(value="ak.txt")
    register_rk_file_var = tk.StringVar(value="rk.txt")

    register_sub2api_base_url_var = tk.StringVar()
    register_sub2api_admin_api_key_var = tk.StringVar()
    register_group_name_var = tk.StringVar(value="Codex")
    register_sync_site_var = tk.BooleanVar(value=True)
    register_show_secrets_var = tk.BooleanVar(value=False)
    register_summary_var = tk.StringVar(value="先选择注册机目录，再点击“读取原配置”。")
    register_config_path_var = tk.StringVar(value="")
    register_runtime_hint_var = tk.StringVar(value="")
    register_secret_entries: list[Any] = []

    def raise_cli_error(message: str) -> None:
        raise cli_error_cls(message)

    def mask_secret(value: str) -> str:
        secret = str(value or "").strip()
        if not secret:
            return "(空)"
        if len(secret) <= 10:
            return "*" * len(secret)
        return f"{secret[:6]}...{secret[-4:]}"

    def set_register_preview_text(text_widget: Any, content: str) -> None:
        text_widget.configure(state="normal")
        text_widget.delete("1.0", tk.END)
        text_widget.insert("1.0", content.rstrip() + "\n")
        text_widget.configure(state="disabled")

    def set_multiline_text(text_widget: Any, values: list[str]) -> None:
        text_widget.delete("1.0", tk.END)
        if values:
            text_widget.insert("1.0", "\n".join(values))

    def parse_multiline_lines(text_widget: Any) -> list[str]:
        lines: list[str] = []
        seen: set[str] = set()
        for raw_line in text_widget.get("1.0", tk.END).splitlines():
            value = raw_line.strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            lines.append(value)
        return lines

    def update_secret_visibility() -> None:
        show_text = "" if register_show_secrets_var.get() else "*"
        for entry in register_secret_entries:
            entry.configure(show=show_text)

    def add_secret_entry(frame: Any, row: int, column: int, variable: Any) -> Any:
        entry = ttk.Entry(frame, textvariable=variable, show="*")
        entry.grid(row=row, column=column, sticky="ew", pady=3)
        register_secret_entries.append(entry)
        return entry

    def refresh_register_runtime_hint() -> None:
        if register_sync_site_var.get():
            base_preview = non_empty(base_url_var.get()) or "(未填写上方网站地址)"
            key_preview = mask_secret(admin_key_var.get())
            register_runtime_hint_var.set(f"运行时将临时复用上方配置：{base_preview} | Key: {key_preview}")
        else:
            base_preview = non_empty(register_sub2api_base_url_var.get()) or "(空)"
            key_preview = mask_secret(register_sub2api_admin_api_key_var.get())
            register_runtime_hint_var.set(f"运行时使用注册机页内配置：{base_preview} | Key: {key_preview}")

    ttk.Label(tab_register, text="注册机", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
    ttk.Label(
        tab_register,
        text="继续复用外部 chatgpt_register.py；这里只把更多原始 config.json 字段做成可视化表单，保存后仍由原脚本执行注册。",
        style="Hint.TLabel",
        justify="left",
        wraplength=980,
    ).grid(row=1, column=0, sticky="w", pady=(0, 8))

    path_frame = ttk.LabelFrame(tab_register, text="注册机目录", padding=8)
    path_frame.grid(row=2, column=0, sticky="ew")
    path_frame.columnconfigure(1, weight=1)
    ttk.Label(path_frame, text="项目目录").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(path_frame, textvariable=register_root_var).grid(row=0, column=1, sticky="ew", pady=3)
    browse_register_btn = ttk.Button(path_frame, text="选择目录", command=lambda: pick_directory(register_root_var))
    browse_register_btn.grid(row=0, column=2, sticky="w", padx=(8, 0), pady=3)
    action_buttons.append(browse_register_btn)
    ttk.Checkbutton(path_frame, text="显示密钥/密码", variable=register_show_secrets_var, command=update_secret_visibility).grid(row=0, column=3, sticky="w", padx=(8, 0), pady=3)
    ttk.Label(path_frame, textvariable=register_summary_var, style="Hint.TLabel", justify="left", wraplength=920).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

    runtime_frame = ttk.LabelFrame(tab_register, text="常用运行项", padding=8)
    runtime_frame.grid(row=3, column=0, sticky="ew", pady=(8, 0))
    for column in range(4):
        runtime_frame.columnconfigure(column, weight=1 if column in (1, 3) else 0)
    ttk.Label(runtime_frame, text="注册数量").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(runtime_frame, textvariable=register_total_var).grid(row=0, column=1, sticky="ew", pady=3)
    ttk.Label(runtime_frame, text="注册并发").grid(row=0, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(runtime_frame, textvariable=register_workers_var).grid(row=0, column=3, sticky="ew", pady=3)
    ttk.Label(runtime_frame, text="输出文件").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(runtime_frame, textvariable=register_output_var).grid(row=1, column=1, sticky="ew", pady=3)
    ttk.Label(runtime_frame, text="Token 目录").grid(row=1, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(runtime_frame, textvariable=register_token_dir_var).grid(row=1, column=3, sticky="ew", pady=3)
    ttk.Label(runtime_frame, text="代理").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(runtime_frame, textvariable=register_proxy_var).grid(row=2, column=1, sticky="ew", pady=3)
    ttk.Label(runtime_frame, text="邮箱来源").grid(row=2, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Combobox(runtime_frame, textvariable=register_mail_provider_var, values=["tempmail", "mailtm", "tempmail,mailtm", "mailtm,tempmail"], state="readonly").grid(row=2, column=3, sticky="ew", pady=3)
    ttk.Checkbutton(runtime_frame, text="启用 OAuth", variable=register_enable_oauth_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=3)
    ttk.Checkbutton(runtime_frame, text="强制要求拿到 OAuth Token", variable=register_oauth_required_var).grid(row=3, column=2, columnspan=2, sticky="w", pady=3)

    mail_frame = ttk.LabelFrame(tab_register, text="邮箱与域名", padding=8)
    mail_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
    for column in range(4):
        mail_frame.columnconfigure(column, weight=1 if column in (1, 3) else 0)
    ttk.Label(mail_frame, text="Temp-Mail 地址").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(mail_frame, textvariable=register_temp_mail_base_url_var).grid(row=0, column=1, sticky="ew", pady=3)
    ttk.Label(mail_frame, text="Temp-Mail 管理密码").grid(row=0, column=2, sticky="w", padx=(8, 6), pady=3)
    add_secret_entry(mail_frame, 0, 3, register_temp_mail_admin_auth_var)
    ttk.Label(mail_frame, text="自定义鉴权").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
    add_secret_entry(mail_frame, 1, 1, register_temp_mail_custom_auth_var)
    ttk.Label(mail_frame, text="固定域名").grid(row=1, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(mail_frame, textvariable=register_temp_mail_domain_var).grid(row=1, column=3, sticky="ew", pady=3)
    ttk.Label(mail_frame, text="域名轮换索引").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(mail_frame, textvariable=register_temp_mail_domain_index_var).grid(row=2, column=1, sticky="ew", pady=3)
    ttk.Label(mail_frame, text="域名状态文件").grid(row=2, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(mail_frame, textvariable=register_temp_mail_domain_state_file_var).grid(row=2, column=3, sticky="ew", pady=3)
    ttk.Label(mail_frame, text="名称长度").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(mail_frame, textvariable=register_temp_mail_name_length_var).grid(row=3, column=1, sticky="ew", pady=3)
    ttk.Label(mail_frame, text="名称前缀").grid(row=3, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(mail_frame, textvariable=register_temp_mail_name_prefix_var).grid(row=3, column=3, sticky="ew", pady=3)
    ttk.Label(mail_frame, text="拉取邮件数量").grid(row=4, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(mail_frame, textvariable=register_temp_mail_fetch_limit_var).grid(row=4, column=1, sticky="ew", pady=3)
    ttk.Checkbutton(mail_frame, text="Temp-Mail 自动前缀", variable=register_temp_mail_enable_prefix_var).grid(row=4, column=2, columnspan=2, sticky="w", pady=3)
    ttk.Label(mail_frame, text="Mail.tm 地址").grid(row=5, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(mail_frame, textvariable=register_mail_tm_base_url_var).grid(row=5, column=1, sticky="ew", pady=3)
    ttk.Label(mail_frame, text="Mail.tm 域名索引").grid(row=5, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(mail_frame, textvariable=register_mail_tm_domain_index_var).grid(row=5, column=3, sticky="ew", pady=3)
    ttk.Label(mail_frame, text="Mail.tm 密码").grid(row=6, column=0, sticky="w", padx=(0, 6), pady=3)
    add_secret_entry(mail_frame, 6, 1, register_mail_tm_password_var)
    ttk.Label(mail_frame, text="Temp-Mail 域名列表（每行一个）").grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 3))
    ttk.Label(mail_frame, text="屏蔽域名（每行一个）").grid(row=7, column=2, columnspan=2, sticky="w", pady=(6, 3))
    temp_mail_domains_text = ScrolledText(mail_frame, height=4, wrap="word")
    temp_mail_domains_text.grid(row=8, column=0, columnspan=2, sticky="nsew", padx=(0, 6), pady=(0, 3))
    blocked_domains_text = ScrolledText(mail_frame, height=4, wrap="word")
    blocked_domains_text.grid(row=8, column=2, columnspan=2, sticky="nsew", padx=(6, 0), pady=(0, 3))

    retry_frame = ttk.LabelFrame(tab_register, text="网络、重试与调试", padding=8)
    retry_frame.grid(row=5, column=0, sticky="ew", pady=(8, 0))
    for column in range(4):
        retry_frame.columnconfigure(column, weight=1 if column in (1, 3) else 0)
    ttk.Checkbutton(retry_frame, text="启用代理轮换", variable=register_proxy_rotation_var).grid(row=0, column=0, columnspan=2, sticky="w", pady=3)
    ttk.Checkbutton(retry_frame, text="保留协议 Trace", variable=register_protocol_trace_var).grid(row=0, column=2, columnspan=2, sticky="w", pady=3)
    ttk.Label(retry_frame, text="注册策略").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Combobox(retry_frame, textvariable=register_strategy_var, values=["protocol_full"], state="readonly").grid(row=1, column=1, sticky="ew", pady=3)
    ttk.Label(retry_frame, text="Codex 入口").grid(row=1, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(retry_frame, textvariable=register_codex_entry_url_var).grid(row=1, column=3, sticky="ew", pady=3)
    ttk.Label(retry_frame, text="最大重试次数").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(retry_frame, textvariable=register_max_retries_var).grid(row=2, column=1, sticky="ew", pady=3)
    ttk.Label(retry_frame, text="重试延迟(秒)").grid(row=2, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(retry_frame, textvariable=register_retry_delay_var).grid(row=2, column=3, sticky="ew", pady=3)
    ttk.Label(retry_frame, text="网络重试次数").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(retry_frame, textvariable=register_network_retry_count_var).grid(row=3, column=1, sticky="ew", pady=3)
    ttk.Label(retry_frame, text="网络重试延迟(秒)").grid(row=3, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(retry_frame, textvariable=register_network_retry_delay_var).grid(row=3, column=3, sticky="ew", pady=3)
    ttk.Label(retry_frame, text="协议 Trace 目录").grid(row=4, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(retry_frame, textvariable=register_protocol_trace_dir_var).grid(row=4, column=1, sticky="ew", pady=3)
    ttk.Label(retry_frame, text="代理池（每行一个）").grid(row=4, column=2, sticky="w", padx=(8, 6), pady=3)
    proxy_list_text = ScrolledText(retry_frame, height=4, wrap="word")
    proxy_list_text.grid(row=5, column=2, columnspan=2, sticky="nsew", pady=(0, 3))

    oauth_frame = ttk.LabelFrame(tab_register, text="OAuth 与本地输出", padding=8)
    oauth_frame.grid(row=6, column=0, sticky="ew", pady=(8, 0))
    for column in range(4):
        oauth_frame.columnconfigure(column, weight=1 if column in (1, 3) else 0)
    ttk.Label(oauth_frame, text="OAuth Issuer").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(oauth_frame, textvariable=register_oauth_issuer_var).grid(row=0, column=1, sticky="ew", pady=3)
    ttk.Label(oauth_frame, text="OAuth Client ID").grid(row=0, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(oauth_frame, textvariable=register_oauth_client_id_var).grid(row=0, column=3, sticky="ew", pady=3)
    ttk.Label(oauth_frame, text="OAuth Redirect URI").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(oauth_frame, textvariable=register_oauth_redirect_uri_var).grid(row=1, column=1, columnspan=3, sticky="ew", pady=3)
    ttk.Label(oauth_frame, text="AK 文件").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(oauth_frame, textvariable=register_ak_file_var).grid(row=2, column=1, sticky="ew", pady=3)
    ttk.Label(oauth_frame, text="RK 文件").grid(row=2, column=2, sticky="w", padx=(8, 6), pady=3)
    ttk.Entry(oauth_frame, textvariable=register_rk_file_var).grid(row=2, column=3, sticky="ew", pady=3)

    sync_frame = ttk.LabelFrame(tab_register, text="上传到当前站点", padding=8)
    sync_frame.grid(row=7, column=0, sticky="ew", pady=(8, 0))
    sync_frame.columnconfigure(1, weight=1)
    sync_frame.columnconfigure(3, weight=1)
    ttk.Checkbutton(sync_frame, text="开始注册时临时复用上方网站地址和管理员 API Key", variable=register_sync_site_var, command=refresh_register_runtime_hint).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
    ttk.Label(sync_frame, text="sub2api 地址").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(sync_frame, textvariable=register_sub2api_base_url_var).grid(row=1, column=1, sticky="ew", pady=3)
    ttk.Label(sync_frame, text="sub2api 管理 Key").grid(row=1, column=2, sticky="w", padx=(8, 6), pady=3)
    add_secret_entry(sync_frame, 1, 3, register_sub2api_admin_api_key_var)
    ttk.Label(sync_frame, text="目标分组").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
    ttk.Entry(sync_frame, textvariable=register_group_name_var).grid(row=2, column=1, sticky="ew", pady=3)
    ttk.Label(sync_frame, textvariable=register_runtime_hint_var, style="Hint.TLabel", justify="left", wraplength=920).grid(row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))

    action_frame = ttk.Frame(tab_register)
    action_frame.grid(row=8, column=0, sticky="ew", pady=(8, 0))
    action_frame.columnconfigure(5, weight=1)

    preview_frame = ttk.LabelFrame(tab_register, text="当前原始配置预览", padding=8)
    preview_frame.grid(row=9, column=0, sticky="ew", pady=(8, 0))
    preview_frame.columnconfigure(0, weight=1)
    ttk.Label(preview_frame, textvariable=register_config_path_var, style="Hint.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
    register_preview = ScrolledText(preview_frame, height=14, wrap="word")
    register_preview.grid(row=1, column=0, sticky="ew")
    set_register_preview_text(register_preview, "尚未读取注册机配置。")

    def refresh_register_preview(config: dict[str, Any], register_root: Path) -> None:
        config_path = register_root / "config.json"
        register_config_path_var.set(f"配置文件：{config_path}")
        temp_domains = config.get("temp_mail_domains")
        domains_text = ", ".join(str(item).strip() for item in (temp_domains or []) if str(item).strip()) if isinstance(temp_domains, list) else str(temp_domains or "").strip()
        blocked_domains = config.get("blocked_domains")
        blocked_text = ", ".join(str(item).strip() for item in (blocked_domains or []) if str(item).strip()) if isinstance(blocked_domains, list) else str(blocked_domains or "").strip()
        proxy_list = config.get("proxy_list")
        proxy_count = len(proxy_list) if isinstance(proxy_list, list) else 0
        preview_lines = [
            f"register_root: {register_root}",
            f"mail_provider: {config.get('mail_provider', '')}",
            f"temp_mail_base_url: {config.get('temp_mail_base_url', '')}",
            f"temp_mail_domain: {config.get('temp_mail_domain', '')}",
            f"temp_mail_domains: {domains_text or '(空)'}",
            f"blocked_domains: {blocked_text or '(空)'}",
            f"mail_tm_base_url: {config.get('mail_tm_base_url', '')}",
            f"mail_tm_domain_index: {config.get('mail_tm_domain_index', '')}",
            f"output_file: {config.get('output_file', '')}",
            f"token_json_dir: {config.get('token_json_dir', '')}",
            f"proxy_rotation: {bool(config.get('proxy_rotation'))} | proxy_list_count: {proxy_count}",
            f"enable_oauth: {bool(config.get('enable_oauth'))} | oauth_required: {bool(config.get('oauth_required'))}",
            f"oauth_redirect_uri: {config.get('oauth_redirect_uri', '')}",
            f"max_retries: {config.get('max_retries', '')} | retry_delay: {config.get('retry_delay', '')}",
            f"network_retry_count: {config.get('network_retry_count', '')} | network_retry_delay: {config.get('network_retry_delay', '')}",
            f"sub2api_base_url: {config.get('sub2api_base_url', '')}",
            f"sub2api_admin_api_key: {mask_secret(str(config.get('sub2api_admin_api_key') or ''))}",
            f"sub2api_group_name: {config.get('sub2api_group_name', '')}",
            f"protocol_trace: {bool(config.get('protocol_trace'))} | protocol_trace_dir: {config.get('protocol_trace_dir', '')}",
        ]
        set_register_preview_text(register_preview, "\n".join(preview_lines))
        register_summary_var.set(
            f"已读取：{register_root} | 邮箱源 {config.get('mail_provider', '') or '-'} | "
            f"OAuth {'开' if bool(config.get('enable_oauth')) else '关'} | "
            f"Temp-Mail 域名 {len(parse_multiline_lines(temp_mail_domains_text))} 个 | "
            f"输出 {config.get('output_file', '') or 'registered_accounts.txt'}"
        )
        refresh_register_runtime_hint()

    def load_register_config_to_form(*, show_message: bool = False, silent: bool = False) -> Path | None:
        if not register_root_var.get().strip():
            detected_root = find_default_chatgpt_register_root()
            if detected_root is not None:
                register_root_var.set(str(detected_root))
        if not register_root_var.get().strip():
            if silent:
                return None
            raise_cli_error("请先选择 chatgpt_register 项目目录")
        try:
            register_root = normalize_chatgpt_register_root(register_root_var.get())
            config = load_chatgpt_register_config(register_root)
        except ChatGPTRegisterAdapterError as exc:
            register_summary_var.set(str(exc))
            register_config_path_var.set("")
            set_register_preview_text(register_preview, str(exc))
            if silent:
                return None
            raise_cli_error(str(exc))

        register_root_var.set(str(register_root))
        register_total_var.set(str(config.get("total_accounts") or "1"))
        register_workers_var.set(str(config.get("max_workers") or "1"))
        register_output_var.set(str(config.get("output_file") or "registered_accounts.txt"))
        register_token_dir_var.set(str(config.get("token_json_dir") or "codex_tokens"))
        register_proxy_var.set(str(config.get("proxy") or ""))
        register_mail_provider_var.set(str(config.get("mail_provider") or "tempmail"))
        register_enable_oauth_var.set(bool(config.get("enable_oauth")))
        register_oauth_required_var.set(bool(config.get("oauth_required")))
        register_mail_tm_base_url_var.set(str(config.get("mail_tm_base_url") or "https://api.mail.tm"))
        register_mail_tm_domain_index_var.set(str(config.get("mail_tm_domain_index") if config.get("mail_tm_domain_index") is not None else "0"))
        register_mail_tm_password_var.set(str(config.get("mail_tm_password") or ""))
        register_temp_mail_base_url_var.set(str(config.get("temp_mail_base_url") or ""))
        register_temp_mail_admin_auth_var.set(str(config.get("temp_mail_admin_auth") or ""))
        register_temp_mail_custom_auth_var.set(str(config.get("temp_mail_custom_auth") or ""))
        register_temp_mail_domain_var.set(str(config.get("temp_mail_domain") or ""))
        register_temp_mail_domain_index_var.set(str(config.get("temp_mail_domain_index") if config.get("temp_mail_domain_index") is not None else "0"))
        register_temp_mail_domain_state_file_var.set(str(config.get("temp_mail_domain_state_file") or "temp_mail_domain_state.json"))
        register_temp_mail_enable_prefix_var.set(bool(config.get("temp_mail_enable_prefix", True)))
        register_temp_mail_name_length_var.set(str(config.get("temp_mail_name_length") or "9"))
        register_temp_mail_name_prefix_var.set(str(config.get("temp_mail_name_prefix") or ""))
        register_temp_mail_fetch_limit_var.set(str(config.get("temp_mail_fetch_limit") or "10"))
        register_proxy_rotation_var.set(bool(config.get("proxy_rotation", True)))
        register_strategy_var.set(str(config.get("register_strategy") or "protocol_full"))
        register_codex_entry_url_var.set(str(config.get("codex_entry_url") or "https://chatgpt.com/codex"))
        register_protocol_trace_var.set(bool(config.get("protocol_trace", True)))
        register_protocol_trace_dir_var.set(str(config.get("protocol_trace_dir") or "protocol_traces"))
        register_max_retries_var.set(str(config.get("max_retries") if config.get("max_retries") is not None else "3"))
        register_retry_delay_var.set(str(config.get("retry_delay") if config.get("retry_delay") is not None else "2"))
        register_network_retry_count_var.set(str(config.get("network_retry_count") if config.get("network_retry_count") is not None else "2"))
        register_network_retry_delay_var.set(str(config.get("network_retry_delay") if config.get("network_retry_delay") is not None else "1.0"))
        register_oauth_issuer_var.set(str(config.get("oauth_issuer") or "https://auth.openai.com"))
        register_oauth_client_id_var.set(str(config.get("oauth_client_id") or "app_EMoamEEZ73f0CkXaXp7hrann"))
        register_oauth_redirect_uri_var.set(str(config.get("oauth_redirect_uri") or "http://localhost:1455/auth/callback"))
        register_ak_file_var.set(str(config.get("ak_file") or "ak.txt"))
        register_rk_file_var.set(str(config.get("rk_file") or "rk.txt"))
        register_sub2api_base_url_var.set(str(config.get("sub2api_base_url") or ""))
        register_sub2api_admin_api_key_var.set(str(config.get("sub2api_admin_api_key") or ""))
        register_group_name_var.set(str(config.get("sub2api_group_name") or "Codex"))

        set_multiline_text(temp_mail_domains_text, [str(item).strip() for item in (config.get("temp_mail_domains") or []) if str(item).strip()])
        set_multiline_text(blocked_domains_text, [str(item).strip() for item in (config.get("blocked_domains") or []) if str(item).strip()])
        set_multiline_text(proxy_list_text, [str(item).strip() for item in (config.get("proxy_list") or []) if str(item).strip()])
        refresh_register_preview(config, register_root)
        update_secret_visibility()
        if show_message:
            messagebox.showinfo("读取完成", f"已读取：\n{register_root / 'config.json'}")
        return register_root

    def collect_register_updates(*, use_site_override: bool) -> tuple[Path, dict[str, Any]]:
        try:
            register_root = normalize_chatgpt_register_root(register_root_var.get())
        except ChatGPTRegisterAdapterError as exc:
            raise_cli_error(str(exc))

        total_accounts = parse_optional_positive_int(register_total_var.get(), "注册数量") or 1
        max_workers = parse_optional_positive_int(register_workers_var.get(), "注册并发数") or 1
        temp_mail_name_length = parse_optional_positive_int(register_temp_mail_name_length_var.get(), "名称长度") or 9
        temp_mail_fetch_limit = parse_optional_positive_int(register_temp_mail_fetch_limit_var.get(), "拉取邮件数量") or 10
        temp_mail_domain_index = parse_optional_nonnegative_int(register_temp_mail_domain_index_var.get(), "域名轮换索引")
        mail_tm_domain_index = parse_optional_nonnegative_int(register_mail_tm_domain_index_var.get(), "Mail.tm 域名索引")
        max_retries = parse_optional_nonnegative_int(register_max_retries_var.get(), "最大重试次数")
        network_retry_count = parse_optional_nonnegative_int(register_network_retry_count_var.get(), "网络重试次数")
        retry_delay = parse_optional_float(register_retry_delay_var.get(), "重试延迟", min_value=0.0)
        network_retry_delay = parse_optional_float(register_network_retry_delay_var.get(), "网络重试延迟", min_value=0.0)

        updates = build_common_register_updates(
            {
                "total_accounts": total_accounts,
                "max_workers": max_workers,
                "output_file": register_output_var.get().strip() or "registered_accounts.txt",
                "token_json_dir": register_token_dir_var.get().strip() or "codex_tokens",
                "proxy": register_proxy_var.get().strip(),
                "mail_provider": register_mail_provider_var.get().strip() or "tempmail",
                "enable_oauth": bool(register_enable_oauth_var.get()),
                "oauth_required": bool(register_oauth_required_var.get()),
                "mail_tm_base_url": register_mail_tm_base_url_var.get().strip() or "https://api.mail.tm",
                "mail_tm_domain_index": 0 if mail_tm_domain_index is None else mail_tm_domain_index,
                "mail_tm_password": register_mail_tm_password_var.get().strip(),
                "temp_mail_base_url": register_temp_mail_base_url_var.get().strip(),
                "temp_mail_admin_auth": register_temp_mail_admin_auth_var.get().strip(),
                "temp_mail_custom_auth": register_temp_mail_custom_auth_var.get().strip(),
                "temp_mail_domain": register_temp_mail_domain_var.get().strip(),
                "temp_mail_domains": parse_multiline_lines(temp_mail_domains_text),
                "temp_mail_domain_index": 0 if temp_mail_domain_index is None else temp_mail_domain_index,
                "temp_mail_domain_state_file": register_temp_mail_domain_state_file_var.get().strip() or "temp_mail_domain_state.json",
                "temp_mail_enable_prefix": bool(register_temp_mail_enable_prefix_var.get()),
                "temp_mail_name_length": temp_mail_name_length,
                "temp_mail_name_prefix": register_temp_mail_name_prefix_var.get().strip(),
                "temp_mail_fetch_limit": temp_mail_fetch_limit,
                "blocked_domains": parse_multiline_lines(blocked_domains_text),
                "proxy_list": parse_multiline_lines(proxy_list_text),
                "proxy_rotation": bool(register_proxy_rotation_var.get()),
                "register_strategy": register_strategy_var.get().strip() or "protocol_full",
                "codex_entry_url": register_codex_entry_url_var.get().strip() or "https://chatgpt.com/codex",
                "protocol_trace": bool(register_protocol_trace_var.get()),
                "protocol_trace_dir": register_protocol_trace_dir_var.get().strip() or "protocol_traces",
                "max_retries": 0 if max_retries is None else max_retries,
                "retry_delay": 0.0 if retry_delay is None else retry_delay,
                "network_retry_count": 0 if network_retry_count is None else network_retry_count,
                "network_retry_delay": 0.0 if network_retry_delay is None else network_retry_delay,
                "oauth_issuer": register_oauth_issuer_var.get().strip() or "https://auth.openai.com",
                "oauth_client_id": register_oauth_client_id_var.get().strip() or "app_EMoamEEZ73f0CkXaXp7hrann",
                "oauth_redirect_uri": register_oauth_redirect_uri_var.get().strip() or "http://localhost:1455/auth/callback",
                "ak_file": register_ak_file_var.get().strip() or "ak.txt",
                "rk_file": register_rk_file_var.get().strip() or "rk.txt",
                "sub2api_base_url": register_sub2api_base_url_var.get().strip(),
                "sub2api_admin_api_key": register_sub2api_admin_api_key_var.get().strip(),
                "sub2api_group_name": register_group_name_var.get().strip() or "Codex",
            }
        )
        if use_site_override and register_sync_site_var.get():
            base_url = non_empty(base_url_var.get())
            admin_key = non_empty(admin_key_var.get())
            if base_url:
                updates["sub2api_base_url"] = base_url
            if admin_key:
                updates["sub2api_admin_api_key"] = admin_key
        return register_root, updates

    def save_register_config_action(*, show_message: bool) -> Path:
        register_root, updates = collect_register_updates(use_site_override=False)
        config_path = save_chatgpt_register_config(register_root, updates)
        latest_config = load_chatgpt_register_config(register_root)
        refresh_register_preview(latest_config, register_root)
        if show_message:
            messagebox.showinfo("保存成功", f"已写入：\n{config_path}")
        return config_path

    def resolve_register_path(raw_value: str, default_name: str) -> Path:
        register_root = normalize_chatgpt_register_root(register_root_var.get())
        target = Path(raw_value.strip() or default_name)
        if not target.is_absolute():
            target = register_root / target
        return target

    def open_register_output_dir() -> None:
        target = resolve_register_path(register_output_var.get(), "registered_accounts.txt").parent
        target.mkdir(parents=True, exist_ok=True)
        open_local_path(target)

    def open_register_token_dir() -> None:
        target = resolve_register_path(register_token_dir_var.get(), "codex_tokens")
        target.mkdir(parents=True, exist_ok=True)
        open_local_path(target)

    def run_register_action(progress_callback: Callable[[int, int, str], None]) -> Any:
        register_root, manual_updates = collect_register_updates(use_site_override=False)
        _, effective_updates = collect_register_updates(use_site_override=True)
        save_chatgpt_register_config(register_root, effective_updates)
        try:
            result = run_chatgpt_register_job(
                register_root=register_root,
                total_accounts=parse_optional_positive_int(register_total_var.get(), "注册数量") or 1,
                max_workers=parse_optional_positive_int(register_workers_var.get(), "注册并发数") or 1,
                output_file=register_output_var.get().strip() or "registered_accounts.txt",
                proxy=register_proxy_var.get().strip(),
                cancel_event=current_cancel_event_getter(),
                progress_callback=progress_callback,
            )
        except ChatGPTRegisterCancelled as exc:
            raise task_cancelled_cls(str(exc)) from exc
        finally:
            if register_sync_site_var.get():
                save_chatgpt_register_config(register_root, manual_updates)
                refresh_register_preview(load_chatgpt_register_config(register_root), register_root)
        result["config_saved_to"] = str(register_root / "config.json")
        result["site_sync_enabled"] = bool(register_sync_site_var.get())
        result["site_base_url"] = base_url_var.get().strip()
        result["sub2api_group_name"] = register_group_name_var.get().strip() or "Codex"
        result["temp_mail_domain_count"] = len(parse_multiline_lines(temp_mail_domains_text))
        result["blocked_domain_count"] = len(parse_multiline_lines(blocked_domains_text))
        result["proxy_pool_count"] = len(parse_multiline_lines(proxy_list_text))
        return result

    load_register_btn = ttk.Button(action_frame, text="读取原配置", command=safe_ui_action(lambda: load_register_config_to_form(show_message=True)))
    load_register_btn.grid(row=0, column=0, sticky="w")
    action_buttons.append(load_register_btn)
    save_register_btn = ttk.Button(action_frame, text="保存到原配置", command=safe_ui_action(lambda: save_register_config_action(show_message=True)))
    save_register_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))
    action_buttons.append(save_register_btn)
    run_register_btn = ttk.Button(action_frame, text="开始注册", command=lambda: run_action("注册机", run_register_action, determinate=True))
    run_register_btn.grid(row=0, column=2, sticky="w", padx=(8, 0))
    action_buttons.append(run_register_btn)
    open_config_btn = ttk.Button(action_frame, text="打开 config.json", command=safe_ui_action(lambda: open_local_path(resolve_register_path("config.json", "config.json"))))
    open_config_btn.grid(row=0, column=3, sticky="w", padx=(8, 0))
    action_buttons.append(open_config_btn)
    open_tokens_btn = ttk.Button(action_frame, text="打开 Token 目录", command=safe_ui_action(open_register_token_dir))
    open_tokens_btn.grid(row=0, column=4, sticky="w", padx=(8, 0))
    action_buttons.append(open_tokens_btn)
    open_output_btn = ttk.Button(action_frame, text="打开输出目录", command=safe_ui_action(open_register_output_dir))
    open_output_btn.grid(row=0, column=5, sticky="w", padx=(8, 0))
    action_buttons.append(open_output_btn)

    base_url_var.trace_add("write", lambda *_args: refresh_register_runtime_hint())
    admin_key_var.trace_add("write", lambda *_args: refresh_register_runtime_hint())
    register_sync_site_var.trace_add("write", lambda *_args: refresh_register_runtime_hint())
    register_sub2api_base_url_var.trace_add("write", lambda *_args: refresh_register_runtime_hint())
    register_sub2api_admin_api_key_var.trace_add("write", lambda *_args: refresh_register_runtime_hint())

    load_register_config_to_form(silent=True)
    update_secret_visibility()
    refresh_register_runtime_hint()

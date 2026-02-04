"""Flet GUI 应用"""

import flet as ft
from datetime import datetime
from typing import Optional
import threading
from pathlib import Path

import httpx

from .settings import (
    AppSettings,
    load_settings,
    save_settings,
    set_auto_start,
    get_auto_start,
    import_from_iflow_cli,
)
from .server import ServerManager, ServerState
from .keys_store import (
    add_upstream_account,
    ensure_opencode_route,
    generate_client_key,
    load_keys_config,
    save_keys_config,
)
from .routing import KeyRoutingConfig
from .opencode import discover_config_paths, ensure_iflow_provider
from .model_catalog import get_known_models


class IFlow2ApiApp:
    """iflow2api GUI 应用"""

    def __init__(self, page: ft.Page):
        self.page = page
        self.settings = load_settings()

        # 本地 API Key（给 OpenCode/客户端用，不是上游 iFlow key）
        if not self.settings.client_api_key:
            self.settings.client_api_key = generate_client_key()
            save_settings(self.settings)

        # 多账号路由配置（~/.iflow2api/keys.json）
        try:
            self.routing: KeyRoutingConfig = load_keys_config()
        except Exception:
            self.routing = KeyRoutingConfig()

        # 设置 pubsub 用于线程安全的 UI 更新
        self.page.pubsub.subscribe(self._on_pubsub_message)

        self.server = ServerManager(
            on_state_change=self._on_server_state_change_threadsafe
        )

        # UI 组件
        self.status_icon: Optional[ft.Icon] = None
        self.status_text: Optional[ft.Text] = None
        self.host_field: Optional[ft.TextField] = None
        self.port_field: Optional[ft.TextField] = None
        # 单账号模式（可选）
        self.api_key_field: Optional[ft.TextField] = None
        self.base_url_field: Optional[ft.TextField] = None
        self.auto_start_checkbox: Optional[ft.Checkbox] = None
        self.start_minimized_checkbox: Optional[ft.Checkbox] = None
        self.auto_run_checkbox: Optional[ft.Checkbox] = None
        self.close_to_background_checkbox: Optional[ft.Checkbox] = None

        # 多账号模式 UI
        self.client_key_field: Optional[ft.TextField] = None
        self.strategy_dropdown: Optional[ft.Dropdown] = None
        self.accounts_table: Optional[ft.DataTable] = None
        self.res_failure_threshold: Optional[ft.TextField] = None
        self.res_cool_down: Optional[ft.TextField] = None
        self.res_retry_attempts: Optional[ft.TextField] = None
        self.res_retry_backoff: Optional[ft.TextField] = None

        # OpenCode 集成 UI
        self.opencode_path_dropdown: Optional[ft.Dropdown] = None
        self.opencode_provider_field: Optional[ft.TextField] = None
        self.opencode_set_default_checkbox: Optional[ft.Checkbox] = None
        self.opencode_default_model_dropdown: Optional[ft.Dropdown] = None
        self.opencode_set_small_checkbox: Optional[ft.Checkbox] = None
        self.opencode_small_model_dropdown: Optional[ft.Dropdown] = None

        self.start_btn: Optional[ft.Button] = None
        self.stop_btn: Optional[ft.Button] = None
        self.log_list: Optional[ft.ListView] = None

        self._setup_page()
        self._build_ui()

        # 启动时自动运行服务
        if self.settings.auto_run_server:
            self._start_server(None)

        # 启动时最小化
        if self.settings.start_minimized:
            self.page.window.minimized = True

    def _setup_page(self):
        """设置页面"""
        self.page.title = "iflow2api"
        self.page.window.width = 500
        self.page.window.height = 980
        self.page.window.resizable = True
        self.page.window.min_width = 400
        self.page.window.min_height = 500
        self.page.padding = 20

        # 窗口关闭事件
        self.page.window.on_event = self._on_window_event

    def _on_window_event(self, e):
        """窗口事件处理"""
        if e.data == "close":
            if self.settings.close_to_background and self.server.state == ServerState.RUNNING:
                # 后台运行（最小化即可）
                self.page.window.minimized = True
                try:
                    self.page.open(
                        ft.SnackBar(
                            content=ft.Text("已最小化到后台运行（服务仍在运行）"),
                            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        )
                    )
                except Exception:
                    pass
                return

            # 停止服务并退出
            self.server.stop()
            self.page.window.destroy()

    def _build_ui(self):
        """构建 UI"""
        # 状态栏
        self.status_icon = ft.Icon(ft.Icons.CIRCLE, color=ft.Colors.GREY, size=16)
        self.status_text = ft.Text("服务未运行", size=14)

        status_row = ft.Container(
            content=ft.Row([self.status_icon, self.status_text]),
            padding=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            border_radius=8,
        )

        # 服务器配置
        self.host_field = ft.TextField(
            label="监听地址",
            value=self.settings.host,
            hint_text="0.0.0.0",
            expand=True,
        )
        self.port_field = ft.TextField(
            label="监听端口",
            value=str(self.settings.port),
            hint_text="8000",
            keyboard_type=ft.KeyboardType.NUMBER,
            width=120,
        )

        server_config = ft.Container(
            content=ft.Column(
                [
                    ft.Text("服务器配置", weight=ft.FontWeight.BOLD),
                    ft.Row([self.host_field, self.port_field]),
                ]
            ),
            padding=15,
            border=ft.Border.all(1, ft.Colors.OUTLINE),
            border_radius=8,
        )

        # 配置区：账号池（推荐）+ 单账号（兼容）
        config_tabs = ft.Tabs(
            tabs=[
                ft.Tab(text="账号池 (推荐)", content=self._build_pool_config()),
                ft.Tab(text="单账号", content=self._build_single_config()),
            ]
        )

        # 应用设置
        self.auto_start_checkbox = ft.Checkbox(
            label="开机自启动",
            value=get_auto_start(),
            on_change=self._on_auto_start_change,
        )
        self.start_minimized_checkbox = ft.Checkbox(
            label="启动时最小化",
            value=self.settings.start_minimized,
            on_change=self._on_basic_settings_change,
        )
        self.auto_run_checkbox = ft.Checkbox(
            label="启动时自动运行服务",
            value=self.settings.auto_run_server,
            on_change=self._on_basic_settings_change,
        )
        self.close_to_background_checkbox = ft.Checkbox(
            label="点关闭按钮时后台运行（不退出）",
            value=self.settings.close_to_background,
            on_change=self._on_basic_settings_change,
        )

        app_settings = ft.Container(
            content=ft.Column(
                [
                    ft.Text("应用设置", weight=ft.FontWeight.BOLD),
                    self.auto_start_checkbox,
                    self.start_minimized_checkbox,
                    self.auto_run_checkbox,
                    self.close_to_background_checkbox,
                ]
            ),
            padding=15,
            border=ft.Border.all(1, ft.Colors.OUTLINE),
            border_radius=8,
        )

        # 操作按钮
        self.start_btn = ft.Button(
            "启动服务",
            icon=ft.Icons.PLAY_ARROW,
            on_click=self._start_server,
            style=ft.ButtonStyle(bgcolor=ft.Colors.GREEN, color=ft.Colors.WHITE),
        )
        self.stop_btn = ft.Button(
            "停止服务",
            icon=ft.Icons.STOP,
            on_click=self._stop_server,
            disabled=True,
            style=ft.ButtonStyle(bgcolor=ft.Colors.RED, color=ft.Colors.WHITE),
        )
        save_btn = ft.Button(
            "保存配置",
            icon=ft.Icons.SAVE,
            on_click=self._save_settings,
        )

        buttons_row = ft.Row(
            [self.start_btn, self.stop_btn, save_btn],
            alignment=ft.MainAxisAlignment.CENTER,
        )

        # 日志区域
        self.log_list = ft.ListView(
            expand=True,
            spacing=2,
            auto_scroll=True,
        )

        log_container = ft.Container(
            content=ft.Column(
                [
                    ft.Text("日志", weight=ft.FontWeight.BOLD),
                    ft.Container(
                        content=self.log_list,
                        height=150,
                        border=ft.Border.all(1, ft.Colors.OUTLINE),
                        border_radius=8,
                        padding=10,
                    ),
                ]
            ),
        )

        # 组装页面
        self.page.add(
            ft.Column(
                [
                    status_row,
                    server_config,
                    config_tabs,
                    app_settings,
                    buttons_row,
                    log_container,
                ],
                spacing=15,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            )
        )

        self._add_log("应用已启动")

    def _build_single_config(self) -> ft.Control:
        """单账号模式（兼容原版 iflow2api）。"""
        self.api_key_field = ft.TextField(
            label="上游 iFlow API Key（单账号）",
            value=self.settings.api_key,
            password=True,
            can_reveal_password=True,
            expand=True,
        )
        self.base_url_field = ft.TextField(
            label="上游 Base URL",
            value=self.settings.base_url,
            hint_text="https://apis.iflow.cn/v1",
        )

        import_btn = ft.TextButton(
            "从 iFlow CLI 导入配置",
            icon=ft.Icons.DOWNLOAD,
            on_click=self._import_from_cli,
        )

        oauth_login_btn = ft.Button(
            "OAuth 登录（写入 ~/.iflow/settings.json）",
            icon=ft.Icons.LOGIN,
            on_click=self._login_with_iflow_oauth,
            style=ft.ButtonStyle(bgcolor=ft.Colors.BLUE, color=ft.Colors.WHITE),
        )

        return ft.Container(
            content=ft.Column(
                [
                    ft.Text("单账号模式", weight=ft.FontWeight.BOLD),
                    self.api_key_field,
                    self.base_url_field,
                    ft.Row(
                        [import_btn, oauth_login_btn],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                ]
            ),
            padding=15,
            border=ft.Border.all(1, ft.Colors.OUTLINE),
            border_radius=8,
        )

    def _build_pool_config(self) -> ft.Control:
        """多账号账号池（推荐）。"""
        # 本地 API Key（给 OpenCode/客户端用）
        self.client_key_field = ft.TextField(
            label="本地 API Key（给 OpenCode 用）",
            value=self.settings.client_api_key,
            password=True,
            can_reveal_password=True,
            expand=True,
        )

        regen_btn = ft.TextButton(
            "重新生成",
            icon=ft.Icons.REFRESH,
            on_click=self._regenerate_client_key,
        )

        self.strategy_dropdown = ft.Dropdown(
            label="负载均衡策略",
            value=self.settings.client_strategy,
            options=[
                ft.dropdown.Option("least_busy"),
                ft.dropdown.Option("round_robin"),
            ],
            on_change=self._on_strategy_change,
            width=180,
        )

        add_account_btn = ft.Button(
            "添加账号（OAuth 登录）",
            icon=ft.Icons.ADD,
            on_click=self._add_account_with_oauth,
            style=ft.ButtonStyle(bgcolor=ft.Colors.BLUE, color=ft.Colors.WHITE),
        )

        import_as_account_btn = ft.TextButton(
            "从 iFlow CLI 导入为新账号",
            icon=ft.Icons.DOWNLOAD,
            on_click=self._import_cli_as_account,
        )

        # 账号表
        self.accounts_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("启用")),
                ft.DataColumn(ft.Text("账号")),
                ft.DataColumn(ft.Text("Key")),
                ft.DataColumn(ft.Text("并发上限")),
                ft.DataColumn(ft.Text("操作")),
            ],
            rows=[],
            expand=True,
        )

        # 稳定性（熔断/failover）
        self.res_failure_threshold = ft.TextField(
            label="连续失败熔断阈值",
            value=str(self.routing.resilience.failure_threshold),
            keyboard_type=ft.KeyboardType.NUMBER,
            width=160,
            on_blur=self._save_resilience_from_ui,
        )
        self.res_cool_down = ft.TextField(
            label="冷却时间(秒)",
            value=str(self.routing.resilience.cool_down_seconds),
            keyboard_type=ft.KeyboardType.NUMBER,
            width=140,
            on_blur=self._save_resilience_from_ui,
        )
        self.res_retry_attempts = ft.TextField(
            label="failover 次数(非流式)",
            value=str(self.routing.resilience.retry_attempts),
            keyboard_type=ft.KeyboardType.NUMBER,
            width=170,
            on_blur=self._save_resilience_from_ui,
        )
        self.res_retry_backoff = ft.TextField(
            label="failover 间隔(ms)",
            value=str(self.routing.resilience.retry_backoff_ms),
            keyboard_type=ft.KeyboardType.NUMBER,
            width=160,
            on_blur=self._save_resilience_from_ui,
        )

        resilience_box = ft.Container(
            content=ft.Column(
                [
                    ft.Text("稳定性（熔断 / failover）", weight=ft.FontWeight.BOLD),
                    ft.Row(
                        [
                            self.res_failure_threshold,
                            self.res_cool_down,
                        ],
                        wrap=True,
                    ),
                    ft.Row(
                        [
                            self.res_retry_attempts,
                            self.res_retry_backoff,
                        ],
                        wrap=True,
                    ),
                ],
                spacing=8,
            ),
            padding=12,
            border=ft.Border.all(1, ft.Colors.OUTLINE),
            border_radius=8,
        )

        # OpenCode 集成
        opencode_paths = discover_config_paths(self.settings.opencode_config_path)
        self.opencode_path_dropdown = ft.Dropdown(
            label="OpenCode 配置文件",
            options=[ft.dropdown.Option(str(p)) for p in opencode_paths],
            value=str(opencode_paths[0]) if opencode_paths else "",
            expand=True,
        )
        self.opencode_provider_field = ft.TextField(
            label="Provider 名称",
            value=self.settings.opencode_provider_name,
            width=160,
        )
        self.opencode_set_default_checkbox = ft.Checkbox(
            label="设为默认模型",
            value=self.settings.opencode_set_default_model,
        )

        model_ids = [m.id for m in get_known_models()]
        self.opencode_default_model_dropdown = ft.Dropdown(
            label="默认模型",
            options=[ft.dropdown.Option(mid) for mid in model_ids],
            value=self.settings.opencode_default_model if self.settings.opencode_default_model in model_ids else (model_ids[0] if model_ids else ""),
            width=220,
        )
        self.opencode_set_small_checkbox = ft.Checkbox(
            label="设为 small_model",
            value=True,
        )
        self.opencode_small_model_dropdown = ft.Dropdown(
            label="small_model",
            options=[ft.dropdown.Option(mid) for mid in model_ids],
            value="iFlow-ROME-30BA3B" if "iFlow-ROME-30BA3B" in model_ids else (model_ids[0] if model_ids else ""),
            width=220,
        )

        opencode_write_btn = ft.Button(
            "一键写入 OpenCode 配置",
            icon=ft.Icons.SETTINGS,
            on_click=self._configure_opencode,
            style=ft.ButtonStyle(bgcolor=ft.Colors.GREEN, color=ft.Colors.WHITE),
        )
        opencode_test_btn = ft.TextButton(
            "本地自检",
            icon=ft.Icons.CHECK,
            on_click=self._local_smoke_test,
        )

        opencode_box = ft.Container(
            content=ft.Column(
                [
                    ft.Text("OpenCode 集成", weight=ft.FontWeight.BOLD),
                    self.opencode_path_dropdown,
                    ft.Row(
                        [
                            self.opencode_provider_field,
                            self.opencode_set_default_checkbox,
                        ],
                        wrap=True,
                    ),
                    ft.Row(
                        [
                            self.opencode_default_model_dropdown,
                            self.opencode_set_small_checkbox,
                            self.opencode_small_model_dropdown,
                        ],
                        wrap=True,
                    ),
                    ft.Row([opencode_write_btn, opencode_test_btn]),
                ],
                spacing=8,
            ),
            padding=12,
            border=ft.Border.all(1, ft.Colors.OUTLINE),
            border_radius=8,
        )

        self._refresh_accounts_table()

        return ft.Container(
            content=ft.Column(
                [
                    ft.Text("账号池模式（推荐）", weight=ft.FontWeight.BOLD),
                    ft.Row([self.client_key_field, regen_btn]),
                    ft.Row([self.strategy_dropdown, add_account_btn, import_as_account_btn], wrap=True),
                    ft.Text(
                        f"本地 Base URL: http://127.0.0.1:{self.settings.port}/v1",
                        size=12,
                        selectable=True,
                    ),
                    ft.Container(
                        content=self.accounts_table,
                        border=ft.Border.all(1, ft.Colors.OUTLINE),
                        border_radius=8,
                        padding=8,
                    ),
                    resilience_box,
                    opencode_box,
                ],
                spacing=12,
            ),
            padding=15,
            border=ft.Border.all(1, ft.Colors.OUTLINE),
            border_radius=8,
        )

    def _add_log(self, message: str):
        """添加日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_list.controls.append(
            ft.Text(f"[{timestamp}] {message}", size=12, selectable=True)
        )
        # 限制日志数量
        if len(self.log_list.controls) > 100:
            self.log_list.controls.pop(0)
        self.page.update()

    def _add_log_threadsafe(self, message: str):
        """线程安全添加日志（从后台线程调用）"""
        try:
            self.page.pubsub.send_all({"type": "log", "message": message})
        except Exception:
            pass

    def _on_pubsub_message(self, message):
        """处理 pubsub 消息 - 在主线程中执行"""
        if isinstance(message, dict) and message.get("type") == "server_state":
            state = message["state"]
            msg = message["message"]
            self._on_server_state_change(state, msg)
        elif isinstance(message, dict) and message.get("type") == "log":
            self._add_log(message.get("message", ""))
        elif isinstance(message, dict) and message.get("type") == "routing_reload":
            try:
                self.routing = load_keys_config()
            except Exception:
                self.routing = KeyRoutingConfig()
            self._refresh_accounts_table()
            self._refresh_resilience_fields()
            # If the server was waiting for accounts, start it now.
            if self.settings.auto_run_server and self.server.state != ServerState.RUNNING:
                try:
                    self._start_server(None)
                except Exception:
                    pass
            self.page.update()
        elif isinstance(message, dict) and message.get("type") == "single_login_success":
            if self.api_key_field:
                self.api_key_field.value = message.get("api_key", "") or ""
            if self.base_url_field:
                self.base_url_field.value = message.get("base_url", "") or self.settings.base_url
            try:
                self.page.open(
                    ft.SnackBar(content=ft.Text("登录成功！配置已自动更新"), bgcolor=ft.Colors.GREEN)
                )
            except Exception:
                pass
            self.page.update()

    def _on_server_state_change_threadsafe(self, state: ServerState, message: str):
        """服务状态变化回调 - 线程安全版本，从后台线程调用"""
        # 通过 pubsub 发送消息到主线程
        try:
            self.page.pubsub.send_all(
                {"type": "server_state", "state": state, "message": message}
            )
        except Exception:
            pass

    def _on_server_state_change(self, state: ServerState, message: str):
        """服务状态变化回调 - 必须在主线程调用"""
        state_config = {
            ServerState.STOPPED: (ft.Colors.GREY, "服务未运行"),
            ServerState.STARTING: (ft.Colors.ORANGE, "服务启动中..."),
            ServerState.RUNNING: (
                ft.Colors.GREEN,
                f"服务运行中 (http://{self.settings.host}:{self.settings.port})",
            ),
            ServerState.STOPPING: (ft.Colors.ORANGE, "服务停止中..."),
            ServerState.ERROR: (ft.Colors.RED, f"错误: {message}"),
        }

        color, text = state_config.get(state, (ft.Colors.GREY, "未知状态"))
        self.status_icon.color = color
        self.status_text.value = text

        # 更新按钮状态
        is_running = state == ServerState.RUNNING
        is_busy = state in (ServerState.STARTING, ServerState.STOPPING)
        self.start_btn.disabled = is_running or is_busy
        self.stop_btn.disabled = not is_running or is_busy

        self._add_log(text)
        self.page.update()

    # ============ 多账号配置相关 ============

    def _persist_routing_config(self):
        """保存 ~/.iflow2api/keys.json（不打印任何密钥）。"""
        # 维持 OpenCode token 的默认路由：始终指向所有启用账号
        ensure_opencode_route(
            self.routing,
            token=self.settings.client_api_key,
            strategy=self.settings.client_strategy,
        )
        try:
            save_keys_config(self.routing)
        except Exception as e:
            self._add_log(f"保存 keys.json 失败: {e}")

    def _refresh_accounts_table(self):
        if not self.accounts_table:
            return

        rows: list[ft.DataRow] = []
        for account_id in sorted(self.routing.accounts.keys()):
            acc = self.routing.accounts[account_id]
            key_mask = ""
            try:
                key_mask = f"...{acc.api_key[-4:]}" if acc.api_key else ""
            except Exception:
                key_mask = ""

            enabled_switch = ft.Switch(
                value=bool(acc.enabled),
                on_change=lambda e, aid=account_id: self._on_account_enabled_change(aid, e),
            )
            concurrency_field = ft.TextField(
                value=str(acc.max_concurrency),
                width=90,
                keyboard_type=ft.KeyboardType.NUMBER,
                on_blur=lambda e, aid=account_id: self._on_account_concurrency_blur(aid, e),
            )
            remove_btn = ft.IconButton(
                icon=ft.Icons.DELETE,
                tooltip="删除账号",
                on_click=lambda e, aid=account_id: self._remove_account(aid),
            )

            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(enabled_switch),
                        ft.DataCell(ft.Text(acc.label or account_id)),
                        ft.DataCell(ft.Text(key_mask)),
                        ft.DataCell(concurrency_field),
                        ft.DataCell(remove_btn),
                    ]
                )
            )

        self.accounts_table.rows = rows

    def _refresh_resilience_fields(self):
        if not self.res_failure_threshold:
            return
        r = self.routing.resilience
        self.res_failure_threshold.value = str(r.failure_threshold)
        self.res_cool_down.value = str(r.cool_down_seconds)
        self.res_retry_attempts.value = str(r.retry_attempts)
        self.res_retry_backoff.value = str(r.retry_backoff_ms)

    def _on_account_enabled_change(self, account_id: str, e):
        try:
            self.routing.accounts[account_id].enabled = bool(e.control.value)
            self._persist_routing_config()
            self._refresh_accounts_table()
            self.page.update()
        except Exception as ex:
            self._add_log(f"更新账号失败: {ex}")

    def _on_account_concurrency_blur(self, account_id: str, e):
        try:
            raw = (e.control.value or "").strip()
            maxc = int(raw) if raw else 0
            if maxc < 0:
                maxc = 0
            self.routing.accounts[account_id].max_concurrency = maxc
            self._persist_routing_config()
        except Exception as ex:
            self._add_log(f"更新并发上限失败: {ex}")

    def _remove_account(self, account_id: str):
        try:
            if account_id in self.routing.accounts:
                del self.routing.accounts[account_id]
            # 清理路由中引用（简单起见：让 ensure_opencode_route 重新生成）
            self._persist_routing_config()
            self._refresh_accounts_table()
            self.page.update()
        except Exception as ex:
            self._add_log(f"删除账号失败: {ex}")

    def _save_resilience_from_ui(self, e):
        try:
            r = self.routing.resilience
            r.failure_threshold = max(1, int((self.res_failure_threshold.value or "3").strip()))
            r.cool_down_seconds = max(1, int((self.res_cool_down.value or "30").strip()))
            r.retry_attempts = max(0, int((self.res_retry_attempts.value or "1").strip()))
            r.retry_backoff_ms = max(0, int((self.res_retry_backoff.value or "200").strip()))
            self._persist_routing_config()
        except Exception as ex:
            self._add_log(f"保存稳定性配置失败: {ex}")

    def _regenerate_client_key(self, e):
        self.settings.client_api_key = generate_client_key()
        if self.client_key_field:
            self.client_key_field.value = self.settings.client_api_key
        save_settings(self.settings)
        # 重新生成 token 后，需要更新 keys.json 路由
        self._persist_routing_config()
        self.page.update()

    def _on_strategy_change(self, e):
        self.settings.client_strategy = e.control.value or "least_busy"
        save_settings(self.settings)
        self._persist_routing_config()

    def _import_cli_as_account(self, e):
        """将当前 iFlow CLI 的配置作为新账号加入账号池。"""
        config = import_from_iflow_cli()
        if not config or not config.api_key:
            self.page.open(
                ft.SnackBar(
                    content=ft.Text("导入失败：请先运行 iflow 并完成登录"),
                    bgcolor=ft.Colors.RED,
                )
            )
            return

        # 追加到账号池
        add_upstream_account(
            self.routing,
            api_key=config.api_key,
            base_url=config.base_url,
            label="from-cli",
            max_concurrency=4,
        )
        self._persist_routing_config()
        self._refresh_accounts_table()
        self.page.update()

    def _add_account_with_oauth(self, e):
        """OAuth 登录并将账号写入 keys.json（多账号模式）。"""
        from .oauth_login import OAuthLoginHandler

        def save_callback(config, user_info, token_data):
            try:
                # 重新从磁盘加载，避免并发覆盖
                cfg = load_keys_config()
            except Exception:
                cfg = KeyRoutingConfig()

            label = user_info.get("username") or user_info.get("phone") or "iflow"
            add_upstream_account(
                cfg,
                api_key=config.api_key,
                base_url=config.base_url,
                label=label,
                max_concurrency=4,
            )
            ensure_opencode_route(
                cfg,
                token=self.settings.client_api_key,
                strategy=self.settings.client_strategy,
            )
            save_keys_config(cfg)
            try:
                self.page.pubsub.send_all({"type": "routing_reload"})
            except Exception:
                pass

        handler = OAuthLoginHandler(
            self._add_log_threadsafe,
            success_callback=None,
            save_callback=save_callback,
        )
        handler.start_login()

    def _configure_opencode(self, e):
        """一键写入 OpenCode 配置文件，添加 provider=iflow。"""
        config_path_str = (self.opencode_path_dropdown.value or "").strip() if self.opencode_path_dropdown else ""
        if not config_path_str:
            self.page.open(
                ft.SnackBar(content=ft.Text("未找到 OpenCode 配置文件"), bgcolor=ft.Colors.RED)
            )
            return

        provider_name = (self.opencode_provider_field.value or "iflow").strip() if self.opencode_provider_field else "iflow"
        self.settings.opencode_provider_name = provider_name
        self.settings.opencode_config_path = config_path_str
        self.settings.opencode_set_default_model = bool(self.opencode_set_default_checkbox.value) if self.opencode_set_default_checkbox else False
        self.settings.opencode_default_model = (self.opencode_default_model_dropdown.value or self.settings.opencode_default_model).strip() if self.opencode_default_model_dropdown else self.settings.opencode_default_model
        save_settings(self.settings)

        port = self.settings.port
        base_url = f"http://127.0.0.1:{port}/v1"

        try:
            ensure_iflow_provider(
                config_path=Path(config_path_str),
                provider_name=provider_name,
                base_url=base_url,
                api_key=self.settings.client_api_key,
                set_default_model=self.settings.opencode_set_default_model,
                default_model=self.settings.opencode_default_model,
                set_small_model=bool(self.opencode_set_small_checkbox.value) if self.opencode_set_small_checkbox else False,
                small_model=(self.opencode_small_model_dropdown.value or "iFlow-ROME-30BA3B") if self.opencode_small_model_dropdown else "iFlow-ROME-30BA3B",
            )
            self.page.open(
                ft.SnackBar(content=ft.Text("已写入 OpenCode 配置"), bgcolor=ft.Colors.GREEN)
            )
            self._add_log("已写入 OpenCode 配置（provider=iflow）")
        except Exception as ex:
            self.page.open(
                ft.SnackBar(content=ft.Text(f"写入失败: {ex}"), bgcolor=ft.Colors.RED)
            )

    def _local_smoke_test(self, e):
        """本地连通性自检（不调用上游模型）。"""
        port = self.settings.port
        base = f"http://127.0.0.1:{port}"
        try:
            with httpx.Client(timeout=3.0) as c:
                h = c.get(f"{base}/health")
                m = c.get(f"{base}/v1/models")
            ok = h.status_code == 200 and m.status_code == 200
            self.page.open(
                ft.SnackBar(
                    content=ft.Text("自检通过" if ok else f"自检失败: {h.status_code}/{m.status_code}"),
                    bgcolor=ft.Colors.GREEN if ok else ft.Colors.RED,
                )
            )
        except Exception as ex:
            self.page.open(ft.SnackBar(content=ft.Text(f"自检异常: {ex}"), bgcolor=ft.Colors.RED))

    def _start_server(self, e):
        """启动服务"""
        self._update_settings_from_ui()
        if self.server.start(self.settings):
            self._add_log("正在启动服务...")

    def _stop_server(self, e):
        """停止服务"""
        if self.server.stop():
            self._add_log("正在停止服务...")

    def _save_settings(self, e):
        """保存配置"""
        self._update_settings_from_ui()
        save_settings(self.settings)
        self._add_log("配置已保存")

        # 显示提示
        self.page.open(
            ft.SnackBar(content=ft.Text("配置已保存"), bgcolor=ft.Colors.GREEN)
        )

    def _update_settings_from_ui(self):
        """从 UI 更新配置"""
        self.settings.host = self.host_field.value or "0.0.0.0"
        try:
            self.settings.port = int(self.port_field.value or "8000")
        except ValueError:
            self.settings.port = 8000
        if self.api_key_field:
            self.settings.api_key = self.api_key_field.value or ""
        if self.base_url_field:
            self.settings.base_url = self.base_url_field.value or "https://apis.iflow.cn/v1"
        self.settings.start_minimized = self.start_minimized_checkbox.value
        self.settings.auto_run_server = self.auto_run_checkbox.value
        if self.close_to_background_checkbox:
            self.settings.close_to_background = bool(self.close_to_background_checkbox.value)

    def _import_from_cli(self, e):
        """从 iFlow CLI 导入配置"""
        config = import_from_iflow_cli()
        if config:
            self.api_key_field.value = config.api_key
            self.base_url_field.value = config.base_url
            self.page.update()
            self._add_log("已从 iFlow CLI 导入配置")
            self.page.open(
                ft.SnackBar(
                    content=ft.Text("已从 iFlow CLI 导入配置"), bgcolor=ft.Colors.GREEN
                )
            )
        else:
            self._add_log("无法导入 iFlow CLI 配置")
            self.page.open(
                ft.SnackBar(
                    content=ft.Text("无法导入配置，请确保已运行 iflow 并完成登录"),
                    bgcolor=ft.Colors.RED,
                )
            )

    def _on_auto_start_change(self, e):
        """开机自启动设置变化"""
        success = set_auto_start(e.control.value)
        if success:
            self._add_log(f"开机自启动已{'启用' if e.control.value else '禁用'}")
        else:
            e.control.value = not e.control.value
            self.page.update()
            self._add_log("设置开机自启动失败")

    def _on_basic_settings_change(self, e):
        """基础设置变化（无需手动点保存）"""
        try:
            self._update_settings_from_ui()
            save_settings(self.settings)
        except Exception:
            pass

    def _login_with_iflow_oauth(self, e):
        """使用 iFlow OAuth 登录"""
        from .oauth_login import OAuthLoginHandler

        def on_login_success(config, user_info=None):
            """OAuth 登录成功后的回调"""
            try:
                self.page.pubsub.send_all(
                    {"type": "single_login_success", "api_key": config.api_key, "base_url": config.base_url}
                )
            except Exception:
                pass

        handler = OAuthLoginHandler(self._add_log_threadsafe, success_callback=on_login_success)
        handler.start_login()


def main(page: ft.Page):
    """Flet 应用入口"""
    IFlow2ApiApp(page)


if __name__ == "__main__":
    ft.run(main)

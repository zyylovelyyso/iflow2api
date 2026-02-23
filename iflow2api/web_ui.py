"""iflow2api Web 控制台（中文高质感界面 + 自动续期可观测）。"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .edge import launch_edge, list_edge_profiles
from .keys_store import (
    add_upstream_account,
    ensure_opencode_route,
    generate_client_key,
    load_keys_config,
    save_keys_config,
)
from .model_catalog import get_recommended_models
from .oauth import IFlowOAuth
from .opencode import discover_config_paths, ensure_iflow_provider
from .routing import KeyRoutingConfig, get_routing_file_path_in_use
from .routing_refresher import (
    DEFAULT_REFRESH_BUFFER_SECONDS,
    DEFAULT_REFRESH_CHECK_INTERVAL_SECONDS,
    RoutingOAuthRefresher,
)
from .settings import load_settings, save_settings


router = APIRouter()
_ALLOWED_STRATEGIES = ("least_busy", "round_robin")


def _ui_allowed(request: Request) -> bool:
    allow_remote = (request.headers.get("X-IFLOW2API-UI-ALLOW-REMOTE") or "").strip() == "1"
    if allow_remote:
        return True
    host = ""
    try:
        host = request.client.host if request.client else ""
    except Exception:
        host = ""
    return host in ("127.0.0.1", "::1")


def _require_ui_allowed(request: Request) -> None:
    if not _ui_allowed(request):
        raise HTTPException(status_code=403, detail="Web UI 仅允许本机访问")


def _mask_secret(value: str, *, show: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= show:
        return "*" * len(value)
    return f"{'*' * (len(value) - show)}{value[-show:]}"


def _normalize_strategy(value: str) -> str:
    strategy = (value or "").strip().lower()
    return strategy if strategy in _ALLOWED_STRATEGIES else "least_busy"


def _ensure_local_client_key() -> tuple[str, str]:
    settings = load_settings()
    changed = False
    if not settings.client_api_key:
        settings.client_api_key = generate_client_key()
        changed = True
    strategy = _normalize_strategy(settings.client_strategy)
    if settings.client_strategy != strategy:
        settings.client_strategy = strategy
        changed = True
    if changed:
        save_settings(settings)
    return settings.client_api_key, strategy


def _recommended_model_ids() -> list[str]:
    return [m.id for m in get_recommended_models()]


def _pick_models(*, preferred_default: str, preferred_small: str) -> tuple[str, str]:
    model_ids = _recommended_model_ids()
    if not model_ids:
        return "", ""
    default_model = preferred_default if preferred_default in model_ids else model_ids[0]
    fallback_small = model_ids[1] if len(model_ids) > 1 else model_ids[0]
    small_model = preferred_small if preferred_small in model_ids else fallback_small
    return default_model, small_model


def _load_routing_safely() -> KeyRoutingConfig:
    try:
        return load_keys_config()
    except Exception:
        return KeyRoutingConfig()


def _sync_client_route(routing: KeyRoutingConfig, *, token: str, strategy: str) -> None:
    ensure_opencode_route(routing, token=token, strategy=strategy)


def _humanize_minutes(minutes: Optional[int]) -> str:
    if minutes is None:
        return "-"
    if minutes <= 0:
        return "已过期"
    if minutes < 60:
        return f"约 {minutes} 分钟"
    hours = minutes // 60
    rem_minutes = minutes % 60
    if hours < 24:
        if rem_minutes == 0:
            return f"约 {hours} 小时"
        return f"约 {hours} 小时 {rem_minutes} 分"
    days = hours // 24
    rem_hours = hours % 24
    if rem_hours == 0:
        return f"约 {days} 天"
    return f"约 {days} 天 {rem_hours} 小时"


def _simple_result_html(title: str, message: str, *, ok: bool) -> str:
    color = "#16a34a" if ok else "#dc2626"
    return (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{title}</title>"
        "<body style='font-family:Microsoft YaHei,Segoe UI,sans-serif;background:#0b1220;color:#e2e8f0;padding:20px'>"
        f"<h2 style='margin:0 0 8px;color:{color}'>{title}</h2>"
        f"<p style='margin:0 0 10px'>{message}</p>"
        "<p style='margin:0;color:#94a3b8'>可关闭此窗口并返回 iflow2api 控制台。</p>"
        "<script>setTimeout(()=>window.close(),1500);</script>"
        "</body></html>"
    )


_LOCK = threading.Lock()


@dataclass
class _PendingOAuth:
    created_at: float
    redirect_uri: str
    profile_directory: Optional[str]
    inprivate: bool
    max_concurrency: int
    label_override: Optional[str]
    base_url: str


_PENDING: dict[str, _PendingOAuth] = {}


def _cleanup_pending(now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    ttl = 15 * 60
    with _LOCK:
        for state in list(_PENDING.keys()):
            pending = _PENDING.get(state)
            if not pending or now - float(pending.created_at) > ttl:
                _PENDING.pop(state, None)


UI_HTML = """<!doctype html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>iflow2api 控制台</title>
<style>
:root {
  --bg: #060a15;
  --bg-2: #0a1224;
  --surface: rgba(11, 19, 37, 0.74);
  --surface-strong: rgba(8, 14, 29, 0.94);
  --card-top: rgba(15, 24, 46, 0.92);
  --card-bottom: rgba(8, 15, 31, 0.88);
  --border: rgba(148, 163, 184, 0.26);
  --text: #e6edf8;
  --muted: #91a4c4;
  --primary: #36d9ff;
  --secondary: #8b7dff;
  --accent: #9ef07a;
  --ok: #22c55e;
  --warn: #f59e0b;
  --danger: #fb7185;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: \"MiSans\", \"HarmonyOS Sans SC\", \"PingFang SC\", \"Microsoft YaHei UI\", sans-serif;
  color: var(--text);
  background:
    radial-gradient(1200px 560px at 10% -14%, rgba(54,217,255,.26), transparent),
    radial-gradient(1050px 520px at 94% -18%, rgba(139,125,255,.25), transparent),
    linear-gradient(180deg, #04070f 0%, var(--bg) 40%, var(--bg-2) 100%);
  min-height: 100vh;
}
body::before {
  content: \"\";
  position: fixed;
  inset: 0;
  pointer-events: none;
  background:
    linear-gradient(115deg, rgba(255,255,255,.03) 0%, transparent 28%, transparent 72%, rgba(255,255,255,.03) 100%),
    radial-gradient(circle at 18% 86%, rgba(54,217,255,.12), transparent 28%),
    radial-gradient(circle at 80% 78%, rgba(158,240,122,.08), transparent 24%);
  mix-blend-mode: screen;
}
.wrap {
  position: relative;
  max-width: 1320px;
  margin: 0 auto;
  padding: 24px 18px 30px;
}
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  border-radius: 22px;
  border: 1px solid var(--border);
  background: linear-gradient(160deg, rgba(14,24,49,.88), rgba(10,18,35,.78));
  backdrop-filter: blur(14px);
  box-shadow:
    0 14px 40px rgba(2, 6, 23, .45),
    inset 0 1px 0 rgba(255,255,255,.08);
  padding: 18px 22px;
}
.brand { display: flex; align-items: center; gap: 12px; }
.brand-icon {
  width: 50px;
  height: 50px;
  border-radius: 16px;
  background:
    radial-gradient(circle at 24% 20%, rgba(255,255,255,.55), rgba(255,255,255,0) 34%),
    linear-gradient(145deg, var(--primary), var(--secondary));
  display: grid;
  place-items: center;
  color: #081325;
  font-weight: 800;
  letter-spacing: .5px;
  box-shadow:
    0 12px 30px rgba(54,217,255,.25),
    0 8px 24px rgba(139,125,255,.2);
}
.brand h1 { margin: 0; font-size: 22px; font-weight: 760; letter-spacing: .3px; }
.brand p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 8px 15px;
  background: rgba(11,20,38,.72);
  font-size: 13px;
  backdrop-filter: blur(6px);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.08);
}
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--warn); }
.status-pill.ok { border-color: rgba(34,197,94,.45); color: #bbf7d0; }
.status-pill.ok .status-dot { background: var(--ok); }
.status-pill.err { border-color: rgba(251,113,133,.5); color: #fecdd3; }
.status-pill.err .status-dot { background: var(--danger); }

.hero-chips {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.chip {
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 5px 12px;
  background: rgba(11,20,38,.55);
  font-size: 12px;
  color: #ccdeff;
}
.chip strong { color: #eaffff; font-weight: 700; }

.metrics {
  margin-top: 14px;
  display: grid;
  grid-template-columns: repeat(4, minmax(130px, 1fr));
  gap: 12px;
}
.metric {
  position: relative;
  overflow: hidden;
  border-radius: 16px;
  border: 1px solid var(--border);
  background: linear-gradient(160deg, var(--card-top) 0%, var(--card-bottom) 100%);
  box-shadow: 0 10px 28px rgba(2, 6, 23, .35);
  padding: 13px 15px;
}
.metric::after {
  content: \"\";
  position: absolute;
  right: -20px;
  top: -20px;
  width: 80px;
  height: 80px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(54,217,255,.32), transparent 70%);
  pointer-events: none;
  filter: blur(2px);
}
.metric .k { color: var(--muted); font-size: 12px; }
.metric .v { margin-top: 4px; font-size: 26px; font-weight: 760; letter-spacing: .4px; }

.layout {
  margin-top: 14px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 392px;
  gap: 14px;
}
@media (max-width: 1080px) {
  .metrics { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
  .layout { grid-template-columns: 1fr; }
}

.card {
  position: relative;
  overflow: hidden;
  border-radius: 18px;
  border: 1px solid var(--border);
  background: linear-gradient(165deg, rgba(14,24,46,.92) 0%, rgba(8,14,30,.86) 100%);
  backdrop-filter: blur(10px) saturate(115%);
  box-shadow:
    0 16px 34px rgba(2, 6, 23, .35),
    inset 0 1px 0 rgba(255,255,255,.06);
  padding: 15px;
}
.card + .card { margin-top: 12px; }
.card h2 {
  margin: 0 0 11px;
  font-size: 15px;
  font-weight: 760;
  letter-spacing: .2px;
  color: #e5edff;
}
.hint { margin: 0 0 10px; font-size: 12px; color: var(--muted); }

.row { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.field { display: flex; flex-direction: column; gap: 4px; flex: 1; min-width: 125px; }
.field label { font-size: 12px; color: var(--muted); }
input, select {
  width: 100%;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: rgba(3,8,22,.62);
  color: var(--text);
  padding: 10px 11px;
  outline: none;
  font-size: 14px;
  transition: .18s ease;
}
input:focus, select:focus {
  border-color: rgba(54,217,255,.76);
  box-shadow: 0 0 0 3px rgba(54,217,255,.16);
  transform: translateY(-1px);
}
button {
  border: none;
  border-radius: 12px;
  padding: 9px 13px;
  font-size: 13px;
  font-weight: 760;
  color: #ecfeff;
  cursor: pointer;
  background: linear-gradient(135deg, #17c9ea, #7f7dff);
  box-shadow: 0 10px 24px rgba(54,217,255,.2);
  transition: .16s ease;
}
button.alt {
  border: 1px solid var(--border);
  background: rgba(4,10,25,.52);
  color: var(--text);
  box-shadow: none;
}
button.danger {
  border: 1px solid rgba(251,113,133,.45);
  background: rgba(190,24,93,.22);
  color: #fecdd3;
}
button:hover { transform: translateY(-1px); filter: brightness(1.04); }
button:active { transform: translateY(0); }
button:disabled { opacity: .45; cursor: not-allowed; transform: none; }
button.sm { padding: 5px 8px; font-size: 12px; }

table {
  width: 100%;
  border-collapse: collapse;
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  background: rgba(2,7,20,.42);
}
th, td {
  padding: 9px 8px;
  font-size: 13px;
  border-bottom: 1px solid rgba(148,163,184,.16);
  text-align: left;
}
th {
  background: rgba(5,12,28,.75);
  color: #cfdbf3;
  font-weight: 680;
}
tr:hover td { background: rgba(54,217,255,.05); }

.switch {
  width: 38px;
  height: 20px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: rgba(100,116,139,.4);
  position: relative;
  cursor: pointer;
}
.switch::after {
  content: \"\";
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: white;
  position: absolute;
  left: 2px;
  top: 2px;
  transition: .16s;
}
.switch.on { background: rgba(34,197,94,.6); }
.switch.on::after { left: 20px; }

.tag {
  display: inline-block;
  border-radius: 999px;
  border: 1px solid var(--border);
  padding: 2px 7px;
  font-size: 11px;
}
.tag.ok { color: #bbf7d0; border-color: rgba(34,197,94,.5); background: rgba(34,197,94,.14); }
.tag.warn { color: #fde68a; border-color: rgba(245,158,11,.48); background: rgba(245,158,11,.14); }
.tag.err { color: #fecdd3; border-color: rgba(251,113,133,.5); background: rgba(251,113,133,.14); }

.kv {
  display: grid;
  grid-template-columns: 105px 1fr;
  gap: 8px 10px;
}
.kv .k { color: var(--muted); font-size: 12px; }
.code {
  font-family: Consolas, \"JetBrains Mono\", monospace;
  background: rgba(2,7,20,.62);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 2px 6px;
  word-break: break-all;
}
.paths, .log {
  max-height: 180px;
  overflow: auto;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: rgba(2,6,23,.5);
  padding: 8px;
}
.path-line, .line { margin-bottom: 5px; font-size: 12px; word-break: break-all; }
.time { color: #7dd3fc; margin-right: 6px; }
.empty {
  border: 1px dashed var(--border);
  border-radius: 10px;
  padding: 12px;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
}
.toast {
  position: fixed;
  right: 12px;
  bottom: 12px;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: var(--surface-strong);
  color: var(--text);
  padding: 9px 11px;
  opacity: 0;
  transform: translateY(8px);
  transition: .2s;
}
.toast.show { opacity: 1; transform: translateY(0); }
</style>
</head>
<body>
<div class=\"wrap\">
  <div class=\"header\">
    <div class=\"brand\">
      <div class=\"brand-icon\">IF</div>
      <div>
        <h1>iflow2api 控制台</h1>
        <p>多账号路由 · OpenCode 一键接入 · OAuth 自动续期</p>
      </div>
    </div>
    <div id=\"status\" class=\"status-pill\"><span class=\"status-dot\"></span><span id=\"statusText\">加载中...</span></div>
  </div>
  <div class=\"hero-chips\">
    <span class=\"chip\"><strong>首次登录</strong> 仅需一次浏览器确认</span>
    <span class=\"chip\"><strong>后续续期</strong> 自动在后台完成</span>
    <span class=\"chip\"><strong>OpenCode</strong> CLI 与桌面配置可一键同步</span>
  </div>

  <div class=\"metrics\">
    <div class=\"metric\"><div class=\"k\">账号总数</div><div id=\"mTotal\" class=\"v\">0</div></div>
    <div class=\"metric\"><div class=\"k\">启用账号</div><div id=\"mEnabled\" class=\"v\">0</div></div>
    <div class=\"metric\"><div class=\"k\">OAuth账号</div><div id=\"mOauth\" class=\"v\">0</div></div>
    <div class=\"metric\"><div class=\"k\">可用模型</div><div id=\"mModels\" class=\"v\">0</div></div>
  </div>

  <div class=\"layout\">
    <div>
      <div class=\"card\">
        <h2>账号接入（OAuth）</h2>
        <p class=\"hint\">首次登录需要你在浏览器确认；之后服务会自动续期 token（无需重复登录）。</p>
        <div class=\"row\">
          <div class=\"field\"><label>Edge Profile</label><select id=\"profile\"></select></div>
          <div class=\"field\" style=\"max-width:140px\"><label>最大并发</label><input id=\"concurrency\" type=\"number\" min=\"0\" value=\"4\" /></div>
          <div class=\"field\"><label>账号标签（可选）</label><input id=\"label\" placeholder=\"主账号 / 备用账号\" /></div>
        </div>
        <div class=\"row\">
          <button id=\"btnOauth\">Profile 登录并添加</button>
          <button class=\"alt\" id=\"btnPrivate\">InPrivate 登录</button>
          <button class=\"alt\" id=\"btnProfiles\">刷新 Profile</button>
        </div>
      </div>

      <div class=\"card\">
        <h2>账号池管理</h2>
        <div id=\"empty\" class=\"empty\">暂无账号，请先完成一次 OAuth 登录。</div>
        <table id=\"tbl\" style=\"display:none\">
          <thead>
            <tr><th>启用</th><th>账号</th><th>并发</th><th>到期</th><th>续期状态</th><th>操作</th></tr>
          </thead>
          <tbody id=\"tbody\"></tbody>
        </table>
      </div>
    </div>

    <div>
      <div class=\"card\">
        <h2>自动续期状态</h2>
        <div class=\"kv\" id=\"renewKV\"></div>
        <div class=\"row\" style=\"margin-top:8px\">
          <button class=\"alt\" id=\"btnRefreshNow\">立即执行续期检查</button>
        </div>
      </div>

      <div class=\"card\">
        <h2>客户端访问配置</h2>
        <div class=\"kv\" id=\"kv\"></div>
        <div class=\"row\">
          <div class=\"field\"><label>路由策略</label><select id=\"strategy\"><option value=\"least_busy\">least_busy（推荐）</option><option value=\"round_robin\">round_robin</option></select></div>
        </div>
        <div class=\"row\">
          <button class=\"alt\" id=\"btnStrategy\">应用策略</button>
          <button class=\"alt\" id=\"btnKey\">重置 API Key</button>
        </div>
      </div>

      <div class=\"card\">
        <h2>OpenCode 同步</h2>
        <p class=\"hint\">同步 iflow provider 到所有检测到的 OpenCode 配置（CLI + 桌面版）。模型按请求名严格匹配；思考参数默认开启。</p>
        <div id=\"paths\" class=\"paths\"></div>
        <div class=\"row\" style=\"margin-top:8px\">
          <div class=\"field\" style=\"max-width:130px\"><label>Provider</label><input id=\"provider\" value=\"iflow\" /></div>
          <div class=\"field\"><label>默认模型</label><select id=\"model\"></select></div>
          <div class=\"field\"><label>small_model</label><select id=\"small\"></select></div>
        </div>
        <div class=\"row\">
          <button id=\"btnSync\">一键同步 OpenCode</button>
          <button class=\"alt\" id=\"btnRefresh\">刷新状态</button>
        </div>
      </div>

      <div class=\"card\">
        <h2>三模型连通性自检</h2>
        <p class=\"hint\">直接走本地网关 `/v1/chat/completions`，检查模型名一致性与思考字段返回（reasoning_content/reasoning）。</p>
        <div class=\"row\">
          <button id=\"btnProbeModels\">执行三模型自检</button>
          <button class=\"alt\" id=\"btnAutoRefresh\">开启自动刷新</button>
          <div class=\"field\" style=\"max-width:130px\">
            <label>刷新频率</label>
            <select id=\"autoRefreshSeconds\">
              <option value=\"10\">10 秒</option>
              <option value=\"20\" selected>20 秒</option>
              <option value=\"30\">30 秒</option>
              <option value=\"60\">60 秒</option>
            </select>
          </div>
        </div>
        <div id=\"probeSummary\" class=\"hint\">尚未执行自检</div>
        <table id=\"probeTable\" style=\"display:none\">
          <thead>
            <tr><th>请求模型</th><th>返回模型</th><th>模型一致</th><th>思考字段</th><th>耗时</th><th>结果</th></tr>
          </thead>
          <tbody id=\"probeTbody\"></tbody>
        </table>
      </div>

      <div class=\"card\">
        <h2>运行日志</h2>
        <div id=\"log\" class=\"log\"></div>
      </div>
    </div>
  </div>
</div>

<div id=\"toast\" class=\"toast\"></div>

<script>
const $ = (id) => document.getElementById(id);
const logEl = $('log');
const statusEl = $('status');
const statusTextEl = $('statusText');
const toastEl = $('toast');
let toastTimer;
let autoRefreshTimer = null;
let autoRefreshEnabled = false;

function esc(v){
  return String(v ?? '')
    .replaceAll('&','&amp;')
    .replaceAll('<','&lt;')
    .replaceAll('>','&gt;')
    .replaceAll('"','&quot;')
    .replaceAll("'",'&#39;');
}

function nowTime(){
  return new Date().toLocaleTimeString('zh-CN', {hour12:false});
}

function log(msg){
  const line = document.createElement('div');
  line.className = 'line';
  line.innerHTML = `<span class=\"time\">[${nowTime()}]</span>${msg}`;
  logEl.appendChild(line);
  if (logEl.children.length > 180) logEl.removeChild(logEl.firstChild);
  logEl.scrollTop = logEl.scrollHeight;
}

function toast(msg){
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2200);
}

async function api(url, options={}){
  const res = await fetch(url, {
    ...options,
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      ...(options.headers || {})
    }
  });
  if(!res.ok) throw new Error(await res.text());
  const text = await res.text();
  return text ? JSON.parse(text) : {};
}

function setStatus(ok, text){
  statusEl.classList.remove('ok','err');
  statusEl.classList.add(ok ? 'ok' : 'err');
  statusTextEl.textContent = text;
}

function boolTag(ok){
  return ok ? '<span class=\"tag ok\">是</span>' : '<span class=\"tag err\">否</span>';
}

function formatMinutes(mins){
  if (mins === null || mins === undefined) return '-';
  const m = Number(mins);
  if (m <= 0) return '已过期';
  if (m < 60) return `约 ${m} 分钟`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  if (h < 24) return rm ? `约 ${h} 小时 ${rm} 分` : `约 ${h} 小时`;
  const d = Math.floor(h / 24);
  const rh = h % 24;
  return rh ? `约 ${d} 天 ${rh} 小时` : `约 ${d} 天`;
}

function refreshTag(acc){
  if (!acc.oauth_refresh_token) return '<span class=\"tag\">API Key</span>';
  if (acc.last_refresh_error) return `<span class=\"tag err\">失败(${acc.refresh_failures||0})</span>`;
  if (acc.oauth_expires_in_minutes !== null && acc.oauth_expires_in_minutes <= 0) return '<span class=\"tag err\">待续期</span>';
  if (acc.oauth_expires_in_minutes !== null && acc.oauth_expires_in_minutes <= 60) return '<span class=\"tag warn\">即将续期</span>';
  return '<span class=\"tag ok\">正常</span>';
}

function renderRenew(state){
  $('renewKV').innerHTML = `
    <div class=\"k\">策略</div><div>首次手动登录后自动续期</div>
    <div class=\"k\">检查间隔</div><div><span class=\"code\">${Math.round((state.auto_refresh_check_interval_seconds||0)/60)} 分钟</span></div>
    <div class=\"k\">提前续期</div><div><span class=\"code\">到期前 ${Math.round((state.auto_refresh_buffer_seconds||0)/3600)} 小时</span></div>
  `;
}

function renderClient(state){
  const full = state.client_api_key || '';
  const masked = state.client_api_key_mask || full;
  $('kv').innerHTML = `
    <div class=\"k\">Base URL</div>
    <div><span class=\"code\">${esc(state.base_url || '')}</span> <button class=\"sm alt\" data-copy=\"${esc(state.base_url || '')}\">复制</button></div>
    <div class=\"k\">本地 API Key</div>
    <div><span id=\"clientKey\" class=\"code\" data-full=\"${esc(full)}\" data-mask=\"${esc(masked)}\">${esc(masked)}</span>
      <button class=\"sm alt\" id=\"btnToggleKey\">显示/隐藏</button>
      <button class=\"sm alt\" data-copy=\"${esc(full)}\">复制</button>
    </div>
    <div class=\"k\">当前策略</div><div><span class=\"code\">${esc(state.client_strategy || '')}</span></div>
  `;

  $('strategy').value = state.client_strategy || 'least_busy';

  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.copy || '');
        toast('已复制');
      } catch (_) {
        toast('复制失败');
      }
    };
  });

  const btnToggle = $('btnToggleKey');
  if (btnToggle) {
    btnToggle.onclick = () => {
      const keyEl = $('clientKey');
      if (!keyEl) return;
      const isMasked = (keyEl.textContent || '').includes('*');
      keyEl.textContent = isMasked ? keyEl.dataset.full : keyEl.dataset.mask;
    };
  }
}

function renderOpenCode(state){
  $('provider').value = state.opencode_provider_name || 'iflow';
  const models = state.recommended_models || [];
  const modelEl = $('model');
  const smallEl = $('small');
  modelEl.innerHTML = '';
  smallEl.innerHTML = '';
  for (const model of models) {
    const op1 = document.createElement('option');
    op1.value = model; op1.textContent = model;
    modelEl.appendChild(op1);
    const op2 = document.createElement('option');
    op2.value = model; op2.textContent = model;
    smallEl.appendChild(op2);
  }
  if (state.opencode_default_model) modelEl.value = state.opencode_default_model;
  if (state.opencode_small_model) smallEl.value = state.opencode_small_model;

  const paths = state.opencode_paths || [];
  $('paths').innerHTML = paths.length
    ? paths.map((p) => `<div class=\"path-line\">${esc(p)}</div>`).join('')
    : '<div class=\"path-line\">未发现 OpenCode 配置文件。</div>';
}

function renderAccounts(state){
  $('mTotal').textContent = String(state.accounts_total || 0);
  $('mEnabled').textContent = String(state.accounts_enabled || 0);
  $('mOauth').textContent = String(state.oauth_accounts || 0);
  $('mModels').textContent = String((state.recommended_models || []).length);

  const accounts = state.accounts || [];
  const tbl = $('tbl');
  const empty = $('empty');
  const tbody = $('tbody');

  if (!accounts.length) {
    tbl.style.display = 'none';
    empty.style.display = 'block';
    tbody.innerHTML = '';
    return;
  }

  empty.style.display = 'none';
  tbl.style.display = 'table';

  tbody.innerHTML = accounts.map((acc) => {
    const expiry = formatMinutes(acc.oauth_expires_in_minutes);
    const refreshHint = acc.last_refresh_error ? esc(acc.last_refresh_error) : (acc.last_refresh_at ? `上次成功：${esc(acc.last_refresh_at)}` : '等待首次续期');
    return `
      <tr>
        <td><div class=\"switch ${acc.enabled ? 'on' : ''}\" onclick=\"toggleAccount('${acc.id}', ${acc.enabled ? 'true' : 'false'})\"></div></td>
        <td>
          <div>${esc(acc.label || acc.id)}</div>
          <div style=\"font-size:11px;color:#94a3b8\">${esc(acc.id)}</div>
        </td>
        <td><input style=\"width:80px\" type=\"number\" min=\"0\" value=\"${acc.max_concurrency}\" onchange=\"setConcurrency('${acc.id}', this.value)\" /></td>
        <td><span class=\"code\">${esc(expiry)}</span></td>
        <td>${refreshTag(acc)}<div style=\"font-size:11px;color:#94a3b8;margin-top:2px\">${refreshHint}</div></td>
        <td><button class=\"sm danger\" onclick=\"deleteAccount('${acc.id}')\">删除</button></td>
      </tr>
    `;
  }).join('');
}

function renderProbeReport(report){
  const summaryEl = $('probeSummary');
  const tableEl = $('probeTable');
  const tbodyEl = $('probeTbody');
  const rows = report?.results || [];
  if (!rows.length) {
    summaryEl.textContent = '未拿到自检结果';
    tableEl.style.display = 'none';
    tbodyEl.innerHTML = '';
    return;
  }

  tableEl.style.display = 'table';
  tbodyEl.innerHTML = rows.map((row) => {
    const ok = Boolean(row.ok);
    return `
      <tr>
        <td><span class=\"code\">${esc(row.model_request || '')}</span></td>
        <td><span class=\"code\">${esc(row.model_response || '-')}</span></td>
        <td>${boolTag(Boolean(row.model_match))}</td>
        <td>${boolTag(Boolean(row.has_reasoning))}</td>
        <td><span class=\"code\">${esc(row.latency_ms ?? '-')} ms</span></td>
        <td>${ok ? '<span class=\"tag ok\">通过</span>' : `<span class=\"tag err\">失败</span>${row.error ? `<div style=\"font-size:11px;color:#fecdd3;margin-top:2px\">${esc(row.error)}</div>` : ''}`}</td>
      </tr>
    `;
  }).join('');

  const pass = rows.filter((row) => row.ok).length;
  const all = rows.length;
  const checkedAt = report.checked_at || '';
  summaryEl.innerHTML = `最近一次自检：<span class=\"code\">${esc(checkedAt)}</span>，通过 <span class=\"code\">${pass}/${all}</span>`;
}

function setAutoRefresh(enabled){
  autoRefreshEnabled = Boolean(enabled);
  if (autoRefreshTimer) {
    clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
  const btn = $('btnAutoRefresh');
  if (!btn) return;

  if (!autoRefreshEnabled) {
    btn.textContent = '开启自动刷新';
    btn.classList.add('alt');
    log('已关闭自动刷新');
    return;
  }

  const seconds = Number($('autoRefreshSeconds')?.value || 20);
  autoRefreshTimer = setInterval(() => {
    refreshState().catch(() => {});
  }, Math.max(5, seconds) * 1000);
  btn.textContent = `停止自动刷新（${seconds}s）`;
  btn.classList.remove('alt');
  log(`已开启自动刷新：每 ${seconds} 秒`);
}

async function refreshProfiles(){
  try {
    const data = await api('/ui/api/edge/profiles');
    const profileEl = $('profile');
    profileEl.innerHTML = '';
    for (const profile of (data.profiles || [])) {
      const op = document.createElement('option');
      op.value = profile.directory;
      op.textContent = `${profile.name} (${profile.directory})`;
      profileEl.appendChild(op);
    }
    log('已刷新 Edge Profile 列表');
  } catch (error) {
    log(`刷新 Profile 失败：${error}`);
    toast('刷新 Profile 失败');
  }
}

async function refreshState(){
  try {
    const state = await api('/ui/api/state');
    setStatus(Boolean(state.iflow_logged_in), state.iflow_logged_in ? '账号可用' : '未配置可用账号');
    renderRenew(state);
    renderClient(state);
    renderOpenCode(state);
    renderAccounts(state);
  } catch (error) {
    setStatus(false, '状态获取失败');
    log(`刷新状态失败：${error}`);
  }
}

async function toggleAccount(id, enabled){
  try {
    await api(`/ui/api/accounts/${id}`, { method: 'POST', body: JSON.stringify({ enabled: !enabled }) });
    log(`账号 ${id} 已${enabled ? '禁用' : '启用'}`);
    await refreshState();
  } catch (error) {
    log(`切换账号失败：${error}`);
  }
}

async function setConcurrency(id, value){
  try {
    await api(`/ui/api/accounts/${id}`, { method: 'POST', body: JSON.stringify({ max_concurrency: Number(value || 0) }) });
    log(`账号 ${id} 并发更新为 ${value}`);
  } catch (error) {
    log(`更新并发失败：${error}`);
  }
}

async function deleteAccount(id){
  if (!confirm(`确认删除账号 ${id} 吗？`)) return;
  try {
    await api(`/ui/api/accounts/${id}`, { method: 'DELETE' });
    log(`账号 ${id} 已删除`);
    await refreshState();
  } catch (error) {
    log(`删除失败：${error}`);
  }
}

async function startOAuth(inprivate){
  try {
    const payload = {
      profile_directory: inprivate ? null : ($('profile').value || null),
      inprivate: Boolean(inprivate),
      open_browser: true,
      max_concurrency: Number($('concurrency').value || 0),
      label: $('label').value || null,
    };
    const result = await api('/ui/api/oauth/start', { method: 'POST', body: JSON.stringify(payload) });
    log(result.opened ? '已打开登录页面，请完成授权。' : '无法自动打开浏览器，请手动访问授权链接。');
    log(`授权链接：${result.auth_url}`);
    toast('OAuth 已启动');
  } catch (error) {
    log(`OAuth 启动失败：${error}`);
    toast('OAuth 启动失败');
  }
}

async function applyStrategy(){
  try {
    const strategy = $('strategy').value;
    await api('/ui/api/client-config', { method: 'POST', body: JSON.stringify({ strategy, regenerate_key: false }) });
    log(`路由策略已更新为 ${strategy}`);
    await refreshState();
  } catch (error) {
    log(`更新策略失败：${error}`);
  }
}

async function regenerateKey(){
  if (!confirm('确认重置本地 API Key？旧 Key 将失效。')) return;
  try {
    await api('/ui/api/client-config', { method: 'POST', body: JSON.stringify({ regenerate_key: true }) });
    log('本地 API Key 已重置并同步路由');
    await refreshState();
  } catch (error) {
    log(`重置 API Key 失败：${error}`);
  }
}

async function syncOpenCode(){
  try {
    const payload = {
      provider_name: $('provider').value || 'iflow',
      default_model: $('model').value || null,
      small_model: $('small').value || null,
      set_default_model: true,
      set_small_model: true,
      create_backup: true,
    };
    const result = await api('/ui/api/opencode/sync', { method: 'POST', body: JSON.stringify(payload) });
    const ok = (result.updated || []).length;
    const fail = (result.failed || []).length;
    log(`OpenCode 同步完成：成功 ${ok}，失败 ${fail}`);
    for (const item of (result.failed || [])) {
      log(`同步失败：${item.path} -> ${item.error}`);
    }
    toast(fail ? `部分失败(${fail})` : `已同步 ${ok} 个配置`);
    await refreshState();
  } catch (error) {
    log(`OpenCode 同步失败：${error}`);
  }
}

async function refreshNow(){
  try {
    await api('/ui/api/oauth/refresh-now', { method: 'POST', body: '{}' });
    log('已触发一次立即续期检查');
    await refreshState();
    toast('续期检查已执行');
  } catch (error) {
    log(`立即续期检查失败：${error}`);
  }
}

async function probeModels(){
  const btn = $('btnProbeModels');
  if (btn) btn.disabled = true;
  try {
    const report = await api('/ui/api/models/probe', { method: 'POST', body: '{}' });
    renderProbeReport(report);
    const okCount = (report.results || []).filter((row) => row.ok).length;
    const total = (report.results || []).length;
    const text = `三模型自检完成：${okCount}/${total} 通过`;
    log(text);
    toast(text);
  } catch (error) {
    log(`三模型自检失败：${error}`);
    toast('三模型自检失败');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function toggleAutoRefresh(){
  setAutoRefresh(!autoRefreshEnabled);
}

$('btnProfiles').onclick = refreshProfiles;
$('btnOauth').onclick = () => startOAuth(false);
$('btnPrivate').onclick = () => startOAuth(true);
$('btnStrategy').onclick = applyStrategy;
$('btnKey').onclick = regenerateKey;
$('btnSync').onclick = syncOpenCode;
$('btnRefresh').onclick = refreshState;
$('btnRefreshNow').onclick = refreshNow;
$('btnProbeModels').onclick = probeModels;
$('btnAutoRefresh').onclick = toggleAutoRefresh;
$('autoRefreshSeconds').onchange = () => {
  if (autoRefreshEnabled) setAutoRefresh(true);
};

(async () => {
  log('控制台已就绪');
  await refreshProfiles();
  await refreshState();
})();
</script>
</body>
</html>
"""


class OAuthStartRequest(BaseModel):
    profile_directory: Optional[str] = None
    inprivate: bool = False
    open_browser: bool = True
    max_concurrency: int = Field(default=4, ge=0)
    label: Optional[str] = None


class AccountUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    max_concurrency: Optional[int] = Field(default=None, ge=0)
    label: Optional[str] = None


class ClientConfigRequest(BaseModel):
    strategy: Optional[str] = None
    regenerate_key: bool = False


class OpenCodeSyncRequest(BaseModel):
    paths: Optional[list[str]] = None
    provider_name: str = "iflow"
    set_default_model: bool = True
    default_model: Optional[str] = None
    set_small_model: bool = True
    small_model: Optional[str] = None
    create_backup: bool = True


@router.get("/ui", response_class=HTMLResponse)
async def ui_index(request: Request):
    _require_ui_allowed(request)
    return HTMLResponse(UI_HTML, headers={"Cache-Control": "no-store"})


@router.get("/ui/api/edge/profiles")
async def ui_edge_profiles(request: Request):
    _require_ui_allowed(request)
    profiles = [{"directory": p.directory, "name": p.name} for p in list_edge_profiles()]
    if not profiles:
        profiles = [{"directory": "Default", "name": "Default"}]
    return {"profiles": profiles}


@router.get("/ui/api/state")
async def ui_state(request: Request):
    _require_ui_allowed(request)

    try:
        base_url = str(request.base_url).rstrip("/") + "/v1"
    except Exception:
        base_url = "http://127.0.0.1:8000/v1"

    settings = load_settings()
    routing = _load_routing_safely()
    client_api_key, client_strategy = _ensure_local_client_key()

    accounts = []
    now = datetime.now(timezone.utc)
    for account_id, account in routing.accounts.items():
        exp_min: Optional[int] = None
        if account.oauth_expires_at is not None:
            try:
                exp = account.oauth_expires_at
                exp_utc = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
                exp_min = int((exp_utc - now).total_seconds() // 60)
            except Exception:
                exp_min = None

        accounts.append(
            {
                "id": account_id,
                "label": account.label,
                "enabled": bool(account.enabled),
                "max_concurrency": int(account.max_concurrency or 0),
                "api_key_mask": f"...{account.api_key[-4:]}" if account.api_key else "",
                "oauth_refresh_token": bool(account.oauth_refresh_token),
                "oauth_expires_in_minutes": exp_min,
                "oauth_expires_human": _humanize_minutes(exp_min),
                "last_refresh_at": account.last_refresh_at,
                "refresh_failures": int(getattr(account, "refresh_failures", 0) or 0),
                "last_refresh_error": getattr(account, "last_refresh_error", None),
            }
        )

    iflow_logged_in = bool(routing.accounts)
    if not iflow_logged_in:
        try:
            from .config import check_iflow_login

            iflow_logged_in = bool(check_iflow_login())
        except Exception:
            iflow_logged_in = False

    default_model, small_model = _pick_models(
        preferred_default=settings.opencode_default_model,
        preferred_small=settings.opencode_small_model,
    )

    return {
        "iflow_logged_in": iflow_logged_in,
        "base_url": base_url,
        "client_api_key": client_api_key,
        "client_api_key_mask": _mask_secret(client_api_key, show=6),
        "client_strategy": client_strategy,
        "accounts_total": len(routing.accounts),
        "accounts_enabled": sum(1 for account in routing.accounts.values() if account.enabled),
        "oauth_accounts": sum(1 for account in routing.accounts.values() if bool(account.oauth_refresh_token)),
        "accounts": sorted(accounts, key=lambda item: item["id"]),
        "recommended_models": _recommended_model_ids(),
        "opencode_paths": [str(path) for path in discover_config_paths(settings.opencode_config_path)],
        "opencode_provider_name": settings.opencode_provider_name or "iflow",
        "opencode_default_model": default_model,
        "opencode_small_model": small_model,
        "auto_refresh_check_interval_seconds": DEFAULT_REFRESH_CHECK_INTERVAL_SECONDS,
        "auto_refresh_buffer_seconds": DEFAULT_REFRESH_BUFFER_SECONDS,
    }


@router.post("/ui/api/oauth/refresh-now")
async def ui_oauth_refresh_now(request: Request):
    _require_ui_allowed(request)
    refresher = RoutingOAuthRefresher(log=None)
    await asyncio.to_thread(refresher.refresh_once)
    return {"ok": True}


@router.post("/ui/api/models/probe")
async def ui_probe_models(request: Request):
    _require_ui_allowed(request)
    client_api_key, _ = _ensure_local_client_key()
    model_ids = _recommended_model_ids()

    try:
        chat_url = str(request.base_url).rstrip("/") + "/v1/chat/completions"
    except Exception:
        chat_url = "http://127.0.0.1:8000/v1/chat/completions"

    headers = {"Authorization": f"Bearer {client_api_key}", "Content-Type": "application/json"}
    results: list[dict] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(45.0, connect=8.0),
        follow_redirects=True,
    ) as client:
        for model_id in model_ids:
            started = time.perf_counter()
            try:
                response = await client.post(
                    chat_url,
                    headers=headers,
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": "只回复 OK"}],
                        "stream": False,
                        "max_tokens": 80,
                    },
                )
                latency_ms = int((time.perf_counter() - started) * 1000)

                payload: dict = {}
                try:
                    payload = response.json()
                except Exception:
                    payload = {}

                if response.status_code >= 400:
                    err_msg = ""
                    if isinstance(payload, dict):
                        err_msg = str(payload.get("detail") or payload.get("msg") or payload.get("message") or "").strip()
                    if not err_msg:
                        err_msg = (response.text or "").strip()[:180] or f"HTTP {response.status_code}"
                    results.append(
                        {
                            "model_request": model_id,
                            "model_response": None,
                            "model_match": False,
                            "has_reasoning": False,
                            "latency_ms": latency_ms,
                            "ok": False,
                            "error": err_msg,
                        }
                    )
                    continue

                model_response = payload.get("model") if isinstance(payload, dict) else None
                message: dict = {}
                if isinstance(payload, dict):
                    choices = payload.get("choices")
                    if isinstance(choices, list) and choices:
                        first = choices[0] if isinstance(choices[0], dict) else {}
                        msg = first.get("message")
                        if isinstance(msg, dict):
                            message = msg

                has_reasoning = bool(message.get("reasoning_content") or message.get("reasoning"))
                model_match = bool(isinstance(model_response, str) and model_response == model_id)
                results.append(
                    {
                        "model_request": model_id,
                        "model_response": model_response,
                        "model_match": model_match,
                        "has_reasoning": has_reasoning,
                        "latency_ms": latency_ms,
                        "ok": bool(model_match and has_reasoning),
                        "error": None,
                    }
                )
            except Exception as ex:
                latency_ms = int((time.perf_counter() - started) * 1000)
                results.append(
                    {
                        "model_request": model_id,
                        "model_response": None,
                        "model_match": False,
                        "has_reasoning": False,
                        "latency_ms": latency_ms,
                        "ok": False,
                        "error": f"{type(ex).__name__}: {ex}",
                    }
                )

    return {
        "ok": all(bool(item.get("ok")) for item in results) if results else False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }


@router.post("/ui/api/client-config")
async def ui_client_config(request: Request, body: ClientConfigRequest):
    _require_ui_allowed(request)
    settings = load_settings()
    old_key = settings.client_api_key

    if body.regenerate_key:
        settings.client_api_key = generate_client_key()
    if body.strategy is not None:
        settings.client_strategy = _normalize_strategy(body.strategy)

    if not settings.client_api_key:
        settings.client_api_key = generate_client_key()
    settings.client_strategy = _normalize_strategy(settings.client_strategy)
    save_settings(settings)

    routing = _load_routing_safely()
    if old_key and old_key != settings.client_api_key and old_key in routing.keys:
        del routing.keys[old_key]
    _sync_client_route(
        routing,
        token=settings.client_api_key,
        strategy=settings.client_strategy,
    )
    save_keys_config(routing)

    return {
        "ok": True,
        "client_api_key": settings.client_api_key,
        "client_api_key_mask": _mask_secret(settings.client_api_key, show=6),
        "client_strategy": settings.client_strategy,
    }


@router.post("/ui/api/opencode/sync")
async def ui_sync_opencode(request: Request, body: OpenCodeSyncRequest):
    _require_ui_allowed(request)

    settings = load_settings()
    client_api_key, client_strategy = _ensure_local_client_key()
    provider_name = (body.provider_name or settings.opencode_provider_name or "iflow").strip() or "iflow"

    default_model, small_model = _pick_models(
        preferred_default=body.default_model or settings.opencode_default_model,
        preferred_small=body.small_model or settings.opencode_small_model,
    )

    targets: list[Path] = []
    if body.paths:
        for raw in body.paths:
            path = Path(raw).expanduser()
            if path.exists() and path not in targets:
                targets.append(path)
    if not targets:
        targets = discover_config_paths(settings.opencode_config_path)

    if not targets:
        raise HTTPException(status_code=404, detail="未发现 OpenCode 配置文件")

    settings.opencode_provider_name = provider_name
    settings.opencode_set_default_model = bool(body.set_default_model)
    settings.opencode_default_model = default_model
    settings.opencode_set_small_model = bool(body.set_small_model)
    settings.opencode_small_model = small_model
    settings.opencode_config_path = str(targets[0])
    save_settings(settings)

    base_url = f"http://127.0.0.1:{int(settings.port or 8000)}/v1"
    updated = []
    failed = []
    for target in targets:
        try:
            result = ensure_iflow_provider(
                config_path=target,
                provider_name=provider_name,
                base_url=base_url,
                api_key=client_api_key,
                set_default_model=settings.opencode_set_default_model,
                default_model=settings.opencode_default_model,
                set_small_model=settings.opencode_set_small_model,
                small_model=settings.opencode_small_model,
                create_backup=bool(body.create_backup),
            )
            updated.append(
                {
                    "path": str(result.path),
                    "backup_path": str(result.backup_path) if result.backup_path else None,
                }
            )
        except Exception as ex:
            failed.append({"path": str(target), "error": str(ex)})

    routing = _load_routing_safely()
    _sync_client_route(routing, token=client_api_key, strategy=client_strategy)
    save_keys_config(routing)

    return {
        "ok": not failed,
        "updated": updated,
        "failed": failed,
        "provider_name": provider_name,
        "base_url": base_url,
        "client_api_key_mask": _mask_secret(client_api_key, show=6),
        "default_model": settings.opencode_default_model,
        "small_model": settings.opencode_small_model,
    }


@router.post("/ui/api/oauth/start")
async def ui_oauth_start(request: Request, body: OAuthStartRequest):
    _require_ui_allowed(request)
    _cleanup_pending()

    if get_routing_file_path_in_use() is None:
        raise HTTPException(status_code=400, detail="keys.json 来自环境变量，当前无法通过 UI 写入账号")

    callback_url = str(request.url_for("iflow2api_ui_oauth_callback"))
    state = secrets.token_urlsafe(16)

    settings = load_settings()
    pending = _PendingOAuth(
        created_at=time.time(),
        redirect_uri=callback_url,
        profile_directory=(body.profile_directory or "").strip() or None,
        inprivate=bool(body.inprivate),
        max_concurrency=max(0, int(body.max_concurrency or 0)),
        label_override=(body.label or "").strip() or None,
        base_url=(settings.base_url or "https://apis.iflow.cn/v1").rstrip("/"),
    )
    with _LOCK:
        _PENDING[state] = pending

    oauth = IFlowOAuth()
    auth_url = oauth.get_auth_url(redirect_uri=callback_url, state=state)
    opened = False
    if body.open_browser:
        opened = launch_edge(
            auth_url,
            profile_directory=pending.profile_directory,
            inprivate=pending.inprivate,
            new_window=True,
        )
        if not opened:
            try:
                import webbrowser

                opened = webbrowser.open(auth_url)
            except Exception:
                opened = False

    return {
        "state": state,
        "auth_url": auth_url,
        "callback_url": callback_url,
        "opened": bool(opened),
    }


@router.get("/ui/oauth/callback", name="iflow2api_ui_oauth_callback", response_class=HTMLResponse)
async def ui_oauth_callback(request: Request):
    _require_ui_allowed(request)

    code = (request.query_params.get("code") or "").strip()
    error = (request.query_params.get("error") or "").strip()
    state = (request.query_params.get("state") or "").strip()

    if error:
        return HTMLResponse(
            _simple_result_html("登录失败", error, ok=False),
            headers={"Cache-Control": "no-store"},
        )
    if not code or not state:
        return HTMLResponse(
            _simple_result_html("回调参数无效", "缺少 code 或 state", ok=False),
            headers={"Cache-Control": "no-store"},
        )

    pending = _PENDING.pop(state, None)
    if not pending:
        return HTMLResponse(
            _simple_result_html("请求已过期", "请返回控制台重新发起 OAuth", ok=False),
            headers={"Cache-Control": "no-store"},
        )

    oauth = IFlowOAuth()
    try:
        token_data = await oauth.get_token(code, redirect_uri=pending.redirect_uri)
        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")
        expires_at = token_data.get("expires_at")

        user_info = await oauth.get_user_info(access_token)
        api_key = user_info.get("apiKey") or user_info.get("searchApiKey")
        if not api_key:
            raise ValueError("OAuth 返回未包含 apiKey")

        routing = _load_routing_safely()
        result = add_upstream_account(
            routing,
            api_key=api_key,
            base_url=pending.base_url,
            max_concurrency=pending.max_concurrency,
            label=pending.label_override,
            auth_type="oauth-iflow",
            oauth_access_token=access_token,
            oauth_refresh_token=refresh_token,
            oauth_expires_at=expires_at,
        )

        client_api_key, client_strategy = _ensure_local_client_key()
        _sync_client_route(
            routing,
            token=client_api_key,
            strategy=client_strategy,
        )
        save_keys_config(routing)

        return HTMLResponse(
            _simple_result_html("登录成功", f"账号 {result.account_id} 已加入账号池，后续将自动续期。", ok=True),
            headers={"Cache-Control": "no-store"},
        )
    except Exception as ex:
        return HTMLResponse(
            _simple_result_html("登录处理失败", str(ex), ok=False),
            headers={"Cache-Control": "no-store"},
        )
    finally:
        await oauth.close()


@router.post("/ui/api/accounts/{account_id}")
async def ui_update_account(request: Request, account_id: str, body: AccountUpdateRequest):
    _require_ui_allowed(request)
    routing = _load_routing_safely()
    if account_id not in routing.accounts:
        raise HTTPException(status_code=404, detail="账号不存在")

    account = routing.accounts[account_id]
    if body.enabled is not None:
        account.enabled = body.enabled
    if body.max_concurrency is not None:
        account.max_concurrency = body.max_concurrency
    if body.label is not None:
        account.label = body.label

    client_api_key, client_strategy = _ensure_local_client_key()
    _sync_client_route(routing, token=client_api_key, strategy=client_strategy)
    save_keys_config(routing)
    return {"ok": True}


@router.delete("/ui/api/accounts/{account_id}")
async def ui_delete_account(request: Request, account_id: str):
    _require_ui_allowed(request)
    routing = _load_routing_safely()
    if account_id not in routing.accounts:
        raise HTTPException(status_code=404, detail="账号不存在")

    del routing.accounts[account_id]

    for key, route in list(routing.keys.items()):
        if route.account == account_id:
            del routing.keys[key]
            continue
        if route.accounts and account_id in route.accounts:
            route.accounts = [candidate for candidate in route.accounts if candidate != account_id]

    if routing.default:
        if routing.default.account == account_id:
            routing.default = None
        elif routing.default.accounts and account_id in routing.default.accounts:
            routing.default.accounts = [
                candidate for candidate in routing.default.accounts if candidate != account_id
            ]

    client_api_key, client_strategy = _ensure_local_client_key()
    _sync_client_route(routing, token=client_api_key, strategy=client_strategy)
    save_keys_config(routing)
    return {"ok": True}

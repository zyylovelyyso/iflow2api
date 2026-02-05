"""Local Web UI for managing iflow2api (accounts, OAuth login, status).

Design goals:
- No external frontend build toolchain.
- Safe-by-default: UI API is only available on localhost unless explicitly allowed.
- Works even when there is no valid iFlow login yet (so you can login via UI).
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .edge import list_edge_profiles, launch_edge
from .keys_store import (
    add_upstream_account,
    ensure_opencode_route,
    generate_client_key,
    load_keys_config,
    save_keys_config,
)
from .oauth import IFlowOAuth
from .routing import KeyRoutingConfig
from .routing import get_routing_file_path_in_use
from .settings import load_settings, save_settings


router = APIRouter()


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
        raise HTTPException(status_code=403, detail="Web UI is only available on localhost")


def _mask_secret(value: str, *, show: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= show:
        return "*" * len(value)
    return f"{'*' * (len(value) - show)}{value[-show:]}"


def _ensure_local_client_key() -> tuple[str, str]:
    settings = load_settings()
    if not settings.client_api_key:
        settings.client_api_key = generate_client_key()
        save_settings(settings)
    if not settings.client_strategy:
        settings.client_strategy = "least_busy"
        save_settings(settings)
    return settings.client_api_key, settings.client_strategy


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
    ttl = 15 * 60  # 15 minutes
    with _LOCK:
        for state in list(_PENDING.keys()):
            p = _PENDING.get(state)
            if not p:
                _PENDING.pop(state, None)
                continue
            if now - float(p.created_at) > ttl:
                _PENDING.pop(state, None)


UI_HTML = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>iflow2api 控制台</title>
    <style>
      :root {
        --bg: #0b0f17;
        --card: #121a2a;
        --muted: #9db0c8;
        --text: #e6eef8;
        --accent: #4aa3ff;
        --good: #2ecc71;
        --warn: #f39c12;
        --bad: #e74c3c;
        --border: rgba(255,255,255,.10);
      }
      body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; background: var(--bg); color: var(--text); }
      a { color: var(--accent); text-decoration: none; }
      .wrap { max-width: 980px; margin: 24px auto; padding: 0 16px; }
      .top { display:flex; align-items:center; justify-content:space-between; gap:16px; }
      .title { font-size: 18px; font-weight: 700; }
      .pill { padding: 6px 10px; border-radius: 999px; background: rgba(255,255,255,.06); border: 1px solid var(--border); color: var(--muted); font-size: 12px; }
      .grid { display:grid; grid-template-columns: 1fr; gap: 14px; margin-top: 14px; }
      @media (min-width: 900px) { .grid { grid-template-columns: 1.2fr .8fr; } }
      .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 14px; }
      .card h2 { margin: 0 0 10px 0; font-size: 14px; color: #cfe0f7; }
      .row { display:flex; gap: 10px; align-items:center; flex-wrap: wrap; }
      .col { display:flex; flex-direction: column; gap: 8px; }
      label { font-size: 12px; color: var(--muted); }
      input, select { background: rgba(0,0,0,.25); border: 1px solid var(--border); color: var(--text); border-radius: 10px; padding: 10px 10px; outline: none; min-width: 220px; }
      input[type=number] { min-width: 120px; }
      button { border: 1px solid var(--border); background: rgba(255,255,255,.06); color: var(--text); border-radius: 10px; padding: 10px 12px; cursor: pointer; }
      button.primary { background: rgba(74,163,255,.18); border-color: rgba(74,163,255,.35); }
      button.danger { background: rgba(231,76,60,.18); border-color: rgba(231,76,60,.35); }
      button:disabled { opacity: .5; cursor: not-allowed; }
      .kv { display:grid; grid-template-columns: 160px 1fr; gap: 8px; font-size: 12px; color: var(--muted); }
      .kv div:nth-child(2n) { color: var(--text); overflow-wrap: anywhere; }
      table { width: 100%; border-collapse: collapse; font-size: 12px; }
      th, td { padding: 10px 8px; border-top: 1px solid var(--border); vertical-align: top; }
      th { text-align: left; color: var(--muted); font-weight: 600; }
      .tag { display:inline-block; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); font-size: 11px; }
      .tag.good { color: var(--good); border-color: rgba(46,204,113,.35); background: rgba(46,204,113,.10); }
      .tag.warn { color: var(--warn); border-color: rgba(243,156,18,.35); background: rgba(243,156,18,.10); }
      .tag.bad { color: var(--bad); border-color: rgba(231,76,60,.35); background: rgba(231,76,60,.10); }
      .log { height: 160px; overflow:auto; background: rgba(0,0,0,.20); border: 1px solid var(--border); border-radius: 12px; padding: 10px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: #cfe0f7; }
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="top">
        <div class="title">iflow2api 控制台</div>
        <div class="pill" id="statusPill">加载中...</div>
      </div>

      <div class="grid">
        <div class="card">
          <h2>账号池（多账号负载均衡）</h2>
          <div class="row" style="margin-bottom: 10px;">
            <div class="col">
              <label>Edge Profile（每个账号建议一个 Profile）</label>
              <select id="edgeProfile"></select>
            </div>
            <div class="col">
              <label>并发上限</label>
              <input id="maxConcurrency" type="number" min="0" value="4" />
            </div>
            <div class="col">
              <label>标签（可选）</label>
              <input id="labelOverride" placeholder="比如：手机号/昵称" />
            </div>
          </div>
          <div class="row">
            <button class="primary" id="btnAdd">添加账号（Edge Profile OAuth）</button>
            <button id="btnAddInPrivate">InPrivate 临时登录</button>
            <button id="btnRefresh">刷新状态</button>
          </div>
          <div style="height: 10px"></div>
          <table>
            <thead>
              <tr>
                <th>启用</th>
                <th>账号</th>
                <th>Key</th>
                <th>并发</th>
                <th>Auth</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody id="accountsBody"></tbody>
          </table>
        </div>

        <div class="card">
          <h2>OpenCode / 客户端接入</h2>
          <div class="kv" id="kv"></div>
          <div style="height: 10px"></div>
          <h2>日志</h2>
          <div class="log" id="log"></div>
        </div>
      </div>
    </div>

    <script>
      const logEl = document.getElementById("log");
      const statusPill = document.getElementById("statusPill");
      const kv = document.getElementById("kv");
      const accountsBody = document.getElementById("accountsBody");
      const edgeProfile = document.getElementById("edgeProfile");

      function log(line) {
        const ts = new Date().toLocaleTimeString();
        logEl.textContent += `[${ts}] ${line}\\n`;
        logEl.scrollTop = logEl.scrollHeight;
      }

      function tag(text, kind) {
        return `<span class="tag ${kind||""}">${text}</span>`;
      }

      async function jget(url) {
        const r = await fetch(url, { headers: { "Accept": "application/json" }});
        if (!r.ok) throw new Error(await r.text());
        return await r.json();
      }

      async function jpost(url, body) {
        const r = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify(body || {}),
        });
        if (!r.ok) throw new Error(await r.text());
        return await r.json();
      }

      async function jdel(url) {
        const r = await fetch(url, { method: "DELETE" });
        if (!r.ok) throw new Error(await r.text());
        return await r.json();
      }

      function renderKV(state) {
        const baseURL = state.base_url || "";
        const apiKey = state.client_api_key || "";
        kv.innerHTML = `
          <div>Base URL</div><div><code>${baseURL}</code></div>
          <div>API Key（本地）</div><div><code>${apiKey}</code></div>
          <div>策略</div><div><code>${state.client_strategy}</code></div>
          <div>账号池</div><div>${state.accounts_enabled}/${state.accounts_total} · oauth ${state.oauth_accounts}</div>
          <div>提示</div><div>这个 Key 是本地网关用的，不是 iFlow/OpenAI Key。把它填进 OpenCode 的 provider=iflow 即可。</div>
        `;
      }

      function renderAccounts(state) {
        accountsBody.innerHTML = "";
        for (const a of state.accounts || []) {
          const auth = a.oauth_refresh_token ? `oauth · exp ${a.oauth_expires_in_minutes ?? "?"}m` : "api-key";
          const authTag = a.oauth_refresh_token ? tag(auth, (a.oauth_expires_in_minutes !== null && a.oauth_expires_in_minutes <= 5) ? "warn" : "good") : tag(auth, "");
          const enabledChecked = a.enabled ? "checked" : "";
          accountsBody.innerHTML += `
            <tr>
              <td><input type="checkbox" ${enabledChecked} onchange="window._toggle('${a.id}', this.checked)"></td>
              <td>
                <div>${a.label || a.id}</div>
                <div style="color:#9db0c8; font-size:11px">${a.id}</div>
              </td>
              <td><code>${a.api_key_mask || ""}</code></td>
              <td>
                <input type="number" min="0" value="${a.max_concurrency}" style="width:90px"
                  onchange="window._setConc('${a.id}', this.value)">
              </td>
              <td>${authTag}</td>
              <td>
                <button class="danger" onclick="window._del('${a.id}')">删除</button>
              </td>
            </tr>
          `;
        }
      }

      window._toggle = async (id, enabled) => {
        try {
          await jpost(`/ui/api/accounts/${id}`, { enabled });
          log(`账号 ${id} enabled=${enabled}`);
        } catch (e) {
          log(`更新失败: ${e}`);
        } finally {
          await refresh();
        }
      };
      window._setConc = async (id, maxConcurrency) => {
        try {
          await jpost(`/ui/api/accounts/${id}`, { max_concurrency: Number(maxConcurrency || 0) });
          log(`账号 ${id} max_concurrency=${maxConcurrency}`);
        } catch (e) {
          log(`更新失败: ${e}`);
        } finally {
          await refresh();
        }
      };
      window._del = async (id) => {
        if (!confirm(`确定删除账号 ${id} ?`)) return;
        try {
          await jdel(`/ui/api/accounts/${id}`);
          log(`已删除账号 ${id}`);
        } catch (e) {
          log(`删除失败: ${e}`);
        } finally {
          await refresh();
        }
      };

      async function refreshProfiles() {
        try {
          const ps = await jget("/ui/api/edge/profiles");
          edgeProfile.innerHTML = "";
          for (const p of ps.profiles || []) {
            const opt = document.createElement("option");
            opt.value = p.directory;
            opt.textContent = `${p.name} (${p.directory})`;
            edgeProfile.appendChild(opt);
          }
        } catch (e) {
          log(`读取 Edge Profiles 失败: ${e}`);
        }
      }

      async function refresh() {
        try {
          const state = await jget("/ui/api/state");
          statusPill.textContent = state.iflow_logged_in ? "已就绪" : "未登录/未配置";
          statusPill.style.color = state.iflow_logged_in ? "#2ecc71" : "#f39c12";
          renderKV(state);
          renderAccounts(state);
        } catch (e) {
          statusPill.textContent = "加载失败";
          statusPill.style.color = "#e74c3c";
          log(`刷新失败: ${e}`);
        }
      }

      document.getElementById("btnRefresh").onclick = refresh;
      document.getElementById("btnAdd").onclick = async () => {
        const profileDirectory = edgeProfile.value || null;
        const maxConcurrency = Number(document.getElementById("maxConcurrency").value || 0);
        const labelOverride = document.getElementById("labelOverride").value || null;
        try {
          const r = await jpost("/ui/api/oauth/start", { profile_directory: profileDirectory, max_concurrency: maxConcurrency, label: labelOverride, inprivate: false, open_browser: true });
          log(r.opened ? "已打开 Edge 登录窗口" : "未能自动打开 Edge，请复制链接手动打开");
          log(`授权链接: ${r.auth_url}`);
        } catch (e) {
          log(`启动 OAuth 失败: ${e}`);
        }
      };
      document.getElementById("btnAddInPrivate").onclick = async () => {
        const maxConcurrency = Number(document.getElementById("maxConcurrency").value || 0);
        const labelOverride = document.getElementById("labelOverride").value || null;
        try {
          const r = await jpost("/ui/api/oauth/start", { profile_directory: null, max_concurrency: maxConcurrency, label: labelOverride, inprivate: true, open_browser: true });
          log(r.opened ? "已打开 Edge InPrivate 登录窗口" : "未能自动打开 Edge，请复制链接手动打开");
          log(`授权链接: ${r.auth_url}`);
        } catch (e) {
          log(`启动 OAuth 失败: ${e}`);
        }
      };

      (async () => {
        log("控制台加载完成");
        await refreshProfiles();
        await refresh();
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


@router.get("/ui", response_class=HTMLResponse)
async def ui_index(request: Request):
    _require_ui_allowed(request)
    return HTMLResponse(UI_HTML)


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

    # Base URL for OpenAI-compatible clients
    try:
        base_url = str(request.base_url).rstrip("/") + "/v1"
    except Exception:
        base_url = "http://127.0.0.1:8000/v1"

    # Load routing config from disk (works even if server has no valid iFlow login yet).
    try:
        routing = load_keys_config()
    except Exception:
        routing = KeyRoutingConfig()

    client_api_key, client_strategy = _ensure_local_client_key()

    accounts = []
    now = datetime.now(timezone.utc)
    for account_id, acc in routing.accounts.items():
        exp_min: Optional[int] = None
        if acc.oauth_expires_at is not None:
            try:
                exp = acc.oauth_expires_at
                exp_utc = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
                exp_min = int((exp_utc - now).total_seconds() // 60)
            except Exception:
                exp_min = None

        accounts.append(
            {
                "id": account_id,
                "label": acc.label,
                "enabled": bool(acc.enabled),
                "base_url": acc.base_url,
                "max_concurrency": int(acc.max_concurrency or 0),
                "api_key_mask": f"...{acc.api_key[-4:]}" if acc.api_key else "",
                "auth_type": acc.auth_type,
                "oauth_refresh_token": bool(acc.oauth_refresh_token),
                "oauth_expires_in_minutes": exp_min,
            }
        )

    # Determine "logged in" state: either has accounts or has ~/.iflow login.
    iflow_logged_in = bool(routing.accounts)
    if not iflow_logged_in:
        try:
            from .config import check_iflow_login

            iflow_logged_in = bool(check_iflow_login())
        except Exception:
            iflow_logged_in = False

    return {
        "iflow_logged_in": iflow_logged_in,
        "base_url": base_url,
        "client_api_key": client_api_key,
        "client_api_key_mask": _mask_secret(client_api_key, show=6),
        "client_strategy": client_strategy,
        "accounts_total": len(routing.accounts),
        "accounts_enabled": sum(1 for a in routing.accounts.values() if a.enabled),
        "oauth_accounts": sum(1 for a in routing.accounts.values() if bool(a.oauth_refresh_token)),
        "accounts": sorted(accounts, key=lambda x: x["id"]),
    }


@router.post("/ui/api/oauth/start")
async def ui_oauth_start(request: Request, body: OAuthStartRequest):
    _require_ui_allowed(request)
    _cleanup_pending()

    # Must have a writable routing file (env JSON cannot be persisted).
    routing_path = get_routing_file_path_in_use()
    if routing_path is None:
        raise HTTPException(status_code=400, detail="keys.json is provided via env; cannot add accounts via UI")

    # Redirect back to this server
    callback_url = str(request.url_for("iflow2api_ui_oauth_callback"))

    state = secrets.token_urlsafe(16)
    max_conc = int(body.max_concurrency or 0)
    if max_conc < 0:
        max_conc = 0

    settings = load_settings()
    base_url = (settings.base_url or "https://apis.iflow.cn/v1").rstrip("/")

    pending = _PendingOAuth(
        created_at=time.time(),
        redirect_uri=callback_url,
        profile_directory=(body.profile_directory or "").strip() or None,
        inprivate=bool(body.inprivate),
        max_concurrency=max_conc,
        label_override=(body.label or "").strip() or None,
        base_url=base_url,
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

    return {"state": state, "auth_url": auth_url, "callback_url": callback_url, "opened": bool(opened)}


@router.get("/ui/oauth/callback", name="iflow2api_ui_oauth_callback", response_class=HTMLResponse)
async def ui_oauth_callback(request: Request):
    _require_ui_allowed(request)

    code = (request.query_params.get("code") or "").strip()
    error = (request.query_params.get("error") or "").strip()
    state = (request.query_params.get("state") or "").strip()

    if error:
        return HTMLResponse(f"<h3>登录失败</h3><pre>{error}</pre>")
    if not state or not code:
        return HTMLResponse("<h3>登录失败</h3><pre>missing code/state</pre>")

    with _LOCK:
        pending = _PENDING.pop(state, None)
    if not pending:
        return HTMLResponse("<h3>登录失败</h3><pre>invalid/expired state</pre>")

    # Exchange code -> tokens -> user info -> apiKey
    oauth = IFlowOAuth()
    try:
        token_data = await oauth.get_token(code, redirect_uri=pending.redirect_uri)
        access_token = token_data.get("access_token") or ""
        user_info = await oauth.get_user_info(access_token)
        api_key = user_info.get("apiKey") or user_info.get("searchApiKey")
        if not api_key:
            raise ValueError("未能从 iFlow 获取 apiKey")
    finally:
        await oauth.close()

    # Persist into keys.json
    try:
        cfg = load_keys_config()
    except Exception:
        cfg = KeyRoutingConfig()

    client_api_key, client_strategy = _ensure_local_client_key()

    label = pending.label_override or user_info.get("username") or user_info.get("phone") or "iflow"
    add_upstream_account(
        cfg,
        api_key=api_key,
        base_url=pending.base_url,
        label=label,
        max_concurrency=pending.max_concurrency,
        auth_type="oauth-iflow",
        oauth_access_token=token_data.get("access_token"),
        oauth_refresh_token=token_data.get("refresh_token"),
        oauth_expires_at=token_data.get("expires_at"),
    )
    ensure_opencode_route(cfg, token=client_api_key, strategy=client_strategy)
    save_keys_config(cfg)

    # Friendly page that can be closed.
    return HTMLResponse(
        """
<!doctype html>
<html><head><meta charset="utf-8"><title>登录成功</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;background:#0b0f17;color:#e6eef8;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
  .card{background:#121a2a;border:1px solid rgba(255,255,255,.10);border-radius:14px;padding:22px;max-width:520px}
  .ok{color:#2ecc71;font-size:18px;font-weight:700;margin-bottom:8px}
  .muted{color:#9db0c8;font-size:13px}
  button{margin-top:14px;border:1px solid rgba(255,255,255,.10);background:rgba(255,255,255,.06);color:#e6eef8;border-radius:10px;padding:10px 12px;cursor:pointer}
</style>
</head>
<body>
  <div class="card">
    <div class="ok">登录成功，账号已加入账号池</div>
    <div class="muted">你可以关闭此页面，回到 iflow2api 控制台刷新查看账号。</div>
    <button onclick="window.close()">关闭</button>
  </div>
  <script>setTimeout(()=>{try{window.close()}catch(e){}}, 5000);</script>
</body></html>
"""
    )


@router.post("/ui/api/accounts/{account_id}")
async def ui_account_update(request: Request, account_id: str, body: AccountUpdateRequest):
    _require_ui_allowed(request)
    routing_path = get_routing_file_path_in_use()
    if routing_path is None:
        raise HTTPException(status_code=400, detail="keys.json is provided via env; cannot edit via UI")

    try:
        cfg = load_keys_config()
    except Exception:
        cfg = KeyRoutingConfig()

    acc = cfg.accounts.get(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")

    if body.enabled is not None:
        acc.enabled = bool(body.enabled)
    if body.max_concurrency is not None:
        acc.max_concurrency = int(body.max_concurrency)
    if body.label is not None:
        acc.label = (body.label or "").strip() or None

    # Keep default route updated.
    client_api_key, client_strategy = _ensure_local_client_key()
    ensure_opencode_route(cfg, token=client_api_key, strategy=client_strategy)
    save_keys_config(cfg)
    return {"ok": True}


@router.delete("/ui/api/accounts/{account_id}")
async def ui_account_delete(request: Request, account_id: str):
    _require_ui_allowed(request)
    routing_path = get_routing_file_path_in_use()
    if routing_path is None:
        raise HTTPException(status_code=400, detail="keys.json is provided via env; cannot edit via UI")

    try:
        cfg = load_keys_config()
    except Exception:
        cfg = KeyRoutingConfig()

    if account_id not in cfg.accounts:
        return {"ok": True}
    del cfg.accounts[account_id]

    client_api_key, client_strategy = _ensure_local_client_key()
    ensure_opencode_route(cfg, token=client_api_key, strategy=client_strategy)
    save_keys_config(cfg)
    return {"ok": True}


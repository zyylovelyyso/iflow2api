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
        --bg0: #05070d;
        --bg1: #0b1020;
        --text: #e7f0ff;
        --muted: rgba(231,240,255,0.66);
        --faint: rgba(231,240,255,0.42);
        --accent: #51d6ff;
        --accent2: #b6ff6f;
        --good: #2ecc71;
        --warn: #f5b942;
        --bad: #ff4d6d;
        --border: rgba(255,255,255,0.10);
        --border2: rgba(255,255,255,0.16);
        --shadow: 0 18px 50px rgba(0,0,0,.45);
        --radius: 16px;
      }
      * { box-sizing: border-box; }
      body {
        margin:0;
        min-height:100vh;
        color: var(--text);
        background: linear-gradient(180deg, var(--bg1), var(--bg0) 60%, #04060c 100%);
        font-family: "Segoe UI Variable Display", "Bahnschrift", "Segoe UI", system-ui, sans-serif;
        letter-spacing: 0.2px;
      }
      body::before {
        content:"";
        position: fixed;
        inset:0;
        pointer-events:none;
        background:
          radial-gradient(900px 520px at 12% 10%, rgba(81,214,255,.22), transparent 60%),
          radial-gradient(900px 520px at 88% 18%, rgba(182,255,111,.16), transparent 58%),
          radial-gradient(700px 520px at 70% 92%, rgba(170,120,255,.12), transparent 60%);
        mix-blend-mode: screen;
        opacity: 0.85;
      }
      body::after {
        content:"";
        position: fixed;
        inset:-180px;
        pointer-events:none;
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='220' height='220'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='220' height='220' filter='url(%23n)' opacity='.25'/%3E%3C/svg%3E");
        opacity: 0.08;
        mix-blend-mode: overlay;
      }
      a { color: var(--accent); text-decoration: none; }
      .wrap { max-width: 1040px; margin: 26px auto; padding: 0 16px 40px; }
      .hero {
        display:flex;
        align-items:flex-end;
        justify-content:space-between;
        gap: 16px;
        padding: 14px 14px 16px;
        border-radius: var(--radius);
        background: linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.03));
        border: 1px solid var(--border);
        box-shadow: var(--shadow);
        position: sticky;
        top: 10px;
        z-index: 5;
        backdrop-filter: blur(10px);
      }
      .brand { display:flex; gap: 12px; align-items:center; }
      .logo {
        width: 42px;
        height: 42px;
        border-radius: 14px;
        background: linear-gradient(135deg, rgba(81,214,255,.28), rgba(182,255,111,.14));
        border: 1px solid rgba(81,214,255,.35);
        box-shadow: 0 12px 30px rgba(81,214,255,.10);
        display:flex;
        align-items:center;
        justify-content:center;
        font-weight: 800;
        letter-spacing: .8px;
        color: #dff8ff;
      }
      .title { font-size: 18px; font-weight: 800; line-height: 1.1; }
      .subtitle { font-size: 12px; color: var(--muted); margin-top: 4px; }
      .heroRight { display:flex; align-items:center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
      .pill {
        display:flex;
        align-items:center;
        gap: 8px;
        padding: 7px 10px;
        border-radius: 999px;
        background: rgba(0,0,0,.22);
        border: 1px solid var(--border);
        color: var(--muted);
        font-size: 12px;
      }
      .dot { width: 8px; height: 8px; border-radius: 999px; background: var(--warn); box-shadow: 0 0 0 4px rgba(245,185,66,.12); }
      .pill.good .dot { background: var(--good); box-shadow: 0 0 0 4px rgba(46,204,113,.12); }
      .pill.bad .dot { background: var(--bad); box-shadow: 0 0 0 4px rgba(255,77,109,.12); }
      .grid { display:grid; grid-template-columns: 1fr; gap: 14px; margin-top: 14px; }
      @media (min-width: 980px) { .grid { grid-template-columns: 1.25fr .75fr; } }
      .card {
        background: linear-gradient(180deg, rgba(18,26,42,.82), rgba(18,26,42,.56));
        border: 1px solid var(--border);
        border-radius: var(--radius);
        padding: 14px;
        box-shadow: 0 14px 35px rgba(0,0,0,.25);
        backdrop-filter: blur(10px);
      }
      .cardHeader { display:flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
      .card h2 { margin: 0; font-size: 13px; letter-spacing: .4px; text-transform: uppercase; color: rgba(231,240,255,.82); }
      .hint { font-size: 12px; color: var(--faint); }
      .row { display:flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
      .col { display:flex; flex-direction: column; gap: 8px; }
      label { font-size: 12px; color: var(--muted); }
      input, select {
        background: rgba(0,0,0,.22);
        border: 1px solid var(--border);
        color: var(--text);
        border-radius: 12px;
        padding: 10px 10px;
        outline: none;
        min-width: 220px;
        transition: border-color .15s ease, box-shadow .15s ease, transform .08s ease;
      }
      input[type=number] { min-width: 120px; }
      input:focus, select:focus { border-color: rgba(81,214,255,.55); box-shadow: 0 0 0 4px rgba(81,214,255,.12); }
      select {
        appearance: none;
        background-image:
          linear-gradient(45deg, transparent 50%, rgba(231,240,255,.55) 50%),
          linear-gradient(135deg, rgba(231,240,255,.55) 50%, transparent 50%);
        background-position: calc(100% - 16px) calc(50% - 2px), calc(100% - 11px) calc(50% - 2px);
        background-size: 5px 5px, 5px 5px;
        background-repeat: no-repeat;
        padding-right: 30px;
      }
      button {
        border: 1px solid var(--border);
        background: rgba(255,255,255,.06);
        color: var(--text);
        border-radius: 12px;
        padding: 10px 12px;
        cursor: pointer;
        transition: transform .08s ease, background .15s ease, border-color .15s ease, box-shadow .15s ease;
      }
      button:hover { transform: translateY(-1px); border-color: rgba(255,255,255,.20); background: rgba(255,255,255,.08); }
      button:active { transform: translateY(0px); }
      button.primary { background: linear-gradient(180deg, rgba(81,214,255,.26), rgba(81,214,255,.10)); border-color: rgba(81,214,255,.42); box-shadow: 0 12px 26px rgba(81,214,255,.10); }
      button.primary:hover { border-color: rgba(81,214,255,.70); }
      button.ghost { background: transparent; }
      button.danger { background: linear-gradient(180deg, rgba(255,77,109,.22), rgba(255,77,109,.10)); border-color: rgba(255,77,109,.42); }
      button.danger:hover { border-color: rgba(255,77,109,.70); }
      button.mini { padding: 6px 9px; border-radius: 10px; font-size: 12px; }
      button:disabled { opacity: .5; cursor: not-allowed; transform: none; }
      .kv { display:grid; grid-template-columns: 140px 1fr; gap: 10px; font-size: 12px; color: var(--muted); }
      .kv .v { color: var(--text); overflow-wrap: anywhere; display:flex; align-items:center; gap: 8px; flex-wrap: wrap; }
      code {
        font-family: "Cascadia Code", "Cascadia Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 12px;
        background: rgba(0,0,0,.20);
        border: 1px solid rgba(255,255,255,.10);
        padding: 3px 6px;
        border-radius: 9px;
      }
      table { width: 100%; border-collapse: collapse; font-size: 12px; overflow:hidden; border-radius: 14px; border: 1px solid rgba(255,255,255,.08); }
      thead { background: rgba(255,255,255,.04); }
      th, td { padding: 10px 8px; border-top: 1px solid rgba(255,255,255,.07); vertical-align: top; }
      th { text-align: left; color: var(--muted); font-weight: 600; }
      tbody tr:nth-child(2n) { background: rgba(255,255,255,.02); }
      tbody tr:hover { background: rgba(81,214,255,.06); }
      .tag { display:inline-flex; align-items:center; gap: 6px; padding: 2px 10px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); font-size: 11px; }
      .tag.good { color: var(--good); border-color: rgba(46,204,113,.35); background: rgba(46,204,113,.10); }
      .tag.warn { color: var(--warn); border-color: rgba(245,185,66,.35); background: rgba(245,185,66,.10); }
      .tag.bad { color: var(--bad); border-color: rgba(255,77,109,.35); background: rgba(255,77,109,.10); }
      .log { height: 180px; overflow:auto; background: rgba(0,0,0,.18); border: 1px solid rgba(255,255,255,.10); border-radius: 14px; padding: 10px; font-family: "Cascadia Mono", ui-monospace, Menlo, Consolas, monospace; font-size: 12px; color: rgba(231,240,255,.88); }
      .toast {
        position: fixed;
        left: 50%;
        bottom: 18px;
        transform: translateX(-50%);
        padding: 10px 12px;
        border-radius: 999px;
        background: rgba(10,14,24,.72);
        border: 1px solid rgba(255,255,255,.14);
        box-shadow: 0 18px 40px rgba(0,0,0,.35);
        color: rgba(231,240,255,.92);
        font-size: 12px;
        opacity: 0;
        pointer-events: none;
        transition: opacity .2s ease, transform .2s ease;
        backdrop-filter: blur(10px);
      }
      .toast.show { opacity: 1; transform: translateX(-50%) translateY(-6px); }
      .toast.good { border-color: rgba(46,204,113,.35); }
      .toast.bad { border-color: rgba(255,77,109,.35); }
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="hero">
        <div class="brand">
          <div class="logo">IF</div>
          <div>
            <div class="title">iflow2api 控制台</div>
            <div class="subtitle">本地网关 · 多账号负载均衡 · Edge Profile OAuth</div>
          </div>
        </div>
        <div class="heroRight">
          <div class="pill" id="statusPill"><span class="dot"></span><span id="statusText">加载中...</span></div>
          <button class="ghost mini" id="btnProfiles">刷新 Profiles</button>
          <button class="ghost mini" id="btnRefresh">刷新状态</button>
        </div>
      </div>

      <div class="grid">
        <div class="card">
          <div class="cardHeader">
            <h2>账号池（多账号负载均衡）</h2>
            <div class="hint">建议：Edge 每个账号一个 Profile</div>
          </div>
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
            <button class="ghost" id="btnAddInPrivate">InPrivate 临时登录</button>
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
          <div class="cardHeader">
            <h2>OpenCode / 客户端接入</h2>
            <div class="hint">复制 Base URL + 本地 API Key</div>
          </div>
          <div class="kv" id="kv"></div>
          <div style="height: 10px"></div>
          <div class="cardHeader" style="margin-top: 8px;">
            <h2>日志</h2>
            <div class="hint">只写本地信息，不显示明文密钥</div>
          </div>
          <div class="log" id="log"></div>
        </div>
      </div>
    </div>
    <div id="toast" class="toast"></div>

    <script>
      const logEl = document.getElementById("log");
      const statusPill = document.getElementById("statusPill");
      const statusText = document.getElementById("statusText");
      const kv = document.getElementById("kv");
      const accountsBody = document.getElementById("accountsBody");
      const edgeProfile = document.getElementById("edgeProfile");
      const toastEl = document.getElementById("toast");
      let toastTimer = null;

      function toast(msg, kind) {
        if (!toastEl) return;
        toastEl.textContent = msg;
        toastEl.className = "toast show" + (kind ? (" " + kind) : "");
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(() => {
          toastEl.classList.remove("show");
        }, 2200);
      }

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

      function bindCopyButtons() {
        document.querySelectorAll("[data-copy]").forEach((btn) => {
          if (btn.dataset.bound === "1") return;
          btn.dataset.bound = "1";
          btn.addEventListener("click", async () => {
            const text = btn.getAttribute("data-copy") || "";
            const label = btn.getAttribute("data-label") || "内容";
            try {
              await navigator.clipboard.writeText(text);
              toast(`已复制：${label}`, "good");
            } catch (e) {
              toast("复制失败（浏览器权限）", "bad");
            }
          });
        });
      }

      function renderKV(state) {
        const baseURL = state.base_url || "";
        const apiKey = state.client_api_key || "";
        const apiKeyMask = state.client_api_key_mask || "";
        kv.innerHTML = `
          <div>Base URL</div>
          <div class="v"><code>${baseURL}</code><button class="mini" data-copy="${baseURL}" data-label="Base URL">复制</button></div>

          <div>API Key（本地）</div>
          <div class="v">
            <code id="clientKeyCode" data-full="${apiKey}">${apiKeyMask || apiKey}</code>
            <button class="mini ghost" id="btnRevealKey">显示</button>
            <button class="mini" data-copy="${apiKey}" data-label="API Key">复制</button>
          </div>

          <div>策略</div>
          <div class="v"><code>${state.client_strategy}</code></div>

          <div>账号池</div>
          <div class="v">${state.accounts_enabled}/${state.accounts_total} · oauth ${state.oauth_accounts}</div>

          <div>提示</div>
          <div class="v">这个 Key 是本地网关用的，不是 iFlow/OpenAI Key。把它填进 OpenCode 的 provider=iflow 即可。</div>
        `;
        bindCopyButtons();

        const btn = document.getElementById("btnRevealKey");
        const code = document.getElementById("clientKeyCode");
        if (btn && code) {
          btn.onclick = () => {
            const full = code.getAttribute("data-full") || "";
            const shown = code.textContent || "";
            const isMasked = shown.includes("*");
            code.textContent = isMasked ? full : (apiKeyMask || shown);
            btn.textContent = isMasked ? "隐藏" : "显示";
          };
        }
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
          toast("Profiles 已刷新", "good");
        } catch (e) {
          log(`读取 Edge Profiles 失败: ${e}`);
          toast("Profiles 刷新失败", "bad");
        }
      }

      async function refresh() {
        try {
          const state = await jget("/ui/api/state");
          statusPill.classList.remove("good", "bad");
          if (state.iflow_logged_in) {
            statusPill.classList.add("good");
            statusText.textContent = "已就绪";
          } else {
            statusText.textContent = "未登录 / 未配置";
          }
          renderKV(state);
          renderAccounts(state);
        } catch (e) {
          statusPill.classList.remove("good");
          statusPill.classList.add("bad");
          statusText.textContent = "加载失败";
          log(`刷新失败: ${e}`);
          toast("刷新失败", "bad");
        }
      }

      document.getElementById("btnRefresh").onclick = refresh;
      document.getElementById("btnProfiles").onclick = refreshProfiles;
      document.getElementById("btnAdd").onclick = async () => {
        const profileDirectory = edgeProfile.value || null;
        const maxConcurrency = Number(document.getElementById("maxConcurrency").value || 0);
        const labelOverride = document.getElementById("labelOverride").value || null;
        try {
          const r = await jpost("/ui/api/oauth/start", { profile_directory: profileDirectory, max_concurrency: maxConcurrency, label: labelOverride, inprivate: false, open_browser: true });
          log(r.opened ? "已打开 Edge 登录窗口" : "未能自动打开 Edge，请复制链接手动打开");
          log(`授权链接: ${r.auth_url}`);
          toast("已发起 OAuth 登录", "good");
        } catch (e) {
          log(`启动 OAuth 失败: ${e}`);
          toast("启动 OAuth 失败", "bad");
        }
      };
      document.getElementById("btnAddInPrivate").onclick = async () => {
        const maxConcurrency = Number(document.getElementById("maxConcurrency").value || 0);
        const labelOverride = document.getElementById("labelOverride").value || null;
        try {
          const r = await jpost("/ui/api/oauth/start", { profile_directory: null, max_concurrency: maxConcurrency, label: labelOverride, inprivate: true, open_browser: true });
          log(r.opened ? "已打开 Edge InPrivate 登录窗口" : "未能自动打开 Edge，请复制链接手动打开");
          log(`授权链接: ${r.auth_url}`);
          toast("已发起 InPrivate 登录", "good");
        } catch (e) {
          log(`启动 OAuth 失败: ${e}`);
          toast("启动 OAuth 失败", "bad");
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
        return HTMLResponse(
            f"<h3>登录失败</h3><pre>{error}</pre>",
            headers={"Cache-Control": "no-store"},
        )
    if not state or not code:
        return HTMLResponse(
            "<h3>登录失败</h3><pre>missing code/state</pre>",
            headers={"Cache-Control": "no-store"},
        )

    with _LOCK:
        pending = _PENDING.pop(state, None)
    if not pending:
        return HTMLResponse(
            "<h3>登录失败</h3><pre>invalid/expired state</pre>",
            headers={"Cache-Control": "no-store"},
        )

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
""",
        headers={"Cache-Control": "no-store"},
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

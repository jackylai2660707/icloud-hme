#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iCloud HME Web UI — 多账号聚合管理平台 — Flask single-page app."""
import sys, os, json, time, queue, secrets, threading, csv, io, sqlite3
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path: sys.path.insert(0, str(HERE))

from flask import Flask, Response, request, jsonify, render_template_string, redirect, make_response
from icloud_hme import ICloudHME, extract_chrome_cookies
from account_manager import AccountManager
from inbound_mail import InboundMailStore
from cf_compat import CfCompatStore, normalize_password_secret, normalize_jwt_token, norm_email

# ---- config ----
RESULTS_DIR = HERE / "results"
LOGS_DIR = HERE / "logs"
SCHEDULER_CONFIG_FILE = HERE / "scheduler_config.json"
APP_SETTINGS_FILE = HERE / "app_settings.json"
CLOUD_ALIASES_CACHE_FILE = RESULTS_DIR / "cloud_aliases_cache.json"
INBOUND_CONFIG_FILE = HERE / "inbound_config.json"
INBOUND_DB_FILE = RESULTS_DIR / "inbound_mail.db"
CF_COMPAT_CONFIG_FILE = HERE / "cf_compat_config.json"
DEPLOY_SECRETS_FILE = HERE / ".deploy-secrets"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
_log_queues = []
_queues_lock = threading.Lock()
_log_history = []
_history_lock = threading.Lock()
_log_counter = 0
_counter_lock = threading.Lock()
_today_key = datetime.now().strftime("%Y%m%d")
_global_state = {"running":False,"creating":False,"stopping":False,"round_status":"","total_created":0,"today_created":0,"current_round_created":0,"next_trigger":None,"last_error":None,"cookies_ok":False,"alias_count":0,"alias_active":0,"scheduler_mode":"window_random","scheduler_interval_minutes":60,"scheduler_count_per_run":1,"scheduler_account_interval_sec":3.0,"scheduler_label_prefix":"","scheduler_selected_accounts":[],"alias_split_enabled":False,"alias_split_count":4,"forward_to_email":""}
_lock = threading.Lock()
_config_lock = threading.Lock()
_settings_lock = threading.Lock()
_scheduler_thread = None
_stop_event = threading.Event()
_account_mgr = AccountManager()
_inbound_store = InboundMailStore(INBOUND_DB_FILE)
_cf_store = CfCompatStore(INBOUND_DB_FILE, CF_COMPAT_CONFIG_FILE, DEPLOY_SECRETS_FILE)
_scheduler_config = {}
_scheduler_runtime = {"rr_index":0}
_app_settings = {}
_inbound_config = {}
ADMIN_COOKIE_NAME = "icloud_admin_auth"

_RATE_LIMIT_KW = ["limit","exceeded","maximum","quota","429","too many","try again","unavailable","上限","超过","过多","频繁","rate limit","throttle","blocked"]

def _is_limit_error(err: str) -> bool: return any(kw in err.lower() for kw in _RATE_LIMIT_KW)

_time_offset = 0.0
def _sync_time():
    global _time_offset
    for url in ["https://www.baidu.com","https://www.cloudflare.com","https://www.microsoft.com"]:
        try:
            import requests as _r
            resp = _r.head(url, timeout=5)
            date_str = resp.headers.get("Date","")
            if date_str:
                from email.utils import parsedate_to_datetime
                net_time = parsedate_to_datetime(date_str)
                _time_offset = (net_time - datetime.now()).total_seconds()
                return _time_offset
        except: continue
    return 0.0

def _now() -> datetime: return datetime.now() + timedelta(seconds=_time_offset)

def _emit_log(level, msg):
    global _log_counter
    with _counter_lock:
        _log_counter += 1
        current_id = _log_counter
    entry = {"id":current_id,"time":_now().strftime("%H:%M:%S"),"level":level,"msg":msg}
    print(f"[{level.upper()}] {msg}")
    with _history_lock:
        _log_history.append(entry)
        if len(_log_history) > 200:
            _log_history.pop(0)
    with _queues_lock:
        for q in _log_queues:
            q.put(entry)

def _update_state(**kw):
    global _today_key
    with _lock:
        today = _now().strftime("%Y%m%d")
        if today != _today_key: _global_state["today_created"] = 0; _today_key = today
        _global_state.update(kw)

def _increment_state(**kw):
    global _today_key
    with _lock:
        today = _now().strftime("%Y%m%d")
        if today != _today_key: _global_state["today_created"] = 0; _today_key = today
        for k, delta in kw.items(): _global_state[k] = _global_state.get(k,0) + delta

def _default_app_settings() -> dict:
    return {
        "alias_split_enabled": False,
        "alias_split_count": 4,
        "forward_to_email": "",
    }

def _sanitize_email(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if "@" not in value or any(ch.isspace() for ch in value):
        raise ValueError("转发地址格式不正确")
    return value.lower()

def _sanitize_app_settings(raw) -> dict:
    cfg = _default_app_settings()
    if not isinstance(raw, dict):
        return cfg
    cfg["alias_split_enabled"] = bool(raw.get("alias_split_enabled", cfg["alias_split_enabled"]))
    try:
        cfg["alias_split_count"] = max(1, min(int(float(raw.get("alias_split_count", cfg["alias_split_count"]))), 20))
    except Exception:
        pass
    cfg["forward_to_email"] = _sanitize_email(raw.get("forward_to_email", cfg["forward_to_email"]))
    return cfg

def _load_app_settings() -> dict:
    if APP_SETTINGS_FILE.exists():
        try:
            return _sanitize_app_settings(json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return _default_app_settings()

def _save_app_settings(cfg: dict):
    APP_SETTINGS_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

def _get_app_settings() -> dict:
    with _settings_lock:
        return dict(_app_settings)

def _sync_app_state(cfg: dict = None):
    cfg = cfg or _get_app_settings()
    _update_state(
        alias_split_enabled=cfg.get("alias_split_enabled", False),
        alias_split_count=cfg.get("alias_split_count", 4),
        forward_to_email=cfg.get("forward_to_email", ""),
    )

def _set_app_settings(raw, persist: bool = True) -> dict:
    cfg = _sanitize_app_settings(raw)
    with _settings_lock:
        _app_settings.clear()
        _app_settings.update(cfg)
    if persist:
        _save_app_settings(cfg)
    _sync_app_state(cfg)
    return cfg

def _email_plus_variant(email: str, index: int) -> str:
    local, domain = str(email).rsplit("@", 1)
    return f"{local}+{index}@{domain}"

def _expand_email_records(records: list, settings: dict = None) -> list:
    settings = settings or _get_app_settings()
    if not settings.get("alias_split_enabled"):
        return records
    count = int(settings.get("alias_split_count", 4) or 4)
    expanded = []
    for rec in records:
        expanded.append(rec)
        email = rec.get("email", "")
        if "@" not in email:
            continue
        for i in range(1, count + 1):
            v = dict(rec)
            v["email"] = _email_plus_variant(email, i)
            v["base_email"] = email
            v["variant_index"] = i
            v["derived"] = True
            expanded.append(v)
    return expanded

def _email_record_key(rec: dict):
    return (
        str(rec.get("account_id", "") or "").strip(),
        str(rec.get("email", "") or "").strip().lower(),
    )

def _dedupe_email_records(records: list) -> list:
    """按账号+邮箱去重；云端同步记录优先补全标签、状态、anonymousId 等信息。"""
    order = []
    merged = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        email = str(rec.get("email", "") or "").strip().lower()
        if "@" not in email:
            continue
        item = dict(rec)
        item["email"] = email
        key = _email_record_key(item)
        if key not in merged:
            order.append(key)
            merged[key] = item
            continue
        old = merged[key]
        # local 记录保留创建顺序；cloud 记录用于补全实时元数据。
        if item.get("source") == "cloud":
            new = dict(old)
            for k, v in item.items():
                if v not in (None, "") or k in ("active", "derived"):
                    new[k] = v
            merged[key] = new
        else:
            for k, v in item.items():
                if k not in old or old.get(k) in (None, ""):
                    old[k] = v
    return [merged[k] for k in order]

def _read_local_email_records(limit: int = 0) -> list:
    records = []
    f = RESULTS_DIR / "latest_emails.txt"
    if f.exists():
        lines = f.read_text(encoding="utf-8").strip().split("\n")
        if limit > 0 and len(lines) > limit:
            lines = lines[-limit:]
        for line in lines:
            line = line.strip()
            if line and "@" in line:
                parts = line.split("\t")
                records.append({
                    "email": parts[0],
                    "account_id": parts[1] if len(parts) > 1 else "",
                    "created_at": "",
                    "derived": False,
                    "source": "local",
                })
    records.reverse()
    return records

def _load_cached_cloud_aliases() -> list:
    if not CLOUD_ALIASES_CACHE_FILE.exists():
        return []
    try:
        raw = json.loads(CLOUD_ALIASES_CACHE_FILE.read_text(encoding="utf-8"))
        aliases = raw.get("aliases", []) if isinstance(raw, dict) else raw
        return aliases if isinstance(aliases, list) else []
    except Exception:
        return []

def _save_cached_cloud_aliases(aliases: list):
    CLOUD_ALIASES_CACHE_FILE.write_text(
        json.dumps({
            "updated_at": _now().isoformat(),
            "aliases": aliases,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

def _sync_cloud_alias_cache() -> list:
    aliases = []
    for alias in _account_mgr.get_all_aliases():
        item = dict(alias)
        item["source"] = "cloud"
        item["derived"] = False
        item["created_at"] = item.get("created_at") or item.get("createdAt") or ""
        aliases.append(item)
    aliases = _dedupe_email_records(aliases)
    _save_cached_cloud_aliases(aliases)
    return aliases

def _load_inbound_config() -> dict:
    cfg = {"token": "", "public_base_url": ""}
    if INBOUND_CONFIG_FILE.exists():
        try:
            raw = json.loads(INBOUND_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: str(v or "") for k, v in raw.items()})
        except Exception:
            pass
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(32)
        _save_inbound_config(cfg)
    return cfg

def _save_inbound_config(cfg: dict):
    INBOUND_CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

def _get_inbound_config() -> dict:
    return dict(_inbound_config)

def _regenerate_inbound_token() -> dict:
    _inbound_config["token"] = secrets.token_urlsafe(32)
    _save_inbound_config(_inbound_config)
    return _get_inbound_config()

def _share_base_url() -> str:
    cfg = _get_inbound_config()
    base = (cfg.get("public_base_url") or "").strip().rstrip("/")
    if base:
        return base
    try:
        proto = request.headers.get("X-Forwarded-Proto") or request.scheme or "https"
        host = request.headers.get("X-Forwarded-Host") or request.host
        if host:
            if proto == "http" and not (host.startswith("127.0.0.1") or host.startswith("localhost")):
                proto = "https"
            return f"{proto}://{host}".rstrip("/")
        return request.url_root.rstrip("/")
    except Exception:
        return "https://icloud.armsg.yueseng-ys.com"

def _known_alias_records_for_inbound() -> list:
    records = _dedupe_email_records(_read_local_email_records() + _load_cached_cloud_aliases())
    return _dedupe_email_records(_expand_email_records(records))

def _known_aliases_and_account_map():
    records = _known_alias_records_for_inbound()
    aliases = []
    account_map = {}
    for r in records:
        email = str(r.get("email", "") or "").strip().lower()
        if email and "@" in email:
            aliases.append(email)
            if r.get("account_id"):
                account_map[email] = r.get("account_id")
    return aliases, account_map

def _check_inbound_auth() -> bool:
    token = (_get_inbound_config().get("token") or "").strip()
    auth = request.headers.get("Authorization", "")
    supplied = ""
    if auth.lower().startswith("bearer "):
        supplied = auth.split(" ", 1)[1].strip()
    elif request.headers.get("X-Inbound-Token"):
        supplied = request.headers.get("X-Inbound-Token", "").strip()
    return bool(token and supplied and secrets.compare_digest(token, supplied))

def _admin_auth_ok() -> bool:
    candidates = [
        request.headers.get("x-admin-auth", ""),
        request.cookies.get(ADMIN_COOKIE_NAME, ""),
    ]
    for value in candidates:
        if value and _cf_store.verify_admin_secret(value):
            return True
    return False

def _has_bearer_token() -> bool:
    return request.headers.get("Authorization", "").lower().startswith("bearer ")

def _get_bearer_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return normalize_jwt_token(auth.split(" ", 1)[1])
    return ""

def _credential_error_code(exc: Exception | str) -> tuple[str, str]:
    msg = str(exc or "").strip()
    lower = msg.lower()
    if not msg:
        return "InvalidAddressCredentialMsg", "unknown"
    if "expired" in lower:
        return "AddressCredentialExpiredMsg", "expired"
    if "signature" in lower:
        return "InvalidAddressCredentialMsg", "bad_signature"
    if "address not found" in lower:
        return "AddressCredentialAddressNotFoundMsg", "address_not_found"
    if "malformed" in lower or "invalid token" in lower or "incorrect padding" in lower:
        return "InvalidAddressCredentialMsg", "malformed_token"
    return "InvalidAddressCredentialMsg", "invalid_token"

def _address_payload_or_error():
    token = _get_bearer_token()
    if not token:
        return None, "MissingAddressCredentialMsg", "missing"
    try:
        return _cf_store.verify_address_token(token), "", ""
    except Exception as e:
        code, detail = _credential_error_code(e)
        return None, code, detail

def _address_payload_or_none():
    payload, _, _ = _address_payload_or_error()
    return payload

def _user_payload_or_none():
    token = request.headers.get("x-user-token", "") or request.cookies.get("cf_user_token", "")
    if not token:
        return None
    try:
        return _cf_store.verify_user_token(token)
    except Exception:
        return None

def _sync_cf_addresses() -> int:
    records = _known_alias_records_for_inbound()
    n = _cf_store.sync_addresses(records)
    # 兜底把已经收到过邮件但不在当前云端缓存里的地址也加入凭证表。
    extra = []
    for row in _inbound_store.list_aliases():
        if row.get("alias"):
            extra.append({"email": row.get("alias"), "account_id": row.get("account_id", ""), "source": "inbound"})
    if extra:
        n += _cf_store.sync_addresses(extra)
    return n

def _cf_mail_row(msg: dict, include_raw: bool = True, parsed: bool = False) -> dict:
    row = {
        "id": msg.get("id"),
        "address": msg.get("hme_alias") or msg.get("address") or "",
        "message_id": msg.get("message_id") or "",
        "from": msg.get("from") or msg.get("source_from") or msg.get("sender_name") or "",
        "sender": msg.get("sender_name") or msg.get("from") or msg.get("source_from") or "",
        "subject": msg.get("subject") or "(无主题)",
        "created_at": msg.get("created_at") or "",
        "updated_at": msg.get("created_at") or "",
    }
    if include_raw:
        row["raw"] = msg.get("raw") or ""
        row["source"] = msg.get("raw") or ""
    if parsed:
        row["text"] = msg.get("text") or ""
        row["html"] = msg.get("html") or ""
        row["attachments"] = []
    return row

def _list_cf_mails(
    addresses: list,
    limit: int = 50,
    offset: int = 0,
    include_raw: bool = True,
    parsed: bool = False,
    include_body: bool = True,
):
    addresses = [norm_email(a) for a in addresses if norm_email(a)]
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    if not addresses:
        return {"results": [], "count": 0, "limit": limit, "offset": offset}
    # 直接读 inbound_mail.db，避免 list_messages 对单个 alias 的限制。
    placeholders = ",".join("?" for _ in addresses)
    with _inbound_store._lock, _inbound_store._connect() as conn:
        count = conn.execute(
            f"SELECT COUNT(*) AS c FROM inbound_mails WHERE hme_alias IN ({placeholders})",
            addresses,
        ).fetchone()["c"]
        raw_expr = "raw" if include_raw else "'' AS raw"
        text_expr = "text" if (parsed and include_body) else "'' AS text"
        html_expr = "html" if (parsed and include_body) else "'' AS html"
        rows = conn.execute(
            f"""
            SELECT id, message_id, source_from, source_from AS "from", envelope_to,
                   hme_alias, base_alias, account_id, subject, sender_name,
                   {text_expr}, {html_expr}, {raw_expr}, created_at
            FROM inbound_mails
            WHERE hme_alias IN ({placeholders})
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            addresses + [limit, offset],
        ).fetchall()
    return {
        "results": [_cf_mail_row(dict(r), include_raw=include_raw, parsed=parsed) for r in rows],
        "count": count,
        "limit": limit,
        "offset": offset,
    }

def _get_cf_mail(mail_id: int, addresses: list, include_raw: bool = True, parsed: bool = False):
    addresses = [norm_email(a) for a in addresses if norm_email(a)]
    if not addresses:
        return None
    placeholders = ",".join("?" for _ in addresses)
    with _inbound_store._lock, _inbound_store._connect() as conn:
        row = conn.execute(
            f"""
            SELECT id, message_id, source_from, source_from AS "from", envelope_to,
                   hme_alias, base_alias, account_id, subject, sender_name,
                   text, html, raw, headers_json, created_at
            FROM inbound_mails
            WHERE id=? AND hme_alias IN ({placeholders})
            """,
            [int(mail_id)] + addresses,
        ).fetchone()
    return _cf_mail_row(dict(row), include_raw=include_raw, parsed=parsed) if row else None

def _list_all_cf_mails(
    limit: int = 50,
    offset: int = 0,
    include_raw: bool = True,
    parsed: bool = False,
    include_body: bool = True,
):
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    with _inbound_store._lock, _inbound_store._connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM inbound_mails").fetchone()["c"]
        raw_expr = "raw" if include_raw else "'' AS raw"
        text_expr = "text" if (parsed and include_body) else "'' AS text"
        html_expr = "html" if (parsed and include_body) else "'' AS html"
        rows = conn.execute(
            f"""
            SELECT id, message_id, source_from, source_from AS "from", envelope_to,
                   hme_alias, base_alias, account_id, subject, sender_name,
                   {text_expr}, {html_expr}, {raw_expr}, created_at
            FROM inbound_mails
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [limit, offset],
        ).fetchall()
    return {
        "results": [_cf_mail_row(dict(r), include_raw=include_raw, parsed=parsed) for r in rows],
        "count": count,
        "limit": limit,
        "offset": offset,
    }

def _json_error(message: str, status: int = 401, detail: str = ""):
    body = {"success": False, "error": message}
    if detail:
        body["detail"] = detail
    return jsonify(body), status

def _path_is_cf_address_api(path: str) -> bool:
    exact = {"/api/settings", "/api/mails", "/api/parsed_mails", "/api/delete_address", "/api/clear_inbox", "/api/clear_sent_items", "/api/address_login"}
    return path in exact or path.startswith("/api/mail/") or path.startswith("/api/parsed_mail/")

def _path_is_public(path: str) -> bool:
    if path in ("/", "/index.html", "/admin", "/user", "/login", "/logout", "/favicon.ico", "/cloudflare_inbound_worker.js", "/api/address_login"):
        return True
    return (
        path.startswith("/open_api/")
        or path.startswith("/user_api/")
        or path.startswith("/share/")
        or path.startswith("/api/shared/")
        or path == "/api/inbound-mail"
    )

@app.before_request
def _app_auth_gate():
    path = request.path
    if _path_is_public(path):
        return None
    if path.startswith("/admin/"):
        if _admin_auth_ok():
            return None
        return _json_error("NeedAdminPasswordMsg", 401)
    if _path_is_cf_address_api(path):
        # 如果请求显式带了 Bearer，优先按地址凭证处理；不要因为同浏览器里
        # 还保留 admin cookie 就把坏凭证误放行到管理员分支，避免前端出现
        # “同一个凭证有时可用、有时 undefined/失效”的混乱状态。
        if _has_bearer_token():
            payload, err, detail = _address_payload_or_error()
            if payload:
                return None
            return _json_error(err, 401, detail)
        if _admin_auth_ok():
            return None
        return _json_error("MissingAddressCredentialMsg", 401, "missing")
    # 这些创建接口兼容 cftempmail，但本项目默认要求管理员或已登录用户。
    if path == "/api/new_address":
        if _admin_auth_ok() or _user_payload_or_none():
            return None
        return _json_error("NewAddressAnonymousDisabledMsg", 403)
    # 其余 /api 都是管理员能力；外层 Caddy 不再使用 Basic Auth，
    # 管理后台只接受 app 内 admin cookie 或 x-admin-auth。
    if path.startswith("/api/"):
        if _admin_auth_ok():
            return None
        return _json_error("NeedAdminPasswordMsg", 401)
    return None

_inbound_config.update(_load_inbound_config())

def _default_scheduler_config() -> dict:
    return {
        "mode": "window_random",
        "interval_minutes": 60,
        "count_per_run": 1,
        "account_interval_sec": 3.0,
        "label_prefix": "",
        "selected_accounts": [],
    }

def _sanitize_scheduler_config(raw) -> dict:
    cfg = _default_scheduler_config()
    if not isinstance(raw, dict):
        return cfg
    mode = str(raw.get("mode", cfg["mode"])).strip().lower()
    cfg["mode"] = mode if mode in ("window_random", "interval") else cfg["mode"]
    try: cfg["interval_minutes"] = max(1, min(int(float(raw.get("interval_minutes", cfg["interval_minutes"]))), 1440))
    except Exception: pass
    try: cfg["count_per_run"] = max(1, min(int(float(raw.get("count_per_run", cfg["count_per_run"]))), 20))
    except Exception: pass
    try: cfg["account_interval_sec"] = max(0.0, min(float(raw.get("account_interval_sec", cfg["account_interval_sec"])), 600.0))
    except Exception: pass
    cfg["label_prefix"] = str(raw.get("label_prefix", cfg["label_prefix"])).strip()[:80]
    valid_ids = set(_account_mgr.accounts.keys())
    selected_accounts = raw.get("selected_accounts", [])
    if isinstance(selected_accounts, list):
        seen = set()
        picked = []
        for acc_id in selected_accounts:
            acc_id = str(acc_id).strip()
            if acc_id and acc_id in valid_ids and acc_id not in seen:
                picked.append(acc_id); seen.add(acc_id)
        cfg["selected_accounts"] = picked
    return cfg

def _load_scheduler_config() -> dict:
    if SCHEDULER_CONFIG_FILE.exists():
        try:
            return _sanitize_scheduler_config(json.loads(SCHEDULER_CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return _default_scheduler_config()

def _save_scheduler_config(cfg: dict):
    SCHEDULER_CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

def _get_scheduler_config() -> dict:
    with _config_lock:
        return dict(_scheduler_config)

def _sync_scheduler_state(cfg: dict = None):
    cfg = cfg or _get_scheduler_config()
    _update_state(
        scheduler_mode=cfg.get("mode", "window_random"),
        scheduler_interval_minutes=cfg.get("interval_minutes", 60),
        scheduler_count_per_run=cfg.get("count_per_run", 1),
        scheduler_account_interval_sec=cfg.get("account_interval_sec", 3.0),
        scheduler_label_prefix=cfg.get("label_prefix", ""),
        scheduler_selected_accounts=cfg.get("selected_accounts", []),
    )

def _set_scheduler_config(raw, persist: bool = True) -> dict:
    cfg = _sanitize_scheduler_config(raw)
    with _config_lock:
        _scheduler_config.clear()
        _scheduler_config.update(cfg)
    if persist:
        _save_scheduler_config(cfg)
    _sync_scheduler_state(cfg)
    return cfg

def _get_scheduler_accounts(cfg: dict = None):
    cfg = cfg or _get_scheduler_config()
    active_accounts = [a for a in _account_mgr.list_accounts() if a.get("status") == "active"]
    selected_ids = cfg.get("selected_accounts") or []
    if not selected_ids:
        return active_accounts
    active_map = {a.get("id"): a for a in active_accounts}
    return [active_map[acc_id] for acc_id in selected_ids if acc_id in active_map]

def _make_scheduler_label(account: dict, cfg: dict, idx: int = 1) -> str:
    acc_name = account.get("name") or account.get("real_email") or account.get("id") or "account"
    prefix = cfg.get("label_prefix", "").strip()
    base = f"{acc_name} {_now().strftime('%m%d%H%M')}-{idx}"
    return f"{prefix} {base}".strip() if prefix else base

def _create_one_scheduled_alias(acc_id: str, label: str) -> dict:
    try:
        forward_to = _get_app_settings().get("forward_to_email", "")
        results = _account_mgr.create_aliases_for_account(acc_id, count=1, label=label, forward_to=forward_to)
        if results:
            return results[0]
        return {"ok": False, "email": None, "account_id": acc_id, "error": "create_alias 返回空结果"}
    except Exception as e:
        return {"ok": False, "email": None, "account_id": acc_id, "error": str(e)}

_app_settings.update(_load_app_settings())
_sync_app_state(_app_settings)
_scheduler_config.update(_load_scheduler_config())
_sync_scheduler_state(_scheduler_config)

def _scheduler_loop_random_window():
    """后台调度器：北京时间 7:00-20:00，随机间隔 60-90min，每账号随机 3-5 个。"""
    import random as _random
    _update_state(running=True, round_status="等待触发窗口")
    _emit_log("info", "调度器已启动 (随机窗口模式: BJ 7-20h, 间隔 60-90min, 每轮每账号 3-5 个)")
    def _bj_hour() -> int: return (_now().hour + 8) % 24
    while not _stop_event.is_set():
        cfg = _get_scheduler_config()
        h = _bj_hour()
        if h < 7 or h >= 20:
            _update_state(creating=False, round_status=f"非窗口时段 (BJ {h}:00)，等待...", next_trigger=None)
            _stop_event.wait(1800)
            continue
        active_accounts = _get_scheduler_accounts(cfg)
        if not active_accounts:
            _update_state(creating=False, round_status="无活跃账号，跳过", next_trigger=None)
            _stop_event.wait(1800)
            continue
        round_total = 0
        _update_state(creating=True, round_status=f"随机窗口执行中 ({len(active_accounts)} 个账号)")
        for i, account in enumerate(active_accounts):
            if _stop_event.is_set(): break
            acc_id = account["id"]; acc_name = account.get("name", acc_id)
            target_count = _random.randint(3, 5)
            _emit_log("info", f"[{acc_name}] 本轮目标 {target_count} 个")
            created = 0; errors = 0
            while created < target_count and errors < 3 and not _stop_event.is_set():
                result = _create_one_scheduled_alias(acc_id, _make_scheduler_label(account, cfg, created + 1))
                email = result.get("email", "")
                if result.get("ok") and email:
                    created += 1; round_total += 1
                    _emit_log("success", f"[{acc_name}] ({created}/{target_count}) {email}")
                    _increment_state(today_created=1, total_created=1)
                    errors = 0
                    _stop_event.wait(_random.uniform(15, 45))
                else:
                    err_str = str(result.get("error", "未知错误"))
                    if _is_limit_error(err_str):
                        _emit_log("info", f"[{acc_name}] 触达上限: {err_str[:80]}")
                        break
                    errors += 1
                    _emit_log("warn", f"[{acc_name}] 失败: {err_str[:100]}")
            if i < len(active_accounts)-1 and not _stop_event.is_set():
                _stop_event.wait(_random.uniform(120, 300))
        _update_state(creating=False, current_round_created=round_total, round_status=f"本轮创建 {round_total} 个")
        interval_sec = _random.randint(3600,5400)
        target = _now() + timedelta(seconds=interval_sec)
        _update_state(next_trigger=target.timestamp())
        _emit_log("info", f"下轮 {target.strftime('%H:%M')} (间隔 {interval_sec//60}min)")
        _stop_event.wait(interval_sec)

def _scheduler_loop_interval():
    cfg = _get_scheduler_config()
    _update_state(running=True, round_status="固定间隔计划已就绪")
    _emit_log("info", f"调度器已启动 (固定间隔模式: 每 {cfg.get('interval_minutes',60)} 分钟创建 {cfg.get('count_per_run',1)} 个，活跃账号轮询)")
    while not _stop_event.is_set():
        cfg = _get_scheduler_config()
        active_accounts = _get_scheduler_accounts(cfg)
        interval_minutes = cfg.get("interval_minutes", 60)
        count_per_run = cfg.get("count_per_run", 1)
        if not active_accounts:
            _update_state(creating=False, round_status="无活跃账号，等待...", next_trigger=None)
            _stop_event.wait(30)
            continue
        target = _now() + timedelta(minutes=interval_minutes)
        _update_state(creating=False, round_status=f"计划已设置: 每 {interval_minutes} 分钟创建 {count_per_run} 个", next_trigger=target.timestamp())
        _emit_log("info", f"固定间隔计划: 下次 {target.strftime('%H:%M:%S')}，每轮 {count_per_run} 个")
        if _stop_event.wait(interval_minutes * 60): break
        round_total = 0
        round_failed = False
        round_error = ""
        _update_state(creating=True, round_status=f"计划执行中: {count_per_run} 个")
        for i in range(count_per_run):
            if _stop_event.is_set(): break
            active_accounts = _get_scheduler_accounts(cfg)
            if not active_accounts:
                _emit_log("warn", "计划执行中断: 无活跃账号")
                break
            rr_index = _scheduler_runtime.get("rr_index", 0) % len(active_accounts)
            account = active_accounts[rr_index]
            _scheduler_runtime["rr_index"] = (rr_index + 1) % len(active_accounts)
            acc_id = account["id"]; acc_name = account.get("name", acc_id)
            result = _create_one_scheduled_alias(acc_id, _make_scheduler_label(account, cfg, i + 1))
            email = result.get("email", "")
            if result.get("ok") and email:
                round_total += 1
                _increment_state(today_created=1, total_created=1)
                _emit_log("success", f"[{acc_name}] 计划创建成功: {email}")
            else:
                err_str = str(result.get("error", "未知错误"))
                if _is_limit_error(err_str):
                    _emit_log("info", f"[{acc_name}] 计划创建触达上限: {err_str[:100]}")
                else:
                    _emit_log("warn", f"[{acc_name}] 计划创建失败: {err_str[:100]}")
                round_failed = True
                round_error = err_str[:300]
                _update_state(last_error=round_error, round_status="计划创建失败，等待下个周期")
                break
            if i < count_per_run - 1 and not _stop_event.is_set():
                gap = cfg.get("account_interval_sec", 3.0)
                if gap > 0 and _stop_event.wait(gap): break
        if round_failed:
            _update_state(creating=False, current_round_created=round_total, last_error=round_error, round_status=f"上次计划失败，已等待下个周期；本轮成功 {round_total} 个")
        else:
            _update_state(creating=False, current_round_created=round_total, round_status=f"上次计划创建 {round_total} 个")

def _scheduler_loop():
    try:
        _update_state(running=True, stopping=False, last_error=None)
        if _get_scheduler_config().get("mode") == "interval":
            _scheduler_loop_interval()
        else:
            _scheduler_loop_random_window()
    finally:
        _update_state(running=False, creating=False, stopping=False, next_trigger=None, round_status="已停止")
        _emit_log("info", "调度器已停止")

def _health_loop():
    _error_reported = set()
    while not _stop_event.is_set():
        if _stop_event.wait(300): break
        for account in _account_mgr.list_accounts():
            if account.get("status") != "active": continue
            try: _account_mgr.validate_account(account["id"]); _error_reported.discard(account["id"])
            except Exception as e:
                if account["id"] not in _error_reported: _emit_log("warn",f"健康检查失败 [{account.get('name','?')}]: {str(e)[:100]}"); _error_reported.add(account["id"])

# ----- HTML -----
UI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0">
    <title>iCloud HME — 多账号管理</title>
    <style>
        :root {
            --paper: #f3efe4;
            --paper-dim: #e8e2d4;
            --ink: #0f0e0c;
            --ink-soft: #5c564e;
            --ink-faint: #9a938a;
            --rule: rgba(15,14,12,.12);
            --rule-strong: rgba(15,14,12,.22);
            --red: #b7392d;
            --green: #1f8b4c;
            --mono: "SF Mono","Fira Code","Cascadia Code",Consolas,monospace;
            --sans: "PingFang SC","Microsoft YaHei","Noto Sans SC",system-ui,sans-serif;
        }
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        html {
            min-width: 1040px;
            background: var(--paper);
            font-size: 16px;
        }
        body {
            color: var(--ink);
            font-family: var(--sans);
            min-height: 100vh;
            display: flex;
            background: radial-gradient(circle at 10% 8%,rgba(183,57,45,.03),transparent 26%),
                        radial-gradient(circle at 78% 42%,rgba(15,14,12,.025),transparent 30%),
                        linear-gradient(90deg,rgba(15,14,12,.018) 1px,transparent 1px),
                        linear-gradient(rgba(15,14,12,.018) 1px,transparent 1px),
                        var(--paper);
            background-size: auto,auto,64px 64px,64px 64px,auto;
        }
        .sidebar {
            width: 260px;
            background: var(--paper);
            border-right: 1px solid var(--rule-strong);
            padding: 28px 22px;
            display: flex;
            flex-direction: column;
            gap: 3px;
            flex-shrink: 0;
            overflow-y: auto;
        }
        .sidebar .logo {
            font-family: var(--mono);
            font-size: 15px;
            letter-spacing: .28em;
            text-transform: uppercase;
            color: var(--ink-faint);
            margin-bottom: 24px;
            display: flex;
            align-items: center;
            gap: 14px;
        }
        .sidebar .logo .icon {
            width: 16px;
            height: 16px;
            background: var(--red);
            transform: rotate(45deg);
            flex-shrink: 0;
        }
        .sidebar .nav-item {
            padding: 10px 0;
            color: var(--ink-soft);
            font-size: 15px;
            cursor: pointer;
            user-select: none;
            display: flex;
            align-items: center;
            gap: 10px;
            border-bottom: 1px solid transparent;
            transition: border-color .2s,color .2s;
            font-family: var(--mono);
            letter-spacing: .03em;
        }
        .sidebar .nav-item:hover {
            color: var(--ink);
            border-bottom-color: var(--rule);
        }
        .sidebar .nav-item.active {
            color: var(--ink);
            border-bottom-color: var(--red);
            font-weight: 600;
        }
        .sidebar .section-label {
            font-family: var(--mono);
            font-size: 11px;
            color: var(--ink-faint);
            text-transform: uppercase;
            letter-spacing: .3em;
            padding: 22px 0 10px;
        }
        .sidebar .account-item {
            padding: 9px 0;
            font-size: 13px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 10px;
            border-left: 2px solid transparent;
            padding-left: 10px;
            transition: all .15s;
            font-family: var(--mono);
        }
        .sidebar .account-item:hover {
            color: var(--ink);
        }
        .sidebar .account-item.selected {
            border-left-color: var(--red);
            font-weight: 600;
        }
        .sidebar .account-item .acc-dot {
            width: 7px;
            height: 7px;
            transform: rotate(45deg);
            flex-shrink: 0;
        }
        .sidebar .account-item .acc-dot.active {
            background: var(--green);
        }
        .sidebar .account-item .acc-dot.error {
            background: var(--red);
        }
        .sidebar .account-item .acc-name {
            flex: 1;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .sidebar .account-item .acc-del {
            opacity: 0;
            color: var(--red);
            cursor: pointer;
            font-size: 16px;
            line-height: 1;
        }
        .sidebar .account-item:hover .acc-del {
            opacity: .5;
        }
        .sidebar .account-item .acc-del:hover {
            opacity: 1;
        }
        #sidebarAccounts {
            max-height: 340px;
            overflow-y: auto;
        }
        .status-dot {
            display: inline-block;
            width: 7px;
            height: 7px;
            transform: rotate(45deg);
            margin-right: 8px;
            vertical-align: middle;
        }
        .status-dot.online {
            background: var(--green);
        }
        .status-dot.offline {
            background: var(--ink-faint);
        }
        .main {
            flex: 1;
            padding: 32px 44px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 24px;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
        }
        .header h1 {
            font-family: var(--mono);
            font-size: 14px;
            color: var(--ink-faint);
            letter-spacing: .28em;
            text-transform: uppercase;
            font-weight: 400;
        }
        .cards {
            display: grid;
            grid-template-columns: repeat(auto-fit,minmax(180px,1fr));
            gap: 1px;
            background: var(--rule-strong);
            border: 1px solid var(--rule-strong);
        }
        .card {
            background: var(--paper);
            padding: 22px 24px;
            transition: background .15s;
        }
        .card:hover {
            background: var(--paper-dim);
        }
        .card .label {
            font-family: var(--mono);
            font-size: 11px;
            color: var(--ink-faint);
            text-transform: uppercase;
            letter-spacing: .3em;
            margin-bottom: 10px;
        }
        .card .value {
            font-size: 38px;
            font-weight: 800;
            letter-spacing: -1px;
            font-family: var(--mono);
        }
        .card .value.accent {
            color: var(--red);
        }
        .card .value.green {
            color: var(--green);
        }
        .card .value.orange {
            color: var(--ink-soft);
        }
        .card .value.blue {
            color: var(--ink);
        }
        .card .sub {
            font-size: 13px;
            color: var(--ink-faint);
            margin-top: 6px;
            font-family: var(--mono);
        }
        .acc-cards {
            display: grid;
            grid-template-columns: repeat(auto-fill,minmax(300px,1fr));
            gap: 1px;
            background: var(--rule-strong);
            border: 1px solid var(--rule-strong);
            margin-top: 2px;
        }
        .acc-card {
            background: var(--paper);
            padding: 22px 24px;
            transition: background .15s;
        }
        .acc-card:hover {
            background: var(--paper-dim);
        }
        .acc-card .acc-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 14px;
        }
        .acc-card .acc-title {
            font-weight: 700;
            font-size: 16px;
            font-family: var(--mono);
        }
        .acc-card .acc-email {
            font-size: 13px;
            color: var(--ink-faint);
            font-family: var(--mono);
            margin-top: 4px;
        }
        .acc-card .acc-stats {
            display: flex;
            gap: 24px;
            margin-top: 12px;
        }
        .acc-card .acc-stat {
            font-size: 13px;
            font-family: var(--mono);
            color: var(--ink-soft);
        }
        .acc-card .acc-stat .n {
            font-weight: 700;
            color: var(--ink);
        }
        .acc-card .acc-actions {
            margin-top: 14px;
            display: flex;
            gap: 8px;
        }
        .acc-card .status-badge {
            font-family: var(--mono);
            font-size: 11px;
            padding: 2px 0;
            letter-spacing: .08em;
            text-transform: uppercase;
        }
        .acc-card .status-badge.ok {
            color: var(--green);
            border-bottom: 1px solid var(--green);
        }
        .acc-card .status-badge.err {
            color: var(--red);
            border-bottom: 1px solid var(--red);
        }
        .panel {
            background: var(--paper);
            border: 1px solid var(--rule-strong);
            overflow: hidden;
        }
        .panel-header {
            padding: 14px 20px;
            border-bottom: 1px solid var(--rule);
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-family: var(--mono);
            font-size: 12px;
            color: var(--ink-faint);
            text-transform: uppercase;
            letter-spacing: .16em;
        }
        .panel-body {
            padding: 0;
        }
        .btn {
            padding: 9px 22px;
            font-size: 13px;
            cursor: pointer;
            border: none;
            font-family: var(--mono);
            transition: all .15s;
            letter-spacing: .03em;
            background: var(--ink);
            color: var(--paper);
        }
        .btn:hover {
            opacity: .78;
        }
        .btn:disabled {
            opacity: .28;
            cursor: not-allowed;
        }
        .btn-primary {
            background: var(--ink);
            color: var(--paper);
        }
        .btn-outline {
            background: transparent;
            border: 1px solid var(--rule-strong);
            color: var(--ink);
        }
        .btn-outline:hover {
            background: var(--ink);
            color: var(--paper);
            border-color: var(--ink);
            opacity: 1;
        }
        .btn-danger {
            background: transparent;
            color: var(--red);
            border: 1px solid var(--red);
        }
        .btn-danger:hover {
            background: var(--red);
            color: var(--paper);
            opacity: 1;
        }
        .btn-sm {
            padding: 5px 14px;
            font-size: 12px;
        }
        .btn-xs {
            padding: 3px 10px;
            font-size: 11px;
        }
        .btn-group {
            display: flex;
            gap: 10px;
        }
        .chk-group {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            padding: 10px 0;
        }
        .chk-item {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 14px;
            cursor: pointer;
            font-family: var(--mono);
        }
        .chk-item input {
            margin: 0;
            accent-color: var(--red);
            width: 16px;
            height: 16px;
        }
        .email-table {
            width: 100%;
            border-collapse: collapse;
            font-family: var(--mono);
        }
        .email-table th {
            text-align: left;
            padding: 10px 18px;
            font-size: 11px;
            color: var(--ink-faint);
            text-transform: uppercase;
            letter-spacing: .3em;
            border-bottom: 1px solid var(--rule-strong);
            font-weight: 400;
        }
        .email-table td {
            padding: 12px 18px;
            font-size: 14px;
            border-bottom: 1px solid var(--rule);
        }
        .email-table tr:hover td {
            background: var(--paper-dim);
        }
        .email-item:hover {
            background: var(--paper-dim);
        }
        .copy-btn {
            background: none;
            border: 1px solid var(--rule-strong);
            color: var(--ink-soft);
            cursor: pointer;
            font-size: 11px;
            padding: 2px 6px;
            font-family: var(--mono);
            border-radius: 2px;
            transition: all .15s;
            display: inline-block;
        }
        .copy-btn:hover {
            background: var(--ink);
            color: var(--paper);
            border-color: var(--ink);
            opacity: 1;
        }
        .filter-bar {
            display: flex;
            gap: 12px;
            align-items: center;
            padding: 10px 18px;
            border-bottom: 1px solid var(--rule);
        }
        .filter-bar select {
            padding: 6px 10px;
            border: 1px solid var(--rule-strong);
            font-family: var(--mono);
            font-size: 13px;
            background: var(--paper);
            color: var(--ink);
        }
        .filter-bar select:focus {
            outline: none;
            border-color: var(--red);
        }
        .pagination-bar {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 12px;
            padding: 12px 18px;
            border-top: 1px solid var(--rule);
            font-size: 12px;
            color: var(--ink-faint);
            font-family: var(--mono);
        }
        .pagination-bar button {
            background: none;
            border: 1px solid var(--rule-strong);
            color: var(--ink-soft);
            cursor: pointer;
            font-size: 11px;
            padding: 4px 12px;
            font-family: var(--mono);
            border-radius: 3px;
            transition: all .15s;
        }
        .pagination-bar button:hover:not(:disabled) {
            background: var(--ink);
            color: var(--paper);
            border-color: var(--ink);
        }
        .pagination-bar button:disabled {
            opacity: 0.3;
            cursor: default;
        }
        .copy-toast {
            position: fixed;
            top: 24px;
            right: 24px;
            background: var(--ink);
            color: var(--paper);
            padding: 12px 24px;
            font-family: var(--mono);
            font-size: 13px;
            letter-spacing: .03em;
            opacity: 0;
            transform: translateY(-8px);
            transition: all .2s;
            pointer-events: none;
            z-index: 999;
        }
        .copy-toast.show {
            opacity: 1;
            transform: translateY(0);
        }
        .log-feed {
            max-height: 320px;
            overflow-y: auto;
            padding: 14px 20px;
            font-family: var(--mono);
            font-size: 13px;
            line-height: 1.8;
        }
        .log-feed .log-line {
            white-space: pre-wrap;
            word-break: break-all;
        }
        .log-line.info {
            color: var(--ink-soft);
        }
        .log-line.success {
            color: var(--green);
        }
        .log-line.warn {
            color: var(--red);
        }
        .log-line.error {
            color: var(--red);
            font-weight: 600;
        }
        .log-time {
            color: var(--ink-faint);
            margin-right: 10px;
        }
        .empty {
            text-align: center;
            padding: 56px 20px;
            color: var(--ink-faint);
            font-family: var(--mono);
            font-size: 13px;
            letter-spacing: .03em;
        }
        .empty .icon {
            font-size: 42px;
            margin-bottom: 14px;
            opacity: .5;
        }
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(15,14,12,.7);
            z-index: 999;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .modal-box {
            background: var(--paper);
            border: 1px solid var(--ink);
            padding: 32px;
            width: 90%;
            max-width: 560px;
            box-shadow: 8px 8px 0 rgba(15,14,12,.12);
        }
        .modal-box h3 {
            font-family: var(--mono);
            font-size: 15px;
            letter-spacing: .16em;
            text-transform: uppercase;
            margin-bottom: 10px;
            font-weight: 400;
        }
        .modal-box p {
            font-size: 14px;
            color: var(--ink-soft);
            margin-bottom: 16px;
            line-height: 1.6;
        }
        .modal-box input, .modal-box textarea {
            width: 100%;
            background: var(--paper);
            color: var(--ink);
            border: 1px solid var(--rule-strong);
            padding: 12px 14px;
            font-family: var(--mono);
            font-size: 14px;
            margin-bottom: 14px;
        }
        .modal-box textarea {
            height: 130px;
            font-size: 13px;
            resize: vertical;
        }
        .modal-box input:focus, .modal-box textarea:focus {
            outline: none;
            border-color: var(--ink);
        }
        .modal-actions {
            display: flex;
            gap: 12px;
            margin-top: 16px;
            justify-content: flex-end;
        }
        .modal-msg {
            margin-top: 12px;
            font-family: var(--mono);
            font-size: 13px;
        }
        .diamond {
            display: inline-block;
            width: 12px;
            height: 12px;
            background: var(--red);
            transform: rotate(45deg);
            vertical-align: -2px;
            margin-right: 4px;
        }
        code {
            font-family: var(--mono);
            font-size: 12px;
            background: var(--paper-dim);
            padding: 1px 6px;
        }
        .progress-bar {
            height: 3px;
            background: var(--rule);
            margin-top: 10px;
            overflow: hidden;
        }
        .progress-bar .fill {
            height: 100%;
            background: var(--ink);
            transition: width .3s;
        }
        select, input[type=text], input[type=number], input[type=password] {
            font-family: var(--mono);
            font-size: 13px;
            padding: 6px 10px;
            border: 1px solid var(--rule-strong);
            background: var(--paper);
            color: var(--ink);
        }
        select:focus, input:focus {
            outline: none;
            border-color: var(--ink);
        }
        .mail-admin-shell {
            display: grid;
            grid-template-columns: 300px minmax(340px, 430px) minmax(420px, 1fr);
            gap: 1px;
            background: var(--rule-strong);
            border: 1px solid var(--rule-strong);
            min-height: calc(100vh - 190px);
        }
        .mail-pane {
            background: var(--paper);
            min-height: calc(100vh - 190px);
            max-height: calc(100vh - 190px);
            overflow: auto;
        }
        .mail-pane-head {
            position: sticky;
            top: 0;
            z-index: 2;
            background: var(--paper);
            border-bottom: 1px solid var(--rule-strong);
            padding: 12px 14px;
            font-family: var(--mono);
        }
        .mail-pane-title {
            font-size: 11px;
            color: var(--ink-faint);
            letter-spacing: .18em;
            text-transform: uppercase;
        }
        .mail-pane-sub {
            margin-top: 4px;
            color: var(--ink-soft);
            font-size: 12px;
            word-break: break-all;
        }
        .mail-address-row {
            display: block;
            width: 100%;
            border: 0;
            border-bottom: 1px solid var(--rule);
            background: transparent;
            padding: 11px 14px;
            text-align: left;
            cursor: pointer;
            font-family: var(--mono);
            color: var(--ink);
        }
        .mail-address-row:hover,
        .mail-address-row.active {
            background: var(--paper-dim);
        }
        .mail-address-row.active {
            box-shadow: inset 3px 0 0 var(--red);
        }
        .mail-address-main {
            display: flex;
            justify-content: space-between;
            gap: 8px;
            align-items: center;
            font-size: 12px;
            word-break: break-all;
        }
        .mail-badge {
            min-width: 22px;
            padding: 1px 6px;
            border: 1px solid var(--rule-strong);
            color: var(--ink-soft);
            text-align: center;
            font-size: 11px;
            flex: 0 0 auto;
            background: var(--paper);
        }
        .mail-address-meta {
            margin-top: 4px;
            display: flex;
            justify-content: space-between;
            gap: 8px;
            color: var(--ink-faint);
            font-size: 10px;
        }
        .mail-list-item {
            border: 0;
            border-bottom: 1px solid var(--rule);
            background: transparent;
            width: 100%;
            text-align: left;
            padding: 13px 14px;
            cursor: pointer;
            color: var(--ink);
            font-family: var(--mono);
        }
        .mail-list-item:hover,
        .mail-list-item.active {
            background: var(--paper-dim);
        }
        .mail-subject {
            font-weight: 700;
            font-size: 13px;
            margin-bottom: 5px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .mail-meta {
            color: var(--ink-soft);
            font-size: 11px;
            line-height: 1.5;
            word-break: break-all;
        }
        .mail-preview {
            padding: 18px 20px;
        }
        .mail-preview h2 {
            font-size: 18px;
            margin-bottom: 10px;
            line-height: 1.35;
        }
        .mail-preview-meta {
            border: 1px solid var(--rule);
            background: var(--paper-dim);
            padding: 10px 12px;
            font-family: var(--mono);
            font-size: 12px;
            color: var(--ink-soft);
            line-height: 1.7;
            word-break: break-all;
            margin-bottom: 14px;
        }
        .mail-preview-body {
            border: 1px solid var(--rule);
            background: #fff;
            padding: 12px;
            max-height: calc(100vh - 410px);
            overflow: auto;
        }
        .mail-preview-body iframe {
            width: 100%;
            border: 0;
            background: #fff;
            min-height: 320px;
        }
        .mail-preview-body pre {
            white-space: pre-wrap;
            word-break: break-word;
            font-family: var(--mono);
            margin: 0;
        }
        @media(max-width:768px) {
            body {
                flex-direction: column;
            }
            .sidebar {
                width: 100%;
                flex-direction: row;
                flex-wrap: wrap;
                padding: 14px 18px;
                gap: 6px;
            }
            .sidebar .logo {
                margin-bottom: 0;
                margin-right: auto;
            }
            .main {
                padding: 16px;
            }
            .cards {
                grid-template-columns: repeat(2,1fr);
            }
            .acc-cards {
                grid-template-columns: 1fr;
            }
            .mail-admin-shell {
                grid-template-columns: 1fr;
            }
            .mail-pane {
                min-height: 320px;
                max-height: none;
            }
        }
    </style>
</head>
<body>
    <aside class="sidebar">
        <div class="logo"><div class="icon"></div>iCloud HME</div>
        <a class="nav-item active" data-tab="dashboard">仪表盘</a>
        <a class="nav-item" data-tab="emails">邮箱列表</a>
        <a class="nav-item" data-tab="batch">批量创建</a>
        <a class="nav-item" data-tab="local-inbox">收件箱</a>
        <a class="nav-item" data-tab="docs">API 文档</a>
        <a class="nav-item" data-tab="logs">运行日志</a>
        <div class="section-label">账号列表</div>
        <div id="sidebarAccounts"></div>
        <button class="btn btn-outline btn-sm" onclick="showAddAccountModal()" style="margin:8px 0">+ 添加账号</button>
        <div style="margin-top:auto;padding-top:14px;border-top:1px solid var(--rule-strong);font-family:var(--mono);font-size:12px;color:var(--ink-faint)">
            <div style="margin-bottom:6px">
                <span class="status-dot" id="schedDot"></span>
                <span id="schedLabel">调度器: 就绪</span>
            </div>
            <div style="display:grid;gap:8px;margin-top:10px">
                <div>
                    <div style="font-size:11px;color:var(--ink-faint);margin-bottom:4px">计划模式</div>
                    <select id="schedMode" onchange="renderSchedulerConfigForm()" style="width:100%">
                        <option value="window_random">随机窗口</option>
                        <option value="interval">固定间隔</option>
                    </select>
                </div>
                <div id="schedIntervalGroup" style="display:none">
                    <div style="display:flex;gap:8px">
                        <div style="flex:1">
                            <div style="font-size:11px;color:var(--ink-faint);margin-bottom:4px">间隔(分钟)</div>
                            <input type="number" id="schedIntervalMin" value="60" min="1" max="1440" style="width:100%">
                        </div>
                        <div style="width:86px">
                            <div style="font-size:11px;color:var(--ink-faint);margin-bottom:4px">每轮数量</div>
                            <input type="number" id="schedCount" value="1" min="1" max="20" style="width:100%">
                        </div>
                    </div>
                    <div style="display:flex;gap:8px;margin-top:8px">
                        <div style="width:86px">
                            <div style="font-size:11px;color:var(--ink-faint);margin-bottom:4px">间隔秒</div>
                            <input type="number" id="schedGapSec" value="3" min="0" max="600" style="width:100%">
                        </div>
                        <div style="flex:1">
                            <div style="font-size:11px;color:var(--ink-faint);margin-bottom:4px">标签前缀</div>
                            <input type="text" id="schedLabelPrefix" placeholder="可选" style="width:100%">
                        </div>
                    </div>
                    <div style="font-size:11px;color:var(--ink-faint);margin-top:6px">在所有活跃账号之间轮询；启动后按设定间隔执行。</div>
                </div>
                <div id="schedWindowHint" style="font-size:11px;color:var(--ink-faint)">北京时间 7:00-20:00 自动运行，轮次间隔 60-90 分钟，每轮每账号随机创建 3-5 个。</div>
            </div>
            <div style="display:flex;gap:6px;margin-top:8px">
                <button class="btn btn-outline btn-sm" onclick="saveSchedulerConfig()" style="flex:1">保存计划</button>
                <button class="btn btn-sm" id="btnSched" onclick="toggleScheduler()" style="flex:1">启动调度器</button>
            </div>
        </div>
    </aside>
    <main class="main">
        <div class="header">
            <h1 id="tabTitle">仪表盘</h1>
            <div class="btn-group">
                <button class="btn btn-outline btn-sm" onclick="refreshAll()">刷新</button>
                <button class="btn btn-primary btn-sm" onclick="showAddAccountModal()">+ 添加账号</button>
            </div>
        </div>
        
        <div id="view-dashboard">
            <div class="cards" id="summaryCards"></div>
            <div class="panel" style="margin-top:16px">
                <div class="panel-header">
                    <span>全局邮箱设置</span>
                    <span style="font-size:11px;color:var(--ink-faint)">影响新建记录展示 / 新建别名转发</span>
                </div>
                <div class="panel-body" style="padding:14px">
                    <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
                        <label style="font-size:13px;display:flex;gap:6px;align-items:center">
                            <input type="checkbox" id="aliasSplitEnabled">
                            每个隐私邮箱额外派生
                        </label>
                        <input type="number" id="aliasSplitCount" value="4" min="1" max="20" style="width:70px">
                        <span style="font-size:12px;color:var(--ink-faint)">个变体，例如 <code>name+1@icloud.com</code> ~ <code>name+4@icloud.com</code></span>
                    </div>
                    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px">
                        <label style="font-size:13px;font-family:var(--mono)">转发地址:</label>
                        <select id="forwardToEmail" style="width:360px;max-width:100%">
                            <option value="">加载 Apple 账号转发地址中...</option>
                        </select>
                        <button class="btn btn-outline btn-sm" onclick="refreshForwardOptions(true)">刷新转发地址</button>
                        <button class="btn btn-primary btn-sm" onclick="saveAppSettings()">保存设置</button>
                    </div>
                    <div id="forwardOptionsHint" style="font-size:11px;color:var(--ink-faint);margin-top:6px">
                        转发地址来自 Apple 账号里已绑定/允许的邮箱；留空则使用 iCloud 当前默认转发地址。
                    </div>
                    <div style="font-size:11px;color:var(--ink-faint);margin-top:8px">
                        说明：派生地址为本地加号地址，不会额外消耗 iCloud HME 创建额度；转发地址必须是 Apple 账号里已允许的转发邮箱，否则 Apple 可能拒绝创建。
                    </div>
                </div>
            </div>
            <div class="acc-cards" id="accCards"></div>
        </div>
        
        <div id="view-emails" style="display:none">
            <div class="panel">
                <div class="panel-header">
                    <span>隐私邮箱列表</span>
                    <div style="display:flex;gap:8px;align-items:center">
                        <span style="font-size:11px;color:var(--ink-faint)" id="emailCount">0</span>
                        <button class="btn btn-outline btn-sm" onclick="refreshEmails().then(renderAliasTable)">刷新</button>
                        <button class="btn btn-outline btn-sm" onclick="refreshAliases()" title="从 iCloud 云端同步所有历史别名，并生成派生地址">云端同步历史</button>
                        <button class="btn btn-outline btn-sm" onclick="copyAll()">复制全部</button>
                        <button class="btn btn-outline btn-sm" onclick="exportCSV()">CSV</button>
                        <button class="btn btn-primary btn-sm" onclick="exportCredentials()">导出凭证</button>
                    </div>
                </div>
                <div class="filter-bar">
                    <span style="font-size:11px;color:var(--ink-faint)">筛选账号:</span>
                    <select id="aliasFilter" onchange="_aliasPage=1;renderAliasTable()">
                        <option value="all">全部账号</option>
                    </select>
                    <span style="font-size:11px;color:var(--ink-faint)">搜索:</span>
                    <input id="aliasSearch" type="search" placeholder="邮箱 / 标签 / 账号" oninput="_aliasPage=1;renderAliasTable()" style="min-width:260px">
                </div>
                <div class="panel-body">
                    <div id="aliasTableContainer" class="empty">
                        <div class="icon"></div>暂无创建记录 — 请先通过仪表盘或批量创建生成邮箱
                    </div>
                </div>
            </div>
        </div>
        
        <div id="view-batch" style="display:none">
            <div class="panel">
                <div class="panel-header">
                    <span>跨账号批量创建</span>
                    <span style="font-size:11px;color:var(--ink-faint)" id="batchAccCount">0 个可用账号</span>
                </div>
                <div class="panel-body" style="padding:14px">
                    <p style="font-size:12px;color:var(--ink-faint);margin-bottom:10px">勾选目标账号，设置每个账号的创建数量，点击执行将依次为每个账号创建（账号间间隔 3 秒防限流）。</p>
                    <div class="chk-group" id="batchChkGroup"></div>
                    <div style="display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap">
                        <label style="font-size:12px">每账号创建数量:</label>
                        <input type="number" id="batchCount" value="5" min="1" max="50" style="width:70px">
                        <label style="font-size:13px;font-family:var(--mono)">标签前缀:</label>
                        <input type="text" id="batchLabel" placeholder="可选" style="width:150px">
                        <button class="btn btn-primary" id="btnBatchExec" onclick="execBatchCreate()">开始创建</button>
                    </div>
                    <div id="batchProgress" style="margin-top:14px"></div>
                </div>
            </div>
        </div>
        
        <div id="view-inbox" style="display:none">
            <div class="panel">
                <div class="panel-header">
                    <span>收件箱检查</span>
                    <div style="display:flex;gap:8px;align-items:center">
                        <select id="inboxAccount" onchange="updateInboxAliasSelect(this.value);refreshInbox()"></select>
                        <input type="number" id="inboxLimit" value="20" min="1" max="100" style="width:60px" title="邮件数量">
                        <select id="inboxAliasSelect" onchange="filterInboxByAlias()" style="width:220px">
                            <option value="">全部子账号</option>
                        </select>
                        <button class="btn btn-outline btn-sm" onclick="copySelectedInboxAlias()" title="复制选中的子账号邮箱">复制邮箱</button>
                        <button class="btn btn-outline btn-sm" onclick="refreshInbox()">刷新</button>
                        <button class="btn btn-outline btn-sm" onclick="refreshInbox(true)" title="跳过缓存，从 iCloud 重新拉取">强制刷新</button>
                        <button class="btn btn-outline btn-sm" onclick="checkAliasMail()" title="检查所有隐私别名的收件">全部账户别名</button>
                        <button class="btn btn-outline btn-sm" id="btnInboxSettings" onclick="openInboxSettings()" title="修改 iCloud 邮箱或应用密码">设置</button>
                        <span style="font-size:10px;color:var(--ink-faint);font-family:var(--mono)" id="cacheStatus"></span>
                    </div>
                </div>
                <div class="panel-body">
                    <div id="inboxMsgs" class="empty">
                        <div class="icon"></div>选择账号后点击刷新查看收件箱
                    </div>
                </div>
            </div>
        </div>

        <div id="view-local-inbox" style="display:none">
            <div class="panel">
                <div class="panel-header">
                    <span>Admin 收件箱</span>
                    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                        <button class="btn btn-outline btn-sm" onclick="loadAllLocalInbox()">全部邮件</button>
                        <input type="text" id="localAliasSearch" placeholder="搜索邮箱 / 负责人" style="width:220px">
                        <select id="localAssigneeFilter" style="width:140px"><option value="">全部负责人</option></select>
                        <button class="btn btn-outline btn-sm" onclick="refreshLocalInbox(true)">刷新</button>
                        <button class="btn btn-outline btn-sm" onclick="openWorkerConfig()">Worker 配置</button>
                    </div>
                </div>
                <div class="panel-body" style="padding:14px">
                    <div id="localInboxStats" style="font-size:12px;color:var(--ink-faint);margin-bottom:10px"></div>
                    <div class="mail-admin-shell">
                        <aside class="mail-pane">
                            <div class="mail-pane-head">
                                <div class="mail-pane-title">Mailboxes</div>
                                <div class="mail-pane-sub">点邮箱只看该邮箱；点“全部邮件”看所有入站邮件。</div>
                            </div>
                            <div id="localAliasTable">
                                <div class="empty"><div class="icon"></div>加载邮箱列表...</div>
                            </div>
                        </aside>
                        <section class="mail-pane">
                            <div class="mail-pane-head">
                                <div class="mail-pane-title" id="localMessageTitle">全部邮件</div>
                                <div class="mail-pane-sub" id="localMessageSub">所有邮箱的入站邮件</div>
                            </div>
                            <div id="localMessageList">
                                <div class="empty"><div class="icon"></div>加载邮件列表...</div>
                            </div>
                        </section>
                        <section class="mail-pane">
                            <div class="mail-pane-head">
                                <div class="mail-pane-title">Preview</div>
                                <div class="mail-pane-sub" id="localPreviewSub">选择一封邮件查看正文</div>
                            </div>
                            <div id="localPreview" class="empty">
                                <div class="icon"></div>选择中间列表的一封邮件查看内容
                            </div>
                        </section>
                    </div>
                </div>
            </div>
        </div>
        
        <div id="view-docs" style="display:none">
            <div class="panel" style="font-family:var(--mono);font-size:13px;line-height:1.8">
                <div class="panel-header">
                    <span>API 文档</span>
                </div>
                <div class="panel-body" style="padding:20px 24px" id="docsContent"></div>
            </div>
        </div>
        
        <div id="view-logs" style="display:none">
            <div class="panel">
                <div class="panel-header">
                    <span>实时日志</span>
                    <button class="btn btn-outline btn-sm" onclick="clearLogs()">清屏</button>
                </div>
                <div class="panel-body">
                    <div class="log-feed" id="logFeed"></div>
                </div>
            </div>
        </div>
    </main>
    
    <div class="copy-toast" id="toast"></div>
    
    <script>
        var E = function(id){ return document.getElementById(id); };
        var state = {running:false,creating:false,stopping:false,round_status:'',total_created:0,today_created:0,current_round_created:0,next_trigger:null,scheduler_mode:'window_random',scheduler_interval_minutes:60,scheduler_count_per_run:1,scheduler_account_interval_sec:3.0,scheduler_label_prefix:'',scheduler_selected_accounts:[],alias_split_enabled:false,alias_split_count:4,forward_to_email:''};
        var accounts = [], emails = [], logs = [];
        var forwardOptions = {loaded:false,loading:false,emails:[],selected:'',current:'',accounts:[],error:''};
        var localAliases = [], localMessages = [], localSelectedAlias = '', localSelectedAccount = '', localSelectedMailId = null, localMsgOffset = 0, localMsgLimit = 30, localMsgTotal = 0, localInboxLoaded = false;
        var curTab = 'dashboard', sseConn = null;
        
        document.querySelectorAll('.nav-item').forEach(function(el){
            el.addEventListener('click',function(){
                curTab = this.dataset.tab;
                document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});
                this.classList.add('active');
                
                E('view-dashboard').style.display = curTab==='dashboard'?'block':'none';
                E('view-emails').style.display = curTab==='emails'?'block':'none';
                E('view-batch').style.display = curTab==='batch'?'block':'none';
                E('view-inbox').style.display = curTab==='inbox'?'block':'none';
                E('view-local-inbox').style.display = curTab==='local-inbox'?'block':'none';
                E('view-docs').style.display = curTab==='docs'?'block':'none';
                E('view-logs').style.display = curTab==='logs'?'block':'none';
                
                var titles = {dashboard:'仪表盘',emails:'邮箱列表',batch:'批量创建',inbox:'旧 IMAP 收件箱', 'local-inbox':'收件箱', docs:'API 文档',logs:'运行日志'};
                E('tabTitle').textContent = titles[curTab]||curTab;
                
                if(curTab==='emails'){
                    refreshEmails();
                    renderAliasTable();
                }
                if(curTab==='batch') renderBatchPanel();
                if(curTab==='inbox') updateInboxAccountSelect();
                if(curTab==='local-inbox') refreshLocalInbox();
                if(curTab==='docs') renderDocs();
                if(curTab==='logs') renderLogs();
            });
        });
        
        async function api(path,opts){
            var timeout = (opts||{}).timeout || 60000;
            if(opts) delete opts.timeout;
            var ctrl = new AbortController();
            var t = setTimeout(function(){ctrl.abort()},timeout);
            try {
                var r = await fetch(path,Object.assign({signal:ctrl.signal},opts||{}));
                clearTimeout(t);
                return r.json();
            } catch(e) {
                clearTimeout(t);
                var msg = (e.name==='AbortError')?('请求超时 ('+(timeout/1000)+'s)'):(e.message||'网络错误');
                return {ok:false,error:msg};
            }
        }
        
        async function apiSlow(path,opts){
            return api(path,Object.assign({timeout:60000},opts||{}));
        }

        function collectSchedulerConfig(){
            return {
                mode: E('schedMode') ? E('schedMode').value : 'window_random',
                interval_minutes: Math.max(1, Math.min(1440, parseInt((E('schedIntervalMin')||{}).value || '60', 10) || 60)),
                count_per_run: Math.max(1, Math.min(20, parseInt((E('schedCount')||{}).value || '1', 10) || 1)),
                account_interval_sec: Math.max(0, Math.min(600, parseFloat((E('schedGapSec')||{}).value || '3') || 3)),
                label_prefix: ((E('schedLabelPrefix')||{}).value || '').trim()
            };
        }

        function renderSchedulerConfigForm(){
            if(!E('schedMode')) return;
            var mode = E('schedMode').value || 'window_random';
            E('schedIntervalGroup').style.display = mode==='interval' ? 'block' : 'none';
            E('schedWindowHint').style.display = mode==='window_random' ? 'block' : 'none';
        }

        function applySchedulerState(){
            if(E('schedMode')) E('schedMode').value = state.scheduler_mode || 'window_random';
            if(E('schedIntervalMin')) E('schedIntervalMin').value = state.scheduler_interval_minutes || 60;
            if(E('schedCount')) E('schedCount').value = state.scheduler_count_per_run || 1;
            if(E('schedGapSec')) E('schedGapSec').value = state.scheduler_account_interval_sec || 3;
            if(E('schedLabelPrefix')) E('schedLabelPrefix').value = state.scheduler_label_prefix || '';
            renderSchedulerConfigForm();
        }

        function applyAppSettingsState(){
            if(E('aliasSplitEnabled')) E('aliasSplitEnabled').checked = !!state.alias_split_enabled;
            if(E('aliasSplitCount')) E('aliasSplitCount').value = state.alias_split_count || 4;
            renderForwardOptions();
        }

        function _emailInList(email, list){
            email = String(email||'').toLowerCase();
            return (list||[]).some(function(x){ return String(x||'').toLowerCase()===email; });
        }

        function renderForwardOptions(){
            var sel = E('forwardToEmail');
            if(!sel) return;
            var emails = forwardOptions.emails || [];
            var current = state.forward_to_email || forwardOptions.current || '';
            var preferred = current || forwardOptions.selected || '';
            var before = sel.value || preferred;
            var html = ['<option value="">使用 iCloud 当前默认转发地址</option>'];
            if(preferred && !_emailInList(preferred, emails)){
                html.push('<option value="'+escAttr(preferred)+'">当前保存: '+esc(preferred)+'（未在 Apple 可选列表中）</option>');
            }
            emails.forEach(function(email){
                var label = email;
                if(forwardOptions.selected && String(email).toLowerCase()===String(forwardOptions.selected).toLowerCase()){
                    label += '（Apple 当前默认）';
                }
                html.push('<option value="'+escAttr(email)+'">'+esc(label)+'</option>');
            });
            sel.innerHTML = html.join('');
            sel.value = before || preferred || '';
            if(sel.value && !_emailInList(sel.value, emails) && sel.value !== preferred){
                sel.value = preferred || '';
            }

            var hint = E('forwardOptionsHint');
            if(hint){
                if(forwardOptions.loading){
                    hint.textContent = '正在从 Apple 账号读取已绑定转发邮箱...';
                    hint.style.color = 'var(--ink-faint)';
                } else if(forwardOptions.error){
                    hint.textContent = '读取转发地址失败: '+forwardOptions.error;
                    hint.style.color = 'var(--red)';
                } else if(forwardOptions.loaded){
                    var okAccounts = (forwardOptions.accounts||[]).filter(function(a){return a.ok;}).length;
                    hint.textContent = emails.length
                        ? ('已从 '+okAccounts+' 个账号读取到 '+emails.length+' 个可选转发地址。')
                        : '未读取到 Apple 可选转发地址；请确认账号 Cookie 有效，或留空使用 iCloud 默认转发地址。';
                    hint.style.color = 'var(--ink-faint)';
                } else {
                    hint.textContent = '转发地址来自 Apple 账号里已绑定/允许的邮箱；留空则使用 iCloud 当前默认转发地址。';
                    hint.style.color = 'var(--ink-faint)';
                }
            }
        }

        async function refreshForwardOptions(showToast){
            if(forwardOptions.loading) return;
            forwardOptions.loading = true;
            forwardOptions.error = '';
            renderForwardOptions();
            var d = await api('/api/forward-options',{timeout:120000});
            if(d.ok){
                forwardOptions = {
                    loaded:true,
                    loading:false,
                    emails:d.emails||[],
                    selected:d.selected||'',
                    current:d.current||'',
                    accounts:d.accounts||[],
                    error:''
                };
                if(d.current !== undefined) state.forward_to_email = d.current || '';
                renderForwardOptions();
                if(showToast) toast('转发地址已刷新');
            } else {
                forwardOptions.loading = false;
                forwardOptions.loaded = true;
                forwardOptions.error = d.error || '未知错误';
                renderForwardOptions();
                if(showToast) toast('读取转发地址失败: '+forwardOptions.error,true);
            }
        }
        
        var _refreshBusy = false;
        async function refreshAll(){
            if(_refreshBusy) return;
            _refreshBusy = true;
            try {
                var _a = api('/api/accounts'), _s = api('/api/state');
                var a = await _a, s = await _s;
                accounts = a.accounts||[];
                state = s;
                applySchedulerState();
                applyAppSettingsState();
                renderSidebar();
                renderDashboard();
                if(!forwardOptions.loaded){
                    await refreshForwardOptions(false);
                }
                await refreshEmails();
                if(curTab==='emails'){
                    renderAliasTable();
                }
                if(curTab==='batch') renderBatchPanel();
                if(curTab==='local-inbox') refreshLocalInbox();
                updateInboxAccountSelect();
            } finally {
                _refreshBusy = false;
            }
        }
        
        async function refreshLight(){
            if(_refreshBusy) return;
            var s = await api('/api/state');
            state = s;
            applySchedulerState();
            applyAppSettingsState();
            var sd = E('schedDot');
            var running = state.running;
            sd.className = 'status-dot '+(running?'online':'offline');
            E('schedLabel').textContent = '调度器: '+(state.stopping?'停止收尾':(running?(state.creating?'创建中...':'等待下轮'):'已停止'));
            E('btnSched').textContent = running?'停止调度器':'启动调度器';
            E('btnSched').className = 'btn btn-sm '+(running?'btn-danger':'btn-primary');
        }
        
        async function refreshEmails(){
            var d = await api('/api/emails');
            emails = d.emails||[];
            emails.forEach(function(e){
                var acc = accounts.find(function(a){return a.id===e.account_id});
                e.account_name = acc?(acc.name||acc.real_email||''):(e.account_id||'');
                e.account_email = acc?(acc.real_email||''):'';
            });
            E('emailCount').textContent = emails.length;
            updateEmailFilter();
        }
        
        async function refreshAliases(){
            var d = await api('/api/aliases',{timeout:120000});
            var apiAliases = d.aliases||[];
            if(apiAliases.length){
                var order = [], merged = {};
                function keyOf(e){
                    return String(e.account_id||'')+'|'+String(e.email||'').toLowerCase();
                }
                function putRecord(e, preferIncoming){
                    if(!e || !e.email) return;
                    var k = keyOf(e);
                    if(!merged[k]){
                        order.push(k);
                        merged[k] = Object.assign({}, e);
                        return;
                    }
                    var old = merged[k], next = Object.assign({}, old);
                    Object.keys(e).forEach(function(field){
                        var v = e[field];
                        if(preferIncoming || old[field]===undefined || old[field]===null || old[field]===''){
                            if(v!==undefined && v!==null && v!=='') next[field] = v;
                            if(field==='active' || field==='derived') next[field] = v;
                        }
                    });
                    merged[k] = next;
                }
                emails.forEach(function(e){ putRecord(e,false); });
                apiAliases.forEach(function(a){ putRecord(a,true); });
                emails = order.map(function(k){ return merged[k]; });
            }
            if(d.ok === false) toast('云端同步失败: '+(d.error||'?'),true);
            else toast('云端同步完成: '+(d.base_count||apiAliases.length)+' 个基础别名，含派生共 '+emails.length+' 条');
            E('emailCount').textContent = emails.length;
            updateEmailFilter();
            renderAliasTable();
        }
        
        function renderSidebar(){
            var c = E('sidebarAccounts');
            if(!accounts.length){
                c.innerHTML = '<div style="padding:8px 14px;font-size:11px;color:var(--ink-faint)">暂无账号</div>';
            } else {
                c.innerHTML = accounts.map(function(a,i){
                    var cls = a.status==='active'?'active':'error';
                    var nm = esc(a.name||'未命名');
                    return '<div class="account-item" data-accid="'+escAttr(a.id)+'"><span class="acc-dot '+cls+'"></span><span class="acc-name" title="'+(escAttr(a.real_email)||'')+'">'+nm+'</span><span class="acc-del" title="删除" onclick="event.stopPropagation();removeAccount(\''+escAttr(a.id)+'\')">&times;</span></div>';
                }).join('');
            }
            var sd = E('schedDot');
            sd.className = 'status-dot '+(state.running?'online':'offline');
            var sm = state.stopping?'停止收尾':(state.running?(state.creating?'创建中...':'等待下轮'):'已停止');
            E('schedLabel').textContent = '调度器: '+sm;
            var bs = E('btnSched');
            bs.textContent = state.running?'停止调度器':'启动调度器';
            bs.className = 'btn btn-sm '+(state.running?'btn-danger':'btn-primary');
            applySchedulerState();
        }
        
        function renderDashboard(){
            applyAppSettingsState();
            var summary = {account_count:accounts.length,active_accounts:0,error_accounts:0,total_aliases:0,total_active_aliases:0};
            accounts.forEach(function(a){
                if(a.status==='active') summary.active_accounts++;
                else if(a.status==='error') summary.error_accounts++;
                summary.total_aliases += (a.alias_total||0);
                summary.total_active_aliases += (a.alias_active||0);
            });
            E('summaryCards').innerHTML = '<div class="card"><div class="label">账号总数</div><div class="value blue">'+summary.account_count+'</div><div class="sub">活跃 '+summary.active_accounts+' / 异常 '+summary.error_accounts+'</div></div><div class="card"><div class="label">隐私邮箱总数</div><div class="value accent">'+summary.total_aliases+'</div><div class="sub">活跃 '+summary.total_active_aliases+'</div></div><div class="card"><div class="label">累计创建</div><div class="value">'+(state.total_created||0)+'</div><div class="sub">历史总计</div></div><div class="card"><div class="label">今日创建</div><div class="value green">'+(state.today_created||0)+'</div><div class="sub" id="schedInfo">'+esc(state.round_status||'--')+'</div></div>';
            if(!accounts.length){
                E('accCards').innerHTML = '<div class="empty"><div class="icon"></div>还没有添加账号<br><span style="font-size:12px">点击右上角 "+ 添加账号" 开始</span></div>';
            } else {
                E('accCards').innerHTML = accounts.map(function(a){
                    var stCls = a.status==='active'?'ok':'err';
                    var stText = a.status==='active'?'正常':(a.last_error||'异常');
                    var email = a.real_email||'?';
                    return '<div class="acc-card"><div class="acc-header"><div><div class="acc-title">'+esc(a.name||'未命名')+'</div><div class="acc-email">'+esc(email)+'</div></div><span class="status-badge '+stCls+'" title="'+escAttr(stText)+'">'+esc(stText.substring(0,20))+'</span></div><div class="acc-stats"><div class="acc-stat">别名: <span class="n">'+(a.alias_total||0)+'</span></div><div class="acc-stat">活跃: <span class="n" style="color:var(--green)">'+(a.alias_active||0)+'</span></div></div><div class="acc-actions"><button class="btn btn-outline btn-xs" onclick="createForAccount(\''+escAttr(a.id)+'\',1)">创建 1 个</button><button class="btn btn-outline btn-xs" onclick="createForAccount(\''+escAttr(a.id)+'\',5)">创建 5 个</button><button class="btn btn-outline btn-xs" onclick="validateAccount(\''+escAttr(a.id)+'\')">校验</button><button class="btn btn-outline btn-xs" onclick="showUpdateCookieModal(\''+escAttr(a.id)+'\')">更新 Cookie</button><button class="btn btn-danger btn-xs" onclick="removeAccount(\''+escAttr(a.id)+'\')">删除账号</button></div></div>';
                }).join('');
            }
        }
        
        function updateEmailFilter(){
            var sel = E('aliasFilter'), old = sel.value;
            sel.innerHTML = '<option value="all">全部账号 ('+emails.length+')</option>';
            var byAcc = {};
            emails.forEach(function(e){
                var ak = e.account_id||'?';
                byAcc[ak] = (byAcc[ak]||0)+1;
            });
            Object.keys(byAcc).forEach(function(ak){
                var acc = accounts.find(function(x){return x.id===ak});
                var label = acc?(acc.name||acc.real_email||ak):ak;
                sel.innerHTML += '<option value="'+escAttr(ak)+'">'+esc(label)+' ('+byAcc[ak]+')</option>';
            });
            sel.value = old||'all';
        }
        
        var _aliasPage = 1;
        var _aliasPerPage = 20;

        function getFilteredAliases(){
            var filterEl = E('aliasFilter');
            var filter = filterEl ? filterEl.value : 'all';
            var qEl = E('aliasSearch');
            var q = (qEl ? qEl.value : '').trim().toLowerCase();
            return emails.filter(function(e){
                if(filter !== 'all' && e.account_id !== filter) return false;
                if(!q) return true;
                var hay = [
                    e.email || '',
                    e.label || '',
                    e.account_name || '',
                    e.account_email || '',
                    e.account_id || '',
                    e.forwardToEmail || '',
                    e.forward_to || '',
                    e.source || '',
                    e.derived ? '派生 derived plus' : ''
                ].join(' ').toLowerCase();
                return hay.indexOf(q) >= 0;
            });
        }
        
        function aliasPageNav(delta){
            _aliasPage += delta;
            renderAliasTable();
        }
        
        function renderAliasTable(){
            updateEmailFilter();
            var filtered = getFilteredAliases();
            E('emailCount').textContent = filtered.length+' / '+emails.length;
            var c = E('aliasTableContainer');
            if(!filtered.length){
                c.innerHTML = '<div class="empty"><div class="icon"></div>暂无邮箱记录 — 请先创建，或点击“云端同步历史”拉取之前注册过的隐私邮箱</div>';
                return;
            }
            var totalPages = Math.ceil(filtered.length / _aliasPerPage);
            if(_aliasPage < 1) _aliasPage = 1;
            if(_aliasPage > totalPages) _aliasPage = totalPages;
            var pageItems = filtered.slice((_aliasPage-1)*_aliasPerPage, _aliasPage*_aliasPerPage);
            var h = '<table class="email-table"><thead><tr><th>#</th><th>邮箱地址</th><th>所属账号</th><th>标签</th><th>状态</th><th></th></tr></thead><tbody>';
            pageItems.forEach(function(e,i){
                var accName = e.account_name||e.account_email||e.account_id||'--';
                var statusHtml = e.hasOwnProperty('active')?(e.active?'<span style="color:var(--green)">活跃</span>':'<span style="color:var(--red)">停用</span>'):'<span style="color:var(--ink-faint)">--</span>';
                h += '<tr><td style="color:var(--ink-faint);width:40px">'+((_aliasPage-1)*_aliasPerPage+i+1)+'</td><td class="mono">'+esc(e.email||'')+'</td><td style="font-size:11px">'+esc(accName)+'</td><td style="font-size:11px;color:var(--ink-faint)">'+esc((e.label||'').substring(0,30))+'</td><td>'+statusHtml+'</td><td style="width:132px"><button class="copy-btn" onclick="copyOne(\''+escAttr(e.email)+'\')" title="复制邮箱">复制</button> <button class="copy-btn" onclick="copyAutoLogin(\''+escAttr(e.email)+'\',\''+escAttr(e.account_id||'')+'\',\''+escAttr(e.label||'')+'\')" title="生成并复制自动登录链接">登录链接</button></td></tr>';
            });
            h += '</tbody></table>';
            if(totalPages > 1){
                h += '<div class="pagination-bar">';
                h += '<button onclick="aliasPageNav(-1)"' + (_aliasPage<=1?' disabled':'') + '>← 上一页</button>';
                h += '<span>第 '+_aliasPage+' / '+totalPages+' 页 (共 '+filtered.length+' 条)</span>';
                h += '<button onclick="aliasPageNav(1)"' + (_aliasPage>=totalPages?' disabled':'') + '>下一页 →</button>';
                h += '</div>';
            }
            c.innerHTML = h;
        }
        
        function renderBatchPanel(){
            var activeAccs = accounts.filter(function(a){return a.status==='active'});
            E('batchAccCount').textContent = activeAccs.length+' 个可用账号';
            var g = E('batchChkGroup');
            if(!activeAccs.length){
                g.innerHTML = '<span style="font-size:12px;color:var(--ink-faint)">没有活跃账号，请先添加</span>';
                E('btnBatchExec').disabled = true;
            } else {
                g.innerHTML = activeAccs.map(function(a){
                    var email = a.real_email||a.name||a.id;
                    return '<label class="chk-item"><input type="checkbox" value="'+escAttr(a.id)+'" checked> <span title="'+escAttr(email)+'">'+esc(a.name||email.substring(0,20))+'</span></label>';
                }).join('');
                E('btnBatchExec').disabled = false;
            }
        }
        
        async function execBatchCreate(){
            var checks = document.querySelectorAll('#batchChkGroup input:checked');
            var ids = [];
            checks.forEach(function(c){ids.push(c.value)});
            if(!ids.length){ toast('请勾选至少一个账号',true); return; }
            var count = parseInt(E('batchCount').value)||5;
            var label = E('batchLabel').value.trim();
            var btn = E('btnBatchExec');
            btn.disabled = true;
            btn.textContent = '创建中...';
            var d = await apiSlow('/api/create-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_ids:ids,count_per_account:count,label:label})});
            btn.disabled = false;
            btn.textContent = '开始创建';
            if(d.ok){
                toast('创建完成: '+d.total_created+' 个成功');
            } else {
                toast('失败: '+(d.error||'?'),true);
            }
            refreshAll();
        }

        async function refreshLocalInbox(force){
            if(!E('localAliasTable')) return;
            var q = ((E('localAliasSearch')||{}).value||'').trim();
            var assignee = ((E('localAssigneeFilter')||{}).value||'').trim();
            var d = await api('/api/local-inbox/summary?q='+encodeURIComponent(q)+'&assignee='+encodeURIComponent(assignee));
            if(!d.ok){
                E('localAliasTable').innerHTML = '<div class="empty"><div class="icon"></div>'+esc(d.error||'加载失败')+'</div>';
                return;
            }
            localAliases = d.aliases||[];
            renderLocalAssigneeFilter(d.assignees||[]);
            renderLocalAliasTable(d.stats||{});
            if(!localInboxLoaded || force){
                localInboxLoaded = true;
                localMsgOffset = 0;
                loadLocalMessages(localSelectedAlias || '', 0);
            }
        }

        function renderLocalAssigneeFilter(assignees){
            var sel = E('localAssigneeFilter');
            if(!sel) return;
            var old = sel.value || '';
            var html = '<option value="">全部负责人</option>';
            assignees.forEach(function(name){ html += '<option value="'+escAttr(name)+'">'+esc(name)+'</option>'; });
            sel.innerHTML = html;
            sel.value = old;
            sel.onchange = function(){ localMsgOffset=0; refreshLocalInbox(true); };
            var search = E('localAliasSearch');
            if(search && !search.dataset.bound){
                search.dataset.bound = '1';
                search.addEventListener('keydown', function(e){ if(e.key==='Enter'){ localMsgOffset=0; refreshLocalInbox(true); } });
            }
        }

        function renderLocalAliasTable(stats){
            var box = E('localAliasTable');
            if(!box) return;
            if(E('localInboxStats')){
                E('localInboxStats').textContent = '全部邮件 '+(stats.total_mails||0)+' 封 / 已收件邮箱 '+(stats.alias_count||0)+' 个 / 可分发邮箱 '+(stats.known_alias_count||localAliases.length||0)+' 个 / 已启用分享 '+(stats.share_count||0)+' 个';
            }
            var h = '';
            h += '<button class="mail-address-row '+(!localSelectedAlias?'active':'')+'" id="localAllInboxBtn">'
              + '<div class="mail-address-main"><span>全部邮件</span><span class="mail-badge">'+(stats.total_mails||0)+'</span></div>'
              + '<div class="mail-address-meta"><span>Admin Inbox</span><span>所有邮箱</span></div></button>';
            if(!localAliases.length){
                h += '<div class="empty"><div class="icon"></div>暂无邮箱。配置 Worker 后，收到的邮件会显示在这里。</div>';
                box.innerHTML = h;
                var allBtn = E('localAllInboxBtn'); if(allBtn) allBtn.onclick = loadAllLocalInbox;
                return;
            }
            localAliases.forEach(function(a){
                var alias = a.alias || '';
                var active = alias===localSelectedAlias ? 'active' : '';
                var assignee = a.assignee || '未分配';
                var latest = (a.latest_at||'').substring(0,16) || '暂无邮件';
                h += '<button class="mail-address-row local-address-btn '+active+'" data-alias="'+escAttr(alias)+'">'
                  + '<div class="mail-address-main"><span>'+esc(alias)+'</span><span class="mail-badge">'+(a.mail_count||0)+'</span></div>'
                  + '<div class="mail-address-meta"><span>'+esc(assignee)+'</span><span>'+esc(latest)+'</span></div>'
                  + '</button>';
            });
            box.innerHTML = h;
            var allBtn = E('localAllInboxBtn'); if(allBtn) allBtn.onclick = loadAllLocalInbox;
            box.querySelectorAll('.local-address-btn').forEach(function(btn){
                btn.addEventListener('click', function(){ selectLocalAlias(this.getAttribute('data-alias')||''); });
            });
        }

        function loadAllLocalInbox(){
            localSelectedAlias = '';
            localSelectedAccount = '';
            localSelectedMailId = null;
            localMsgOffset = 0;
            if(E('localPreview')) E('localPreview').innerHTML = '<div class="empty"><div class="icon"></div>选择中间列表的一封邮件查看内容</div>';
            renderLocalAliasTable({total_mails:state.local_mail_count||0, alias_count:state.local_mail_alias_count||0, share_count:state.local_mail_share_count||0, known_alias_count:localAliases.length});
            loadLocalMessages('', 0);
        }

        function selectLocalAlias(alias){
            if(!alias){ loadAllLocalInbox(); return; }
            localSelectedAlias = alias;
            localMsgOffset = 0;
            localSelectedMailId = null;
            var row = localAliases.find(function(a){return a.alias===alias});
            localSelectedAccount = row ? (row.account_id||'') : '';
            if(E('localPreview')) E('localPreview').innerHTML = '<div class="empty"><div class="icon"></div>选择中间列表的一封邮件查看内容</div>';
            renderLocalAliasTable({total_mails:state.local_mail_count||0, alias_count:state.local_mail_alias_count||0, share_count:state.local_mail_share_count||0, known_alias_count:localAliases.length});
            loadLocalMessages(alias, 0);
        }

        async function loadLocalMessages(alias, offset){
            alias = alias || '';
            localSelectedAlias = alias;
            localMsgOffset = Math.max(0, offset||0);
            var title = alias ? alias : '全部邮件';
            var sub = alias ? '只看这个邮箱的邮件' : '所有邮箱的入站邮件';
            if(E('localMessageTitle')) E('localMessageTitle').textContent = title;
            if(E('localMessageSub')) E('localMessageSub').textContent = sub;
            if(E('localMessageList')) E('localMessageList').innerHTML = '<div class="empty"><div class="icon"></div>加载邮件列表...</div>';
            var url = '/api/local-inbox/messages?limit='+localMsgLimit+'&offset='+localMsgOffset;
            if(alias) url += '&alias='+encodeURIComponent(alias);
            var d = await api(url);
            if(!d.ok){
                E('localMessageList').innerHTML = '<div class="empty"><div class="icon"></div>'+esc(d.error||'加载失败')+'</div>';
                return;
            }
            localMessages = d.messages||[];
            localMsgTotal = d.count||0;
            renderLocalMessages();
        }

        function localMsgPage(delta){
            var next = localMsgOffset + delta*localMsgLimit;
            if(next < 0) next = 0;
            if(next >= localMsgTotal) next = Math.max(0, localMsgTotal-localMsgLimit);
            loadLocalMessages(localSelectedAlias, next);
        }

        function renderLocalMessages(){
            var box = E('localMessageList');
            if(!box) return;
            if(!localMessages.length){
                box.innerHTML = '<div class="empty"><div class="icon"></div>'+(localSelectedAlias?'这个邮箱暂无邮件':'暂无入站邮件')+'</div>';
                return;
            }
            var h = '<div style="font-size:11px;color:var(--ink-faint);padding:9px 14px;border-bottom:1px solid var(--rule)">共 '+localMsgTotal+' 封，当前 '+(localMsgOffset+1)+'-'+Math.min(localMsgOffset+localMsgLimit,localMsgTotal)+'</div>';
            localMessages.forEach(function(m){
                var active = String(m.id)===String(localSelectedMailId) ? 'active' : '';
                h += '<button class="mail-list-item local-mail-btn '+active+'" data-id="'+escAttr(m.id)+'">'
                  + '<div class="mail-subject">'+esc(m.subject||'(无主题)')+'</div>'
                  + '<div class="mail-meta">From: '+esc(m.from||m.sender_name||'')+'</div>'
                  + '<div class="mail-meta">To: '+esc(m.hme_alias||'')+'</div>'
                  + '<div class="mail-meta">'+esc((m.created_at||'').substring(0,19))+(m.assignee?' · '+esc(m.assignee):'')+'</div>'
                  + '</button>';
            });
            if(localMsgTotal > localMsgLimit){
                h += '<div class="pagination-bar"><button onclick="localMsgPage(-1)" '+(localMsgOffset<=0?'disabled':'')+'>← 上一页</button><span>'+Math.floor(localMsgOffset/localMsgLimit+1)+' / '+Math.ceil(localMsgTotal/localMsgLimit)+'</span><button onclick="localMsgPage(1)" '+(localMsgOffset+localMsgLimit>=localMsgTotal?'disabled':'')+'>下一页 →</button></div>';
            }
            box.innerHTML = h;
            box.querySelectorAll('.local-mail-btn').forEach(function(btn){
                btn.addEventListener('click', function(){ openLocalMail(this.getAttribute('data-id')); });
            });
            if(!localSelectedMailId && localMessages.length){
                openLocalMail(localMessages[0].id);
            }
        }

        async function openLocalMail(msgId){
            if(!msgId) return;
            localSelectedMailId = String(msgId);
            document.querySelectorAll('.local-mail-btn').forEach(function(btn){
                btn.classList.toggle('active', String(btn.getAttribute('data-id'))===String(msgId));
            });
            var preview = E('localPreview');
            if(preview) preview.innerHTML = '<div class="empty"><div class="icon"></div>加载邮件内容...</div>';
            var d = await apiSlow('/api/local-inbox/messages/'+encodeURIComponent(msgId));
            if(!d.ok || !d.message){
                if(preview) preview.innerHTML = '<div class="empty"><div class="icon"></div>'+(d.error||'加载失败')+'</div>';
                return;
            }
            renderLocalPreview(d.message);
        }

        function renderLocalPreview(m){
            var preview = E('localPreview');
            if(!preview) return;
            if(E('localPreviewSub')) E('localPreviewSub').textContent = (m.hme_alias||'') + ' · ' + ((m.created_at||'').substring(0,19));
            var h = '<div class="mail-preview">'
              + '<div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;margin-bottom:10px">'
              + '<h2>'+esc(m.subject||'(无主题)')+'</h2>'
              + '<button class="copy-btn" id="btnShareCurrentAlias">分配/分享</button>'
              + '</div>'
              + '<div class="mail-preview-meta">'
              + '<div><b>From:</b> '+esc(m.source_from||m.sender_name||m.from||'')+'</div>'
              + '<div><b>To:</b> '+esc(m.hme_alias||'')+'</div>'
              + '<div><b>Time:</b> '+esc((m.created_at||'').substring(0,19))+'</div>'
              + (m.assignee?'<div><b>Assignee:</b> '+esc(m.assignee)+'</div>':'')
              + '</div>'
              + '<div class="mail-preview-body" id="localPreviewBody"></div>'
              + '</div>';
            preview.className = '';
            preview.innerHTML = h;
            var shareBtn = E('btnShareCurrentAlias');
            if(shareBtn) shareBtn.onclick = function(){ openShareModal(m.hme_alias||''); };
            var body = E('localPreviewBody');
            if(m.html){
                var iframe = document.createElement('iframe');
                iframe.sandbox = 'allow-popups';
                iframe.srcdoc = m.html;
                body.innerHTML = '';
                body.appendChild(iframe);
                iframe.onload = function(){ setTimeout(function(){ try{ iframe.style.height=(iframe.contentDocument.documentElement.scrollHeight+20)+'px'; }catch(e){ iframe.style.height='600px'; } },120); };
            }else{
                body.innerHTML = '<pre>'+esc(m.text||'(无正文)')+'</pre>';
            }
        }

        function openShareModal(alias){
            var row = localAliases.find(function(a){return a.alias===alias}) || {alias:alias};
            var h = '<div class="modal-overlay" id="shareModal" onclick="if(event.target===this)closeShareModal()"><div class="modal-box"><h3><i class="diamond"></i> 分配 / 分享收件箱</h3>'
              + '<p>隐私邮箱: <b>'+esc(alias)+'</b><br>分享链接是只读访问，只能看到这个隐私邮箱的邮件。</p>'
              + '<input type="hidden" id="shareAlias" value="'+escAttr(alias)+'">'
              + '<input type="hidden" id="shareAccountId" value="'+escAttr(row.account_id||localSelectedAccount||'')+'">'
              + '<label style="font-size:11px;color:var(--ink-faint);font-family:var(--mono)">分配给</label><input type="text" id="shareAssignee" value="'+escAttr(row.assignee||'')+'" placeholder="例如 张三 / 客户A">'
              + '<label style="font-size:11px;color:var(--ink-faint);font-family:var(--mono)">备注</label><input type="text" id="shareNote" value="'+escAttr(row.note||'')+'" placeholder="可选">'
              + '<label style="display:flex;gap:8px;align-items:center;margin-top:8px;font-size:13px"><input type="checkbox" id="shareEnabled" '+(row.share_enabled?'checked':'')+'> 启用分享链接</label>'
              + (row.share_url?'<div style="margin-top:8px;font-size:11px;word-break:break-all;color:var(--ink-faint)">当前链接: <code>'+esc(row.share_url)+'</code></div>':'')
              + '<div class="modal-actions"><button class="btn btn-outline" onclick="closeShareModal()">取消</button><button class="btn btn-outline" onclick="saveShare(true)">重置链接</button><button class="btn btn-primary" onclick="saveShare(false)">保存</button></div><div class="modal-msg" id="shareMsg"></div></div></div>';
            document.body.insertAdjacentHTML('beforeend', h);
        }

        function closeShareModal(){ var m=E('shareModal'); if(m) m.remove(); }

        async function saveShare(regenerate){
            var payload = {
                alias: E('shareAlias').value,
                account_id: ((E('shareAccountId')||{}).value || localSelectedAccount || ''),
                assignee: E('shareAssignee').value.trim(),
                note: E('shareNote').value.trim(),
                enabled: !!E('shareEnabled').checked,
                regenerate: !!regenerate
            };
            var d = await api('/api/local-inbox/share',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
            if(!d.ok){ E('shareMsg').innerHTML = '<span style="color:var(--red)">'+esc(d.error||'保存失败')+'</span>'; return; }
            var link = d.share.share_url || '';
            E('shareMsg').innerHTML = '<span style="color:var(--green)">已保存</span>'+(link?'<div style="word-break:break-all;margin-top:6px"><code>'+esc(link)+'</code></div>':'');
            if(link) navigator.clipboard.writeText(link).then(function(){ toast('分享链接已复制'); });
            await refreshLocalInbox(true);
        }

        async function openWorkerConfig(){
            var d = await api('/api/inbound-config');
            if(!d.ok){ toast('读取配置失败',true); return; }
            var code = 'INBOUND_URL='+d.inbound_url+'\nINBOUND_TOKEN='+d.token+'\nWorker 模板: '+d.worker_template;
            window._workerConfigText = code;
            var h = '<div class="modal-overlay" id="workerModal" onclick="if(event.target===this)closeWorkerModal()"><div class="modal-box"><h3><i class="diamond"></i> Cloudflare Email Worker 配置</h3>'
              + '<p>Cloudflare Email Routing 已将 <b>inbox@mail.armsg.yueseng-ys.com</b> 路由到 Worker；这里保留配置方便排查。</p>'
              + '<label style="font-size:11px;color:var(--ink-faint);font-family:var(--mono)">INBOUND_URL</label><input type="text" readonly value="'+escAttr(d.inbound_url)+'">'
              + '<label style="font-size:11px;color:var(--ink-faint);font-family:var(--mono)">INBOUND_TOKEN</label><input type="text" readonly value="'+escAttr(d.token)+'">'
              + '<label style="font-size:11px;color:var(--ink-faint);font-family:var(--mono)">Worker 模板</label><input type="text" readonly value="'+escAttr(d.worker_template)+'">'
              + '<div style="font-size:11px;color:var(--ink-faint);margin-top:8px">当前本机收件: '+(d.stats.total_mails||0)+' 封 / '+(d.stats.alias_count||0)+' 个邮箱。</div>'
              + '<div class="modal-actions"><button class="btn btn-outline" onclick="closeWorkerModal()">关闭</button><button class="btn btn-outline" onclick="regenerateInboundToken()">重置 Token</button><button class="btn btn-primary" onclick="copyWorkerConfig()">复制配置</button></div></div></div>';
            document.body.insertAdjacentHTML('beforeend', h);
        }

        function closeWorkerModal(){ var m=E('workerModal'); if(m) m.remove(); }

        function copyWorkerConfig(){ navigator.clipboard.writeText(window._workerConfigText||'').then(function(){toast('配置已复制')}); }

        async function regenerateInboundToken(){
            if(!confirm('确认重置 INBOUND_TOKEN？\n重置后 Cloudflare Worker 必须同步更新，否则无法投递邮件。')) return;
            var d = await api('/api/inbound-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({regenerate_token:true})});
            if(d.ok){ closeWorkerModal(); openWorkerConfig(); toast('Token 已重置'); }
            else toast('重置失败',true);
        }

        var _inboxBusy = false;
        var _inboxSse = null;
        var _inboxStreamMsgs = [];
        var _inboxPage = 1;
        var _inboxPerPage = 10;
        var _inboxFiltered = [];
        
        function refreshInbox(force){
            if(_inboxBusy) return;
            var accId = E('inboxAccount').value;
            if(!accId){
                E('inboxMsgs').innerHTML = '<div class="empty"><div class="icon"></div>请先选择账号</div>';
                return;
            }
            if(force){
                _inboxBusy = true;
                var limit = parseInt(E('inboxLimit').value)||20;
                E('inboxMsgs').innerHTML = '<div class="empty"><div class="icon"></div>强制刷新中...</div>';
                apiSlow('/api/accounts/'+encodeURIComponent(accId)+'/inbox?limit='+limit+'&force=1').then(function(d){
                    _inboxBusy = false;
                    if(d.error){
                        E('inboxMsgs').innerHTML = '<div class="empty"><div class="icon"></div>'+esc(d.error||'连接失败')+'</div>';
                        return;
                    }
                    _inboxStreamMsgs = d.emails || [];
                    filterInboxByAlias(false);
                    updateCacheStatus(d.cached);
                });
                return;
            }
            startInboxStream(accId);
        }
        
        function startInboxStream(accId){
            if(_inboxSse){ _inboxSse.close(); _inboxSse=null; }
            _inboxBusy = true;
            _inboxStreamMsgs = [];
            E('inboxMsgs').innerHTML = '<div class="empty"><div class="icon"></div>正在逐条拉取邮件...</div>';
            var limit = parseInt(E('inboxLimit').value)||20;
            _inboxSse = new EventSource('/api/accounts/'+encodeURIComponent(accId)+'/inbox-stream?limit='+limit);
            _inboxSse.onmessage = function(e){
                try {
                    var d = JSON.parse(e.data);
                    if(d.type==='start'){}
                    else if(d.type==='email'){
                        _inboxStreamMsgs.push(d.email);
                        filterInboxByAlias(true);
                    }else if(d.type==='done'){
                        _inboxSse.close();
                        _inboxSse = null;
                        _inboxBusy = false;
                        filterInboxByAlias(false);
                    }else if(d.type==='error'){
                        _inboxSse.close();
                        _inboxSse = null;
                        _inboxBusy = false;
                        E('inboxMsgs').innerHTML = '<div class="empty"><div class="icon"></div>'+esc(d.error||'连接失败')+'</div>';
                    }
                } catch(_) {}
            };
            _inboxSse.onerror = function(){
                if(_inboxSse){ _inboxSse.close(); _inboxSse = null; }
                _inboxBusy = false;
                if(_inboxStreamMsgs.length){
                    filterInboxByAlias(false);
                } else {
                    E('inboxMsgs').innerHTML = '<div class="empty"><div class="icon"></div>连接失败</div>';
                }
            };
        }
        
        async function checkAliasMail(){
            if(_inboxBusy) return;
            _inboxBusy = true;
            try {
                var accId = E('inboxAccount').value;
                if(!accId){
                    _inboxBusy = false;
                    E('inboxMsgs').innerHTML = '<div class="empty"><div class="icon"></div>请先选择账号</div>';
                    return;
                }
                E('inboxMsgs').innerHTML = '<div class="empty"><div class="icon"></div>正在检查各别名的收件...</div>';
                var d = await apiSlow('/api/accounts/'+encodeURIComponent(accId)+'/alias-mail');
                if(d.error){
                    E('inboxMsgs').innerHTML = '<div class="empty"><div class="icon"></div>'+esc(d.error||'查询失败')+'</div>';
                    return;
                }
                var byAlias = d.by_alias||{};
                var total = 0;
                var aliasKeys = Object.keys(byAlias);
                if(!aliasKeys.length){
                    E('inboxMsgs').innerHTML = '<div class="empty"><div class="icon"></div>所有隐私邮箱暂无收件</div>';
                    return;
                }
                var h = '';
                aliasKeys.forEach(function(alias){
                    var msgs = byAlias[alias]||[];
                    total += msgs.length;
                    h += '<div style="padding:8px 14px;border-bottom:1px solid var(--rule);font-weight:600;font-size:13px;background:var(--paper-dim)">'+esc(alias)+' ('+msgs.length+' 封)</div>';
                    msgs.forEach(function(m){
                        h += '<div style="padding:6px 20px;border-bottom:1px solid var(--rule);font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px"><span><strong>'+esc(m.subject||'(无主题)')+'</strong></span><span style="color:var(--ink-soft)">'+esc(m.from||'').substring(0,30)+'</span><span style="color:var(--ink-faint);font-size:11px">'+(m.date||'').substring(0,19)+'</span></div>';
                    });
                });
                E('inboxMsgs').innerHTML = '<div style="font-size:11px;color:var(--ink-faint);padding:8px 14px;border-bottom:1px solid var(--rule)">共 '+aliasKeys.length+' 个别名收到 '+total+' 封邮件</div>'+h;
            } finally {
                _inboxBusy = false;
            }
        }
        
        function inboxPageNav(delta){
            _inboxPage += delta;
            renderInboxPage();
        }
        
        function renderInboxPage(){
            var msgs = _inboxFiltered;
            var totalPages = Math.ceil(msgs.length / _inboxPerPage);
            if(_inboxPage < 1) _inboxPage = 1;
            if(_inboxPage > totalPages) _inboxPage = totalPages;
            if(_inboxPage < 1) _inboxPage = 1;
            var pageItems = msgs.slice((_inboxPage-1)*_inboxPerPage, _inboxPage*_inboxPerPage);
            var h = '<div style="font-size:11px;color:var(--ink-faint);padding:8px 16px;border-bottom:1px solid var(--rule)">'+esc(_inboxTitle)+'</div>';
            pageItems.forEach(function(m,i){
                var globalIdx = (_inboxPage-1)*_inboxPerPage + i;
                var mid = m.id||'m'+globalIdx;
                h += '<div class="email-item" style="border-bottom:1px solid var(--rule);cursor:pointer" onclick="toggleEmail(\''+escAttr(mid)+'\',\''+escAttr(m.id||'')+'\')">'
                  + '<div style="padding:12px 16px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px">'
                  + '<div style="flex:1;min-width:0">'
                  + '<div style="font-weight:600;font-size:14px;margin-bottom:4px">'+esc(m.subject||'(无主题)')+'</div>'
                  + '<div style="font-size:12px;color:var(--ink-soft)">'+esc(m.from||'')+'</div>'
                  + '<div style="font-size:11px;color:var(--ink-faint);margin-top:2px;display:flex;align-items:center;gap:6px">To: <span>'+esc((m.to||'').substring(0,50))+'</span><button class="copy-btn" style="padding:1px 6px;font-size:10px;" onclick="event.stopPropagation();copyOne(\''+escAttr(m.to||'')+'\')">复制</button></div>'
                  + '</div>'
                  + '<div style="font-size:11px;color:var(--ink-faint);white-space:nowrap;text-align:right">'+(m.date||'').substring(0,19)+'</div>'
                  + '</div>'
                  + '<div id="'+escAttr(mid)+'_body" style="display:none;padding:0 16px 16px;font-size:13px;line-height:1.7;color:var(--ink-soft);max-height:600px;overflow-y:auto;border-top:1px solid var(--rule)"></div>'
                  + '</div>';
            });
            if(totalPages > 1){
                h += '<div class="pagination-bar">';
                h += '<button onclick="inboxPageNav(-1)"' + (_inboxPage<=1?' disabled':'') + '>← 上一页</button>';
                h += '<span>第 '+_inboxPage+' / '+totalPages+' 页 (共 '+msgs.length+' 封)</span>';
                h += '<button onclick="inboxPageNav(1)"' + (_inboxPage>=totalPages?' disabled':'') + '>下一页 →</button>';
                h += '</div>';
            }
            E('inboxMsgs').innerHTML = h;
        }
        
        var _inboxTitle = '';
        
        function renderInboxMsgs(msgs,title){
            _inboxFiltered = msgs;
            _inboxTitle = title;
            if(!msgs.length){
                E('inboxMsgs').innerHTML = '<div class="empty"><div class="icon"></div>收件箱为空</div>';
                return;
            }
            renderInboxPage();
        }
        
        var _expandedEmail = null;
        
        async function toggleEmail(domId,msgId){
            var bodyEl = E(domId+'_body');
            if(!bodyEl) return;
            if(_expandedEmail&&_expandedEmail!==domId){
                var prev = E(_expandedEmail+'_body');
                if(prev) prev.style.display = 'none';
            }
            if(bodyEl.style.display==='block'){
                bodyEl.style.display = 'none';
                _expandedEmail = null;
                return;
            }
            bodyEl.style.display = 'block';
            _expandedEmail = domId;
            
            if(bodyEl.innerHTML.trim()&&bodyEl.innerHTML!=='加载中...') return;
            bodyEl.innerHTML = '加载中...';
            if(!msgId){
                bodyEl.innerHTML = '(无法获取邮件正文)';
                return;
            }
            var accId = E('inboxAccount').value;
            if(!accId){
                bodyEl.innerHTML = '(请先选择账号)';
                return;
            }
            var d = await apiSlow('/api/accounts/'+encodeURIComponent(accId)+'/message/'+encodeURIComponent(msgId));
            if(!d.ok||!d.message){
                bodyEl.innerHTML = '(获取失败: '+(d.error||'未知')+')';
                return;
            }
            var htmlContent = d.message.html;
            if(htmlContent){
                var iframe = document.createElement('iframe');
                iframe.style.width = '100%';
                iframe.style.border = 'none';
                iframe.style.background = '#fff';
                iframe.style.minHeight = '200px';
                iframe.sandbox = 'allow-same-origin allow-popups';
                iframe.srcdoc = htmlContent;
                bodyEl.innerHTML = '';
                bodyEl.appendChild(iframe);
                iframe.onload = function(){
                    setTimeout(function(){
                        try {
                            var doc = iframe.contentDocument||iframe.contentWindow.document;
                            if(doc&&doc.documentElement){
                                iframe.style.height = (doc.documentElement.scrollHeight + 20) + 'px';
                            }
                        } catch(e) {
                            iframe.style.height = '500px';
                        }
                    },150);
                };
            } else {
                bodyEl.innerHTML = '<pre style="white-space:pre-wrap;word-break:break-word;font-family:var(--mono);margin:0;padding:10px;background:#fff;border:1px solid var(--rule)">'+esc(d.message.body||'(无正文内容)')+'</pre>';
            }
        }
        
        function updateCacheStatus(cached){
            if(!cached) return;
            var age = cached.cache_age_sec||0;
            var txt = age<300?'缓存 '+(age<60?Math.round(age)+'s':Math.round(age/60)+'m')+' 前':'';
            E('cacheStatus').textContent = cached.inbox_cached?' | '+cached.inbox_cached+' 封已缓存 '+txt:'';
        }
        
        function openInboxSettings(){
            var accId = E('inboxAccount').value;
            if(!accId){ toast('请先选择账号',true); return; }
            showAppPwdModal(accId);
        }
        
        function showAppPwdModal(accId){
            var acc = accounts.find(function(a){return a.id===accId});
            var name = acc?(acc.name||acc.real_email||accId):accId;
            var icloudEmail = '';
            if(acc&&acc.icloud_email&&(acc.icloud_email.indexOf('@icloud.com')>=0||acc.icloud_email.indexOf('@me.com')>=0||acc.icloud_email.indexOf('@mac.com')>=0)){
                icloudEmail = acc.icloud_email;
            }else if(acc&&acc.real_email&&(acc.real_email.indexOf('@icloud.com')>=0||acc.real_email.indexOf('@me.com')>=0)){
                icloudEmail = acc.real_email;
            }
            var hasPwd = acc&&acc.has_app_password;
            var h = '<div class="modal-overlay" id="appPwdModal" onclick="if(event.target===this)closeAppPwdModal()"><div class="modal-box"><h3><i class="diamond"></i> '+(hasPwd?'修改':'设置')+' iCloud 邮箱和应用密码</h3><p>账号: <b>'+esc(name)+'</b> (Apple ID: '+esc(acc?acc.real_email:'')+')<br>在 <a href="appleid.apple.com">appleid.apple.com</a> → 登录与安全 → App 专用密码 生成。</p><label style="font-family:var(--mono);font-size:11px;color:var(--ink-faint);letter-spacing:.2em;text-transform:uppercase">iCloud 邮箱 (IMAP 登录用)</label><input type="text" id="icloudEmailInput" value="'+escAttr(icloudEmail)+'" placeholder="xxx@icloud.com"><label style="font-family:var(--mono);font-size:11px;color:var(--ink-faint);letter-spacing:.2em;text-transform:uppercase">App 专用密码'+ (hasPwd?' (重新输入以更新)':'') +'</label><input type="password" id="appPwdInput" placeholder="xxxx-xxxx-xxxx-xxxx"><div class="modal-actions"><button class="btn btn-outline" onclick="closeAppPwdModal()">取消</button><button class="btn btn-primary" id="btnSetPwd" onclick="setAppPassword(\''+escAttr(accId)+'\')">保存并测试</button></div><div class="modal-msg" id="appPwdMsg"></div></div></div>';
            document.body.insertAdjacentHTML('beforeend',h);
        }
        
        function closeAppPwdModal(){
            var m = E('appPwdModal');
            if(m) m.remove();
        }
        
        async function setAppPassword(accId){
            var pwd = E('appPwdInput').value.trim();
            var email = E('icloudEmailInput').value.trim();
            if(!email){ E('appPwdMsg').innerHTML = '<span style="color:var(--red)">请输入 iCloud 邮箱</span>'; return; }
            if(!pwd){ E('appPwdMsg').innerHTML = '<span style="color:var(--red)">请输入密码</span>'; return; }
            var btn = E('btnSetPwd');
            btn.disabled = true;
            btn.textContent = '测试中...';
            var d = await api('/api/accounts/'+encodeURIComponent(accId)+'/app-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({app_password:pwd,icloud_email:email})});
            btn.disabled = false;
            btn.textContent = '保存并测试';
            if(d.ok){
                E('appPwdMsg').innerHTML = '<span style="color:var(--green)">连接成功! 收件箱 '+d.inbox_count+' 封</span>';
                var acc = accounts.find(function(a){return a.id===accId});
                if(acc){
                    acc.has_app_password = true;
                    acc.icloud_email = email;
                }
                setTimeout(closeAppPwdModal,1500);
                updateInboxAccountSelect();
            } else {
                E('appPwdMsg').innerHTML = '<span style="color:var(--red)">'+esc(d.error||'连接失败')+'</span>';
            }
        }
        
        async function createForAccount(accId,count){
            var d = await api('/api/accounts/'+encodeURIComponent(accId)+'/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count:count})});
            if(d.ok) toast('成功创建 '+d.created+' 个');
            else toast('失败: '+(d.error||'?'),true);
            refreshAll();
        }
        
        async function validateAccount(accId){
            var d = await api('/api/accounts/'+encodeURIComponent(accId)+'/validate',{method:'POST'});
            if(d.ok) toast('校验通过: '+d.real_email);
            else toast('校验失败: '+(d.error||'?'),true);
            forwardOptions.loaded = false;
            refreshAll();
        }
        
        async function removeAccount(accId){
            var acc = accounts.find(function(a){return a.id===accId});
            var name = acc?(acc.name||acc.real_email||accId):accId;
            if(!confirm('确认删除账号「'+name+'」？\\n\\n只会删除本面板保存的 Cookie 和账号配置，不会删除 iCloud 里已经创建的隐私邮箱。')) return;
            var d = await api('/api/accounts/'+encodeURIComponent(accId)+'/remove',{method:'POST'});
            if(d.ok) toast('已删除');
            else toast('删除失败',true);
            forwardOptions.loaded = false;
            refreshAll();
        }

        function showUpdateCookieModal(accId){
            var acc = accounts.find(function(a){return a.id===accId});
            if(!acc){ toast('账号不存在',true); return; }
            var h = '<div class="modal-overlay" id="updCookieModal" onclick="if(event.target===this)closeUpdateCookieModal()"><div class="modal-box"><h3><i class="diamond"></i> 更新 iCloud Cookie</h3><p>账号: <b>'+esc(acc.name||acc.real_email||accId)+'</b><br>请在浏览器重新登录 icloud.com、完成 2FA 并选择信任此浏览器，然后用 Cookie Editor 导出 Header String 粘贴到这里。</p><input type="hidden" id="updAccId" value="'+escAttr(accId)+'"><input type="text" id="updAccNameInput" value="'+escAttr(acc.name||'')+'" placeholder="账号名称"><textarea id="updCookieInput" placeholder="粘贴新的 Cookie Header String 或 JSON"></textarea><div class="modal-actions"><button class="btn btn-outline" onclick="closeUpdateCookieModal()">取消</button><button class="btn btn-primary" id="btnUpdateCookie" onclick="updateAccountCookie()">保存并校验</button></div><div class="modal-msg" id="updCookieMsg"></div></div></div>';
            document.body.insertAdjacentHTML('beforeend',h);
        }

        function closeUpdateCookieModal(){
            var m = E('updCookieModal');
            if(m) m.remove();
        }

        async function updateAccountCookie(){
            var accId = E('updAccId').value;
            var name = E('updAccNameInput').value.trim();
            var cookies = E('updCookieInput').value.trim();
            if(!cookies){ E('updCookieMsg').innerHTML = '<span style="color:var(--red)">请粘贴新的 Cookie</span>'; return; }
            var btn = E('btnUpdateCookie');
            btn.disabled = true;
            btn.textContent = '校验中...';
            var d = await api('/api/accounts/'+encodeURIComponent(accId)+'/cookies',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,cookie_input:cookies})});
            btn.disabled = false;
            btn.textContent = '保存并校验';
            if(d.ok){
                E('updCookieMsg').innerHTML = '<span style="color:var(--green)">更新成功! '+esc(d.real_email||'')+' ('+(d.alias_total||0)+' 别名)</span>';
                toast('Cookie 已更新');
                setTimeout(closeUpdateCookieModal,1200);
                forwardOptions.loaded = false;
                refreshAll();
            } else {
                E('updCookieMsg').innerHTML = '<span style="color:var(--red)">'+esc(d.error||'更新失败')+'</span>';
            }
        }
        
        async function toggleScheduler(){
            var act = state.running?'stop':'start';
            var opts = {method:'POST'};
            if(act==='start'){
                opts.headers = {'Content-Type':'application/json'};
                opts.body = JSON.stringify(collectSchedulerConfig());
            }
            var d = await api('/api/scheduler/'+act,opts);
            if(d.ok) toast(state.running?'调度器已停止':'调度器已启动');
            else toast(d.error||'操作失败',true);
            refreshAll();
        }

        async function saveSchedulerConfig(){
            var d = await api('/api/scheduler/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collectSchedulerConfig())});
            if(d.ok) toast('计划已保存');
            else toast('保存失败: '+(d.error||'?'),true);
            refreshAll();
        }

        async function saveAppSettings(){
            var payload = {
                alias_split_enabled: !!(E('aliasSplitEnabled')&&E('aliasSplitEnabled').checked),
                alias_split_count: Math.max(1, Math.min(20, parseInt((E('aliasSplitCount')||{}).value||'4',10)||4)),
                forward_to_email: ((E('forwardToEmail')||{}).value||'').trim()
            };
            var d = await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
            if(d.ok){
                if(d.forward_update && d.forward_update.ok) toast('全局设置已保存，Apple 转发地址已同步');
                else if(d.forward_update && !d.forward_update.ok) toast('全局设置已保存，但 Apple 转发同步失败', true);
                else toast('全局设置已保存');
            }
            else toast('保存失败: '+(d.error||'?'),true);
            refreshAll();
        }
        
        function copyOne(email){
            navigator.clipboard.writeText(email).then(function(){
                toast('已复制: '+email);
            });
        }

        async function copyAutoLogin(email, accountId, label){
            var d = await api('/admin/address_credential?address='+encodeURIComponent(email)+'&account_id='+encodeURIComponent(accountId||'')+'&label='+encodeURIComponent(label||''));
            if(!d.ok){
                toast('生成登录链接失败: '+(d.error||'?'), true);
                return;
            }
            var link = d.login_url || (location.origin + '/?credential=' + encodeURIComponent(d.jwt || ''));
            navigator.clipboard.writeText(link).then(function(){
                toast('自动登录链接已复制');
            });
        }
        
        function copyAll(){
            var filtered = getFilteredAliases();
            navigator.clipboard.writeText(filtered.map(function(e){return e.email}).join('\n')).then(function(){
                toast('已复制 '+filtered.length+' 个');
            });
        }
        
        function exportCSV(){
            var filtered = getFilteredAliases();
            var csv = 'email,account,label,active\n'+filtered.map(function(e){
                return e.email+','+(e.account_name||e.account_id||'')+','+(e.label||'')+','+(e.hasOwnProperty('active')?(e.active?'yes':'no'):'');
            }).join('\n');
            var b = new Blob(['\uFEFF'+csv],{type:'text/csv'}), a = document.createElement('a');
            a.href = URL.createObjectURL(b);
            a.download = 'icloud_aliases.csv';
            a.click();
        }

        function exportCredentials(){
            window.open('/admin/export_credentials.csv','_blank');
        }
        
        function clearLogs(){
            logs = [];
            E('logFeed').innerHTML = '';
        }
        
        function toast(msg,isErr){
            var t = E('toast');
            t.textContent = msg;
            t.style.background = isErr?'var(--red)':'var(--ink)';
            t.style.color = 'var(--paper)';
            t.classList.add('show');
            setTimeout(function(){t.classList.remove('show')},2200);
        }
        
        var lastMaxLogId = 0;
        async function fetchLogs(){
            try {
                var d = await api('/api/logs');
                if(Array.isArray(d)){
                    var hasNew = false;
                    var hasCreation = false;
                    d.forEach(function(entry){
                        if(entry.id > lastMaxLogId){
                            if(!logs.some(function(l){return l.id===entry.id})){
                                logs.push(entry);
                                hasNew = true;
                                if(entry.msg && entry.msg.indexOf('创建')>=0) hasCreation = true;
                            }
                            if(entry.id > lastMaxLogId) lastMaxLogId = entry.id;
                        }
                    });
                    if(hasNew){
                        logs.sort(function(a,b){return a.id-b.id});
                        if(logs.length>500) logs = logs.slice(-500);
                        if(curTab==='logs') renderLogs();
                        if(hasCreation) refreshLight();
                    }
                }
            } catch(_) {}
        }
        
        function renderLogs(){
            var f = E('logFeed');
            f.innerHTML = logs.map(function(l){
                return '<div class="log-line '+l.level+'"><span class="log-time">'+esc(l.time)+'</span>'+esc(l.msg)+'</div>';
            }).join('\n');
            f.scrollTop = f.scrollHeight;
        }
        
        function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
        function escAttr(s){ return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
        
        function showAddAccountModal(){
            var h = '<div class="modal-overlay" id="addAccModal" onclick="if(event.target===this)closeAddAccModal()"><div class="modal-box"><h3><i class="diamond"></i> 导入 iCloud Cookie</h3><p>Chrome 安装 <b>Cookie Editor</b> 扩展 → 登录 icloud.com → 导出 <b>Header String</b> 粘贴即可。<br>也支持 JSON 格式: <code>{"name1":"value1"}</code></p><input type="text" id="accNameInput" placeholder="账号名称 (如: 主号)"><textarea id="cookieInput" placeholder="粘贴 Cookie，支持 Header String 或 JSON 格式"></textarea><div class="modal-actions"><button class="btn btn-outline" onclick="closeAddAccModal()">取消</button><button class="btn btn-primary" id="btnAddAccount" onclick="addAccount()">添加并校验</button></div><div class="modal-msg" id="addAccMsg"></div></div></div>';
            document.body.insertAdjacentHTML('beforeend',h);
        }
        
        function closeAddAccModal(){
            var m = E('addAccModal');
            if(m) m.remove();
        }
        
        async function addAccount(){
            var name = E('accNameInput').value.trim()||'未命名账号';
            var cookies = E('cookieInput').value.trim();
            if(!cookies){ E('addAccMsg').innerHTML = '<span style="color:var(--red)">请粘贴 Cookie</span>'; return; }
            var btn = E('btnAddAccount');
            btn.disabled = true;
            btn.textContent = '校验中...';
            var d = await api('/api/accounts/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,cookie_input:cookies})});
            btn.disabled = false;
            btn.textContent = '添加并校验';
            if(d.ok){
                E('addAccMsg').innerHTML = '<span style="color:var(--green)">添加成功! '+esc(d.real_email||'')+' ('+(d.alias_total||0)+' 别名)</span>';
                setTimeout(closeAddAccModal,1500);
                forwardOptions.loaded = false;
                refreshAll();
            } else {
                E('addAccMsg').innerHTML = '<span style="color:var(--red)">'+esc(d.error||'失败')+'</span>';
            }
        }
        
        function renderDocs(){
            var h = '<div style="max-width:900px"><p style="color:var(--ink-soft);margin-bottom:18px">所有接口返回 JSON。Base URL: <code>http://127.0.0.1:5050</code></p>';
            var sections = [
                {title:'账号管理',items:[
                    {method:'GET',path:'/api/accounts',desc:'列出所有账号（脱敏，不含 cookie）'},
                    {method:'POST',path:'/api/accounts/add',desc:'添加账号',body:'{"name":"账号名","cookie_input":"name1=value1; name2=value2"}'},
                    {method:'POST',path:'/api/accounts/{id}/cookies',desc:'重新导入指定账号 Cookie 并校验',body:'{"name":"账号名","cookie_input":"name1=value1; name2=value2"}'},
                    {method:'POST',path:'/api/accounts/{id}/remove',desc:'删除账号'},
                    {method:'POST',path:'/api/accounts/{id}/validate',desc:'重新校验账号会话'}
                ]},
                {title:'状态',items:[
                    {method:'GET',path:'/api/state',desc:'全局状态 + 账号汇总'},
                    {method:'GET',path:'/api/settings',desc:'读取全局邮箱设置'},
                    {method:'GET',path:'/api/forward-options',desc:'读取 Apple 账号已绑定/允许的转发邮箱选项'},
                    {method:'POST',path:'/api/settings',desc:'保存分裂开关和转发地址选择',body:'{"alias_split_enabled":true,"alias_split_count":4,"forward_to_email":"user@example.com"}'}
                ]},
                {title:'别名 / 邮箱',items:[
                    {method:'GET',path:'/api/aliases',desc:'所有账号的别名列表（iCloud API 实时拉取，并写入云端同步缓存，支持派生地址）'},
                    {method:'GET',path:'/api/emails',desc:'本地创建记录 + 最近一次云端同步缓存，支持派生地址'},
                    {method:'POST',path:'/api/accounts/{id}/create',desc:'为指定账号创建别名',body:'{"count":5,"label":"可选标签"}'},
                    {method:'POST',path:'/api/create-batch',desc:'跨账号批量创建',body:'{"account_ids":["id1","id2"],"count_per_account":5}'}
                ]},
                {title:'收件箱 (IMAP)',items:[
                    {method:'GET',path:'/api/accounts/{id}/inbox?limit=20&force=1',desc:'查收件箱。force=1 跳过缓存强制从 IMAP 拉取'},
                    {method:'GET',path:'/api/accounts/{id}/alias-mail?force=1',desc:'查所有隐私别名的收件情况'},
                    {method:'GET',path:'/api/accounts/{id}/mail/{别名邮箱}',desc:'查指定隐私邮箱的收件'},
                    {method:'POST',path:'/api/accounts/{id}/app-password',desc:'设置 App 专用密码并测试 IMAP',body:'{"app_password":"xxxx-xxxx-xxxx-xxxx","icloud_email":"xxx@icloud.com"}'}
                ]},
                {title:'本机收件箱 / 分享',items:[
                    {method:'GET',path:'/api/inbound-config',desc:'读取 Cloudflare Email Worker 投递地址和 token'},
                    {method:'POST',path:'/api/inbound-mail',desc:'Cloudflare Worker 投递原始邮件（Bearer token 认证）',body:'{"from":"sender@example.com","to":"inbox@mail.example.com","raw":"完整 RFC822 邮件","headers":{}}'},
                    {method:'GET',path:'/api/local-inbox/summary',desc:'按隐私邮箱统计本机收到的邮件，并返回分享状态'},
                    {method:'GET',path:'/api/local-inbox/messages?alias=xxx@icloud.com',desc:'查看某个隐私邮箱的独立收件箱'},
                    {method:'POST',path:'/api/local-inbox/share',desc:'给某个隐私邮箱分配负责人并生成只读分享链接',body:'{"alias":"xxx@icloud.com","assignee":"客户A","enabled":true}'},
                    {method:'GET',path:'/share/{token}',desc:'外部分发人员只读分享页面'}
                ]},
                {title:'快捷入口',items:[
                    {method:'GET',path:'/api/mail?email=user@icloud.com',desc:'按主邮箱查所有别名收件'},
                    {method:'GET',path:'/api/mail?email=...&alias=xxx@icloud.com',desc:'按主邮箱查指定别名收件'}
                ]},
                {title:'调度器',items:[
                    {method:'GET',path:'/api/scheduler/config',desc:'读取当前计划任务配置'},
                    {method:'POST',path:'/api/scheduler/config',desc:'保存计划任务配置',body:'{"mode":"interval","interval_minutes":30,"count_per_run":1}'},
                    {method:'POST',path:'/api/scheduler/start',desc:'按当前配置启动定时调度器'},
                    {method:'POST',path:'/api/scheduler/stop',desc:'停止调度器'}
                ]},
                {title:'实时日志',items:[
                    {method:'GET',path:'/api/log-stream',desc:'SSE 实时日志流（EventSource）'}
                ]}
            ];
            
            sections.forEach(function(sec){
                h += '<div style="margin-bottom:24px"><div style="font-size:12px;color:var(--ink-faint);letter-spacing:.2em;text-transform:uppercase;margin-bottom:10px;border-bottom:1px solid var(--rule);padding-bottom:4px">'+esc(sec.title)+'</div>';
                sec.items.forEach(function(item){
                    var methodColor = item.method==='GET'?'var(--green)':item.method==='POST'?'var(--red)':'var(--ink-soft)';
                    h += '<div style="margin-bottom:10px;padding:10px 14px;background:var(--paper-dim)"><span style="font-weight:700;color:'+methodColor+';margin-right:12px;font-size:11px">'+item.method+'</span><code style="font-size:12px">'+esc(item.path)+'</code><div style="color:var(--ink-soft);font-size:12px;margin-top:4px">'+esc(item.desc)+'</div>';
                    if(item.body){
                        h += '<div style="margin-top:6px"><code style="font-size:11px;color:var(--ink-faint);background:var(--paper);padding:3px 8px;display:inline-block">'+esc(item.body)+'</code></div>';
                    }
                    h += '</div>';
                });
                h += '</div>';
            });
            
            h += '<div style="margin-top:32px;padding-top:16px;border-top:1px solid var(--rule-strong);font-size:12px;color:var(--ink-faint)">缓存策略：收件箱接口默认 5 分钟内读本地缓存 (<code>results/mail_cache.json</code>)，首次拉取后终身存储。传 <code>?force=1</code> 跳过缓存从 IMAP 增量拉取。<br>Cookie 导入：支持 Header String (<code>name=value; ...</code>) 和 JSON (<code>{"name":"value"}</code>) 两种格式。</div></div>';
            E('docsContent').innerHTML = h;
        }
        
        function updateInboxAccountSelect(){
            var sel = E('inboxAccount'), old = sel.value;
            sel.innerHTML = '<option value="">-- 选择账号 --</option>';
            accounts.forEach(function(a){
                var hasPwd = a.has_app_password?' [已设]':' [未设密码]';
                var imapEmail = a.icloud_email||a.real_email||'';
                sel.innerHTML += '<option value="'+escAttr(a.id)+'">'+esc((a.name||a.real_email||a.id).substring(0,20))+' | '+esc(imapEmail.substring(0,25))+' '+hasPwd+'</option>';
            });
            sel.value = old||'';
            updateInboxAliasSelect(sel.value);
        }
        
        function updateInboxAliasSelect(accId){
            var sel = E('inboxAliasSelect');
            if(!sel) return;
            var oldVal = sel.value;
            sel.innerHTML = '<option value="">全部子账号</option>';
            if(!accId) return;
            var accEmails = emails.filter(function(e){return e.account_id===accId});
            accEmails.sort(function(a,b){return a.email.localeCompare(b.email)});
            accEmails.forEach(function(e){
                var label = e.email+(e.label?' ('+e.label+')':'');
                sel.innerHTML += '<option value="'+escAttr(e.email)+'">'+esc(label)+'</option>';
            });
            sel.value = oldVal;
        }
        
        function copySelectedInboxAlias(){
            var alias = E('inboxAliasSelect').value;
            if(!alias){ toast('请先选择一个子账号',true); return; }
            navigator.clipboard.writeText(alias).then(function(){
                toast('已复制: '+alias);
            });
        }
        
        function filterInboxByAlias(isLoading){
            _inboxPage = 1;
            var selectedAlias = E('inboxAliasSelect').value;
            var filtered = _inboxStreamMsgs;
            if(selectedAlias){
                filtered = _inboxStreamMsgs.filter(function(m){
                    var toField = (m.to||'').toLowerCase();
                    return toField.indexOf(selectedAlias.toLowerCase()) >= 0;
                });
            }
            var title = '收件箱 ('+_inboxStreamMsgs.length+' 封'+(selectedAlias?', 已筛选出 '+filtered.length+' 封':'')+(isLoading?', 加载中...':'')+')';
            renderInboxMsgs(filtered,title);
        }
        
        refreshAll();
        fetchLogs();
        setInterval(fetchLogs,3000);
        setInterval(refreshLight,10000);
        setInterval(refreshAll,30000);
    </script>
</body>
</html>
"""

SHARED_INBOX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Shared Inbox</title>
  <style>
    :root{--paper:#f7f4ef;--ink:#151515;--muted:#777;--rule:#ddd4c8;--red:#b23a35}
    body{margin:0;background:var(--paper);color:var(--ink);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
    header{padding:18px 22px;border-bottom:1px solid var(--rule);display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap}
    h1{font-size:18px;margin:0 0 6px}
    .sub{font-size:12px;color:var(--muted)}
    main{max-width:1080px;margin:0 auto;padding:18px}
    button{font-family:inherit;border:1px solid var(--ink);background:var(--ink);color:var(--paper);padding:7px 14px;cursor:pointer}
    button.out{background:transparent;color:var(--ink);border-color:var(--rule)}
    .card{border:1px solid var(--rule);background:#fff;margin-bottom:10px}
    .row{padding:12px 14px;border-bottom:1px solid var(--rule);cursor:pointer}
    .row:hover{background:#faf8f5}
    .subject{font-weight:700;margin-bottom:4px}
    .meta{font-size:12px;color:var(--muted);display:flex;gap:12px;flex-wrap:wrap}
    .body{display:none;padding:14px;border-top:1px solid var(--rule);max-height:620px;overflow:auto;background:#fff}
    iframe{width:100%;border:0;background:white;min-height:220px}
    pre{white-space:pre-wrap;word-break:break-word;margin:0}
    .empty{padding:60px;text-align:center;color:var(--muted);border:1px solid var(--rule);background:#fff}
    .pager{display:flex;justify-content:center;align-items:center;gap:12px;margin:16px}
  </style>
</head>
<body>
  <header>
    <div>
      <h1 id="title">Shared Inbox</h1>
      <div class="sub" id="subtitle">加载中...</div>
    </div>
    <button class="out" onclick="loadMessages()">刷新</button>
  </header>
  <main>
    <div id="list" class="empty">加载中...</div>
    <div class="pager" id="pager" style="display:none">
      <button class="out" onclick="page(-1)">上一页</button>
      <span id="pageInfo"></span>
      <button class="out" onclick="page(1)">下一页</button>
    </div>
  </main>
  <script>
    const TOKEN = {{ token|tojson }};
    let offset = 0, limit = 30, total = 0, expanded = null;
    const E = id => document.getElementById(id);
    const esc = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    async function api(path){ const r = await fetch(path); return await r.json(); }
    async function loadMessages(){
      E('list').innerHTML = '<div class="empty">加载中...</div>';
      const d = await api('/api/shared/'+encodeURIComponent(TOKEN)+'/messages?limit='+limit+'&offset='+offset);
      if(!d.ok){ E('list').innerHTML = '<div class="empty">'+esc(d.error||'加载失败')+'</div>'; return; }
      total = d.count || 0;
      const share = d.share || {};
      E('title').textContent = share.alias || 'Shared Inbox';
      E('subtitle').textContent = (share.assignee?('分配给: '+share.assignee+' · '):'') + (share.note||'只读收件箱');
      const msgs = d.messages || [];
      if(!msgs.length){ E('list').innerHTML = '<div class="empty">暂无邮件</div>'; E('pager').style.display='none'; return; }
      E('list').className = 'card';
      E('list').innerHTML = msgs.map(m => (
        '<div class="row" onclick="toggleMsg('+m.id+')">'+
        '<div class="subject">'+esc(m.subject||'(无主题)')+'</div>'+
        '<div class="meta"><span>From: '+esc(m.from||m.sender_name||'')+'</span><span>'+esc((m.created_at||'').substring(0,19))+'</span></div>'+
        '<div class="body" id="body_'+m.id+'">加载中...</div></div>'
      )).join('');
      E('pager').style.display = total > limit ? 'flex' : 'none';
      E('pageInfo').textContent = (offset+1)+' - '+Math.min(offset+limit,total)+' / '+total;
    }
    function page(delta){ offset = Math.max(0, offset + delta*limit); if(offset >= total) offset = Math.max(0, total - limit); loadMessages(); }
    async function toggleMsg(id){
      if(expanded && expanded !== id){ const old=E('body_'+expanded); if(old) old.style.display='none'; }
      const el = E('body_'+id); if(!el) return;
      if(el.style.display==='block'){ el.style.display='none'; expanded=null; return; }
      el.style.display='block'; expanded=id;
      if(el.dataset.loaded) return;
      const d = await api('/api/shared/'+encodeURIComponent(TOKEN)+'/messages/'+id);
      if(!d.ok || !d.message){ el.innerHTML = esc(d.error||'加载失败'); return; }
      const m = d.message;
      if(m.html){
        const iframe = document.createElement('iframe');
        iframe.sandbox = 'allow-popups';
        iframe.srcdoc = m.html;
        el.innerHTML = '';
        el.appendChild(iframe);
        iframe.onload = () => setTimeout(()=>{ try{ iframe.style.height=(iframe.contentDocument.documentElement.scrollHeight+20)+'px'; }catch(e){ iframe.style.height='520px'; }},120);
      }else{
        el.innerHTML = '<pre>'+esc(m.text||'(无正文)')+'</pre>';
      }
      el.dataset.loaded = '1';
    }
    loadMessages();
  </script>
</body>
</html>
"""

ADMIN_LOGIN_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Login · iCloud HME Mail</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f4ef;color:#151515;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.box{width:min(440px,92vw);background:#fff;border:1px solid #d8d0c4;box-shadow:10px 10px 0 #151515;padding:28px}
h1{margin:0 0 8px;font-size:22px}.sub{font-size:12px;color:#777;margin-bottom:22px}
input,button{width:100%;box-sizing:border-box;font:inherit;padding:12px;border:1px solid #151515;margin-top:10px}
button{background:#151515;color:#f7f4ef;cursor:pointer}.err{color:#b23a35;font-size:12px;margin-top:10px;min-height:18px}
a{color:#151515}
</style></head><body>
<div class="box">
  <h1>iCloud Mail Admin</h1>
  <div class="sub">管理员登录后进入 iCloud 隐私邮箱创建、调度、本机收件箱和凭证管理界面。</div>
  <input id="pwd" type="password" placeholder="管理员密码" autofocus onkeydown="if(event.key==='Enter')login()">
  <button onclick="login()">登录管理后台</button>
  <div class="err" id="msg"></div>
  <div class="sub" style="margin-top:18px">个人收件入口：<a href="/user">/user</a></div>
</div>
<script>
async function sha256(s){const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s));return [...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('')}
async function login(){
  const raw=document.getElementById('pwd').value;
  const password=await sha256(raw);
  const r=await fetch('/open_api/admin_login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});
  if(r.ok){localStorage.setItem('icloud_admin_auth',password); location.href='/admin'; return;}
  document.getElementById('msg').textContent=await r.text()||'登录失败';
}
</script></body></html>"""

USER_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>My Inbox · iCloud HME Mail</title>
<style>
:root{--paper:#f7f4ef;--ink:#151515;--muted:#777;--rule:#ddd4c8;--blue:#2463eb;--red:#b23a35;--green:#18864b}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{height:58px;display:flex;justify-content:space-between;align-items:center;padding:0 18px;border-bottom:1px solid var(--rule);background:#fff}
h1{font-size:18px;margin:0}.wrap{display:grid;grid-template-columns:320px 1fr 1.2fr;min-height:calc(100vh - 58px)}
.pane{border-right:1px solid var(--rule);overflow:auto}.pane:last-child{border-right:0}.pad{padding:14px}
.card{background:#fff;border:1px solid var(--rule);padding:14px;margin-bottom:12px}
.muted{color:var(--muted);font-size:12px}.title{font-weight:800}.row{padding:11px 12px;border-bottom:1px solid var(--rule);cursor:pointer;background:#fff}.row:hover,.row.active{background:#f0ebe4}
input,button,select{font:inherit;border:1px solid var(--ink);padding:9px;background:#fff}button{background:var(--ink);color:var(--paper);cursor:pointer}.out{background:#fff;color:var(--ink);border-color:var(--rule)}
.grid{display:grid;gap:8px}.tabs{display:flex;gap:8px;margin-bottom:10px}.tabs button{flex:1}.hidden{display:none}.msg{font-size:12px;min-height:18px}.err{color:var(--red)}.ok{color:var(--green)}
.body{background:#fff;border:1px solid var(--rule);padding:14px;min-height:260px}iframe{width:100%;border:0;background:#fff;min-height:300px}pre{white-space:pre-wrap;word-break:break-word}
@media(max-width:900px){.wrap{grid-template-columns:1fr}.pane{border-right:0;border-bottom:1px solid var(--rule);max-height:none}}
</style></head><body>
<header><h1>My Inbox</h1><div style="display:flex;gap:8px"><button class="out" onclick="location.href='/admin'">管理员入口</button><button class="out" onclick="logout()">退出</button></div></header>
<div class="wrap">
  <aside class="pane"><div class="pad">
    <div class="card">
      <div class="tabs"><button class="out" onclick="mode('cred')">凭证登录</button><button class="out" onclick="mode('user')">用户登录</button></div>
      <div id="credBox" class="grid">
        <div class="muted">粘贴管理员导出的地址 JWT，可直接查看该隐私邮箱收件箱。</div>
        <input id="credential" placeholder="Address JWT / 邮箱凭证">
        <button onclick="credentialLogin()">进入邮箱</button>
      </div>
      <div id="userBox" class="grid hidden">
        <input id="email" placeholder="用户邮箱">
        <input id="password" type="password" placeholder="密码">
        <div style="display:flex;gap:8px"><button style="flex:1" onclick="userLogin()">登录</button><button class="out" style="flex:1" onclick="userRegister()">注册</button></div>
        <input id="bindCredential" placeholder="绑定地址 JWT，可选">
        <button class="out" onclick="bindCredential()">绑定凭证到当前用户</button>
      </div>
      <div class="msg" id="loginMsg"></div>
    </div>
    <div class="card">
      <div class="title">地址</div>
      <div class="muted" id="addrHint">未登录</div>
      <input id="addressSearch" style="width:100%;margin-top:10px" placeholder="搜索邮箱地址" oninput="renderAddresses(currentUserAddresses)">
    </div>
    <div id="addresses"></div>
  </div></aside>
  <main class="pane"><div class="pad">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><div class="title">邮件</div><button class="out" onclick="loadMails()">刷新</button></div>
    <div id="mails" class="card muted">请选择地址</div>
  </div></main>
  <section class="pane"><div class="pad">
    <div class="title" id="subject">预览</div><div class="muted" id="meta">选择邮件查看正文</div><div class="body" id="body">暂无</div>
  </div></section>
</div>
<script>
let addressJwt=localStorage.getItem('address_jwt')||'', userJwt=localStorage.getItem('user_jwt')||'', currentAddress='', currentUserAddresses=[];
const E=id=>document.getElementById(id); const esc=s=>String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function sha256(s){const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s));return [...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('')}
function mode(m){E('credBox').classList.toggle('hidden',m!=='cred');E('userBox').classList.toggle('hidden',m!=='user')}
function setMsg(t,err){E('loginMsg').textContent=t;E('loginMsg').className='msg '+(err?'err':'ok')}
function normalizeCredential(raw){
  let s=String(raw||'').trim(); if(!s)return '';
  for(let i=0;i<3;i++){
    if((s[0]==='"'&&s[s.length-1]==='"')||(s[0]==="'"&&s[s.length-1]==="'")||(s[0]==='<'&&s[s.length-1]==='>')) s=s.slice(1,-1).trim();
    else break;
  }
  s=s.replace(/^Bearer\s+/i,'').trim();
  try{s=decodeURIComponent(s)}catch(e){}
  s=s.replace(/\\r|\\n|\\t/g,'');
  try{
    const u=new URL(s, location.origin);
    const v=u.searchParams.get('credential')||u.searchParams.get('jwt')||u.searchParams.get('token');
    if(v)return normalizeCredential(v);
    const hp=new URLSearchParams((u.hash||'').replace(/^#/,''));
    const hv=hp.get('credential')||hp.get('jwt')||hp.get('token');
    if(hv)return normalizeCredential(hv);
  }catch(e){}
  if(s.indexOf('=')>=0){
    try{
      const p=new URLSearchParams(s.replace(/^[?#]/,''));
      const v=p.get('credential')||p.get('jwt')||p.get('token');
      if(v)return normalizeCredential(v);
    }catch(e){}
  }
  s=s.replace(/\s+/g,'');
  const m=s.match(/[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/);
  return m?m[0]:s;
}
async function readError(r,fallback){
  let t=''; try{t=await r.text()}catch(e){}
  if(t){
    try{const j=JSON.parse(t); return j.error||j.message||j.detail||fallback}catch(e){}
    return t;
  }
  return fallback;
}
function friendlyCredentialError(err){
  const map={
    MissingAddressCredentialMsg:'缺少地址凭证',
    InvalidAddressCredentialMsg:'凭证过期或无效',
    AddressCredentialExpiredMsg:'凭证已过期，请让管理员重新导出',
    AddressCredentialAddressNotFoundMsg:'凭证对应邮箱已从本机地址表删除，请让管理员重新导出'
  };
  return map[err]||err||'凭证过期或无效';
}
async function credentialLogin(){
  const jwt=normalizeCredential(E('credential').value||addressJwt); if(!jwt){setMsg('请粘贴凭证',true);return}
  userJwt=''; localStorage.removeItem('user_jwt');
  addressJwt=jwt; await loadAddressSettings(true);
}
async function loadAddressSettings(clearOnFail){
  const jwt=normalizeCredential(addressJwt); if(!jwt){return false}
  addressJwt=jwt;
  const r=await fetch('/api/settings',{headers:{Authorization:'Bearer '+jwt}});
  if(!r.ok){
    const err=await readError(r,'凭证过期或无效');
    setMsg(friendlyCredentialError(err),true);
    if(clearOnFail){addressJwt='';localStorage.removeItem('address_jwt')}
    return false;
  }
  const d=await r.json();
  if(!d.address){setMsg('凭证响应异常，请刷新后重试',true); if(clearOnFail){addressJwt='';localStorage.removeItem('address_jwt')} return false}
  localStorage.setItem('address_jwt',jwt);
  currentAddress=d.address; currentUserAddresses=[{name:d.address,id:d.address_id||0,mail_count:0}]; renderAddresses(currentUserAddresses); setMsg('已进入 '+d.address,false); loadMails(); return true;
}
async function userRegister(){
  const password=await sha256(E('password').value); const email=E('email').value.trim();
  const r=await fetch('/user_api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
  if(!r.ok){setMsg(await r.text()||'注册失败',true);return} setMsg('注册成功，请登录',false);
}
async function userLogin(){
  const password=await sha256(E('password').value); const email=E('email').value.trim();
  const r=await fetch('/user_api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
  if(!r.ok){setMsg(await r.text()||'登录失败',true);return}
  const d=await r.json(); userJwt=d.jwt; localStorage.setItem('user_jwt',userJwt); setMsg('用户登录成功',false); await loadUserAddresses();
}
async function bindCredential(){
  if(!userJwt){setMsg('请先用户登录',true);return}
  const jwt=normalizeCredential(E('bindCredential').value); if(!jwt){setMsg('请粘贴地址凭证',true);return}
  const r=await fetch('/user_api/bind_address',{method:'POST',headers:{'x-user-token':userJwt,Authorization:'Bearer '+jwt}});
  if(!r.ok){setMsg(await readError(r,'绑定失败'),true);return} setMsg('绑定成功',false); loadUserAddresses();
}
async function loadUserAddresses(){
  const r=await fetch('/user_api/bind_address',{headers:{'x-user-token':userJwt}});
  if(!r.ok){setMsg('用户登录过期',true);userJwt='';localStorage.removeItem('user_jwt');return false}
  const d=await r.json(); currentUserAddresses=d.results||[]; renderAddresses(currentUserAddresses); return true;
}
function renderAddresses(list){
  const q=(E('addressSearch')?E('addressSearch').value:'').trim().toLowerCase();
  const shown=(list||[]).filter(a=>!q||String(a.name||a.address||'').toLowerCase().indexOf(q)>=0);
  E('addrHint').textContent=shown.length+' / '+(list||[]).length+' 个地址';
  E('addresses').innerHTML=shown.map(a=>{
    const name=a.name||a.address||'';
    return '<div class="row" data-address="'+esc(name)+'"><div class="title">'+esc(name)+'</div><div class="muted">'+(a.mail_count||0)+' mails</div></div>';
  }).join('')||'<div class="card muted">没有绑定地址</div>';
  Array.from(E('addresses').querySelectorAll('.row[data-address]')).forEach(el=>el.onclick=()=>selectAddress(el.dataset.address));
  if(list.length&&!currentAddress) currentAddress=list[0].name||list[0].address;
}
function selectAddress(a){currentAddress=a; loadMails();}
async function loadMails(){
  E('mails').innerHTML='加载中...'; let r;
  if(userJwt){r=await fetch('/user_api/mails?address='+encodeURIComponent(currentAddress||'')+'&limit=30&offset=0&summary=1',{headers:{'x-user-token':userJwt}})}
  else if(addressJwt){r=await fetch('/api/parsed_mails?limit=30&offset=0&summary=1',{headers:{Authorization:'Bearer '+addressJwt}})}
  else {E('mails').innerHTML='请先登录';return}
  if(!r.ok){E('mails').innerHTML='加载失败';return}
  const d=await r.json(), rows=d.results||[]; E('mails').className='';
  E('mails').innerHTML=rows.map(m=>'<div class=\"row\" onclick=\"showMail('+m.id+')\"><div class=\"title\">'+esc(m.subject||'(无主题)')+'</div><div class=\"muted\">'+esc(m.sender||m.from||'')+' · '+esc((m.created_at||'').substring(0,19))+'</div></div>').join('')||'<div class=\"card muted\">暂无邮件</div>';
}
async function showMail(id){
  let r;if(userJwt){r=await fetch('/user_api/parsed_mail/'+id,{headers:{'x-user-token':userJwt}})}else{r=await fetch('/api/parsed_mail/'+id,{headers:{Authorization:'Bearer '+addressJwt}})}
  if(!r.ok){E('body').textContent='加载失败';return}
  const m=await r.json(); if(!m){E('body').textContent='邮件不存在';return}
  E('subject').textContent=m.subject||'(无主题)'; E('meta').textContent=(m.sender||m.from||'')+' · '+(m.created_at||'');
  if(m.html){const iframe=document.createElement('iframe'); iframe.sandbox='allow-popups'; iframe.srcdoc=m.html; E('body').innerHTML=''; E('body').appendChild(iframe); iframe.onload=()=>setTimeout(()=>{try{iframe.style.height=(iframe.contentDocument.documentElement.scrollHeight+20)+'px'}catch(e){}},120)}
  else E('body').innerHTML='<pre>'+esc(m.text||m.raw||'(无正文)')+'</pre>';
}
function logout(){localStorage.removeItem('address_jwt');localStorage.removeItem('user_jwt');location.href='/'}
(async()=>{
  addressJwt=normalizeCredential(addressJwt); if(addressJwt)localStorage.setItem('address_jwt',addressJwt);
  const params=new URLSearchParams(location.search);
  const urlJwt=normalizeCredential(params.get('credential')||params.get('jwt')||params.get('token')||'');
  if(urlJwt){
    addressJwt=urlJwt; userJwt='';
    localStorage.removeItem('user_jwt');
    history.replaceState(null,'',location.pathname);
    await loadAddressSettings(true);
    return;
  }
  if(userJwt){mode('user');const ok=await loadUserAddresses(); if(!ok&&addressJwt){mode('cred');await loadAddressSettings(true)}}
  else if(addressJwt){await loadAddressSettings(true)}
})();
</script></body></html>"""

# ----- Flask Routes -----

@app.route("/admin")
def admin_login_page():
    if _admin_auth_ok():
        return make_response(render_template_string(UI_HTML))
    return render_template_string(ADMIN_LOGIN_HTML)

@app.route("/user")
def user_inbox_page():
    return render_template_string(USER_HTML)

@app.route("/login")
def login_page():
    return redirect("/admin")

@app.route("/logout")
def logout_page():
    resp = make_response(redirect("/admin"))
    resp.delete_cookie("cf_admin_auth")
    resp.delete_cookie(ADMIN_COOKIE_NAME)
    resp.delete_cookie("cf_user_token")
    return resp

@app.route("/")
@app.route("/index.html")
def index():
    # 仿 cftempmail：根路径给普通用户 / 地址凭证入口，管理员从 /admin 进入。
    return render_template_string(USER_HTML)

# ----- Cloudflare Temp Email compatible open/user/address/admin APIs -----

@app.route("/open_api/settings")
def open_api_settings():
    return jsonify({
        "title": "iCloud HME Mail",
        "announcement": "iCloud Hide My Email + local inbox",
        "prefix": "",
        "addressRegex": "",
        "minAddressLen": 1,
        "maxAddressLen": 64,
        "defaultDomains": ["icloud.com"],
        "domains": ["icloud.com"],
        "needAuth": False,
        "enableUserCreateEmail": False,
        "disableAnonymousUserCreateEmail": True,
        "disableCustomAddressName": True,
        "enableUserDeleteEmail": True,
        "enableAddressPassword": True,
        "enableAgentEmailInfo": True,
        "version": "icloud-hme-cf-compat",
    })

@app.route("/open_api/admin_login", methods=["POST"])
def open_api_admin_login():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")
    if not _cf_store.verify_admin_secret(password):
        return Response("NeedAdminPasswordMsg", status=401)
    cookie_value = _cf_store.admin_cookie_value(password)
    resp = jsonify({"success": True})
    resp.set_cookie(ADMIN_COOKIE_NAME, cookie_value, max_age=30*24*3600, httponly=True, secure=True, samesite="Lax")
    resp.delete_cookie("cf_admin_auth")
    return resp

@app.route("/open_api/site_login", methods=["POST"])
def open_api_site_login():
    return jsonify({"success": True})

@app.route("/open_api/credential_login", methods=["POST"])
def open_api_credential_login():
    data = request.get_json(silent=True) or {}
    credential = normalize_jwt_token(data.get("credential") or data.get("jwt") or data.get("token") or "")
    try:
        _cf_store.verify_address_token(credential)
        return jsonify({"success": True})
    except Exception as e:
        code, detail = _credential_error_code(e)
        return jsonify({"success": False, "error": code, "detail": detail}), 401

@app.route("/user_api/open_settings")
def user_api_open_settings():
    return jsonify({
        "enable": True,
        "enableMailVerify": False,
        "enableMailAllowList": False,
        "enableEmailCheckRegex": False,
    })

@app.route("/user_api/register", methods=["POST"])
def user_api_register():
    data = request.get_json(silent=True) or {}
    try:
        _cf_store.create_user(data.get("email", ""), data.get("password", ""))
        return jsonify({"success": True})
    except sqlite3.IntegrityError:
        return Response("UserAlreadyExistsMsg", status=400)
    except Exception as e:
        return Response(str(e), status=400)

@app.route("/user_api/login", methods=["POST"])
def user_api_login():
    data = request.get_json(silent=True) or {}
    user = _cf_store.verify_user(data.get("email", ""), data.get("password", ""))
    if not user:
        return Response("InvalidEmailOrPasswordMsg", status=400)
    token = _cf_store.user_token(user)
    resp = jsonify({"jwt": token})
    resp.set_cookie("cf_user_token", token, max_age=30*24*3600, httponly=False, secure=True, samesite="Lax")
    return resp

@app.route("/user_api/settings")
def user_api_settings():
    user = _user_payload_or_none()
    if not user:
        return Response("UserTokenExpiredMsg", status=401)
    return jsonify({"user_email": user.get("user_email"), "user_id": user.get("user_id"), "role": user.get("role", "user")})

@app.route("/user_api/bind_address", methods=["GET", "POST"])
def user_api_bind_address():
    user = _user_payload_or_none()
    if not user:
        return Response("UserTokenExpiredMsg", status=401)
    if request.method == "GET":
        return jsonify({"results": _cf_store.list_user_addresses(user.get("user_id"))})
    payload = _address_payload_or_none()
    if not payload:
        return Response("InvalidAddressCredentialMsg", status=401)
    _cf_store.bind_user_address(user.get("user_id"), payload.get("address_id"))
    return jsonify({"success": True})

@app.route("/user_api/bind_address_jwt/<int:address_id>")
def user_api_bind_address_jwt(address_id):
    user = _user_payload_or_none()
    if not user:
        return Response("UserTokenExpiredMsg", status=401)
    allowed = {int(a.get("id")) for a in _cf_store.list_user_addresses(user.get("user_id"))}
    if int(address_id) not in allowed:
        return Response("AddressNotBindedMsg", status=400)
    return jsonify({"jwt": _cf_store.address_token(address_id=address_id)})

@app.route("/user_api/unbind_address", methods=["POST"])
def user_api_unbind_address():
    user = _user_payload_or_none()
    if not user:
        return Response("UserTokenExpiredMsg", status=401)
    data = request.get_json(silent=True) or {}
    _cf_store.unbind_user_address(user.get("user_id"), data.get("address_id"))
    return jsonify({"success": True})

@app.route("/user_api/mails")
def user_api_mails():
    user = _user_payload_or_none()
    if not user:
        return Response("UserTokenExpiredMsg", status=401)
    address = norm_email(request.args.get("address", ""))
    bound = _cf_store.list_user_addresses(user.get("user_id"))
    addresses = [a["name"] for a in bound]
    if address:
        addresses = [a for a in addresses if a == address]
    summary = request.args.get("summary", "0") == "1"
    return jsonify(_list_cf_mails(
        addresses,
        request.args.get("limit", 50, type=int),
        request.args.get("offset", 0, type=int),
        include_raw=not summary,
        parsed=True,
        include_body=not summary,
    ))

@app.route("/user_api/parsed_mail/<int:mail_id>")
def user_api_parsed_mail(mail_id):
    user = _user_payload_or_none()
    if not user:
        return Response("UserTokenExpiredMsg", status=401)
    addresses = [a["name"] for a in _cf_store.list_user_addresses(user.get("user_id"))]
    msg = _get_cf_mail(mail_id, addresses, include_raw=False, parsed=True)
    return jsonify(msg)

@app.route("/user_api/mails/<int:mail_id>", methods=["DELETE"])
def user_api_delete_mail(mail_id):
    user = _user_payload_or_none()
    if not user:
        return Response("UserTokenExpiredMsg", status=401)
    addresses = [a["name"] for a in _cf_store.list_user_addresses(user.get("user_id"))]
    msg = _get_cf_mail(mail_id, addresses, include_raw=False)
    if not msg:
        return jsonify({"success": False})
    with _inbound_store._lock, _inbound_store._connect() as conn:
        cur = conn.execute("DELETE FROM inbound_mails WHERE id=?", (int(mail_id),))
    return jsonify({"success": cur.rowcount > 0})

@app.route("/api/mails")
def cf_api_mails():
    payload = _address_payload_or_none()
    if not payload:
        return Response("InvalidAddressCredentialMsg", status=401)
    return jsonify(_list_cf_mails([payload["address"]], request.args.get("limit", 50, type=int), request.args.get("offset", 0, type=int), include_raw=True))

@app.route("/api/mail/<int:mail_id>")
def cf_api_mail(mail_id):
    payload = _address_payload_or_none()
    if not payload:
        return Response("InvalidAddressCredentialMsg", status=401)
    return jsonify(_get_cf_mail(mail_id, [payload["address"]], include_raw=True))

@app.route("/api/parsed_mails")
def cf_api_parsed_mails():
    payload = _address_payload_or_none()
    if not payload:
        return Response("InvalidAddressCredentialMsg", status=401)
    summary = request.args.get("summary", "0") == "1"
    return jsonify(_list_cf_mails(
        [payload["address"]],
        request.args.get("limit", 50, type=int),
        request.args.get("offset", 0, type=int),
        include_raw=False,
        parsed=True,
        include_body=not summary,
    ))

@app.route("/api/parsed_mail/<int:mail_id>")
def cf_api_parsed_mail(mail_id):
    payload = _address_payload_or_none()
    if not payload:
        return Response("InvalidAddressCredentialMsg", status=401)
    return jsonify(_get_cf_mail(mail_id, [payload["address"]], include_raw=False, parsed=True))

@app.route("/api/address_login", methods=["POST"])
def cf_api_address_login():
    data = request.get_json(silent=True) or {}
    try:
        _cf_store.verify_address_token(data.get("credential") or data.get("jwt") or data.get("token") or "")
        return jsonify({"success": True})
    except Exception as e:
        code, detail = _credential_error_code(e)
        return jsonify({"success": False, "error": code, "detail": detail}), 401

@app.route("/api/delete_address", methods=["DELETE"])
def cf_api_delete_address():
    payload = _address_payload_or_none()
    if not payload:
        return Response("InvalidAddressCredentialMsg", status=401)
    # 兼容 cftempmail 的地址生命周期接口：这里仅删除本机凭证/绑定，不删除 Apple HME。
    ok = _cf_store.delete_address_record(payload.get("address_id"), delete_mails=False)
    return jsonify({"success": ok})

@app.route("/api/clear_inbox", methods=["DELETE"])
def cf_api_clear_inbox():
    payload = _address_payload_or_none()
    if not payload:
        return Response("InvalidAddressCredentialMsg", status=401)
    with _inbound_store._lock, _inbound_store._connect() as conn:
        cur = conn.execute("DELETE FROM inbound_mails WHERE hme_alias=?", (payload["address"],))
    return jsonify({"success": cur.rowcount >= 0})

@app.route("/api/clear_sent_items", methods=["DELETE"])
def cf_api_clear_sent_items():
    payload = _address_payload_or_none()
    if not payload:
        return Response("InvalidAddressCredentialMsg", status=401)
    return jsonify({"success": True})

@app.route("/admin/address")
def admin_api_address_list():
    if request.args.get("sync", "0") == "1":
        _sync_cf_addresses()
    return jsonify(_cf_store.list_addresses(
        query=request.args.get("query", ""),
        limit=request.args.get("limit", 50, type=int),
        offset=request.args.get("offset", 0, type=int),
        sort_by=request.args.get("sort_by", "id"),
        sort_order=request.args.get("sort_order", "desc"),
    ))

@app.route("/admin/show_password/<int:address_id>")
def admin_api_show_password(address_id):
    jwt = _cf_store.address_token(address_id=address_id)
    return jsonify({"jwt": jwt, "login_url": f"{_share_base_url()}/?credential={jwt}"})

@app.route("/admin/address_credential")
def admin_api_address_credential():
    address = norm_email(request.args.get("address", ""))
    if not address:
        return jsonify({"ok": False, "error": "address required"}), 400
    try:
        row = _cf_store.ensure_address(
            address,
            account_id=request.args.get("account_id", ""),
            label=request.args.get("label", ""),
            source="admin",
        )
        jwt = _cf_store.address_token(address_id=row["id"])
        return jsonify({
            "ok": True,
            "id": row["id"],
            "address": row["name"],
            "jwt": jwt,
            "credential": jwt,
            "login_url": f"{_share_base_url()}/?credential={jwt}",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/admin/export_credentials")
@app.route("/admin/export_credentials.<fmt>")
def admin_api_export_credentials(fmt="json"):
    _sync_cf_addresses()
    rows = _cf_store.export_credentials()
    base = _share_base_url()
    for row in rows:
        jwt = row.get("jwt") or row.get("credential") or ""
        row["login_url"] = f"{base}/?credential={jwt}" if jwt else ""
    if str(fmt).lower() == "csv" or request.args.get("format") == "csv":
        out = io.StringIO()
        fieldnames = ["id", "name", "jwt", "login_url", "account_id", "label", "mail_count", "created_at", "updated_at"]
        writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition":"attachment; filename=icloud_hme_credentials.csv"})
    return jsonify({"results": rows, "count": len(rows)})

@app.route("/admin/new_address", methods=["POST"])
def admin_api_new_address():
    data = request.get_json(silent=True) or {}
    label = str(data.get("label") or data.get("name") or "admin").strip()
    active_accounts = [a for a in _account_mgr.list_accounts() if a.get("status") == "active"]
    if not active_accounts:
        return Response("No active iCloud account", status=400)
    forward_to = _get_app_settings().get("forward_to_email", "")
    results = _account_mgr.create_aliases_for_account(active_accounts[0]["id"], 1, label=label, forward_to=forward_to)
    ok = next((r for r in results if r.get("ok") and r.get("email")), None)
    if not ok:
        return Response((results[0].get("error") if results else "create failed"), status=400)
    row = _cf_store.ensure_address(ok["email"], account_id=ok.get("account_id", ""), label=label, source="admin")
    jwt = _cf_store.address_token(address_id=row["id"])
    return jsonify({"address": row["name"], "address_id": row["id"], "jwt": jwt, "password": None})

@app.route("/api/new_address", methods=["POST"])
def cf_api_new_address():
    # cftempmail 兼容端点。本项目禁用匿名创建；管理员或已登录用户可以调用。
    data = request.get_json(silent=True) or {}
    label = str(data.get("label") or data.get("name") or "api").strip()
    active_accounts = [a for a in _account_mgr.list_accounts() if a.get("status") == "active"]
    if not active_accounts:
        return Response("No active iCloud account", status=400)
    forward_to = _get_app_settings().get("forward_to_email", "")
    results = _account_mgr.create_aliases_for_account(active_accounts[0]["id"], 1, label=label, forward_to=forward_to)
    ok = next((r for r in results if r.get("ok") and r.get("email")), None)
    if not ok:
        return Response((results[0].get("error") if results else "create failed"), status=400)
    row = _cf_store.ensure_address(ok["email"], account_id=ok.get("account_id", ""), label=label, source="api")
    jwt = _cf_store.address_token(address_id=row["id"])
    user = _user_payload_or_none()
    if user:
        _cf_store.bind_user_address(user.get("user_id"), row["id"])
    return jsonify({"address": row["name"], "address_id": row["id"], "jwt": jwt, "password": None})

@app.route("/admin/delete_address/<int:address_id>", methods=["DELETE"])
def admin_api_delete_address(address_id):
    return jsonify({"success": _cf_store.delete_address_record(address_id, delete_mails=False)})

@app.route("/admin/clear_inbox/<int:address_id>", methods=["DELETE"])
def admin_api_clear_inbox(address_id):
    row = _cf_store.get_address(address_id=address_id)
    if not row:
        return jsonify({"success": False})
    with _inbound_store._lock, _inbound_store._connect() as conn:
        cur = conn.execute("DELETE FROM inbound_mails WHERE hme_alias=?", (row["name"],))
    return jsonify({"success": cur.rowcount >= 0})

@app.route("/admin/mails")
def admin_api_mails():
    address = norm_email(request.args.get("address", ""))
    if address:
        return jsonify(_list_cf_mails([address], request.args.get("limit", 50, type=int), request.args.get("offset", 0, type=int), include_raw=True))
    return jsonify(_list_all_cf_mails(request.args.get("limit", 50, type=int), request.args.get("offset", 0, type=int), include_raw=True))

@app.route("/admin/mails/<int:mail_id>", methods=["DELETE"])
def admin_api_delete_mail(mail_id):
    with _inbound_store._lock, _inbound_store._connect() as conn:
        cur = conn.execute("DELETE FROM inbound_mails WHERE id=?", (int(mail_id),))
    return jsonify({"success": cur.rowcount > 0})

@app.route("/admin/users")
def admin_api_users():
    return jsonify(_cf_store.list_users(
        query=request.args.get("query", ""),
        limit=request.args.get("limit", 50, type=int),
        offset=request.args.get("offset", 0, type=int),
    ))

@app.route("/admin/users", methods=["POST"])
def admin_api_create_user():
    data = request.get_json(silent=True) or {}
    try:
        user = _cf_store.create_user(data.get("email", ""), data.get("password", ""), role=data.get("role", "user"))
        return jsonify({"success": True, "user": user})
    except Exception as e:
        return Response(str(e), status=400)

@app.route("/admin/users/<int:user_id>", methods=["DELETE"])
def admin_api_delete_user(user_id):
    return jsonify({"success": _cf_store.delete_user(user_id)})

@app.route("/api/state")
def api_state():
    summary = _account_mgr.get_summary()
    inbound_stats = _inbound_store.stats()
    with _lock:
        state = dict(_global_state); state.update(summary)
        state["cookies_ok"] = summary["active_accounts"] > 0
        state["alias_count"] = summary["total_aliases"]
        state["alias_active"] = summary["total_active_aliases"]
        state["local_mail_count"] = inbound_stats.get("total_mails", 0)
        state["local_mail_alias_count"] = inbound_stats.get("alias_count", 0)
        state["local_mail_share_count"] = inbound_stats.get("share_count", 0)
    return jsonify(state)

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    payload, err, detail = _address_payload_or_error()
    if payload:
        if request.method != "GET":
            return Response("Method not allowed for address credential", status=405)
        return jsonify({
            "address": payload.get("address"),
            "address_id": payload.get("address_id"),
            "send_balance": 0,
        })
    if _has_bearer_token():
        return _json_error(err, 401, detail)
    if request.method == "GET":
        return jsonify({"ok":True,"settings":_get_app_settings()})
    try:
        payload = request.get_json(silent=True) or {}
        cfg = _set_app_settings(payload, persist=True)
        forward_update = None
        if "forward_to_email" in payload and cfg.get("forward_to_email"):
            forward_update = _account_mgr.update_forward_to(cfg.get("forward_to_email"))
            if forward_update.get("ok"):
                try:
                    cached_aliases = _sync_cloud_alias_cache()
                    forward_update["cache_base_count"] = len(cached_aliases)
                except Exception as cache_err:
                    forward_update["cache_error"] = str(cache_err)[:200]
                _emit_log("success", f"Apple HME 转发地址已更新为 {cfg.get('forward_to_email')}")
            else:
                _emit_log("warn", f"Apple HME 转发地址更新未完全成功: {forward_update.get('failed_accounts', 0)} 个账号失败")
        _emit_log("info", f"更新全局邮箱设置: split={cfg.get('alias_split_enabled')} count={cfg.get('alias_split_count')} forward={cfg.get('forward_to_email') or '-'}")
        return jsonify({"ok":True,"settings":cfg,"forward_update":forward_update})
    except ValueError as e:
        return jsonify({"ok":False,"error":str(e)})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/forward-options")
def api_forward_options():
    """从 Apple HME 接口读取账号已绑定/允许的转发邮箱，供前端下拉选择。"""
    try:
        options = _account_mgr.get_forward_options()
        cfg = _get_app_settings()
        return jsonify({
            "ok": True,
            "emails": options.get("emails", []),
            "selected": options.get("selected", ""),
            "current": cfg.get("forward_to_email", ""),
            "accounts": options.get("accounts", []),
        })
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts")
def api_accounts():
    accounts = _account_mgr.list_accounts()
    safe = []
    for a in accounts:
        ac = {k:v for k,v in a.items() if k not in ("cookies","app_password")}
        ac["has_cookies"] = bool(a.get("cookies"))
        ac["has_app_password"] = bool(a.get("app_password"))
        safe.append(ac)
    return jsonify({"accounts":safe,"count":len(safe)})

@app.route("/api/accounts/add", methods=["POST"])
def api_add_account():
    data = request.get_json() or {}
    name = data.get("name","未命名账号")
    cookie_input = data.get("cookie_input","")
    host = data.get("host","icloud.com")
    if not cookie_input: return jsonify({"ok":False,"error":"请提供 cookie_input"})
    try:
        account = _account_mgr.add_account(name, cookie_input, host=host)
        _emit_log("info",f"添加账号: {account.get('name','')} ({account.get('real_email','?')})")
        return jsonify({"ok":True,"id":account["id"],"name":account["name"],"real_email":account.get("real_email",""),"alias_total":account.get("alias_total",0),"alias_active":account.get("alias_active",0),"status":account.get("status","")})
    except ValueError as e: return jsonify({"ok":False,"error":str(e)})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/remove", methods=["POST"])
def api_remove_account(acc_id):
    if _global_state.get("running") or _global_state.get("creating"):
        return jsonify({"ok":False,"error":"调度器运行中，请先停止调度器再删除账号"})
    ok = _account_mgr.remove_account(acc_id)
    if ok: _emit_log("info", f"删除账号: {acc_id}")
    return jsonify({"ok":ok})

@app.route("/api/accounts/<acc_id>/cookies", methods=["POST"])
def api_update_account_cookies(acc_id):
    if _global_state.get("running") or _global_state.get("creating"):
        return jsonify({"ok":False,"error":"调度器运行中，请先停止调度器再更新 Cookie"})
    data = request.get_json() or {}
    cookie_input = data.get("cookie_input","")
    name = data.get("name")
    host = data.get("host")
    if not cookie_input: return jsonify({"ok":False,"error":"请提供 cookie_input"})
    try:
        account = _account_mgr.update_account_cookies(acc_id, cookie_input, name=name, host=host)
        if account.get("status") == "active":
            _emit_log("info",f"更新 Cookie 成功: {account.get('name','')} ({account.get('real_email','?')})")
            return jsonify({"ok":True,"id":account["id"],"name":account["name"],"real_email":account.get("real_email",""),"alias_total":account.get("alias_total",0),"alias_active":account.get("alias_active",0),"status":account.get("status","")})
        _emit_log("warn",f"更新 Cookie 后校验失败: {account.get('name','')} - {account.get('last_error','')[:100]}")
        return jsonify({"ok":False,"id":account["id"],"status":account.get("status",""),"error":account.get("last_error","校验失败")})
    except ValueError as e: return jsonify({"ok":False,"error":str(e)})
    except KeyError as e: return jsonify({"ok":False,"error":str(e)})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/validate", methods=["POST"])
def api_validate_account(acc_id):
    try:
        account = _account_mgr.validate_account(acc_id)
        return jsonify({"ok":True,"real_email":account.get("real_email",""),"alias_total":account.get("alias_total",0)})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/create", methods=["POST"])
def api_create_for_account(acc_id):
    data = request.get_json() or {}
    count = min(int(data.get("count",1)),50)
    label = data.get("label","")
    _update_state(creating=True)
    account = _account_mgr.accounts.get(acc_id)
    acc_name = account.get("name") if account else acc_id
    _emit_log("info",f"[{acc_name}] 手动创建: 账号 {acc_id} x{count}")
    try:
        forward_to = _get_app_settings().get("forward_to_email", "")
        results = _account_mgr.create_aliases_for_account(acc_id, count, label, forward_to=forward_to)
        created = [r["email"] for r in results if r.get("ok")]
        errors = [r["error"] for r in results if not r.get("ok")]
        _update_state(creating=False)
        _increment_state(today_created=len(created), total_created=len(created))
        for email in created:
            _emit_log("success", f"[{acc_name}] 手动创建成功: {email}")
        for err in errors:
            _emit_log("warn", f"[{acc_name}] 手动创建失败: {err[:100]}")
        return jsonify({"ok":len(created)>0,"emails":created,"created":len(created),"errors":len(errors),"error":errors[0] if errors else None})
    except Exception as e:
        _update_state(creating=False)
        _emit_log("error", f"[{acc_name}] 手动创建异常: {str(e)[:100]}")
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/create-batch", methods=["POST"])
def api_create_batch():
    data = request.get_json() or {}
    account_ids = data.get("account_ids",[])
    count = min(int(data.get("count_per_account",5)),20)
    label = data.get("label","")
    interval = float(data.get("interval",3.0))
    if not account_ids: return jsonify({"ok":False,"error":"请选择至少一个账号"})
    _update_state(creating=True)
    _emit_log("info",f"批量创建: {len(account_ids)} 个账号 x{count}")
    try:
        forward_to = _get_app_settings().get("forward_to_email", "")
        all_results = _account_mgr.create_aliases_batch(account_ids, count, interval, label, forward_to=forward_to)
        total_created = sum(sum(1 for r in results if r.get("ok")) for results in all_results.values())
        total_errors = sum(sum(1 for r in results if not r.get("ok")) for results in all_results.values())
        _update_state(creating=False)
        _increment_state(today_created=total_created, total_created=total_created)
        for acc_id, results in all_results.items():
            acc = _account_mgr.accounts.get(acc_id)
            acc_name = acc.get("name") if acc else acc_id
            for r in results:
                if r.get("ok"):
                    _emit_log("success", f"[{acc_name}] 批量创建成功: {r['email']}")
                else:
                    _emit_log("warn", f"[{acc_name}] 批量创建失败: {r.get('error')[:100]}")
        _emit_log("success",f"批量完成: {total_created} 成功 / {total_errors} 失败")
        return jsonify({"ok":True,"total_created":total_created,"total_errors":total_errors,"results":{acc_id:[{"email":r.get("email"),"ok":r.get("ok"),"error":r.get("error")} for r in results] for acc_id,results in all_results.items()}})
    except Exception as e:
        _update_state(creating=False)
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/app-password", methods=["POST"])
def api_set_app_password(acc_id):
    data = request.get_json() or {}
    pwd = data.get("app_password","").strip()
    icloud_email = data.get("icloud_email","").strip()
    if not pwd: return jsonify({"ok":False,"error":"密码不能为空"})
    try:
        _account_mgr.set_app_password(acc_id, pwd)
        if icloud_email: _account_mgr.update_account(acc_id, icloud_email=icloud_email)
        result = _account_mgr.test_imap_connection(acc_id)
        return jsonify(result)
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/inbox")
def api_inbox(acc_id):
    limit = request.args.get("limit",50,type=int)
    force = request.args.get("force","0")=="1"
    try:
        emails = _account_mgr.check_inbox(acc_id, limit=limit, force=force)
        stats = _account_mgr._cache.get_stats(acc_id)
        return jsonify({"emails":emails,"count":len(emails),"cached":stats})
    except Exception as e: return jsonify({"emails":[],"count":0,"error":str(e)})

@app.route("/api/accounts/<acc_id>/inbox-stream")
def api_inbox_stream(acc_id):
    limit = request.args.get("limit",50,type=int)
    days = request.args.get("days",7,type=int)
    def generate():
        yield f"data: {json.dumps({'type':'start'})}\n\n"
        try: mail = _account_mgr.get_mail_client(acc_id)
        except Exception as e: yield f"data: {json.dumps({'type':'error','error':str(e)[:200]})}\n\n"; return
        try:
            count = 0
            for msg in mail.stream_inbox(limit=limit, days=days):
                count += 1
                yield f"data: {json.dumps({'type':'email','count':count,'email':msg},ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type':'done','count':count})}\n\n"
        except GeneratorExit: pass
        except Exception as e: yield f"data: {json.dumps({'type':'error','error':str(e)[:200]})}\n\n"
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/api/accounts/<acc_id>/message/<msg_id>")
def api_message_body(acc_id, msg_id):
    try:
        mail = _account_mgr.get_mail_client(acc_id)
        full = mail.fetch_full(msg_id.encode() if isinstance(msg_id,str) else msg_id)
        return jsonify({"ok":True,"message":full})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/mail/<alias_email>")
def api_specific_alias_mail(acc_id, alias_email):
    limit = request.args.get("limit",20,type=int)
    days = request.args.get("days",30,type=int)
    try:
        msgs = _account_mgr.check_alias_mail(acc_id, alias_email, limit=limit, days=days)
        return jsonify({"emails":msgs,"count":len(msgs),"alias":alias_email})
    except Exception as e: return jsonify({"emails":[],"count":0,"error":str(e)})

@app.route("/api/mail")
def api_mail_by_email():
    email = request.args.get("email","").strip().lower()
    alias = request.args.get("alias","").strip().lower()
    limit = request.args.get("limit",20,type=int)
    days = request.args.get("days",30,type=int)
    if not email: return jsonify({"error":"请提供 email 参数"})
    acc_id = None
    for a in _account_mgr.list_accounts():
        if a.get("icloud_email","").lower()==email or a.get("real_email","").lower()==email: acc_id=a["id"]; break
    if not acc_id: return jsonify({"error":f"未找到邮箱对应的账号: {email}"})
    try:
        if alias:
            msgs = _account_mgr.check_alias_mail(acc_id, alias, limit=limit, days=days)
            return jsonify({"emails":msgs,"count":len(msgs),"alias":alias,"account":email})
        else:
            by_alias = _account_mgr.check_all_aliases_mail(acc_id, limit_per=limit, days=days)
            total = sum(len(v) for v in by_alias.values())
            return jsonify({"by_alias":by_alias,"total":total,"account":email})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/accounts/<acc_id>/alias-mail")
def api_alias_mail(acc_id):
    force = request.args.get("force","0")=="1"
    try:
        by_alias = _account_mgr.check_all_aliases_mail(acc_id, force=force)
        total = sum(len(v) for v in by_alias.values())
        stats = _account_mgr._cache.get_stats(acc_id)
        return jsonify({"by_alias":by_alias,"total":total,"cached":stats})
    except Exception as e: return jsonify({"by_alias":{},"total":0,"error":str(e)})

@app.route("/api/aliases")
def api_aliases():
    try:
        aliases = _sync_cloud_alias_cache()
        expanded = _expand_email_records(aliases)
        expanded = _dedupe_email_records(expanded)
        return jsonify({
            "aliases": expanded,
            "count": len(expanded),
            "base_count": len(aliases),
            "cached": True,
        })
    except Exception as e: return jsonify({"aliases":[],"count":0,"error":str(e)})

@app.route("/api/emails")
def api_emails():
    limit = request.args.get("limit",0,type=int)
    include_cloud_cache = request.args.get("cloud_cache","1") != "0"
    base_records = _read_local_email_records()
    cloud_records = _load_cached_cloud_aliases() if include_cloud_cache else []
    records = _dedupe_email_records(base_records + cloud_records)
    if limit > 0:
        records = records[:limit]
    emails = _expand_email_records(records)
    emails = _dedupe_email_records(emails)
    return jsonify({
        "emails": emails,
        "count": len(emails),
        "base_count": sum(1 for e in emails if not e.get("derived")),
        "cloud_cache_count": len(cloud_records),
    })

@app.route("/api/inbound-config", methods=["GET", "POST"])
def api_inbound_config():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if data.get("regenerate_token"):
            _regenerate_inbound_token()
        if "public_base_url" in data:
            _inbound_config["public_base_url"] = str(data.get("public_base_url") or "").strip().rstrip("/")
            _save_inbound_config(_inbound_config)
    base = _share_base_url()
    cfg = _get_inbound_config()
    return jsonify({
        "ok": True,
        "inbound_url": f"{base}/api/inbound-mail",
        "token": cfg.get("token", ""),
        "public_base_url": cfg.get("public_base_url", ""),
        "worker_template": f"{base}/cloudflare_inbound_worker.js",
        "stats": _inbound_store.stats(),
    })

@app.route("/api/inbound-mail", methods=["POST"])
def api_inbound_mail():
    if not _check_inbound_auth():
        return jsonify({"ok":False,"error":"unauthorized"}), 401
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = {
            "raw": request.get_data(as_text=True),
            "from": request.headers.get("X-Mail-From", ""),
            "to": request.headers.get("X-Mail-To", ""),
            "headers": {k:v for k,v in request.headers.items() if k.lower().startswith("x-mail-")},
        }
    try:
        known_aliases, account_map = _known_aliases_and_account_map()
        result = _inbound_store.ingest(payload, known_aliases=known_aliases, alias_account_map=account_map)
        _emit_log("success", f"本机收信: {result.get('hme_alias') or '?'} ← {result.get('subject','')[:60]}")
        return jsonify({"ok":True, "mail": result})
    except Exception as e:
        _emit_log("warn", f"本机收信失败: {str(e)[:120]}")
        return jsonify({"ok":False,"error":str(e)}), 400

@app.route("/api/local-inbox/summary")
def api_local_inbox_summary():
    alias_filter = request.args.get("q","").strip()
    assignee = request.args.get("assignee","").strip()
    aliases = _inbound_store.list_aliases(alias_filter=alias_filter, assignee=assignee)
    by_alias = {str(row.get("alias") or "").lower(): row for row in aliases if row.get("alias")}
    shares = {str(row.get("alias") or "").lower(): row for row in _inbound_store.list_shares() if row.get("alias")}

    # 把已经同步/创建过的历史隐私邮箱也放进本机收件箱列表，即使当前收件数为 0，
    # 这样可以提前给邮箱分配负责人和生成分享链接。
    q_lower = alias_filter.lower()
    for rec in _known_alias_records_for_inbound():
        alias = str(rec.get("email") or "").strip().lower()
        if not alias or alias in by_alias:
            continue
        if q_lower and q_lower not in alias:
            continue
        share = shares.get(alias, {})
        if assignee and str(share.get("assignee") or "") != assignee:
            continue
        by_alias[alias] = {
            "alias": alias,
            "base_alias": str(rec.get("base_email") or rec.get("base_alias") or alias).lower(),
            "account_id": rec.get("account_id", ""),
            "mail_count": 0,
            "latest_at": "",
            "assignee": share.get("assignee", ""),
            "note": share.get("note", ""),
            "share_token": share.get("share_token", ""),
            "share_enabled": share.get("enabled", 0),
        }

    # 分享记录即使不在当前云端缓存里，也保留展示，方便停用/找回链接。
    for alias, share in shares.items():
        if alias in by_alias:
            continue
        if q_lower and q_lower not in alias:
            continue
        if assignee and str(share.get("assignee") or "") != assignee:
            continue
        by_alias[alias] = {
            "alias": alias,
            "base_alias": share.get("base_alias", alias),
            "account_id": share.get("account_id", ""),
            "mail_count": 0,
            "latest_at": "",
            "assignee": share.get("assignee", ""),
            "note": share.get("note", ""),
            "share_token": share.get("share_token", ""),
            "share_enabled": share.get("enabled", 0),
        }

    aliases = list(by_alias.values())
    aliases.sort(key=lambda r: (int(r.get("mail_count") or 0), str(r.get("latest_at") or ""), str(r.get("alias") or "")), reverse=True)
    base = _share_base_url()
    for row in aliases:
        if row.get("share_token") and row.get("share_enabled"):
            row["share_url"] = f"{base}/share/{row['share_token']}"
        else:
            row["share_url"] = ""
    return jsonify({
        "ok": True,
        "aliases": aliases,
        "assignees": _inbound_store.list_assignees(),
        "stats": {**_inbound_store.stats(), "known_alias_count": len(_known_alias_records_for_inbound())},
    })

@app.route("/api/local-inbox/messages")
def api_local_inbox_messages():
    alias = request.args.get("alias","").strip().lower()
    assignee = request.args.get("assignee","").strip()
    limit = request.args.get("limit",50,type=int)
    offset = request.args.get("offset",0,type=int)
    return jsonify({"ok":True, **_inbound_store.list_messages(alias=alias, assignee=assignee, limit=limit, offset=offset)})

@app.route("/api/local-inbox/messages/<int:mail_id>")
def api_local_inbox_message(mail_id):
    msg = _inbound_store.get_message(mail_id)
    if not msg:
        return jsonify({"ok":False,"error":"邮件不存在"}), 404
    return jsonify({"ok":True,"message":msg})

@app.route("/api/local-inbox/share", methods=["POST"])
def api_local_inbox_share():
    data = request.get_json(silent=True) or {}
    try:
        row = _inbound_store.upsert_share(
            alias=data.get("alias",""),
            account_id=data.get("account_id",""),
            assignee=data.get("assignee",""),
            note=data.get("note",""),
            enabled=bool(data.get("enabled", True)),
            regenerate=bool(data.get("regenerate", False)),
        )
        row["share_url"] = f"{_share_base_url()}/share/{row['share_token']}" if row.get("enabled") else ""
        _emit_log("info", f"更新邮箱分享: {row.get('alias')} → {row.get('assignee') or '-'} enabled={row.get('enabled')}")
        return jsonify({"ok":True,"share":row})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}), 400

@app.route("/api/shared/<token>/messages")
def api_shared_messages(token):
    share = _inbound_store.get_share(token)
    if not share:
        return jsonify({"ok":False,"error":"分享链接无效或已停用"}), 404
    limit = request.args.get("limit",50,type=int)
    offset = request.args.get("offset",0,type=int)
    data = _inbound_store.list_messages(alias=share["alias"], limit=limit, offset=offset)
    return jsonify({"ok":True,"share":{k:share.get(k) for k in ("alias","assignee","note")} , **data})

@app.route("/api/shared/<token>/messages/<int:mail_id>")
def api_shared_message(token, mail_id):
    share = _inbound_store.get_share(token)
    if not share:
        return jsonify({"ok":False,"error":"分享链接无效或已停用"}), 404
    msg = _inbound_store.get_message(mail_id, alias=share["alias"])
    if not msg:
        return jsonify({"ok":False,"error":"邮件不存在"}), 404
    # 分享页面不返回完整 raw，避免外部分发链接暴露过多底层头信息。
    msg.pop("raw", None)
    msg.pop("headers_json", None)
    return jsonify({"ok":True,"message":msg,"share":{k:share.get(k) for k in ("alias","assignee","note")}})

@app.route("/share/<token>")
def shared_inbox_page(token):
    return render_template_string(SHARED_INBOX_HTML, token=token)

@app.route("/cloudflare_inbound_worker.js")
def cloudflare_inbound_worker_template():
    base = _share_base_url()
    js = (HERE / "cloudflare_inbound_worker.js").read_text(encoding="utf-8")
    js = js.replace("__INBOUND_URL__", f"{base}/api/inbound-mail")
    return Response(js, mimetype="application/javascript; charset=utf-8")

@app.route("/api/scheduler/start", methods=["POST"])
def api_scheduler_start():
    global _scheduler_thread, _stop_event
    data = request.get_json(silent=True) or {}
    if data:
        _set_scheduler_config(data, persist=True)
    if _scheduler_thread and _scheduler_thread.is_alive():
        if _stop_event.is_set():
            return jsonify({"ok":False,"error":"调度器正在停止，请稍后再启动","stopping":True})
        return jsonify({"ok":True,"already_running":True,"config":_get_scheduler_config()})
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()
    _update_state(running=True, round_status="启动中...")
    return jsonify({"ok":True,"config":_get_scheduler_config()})

@app.route("/api/scheduler/config", methods=["GET", "POST"])
def api_scheduler_config():
    if request.method == "GET":
        return jsonify({"ok":True,"config":_get_scheduler_config()})
    try:
        cfg = _set_scheduler_config(request.get_json(silent=True) or {}, persist=True)
        return jsonify({"ok":True,"config":cfg})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    if not _scheduler_thread or not _scheduler_thread.is_alive():
        _update_state(running=False, creating=False, stopping=False, next_trigger=None, round_status="已停止")
        return jsonify({"ok":True,"already_stopped":True})
    _stop_event.set()
    _update_state(stopping=True, next_trigger=None, round_status="停止中：等待当前创建请求结束")
    _scheduler_thread.join(timeout=2.0)
    if _scheduler_thread.is_alive():
        _update_state(running=False, creating=False, stopping=False, next_trigger=None, round_status="已请求停止；当前请求结束后会完全退出")
        _emit_log("info", "调度器停止已请求；当前创建请求结束后会完全退出")
        return jsonify({"ok":True,"stopping":True,"note":"current request is still finishing"})
    _update_state(running=False, creating=False, stopping=False, next_trigger=None, round_status="已停止")
    return jsonify({"ok":True})

@app.route("/api/logs")
def api_logs():
    with _history_lock:
        return jsonify(_log_history)

@app.route("/api/log-stream")
def api_log_stream():
    q = queue.Queue()
    with _queues_lock:
        _log_queues.append(q)
    def generate():
        try:
            while True:
                try: entry = q.get(timeout=30); yield f"data: {json.dumps(entry,ensure_ascii=False)}\n\n"
                except queue.Empty: yield ": heartbeat\n\n"
        finally:
            with _queues_lock:
                if q in _log_queues:
                    _log_queues.remove(q)
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

def main():
    import argparse, os, signal as _signal
    parser = argparse.ArgumentParser(description="iCloud HME Web UI")
    parser.add_argument("--port",type=int,default=int(os.environ.get("PORT",5050)))
    parser.add_argument("--host",type=str,default=os.environ.get("HOST","0.0.0.0"))
    parser.add_argument("--scheduler",action="store_true",help="启动时自动运行调度器")
    parser.add_argument("--no-sync",action="store_true",help="跳过时间校准")
    args = parser.parse_args()
    if not args.no_sync:
        offset = _sync_time()
        if abs(offset)>0.5: print(f"[*] Time sync: offset {offset:.1f}s")
    threading.Thread(target=_health_loop, daemon=True).start()
    accounts = _account_mgr.list_accounts()
    if accounts:
        print(f"[+] {len(accounts)} account(s) loaded")
        for a in accounts: print(f"    [OK] {a.get('name','?')} - {a.get('real_email','?')} ({a.get('alias_total',0)} aliases)")
    else: print("[*] No accounts yet")
    if args.scheduler:
        global _scheduler_thread, _stop_event
        _stop_event.clear()
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()
        _update_state(running=True)
        print("[+] Scheduler auto-started")
    def _shutdown(sig,frame): print("\n[*] Shutting down..."); _stop_event.set(); os._exit(0)
    _signal.signal(_signal.SIGINT, _shutdown)
    _signal.signal(_signal.SIGTERM, _shutdown)
    try:
        from waitress import serve
        print(f"\n  Production → http://{args.host}:{args.port}\n")
        serve(app, host=args.host, port=args.port, threads=100)
    except ImportError:
        print(f"\n  Dev server → http://{args.host}:{args.port}\n")
        app.run(host=args.host, port=args.port, debug=False, threaded=True)

if __name__=="__main__": main()

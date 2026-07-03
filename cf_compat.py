#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare Temp Email 兼容层。

为本项目的本机收件箱提供与 dreamhunter2333/cloudflare_temp_email 相近的
地址凭证、用户登录、地址绑定、管理员地址列表 API。核心差异是：地址来源
仍然是 Apple iCloud Hide My Email，邮件仍由 Cloudflare Email Routing 投递到本机。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse

EMAIL_RE = re.compile(r"(?i)^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$")
HEX64_RE = re.compile(r"^[a-f0-9]{64}$", re.I)
JWT_IN_TEXT_RE = re.compile(r"([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)")


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def norm_email(value: Any) -> str:
    value = str(value or "").strip().lower()
    if not value or not EMAIL_RE.match(value):
        return ""
    return value


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data: str) -> bytes:
    data = str(data or "")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sha256_hex(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def normalize_jwt_token(value: Any) -> str:
    """容错清洗 Address/User JWT。

    实际使用里用户经常从 CSV、聊天工具或浏览器地址栏复制凭证，可能带上：
    - 完整登录 URL：/?credential=<jwt>
    - Authorization 前缀：Bearer <jwt>
    - 引号、尖括号、换行/空格、URL 编码

    JWT 本身只使用 base64url 字符和点号，所以去掉空白并提取签名段是安全的；
    真正的权限仍由 HMAC 签名校验决定。
    """
    s = str(value or "").strip()
    if not s:
        return ""

    # 去掉 CSV/聊天工具常见包裹。
    for _ in range(3):
        if len(s) >= 2 and ((s[0], s[-1]) in {('"', '"'), ("'", "'"), ("<", ">")}):
            s = s[1:-1].strip()
        else:
            break

    if s.lower().startswith("bearer "):
        s = s.split(None, 1)[1].strip()

    try:
        decoded = unquote(s)
    except Exception:
        decoded = s
    decoded = decoded.replace("\\r", "").replace("\\n", "").replace("\\t", "")

    # 支持粘贴完整登录链接，或只粘贴 query/hash。
    for candidate in (decoded, decoded.lstrip("?#")):
        try:
            parsed = urlparse(candidate)
            for part in (parsed.query, parsed.fragment):
                params = parse_qs(part, keep_blank_values=True)
                for key in ("credential", "jwt", "token"):
                    if params.get(key):
                        return normalize_jwt_token(params[key][0])
            if "=" in candidate:
                params = parse_qs(candidate, keep_blank_values=True)
                for key in ("credential", "jwt", "token"):
                    if params.get(key):
                        return normalize_jwt_token(params[key][0])
        except Exception:
            pass

    # JWT 不包含空白；这一步修复复制时被换行/空格打断的 token。
    compact = re.sub(r"\s+", "", decoded)
    m = JWT_IN_TEXT_RE.search(compact)
    return m.group(1) if m else compact


def normalize_password_secret(value: str) -> str:
    """兼容 cftempmail 前端传入的 SHA-256，也兼容本项目明文表单。"""
    value = str(value or "").strip()
    if not value:
        return ""
    return value.lower() if HEX64_RE.match(value) else sha256_hex(value)


class CfCompatStore:
    def __init__(self, db_path: Path, config_path: Path, deploy_secrets_path: Path):
        self.db_path = Path(db_path)
        self.config_path = Path(config_path)
        self.deploy_secrets_path = Path(deploy_secrets_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._config = self._load_config()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def _read_deploy_secret(self, key: str) -> str:
        try:
            for line in self.deploy_secrets_path.read_text(encoding="utf-8").splitlines():
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
        except Exception:
            pass
        return ""

    def _load_config(self) -> Dict[str, Any]:
        cfg: Dict[str, Any] = {}
        if self.config_path.exists():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cfg.update(raw)
            except Exception:
                pass
        changed = False
        if not cfg.get("jwt_secret"):
            cfg["jwt_secret"] = secrets.token_urlsafe(48)
            changed = True
        if not cfg.get("admin_password_hash"):
            deploy_pass = self._read_deploy_secret("BASIC_AUTH_PASS")
            if deploy_pass:
                cfg["admin_password_hash"] = sha256_hex(deploy_pass)
                changed = True
        if changed:
            self._save_config(cfg)
        return cfg

    def _save_config(self, cfg: Dict[str, Any]) -> None:
        self.config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            self.config_path.chmod(0o600)
        except Exception:
            pass

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cf_addresses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    account_id TEXT,
                    label TEXT,
                    source TEXT,
                    note TEXT,
                    password_hash TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_cf_addresses_name ON cf_addresses(name);
                CREATE INDEX IF NOT EXISTS idx_cf_addresses_account ON cf_addresses(account_id);

                CREATE TABLE IF NOT EXISTS cf_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT DEFAULT 'user',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_cf_users_email ON cf_users(user_email);

                CREATE TABLE IF NOT EXISTS cf_users_address (
                    user_id INTEGER NOT NULL,
                    address_id INTEGER NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, address_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cf_users_address_user ON cf_users_address(user_id);
                CREATE INDEX IF NOT EXISTS idx_cf_users_address_address ON cf_users_address(address_id);
                """
            )

    # ----- token / admin -----
    def sign(self, payload: Dict[str, Any]) -> str:
        header = {"alg": "HS256", "typ": "JWT"}
        h = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        p = b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        msg = f"{h}.{p}".encode("ascii")
        sig = hmac.new(str(self._config.get("jwt_secret") or "").encode("utf-8"), msg, hashlib.sha256).digest()
        return f"{h}.{p}.{b64url_encode(sig)}"

    def verify(self, token: str) -> Dict[str, Any]:
        token = normalize_jwt_token(token)
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Malformed token")
        msg = f"{parts[0]}.{parts[1]}".encode("ascii")
        expected = hmac.new(str(self._config.get("jwt_secret") or "").encode("utf-8"), msg, hashlib.sha256).digest()
        actual = b64url_decode(parts[2])
        if not hmac.compare_digest(expected, actual):
            raise ValueError("Invalid token signature")
        payload = json.loads(b64url_decode(parts[1]).decode("utf-8"))
        exp = payload.get("exp")
        if exp and int(exp) < int(time.time()):
            raise ValueError("Token expired")
        return payload

    def verify_admin_secret(self, value: str) -> bool:
        supplied = normalize_password_secret(value)
        expected = str(self._config.get("admin_password_hash") or "").strip().lower()
        return bool(supplied and expected and hmac.compare_digest(supplied, expected))

    def admin_cookie_value(self, password: str) -> str:
        return normalize_password_secret(password)

    # ----- addresses -----
    def ensure_address(self, address: str, account_id: str = "", label: str = "", source: str = "cloud", note: str = "") -> Dict[str, Any]:
        address = norm_email(address)
        if not address:
            raise ValueError("invalid address")
        now = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cf_addresses(name, account_id, label, source, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    account_id=COALESCE(NULLIF(excluded.account_id,''), cf_addresses.account_id),
                    label=COALESCE(NULLIF(excluded.label,''), cf_addresses.label),
                    source=COALESCE(NULLIF(excluded.source,''), cf_addresses.source),
                    note=COALESCE(NULLIF(excluded.note,''), cf_addresses.note),
                    updated_at=excluded.updated_at
                """,
                (address, str(account_id or ""), str(label or ""), str(source or ""), str(note or ""), now, now),
            )
            row = conn.execute("SELECT * FROM cf_addresses WHERE name=?", (address,)).fetchone()
        return dict(row)

    def sync_addresses(self, records: Iterable[Dict[str, Any]]) -> int:
        n = 0
        for rec in records or []:
            email = norm_email(rec.get("email") or rec.get("name") or rec.get("alias"))
            if not email:
                continue
            self.ensure_address(
                email,
                account_id=str(rec.get("account_id") or ""),
                label=str(rec.get("label") or ""),
                source=str(rec.get("source") or "cloud"),
            )
            n += 1
        return n

    def get_address(self, address_id: Any = None, name: str = "") -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if address_id is not None and str(address_id).strip():
                row = conn.execute("SELECT * FROM cf_addresses WHERE id=?", (int(address_id),)).fetchone()
            else:
                row = conn.execute("SELECT * FROM cf_addresses WHERE name=?", (norm_email(name),)).fetchone()
        return dict(row) if row else None

    def address_token(self, address_id: Any = None, name: str = "") -> str:
        row = self.get_address(address_id=address_id, name=name)
        if not row:
            raise ValueError("address not found")
        return self.sign({"address": row["name"], "address_id": int(row["id"])})

    def verify_address_token(self, token: str) -> Dict[str, Any]:
        payload = self.verify(token)
        address = norm_email(payload.get("address"))
        address_id = payload.get("address_id")
        if not address:
            raise ValueError("invalid address credential")
        row = None
        if address_id:
            try:
                row = self.get_address(address_id=address_id)
            except Exception:
                row = None
            # 老导出凭证里的 address_id 可能因为删除/重建本机记录而失效；
            # JWT 已经签名校验通过时，地址字符串本身才是稳定身份，允许按地址兜底。
            if row and row.get("name") != address:
                row = None
        if not row:
            row = self.get_address(name=address)
        if not row:
            raise ValueError("address not found")
        payload["address"] = address
        payload["address_id"] = int(row["id"])
        return payload

    def list_addresses(self, query: str = "", limit: int = 50, offset: int = 0, sort_by: str = "id", sort_order: str = "desc") -> Dict[str, Any]:
        query = str(query or "").strip().lower()
        limit = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))
        allowed_sort = {
            "id": "a.id",
            "name": "a.name",
            "created_at": "a.created_at",
            "updated_at": "a.updated_at",
            "mail_count": "mail_count",
        }
        order_col = allowed_sort.get(str(sort_by or "id"), "a.id")
        order_dir = "asc" if str(sort_order).lower() in ("asc", "ascend") else "desc"
        params: List[Any] = []
        where = ""
        if query:
            where = "WHERE a.name LIKE ? OR COALESCE(a.label,'') LIKE ? OR COALESCE(a.account_id,'') LIKE ?"
            like = f"%{query}%"
            params.extend([like, like, like])
        with self._lock, self._connect() as conn:
            count = conn.execute(f"SELECT COUNT(*) AS c FROM cf_addresses a {where}", params).fetchone()["c"]
            rows = conn.execute(
                f"""
                SELECT a.id, a.name, a.account_id, a.label, a.source, a.note, a.created_at, a.updated_at,
                       (SELECT COUNT(*) FROM inbound_mails m WHERE m.hme_alias = a.name) AS mail_count,
                       0 AS send_count
                FROM cf_addresses a
                {where}
                ORDER BY {order_col} {order_dir}
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()
        return {"results": [dict(r) for r in rows], "count": count, "limit": limit, "offset": offset}

    def export_credentials(self) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.name, a.account_id, a.label, a.source, a.note, a.created_at, a.updated_at,
                       (SELECT COUNT(*) FROM inbound_mails m WHERE m.hme_alias = a.name) AS mail_count,
                       0 AS send_count
                FROM cf_addresses a
                ORDER BY a.name ASC
                """
            ).fetchall()
        data = {"results": [dict(r) for r in rows]}
        out = []
        for row in data["results"]:
            item = dict(row)
            item["jwt"] = self.address_token(address_id=row["id"])
            item["credential"] = item["jwt"]
            out.append(item)
        return out

    def delete_address_record(self, address_id: Any, delete_mails: bool = False) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT name FROM cf_addresses WHERE id=?", (int(address_id),)).fetchone()
            if not row:
                return False
            name = row["name"]
            conn.execute("DELETE FROM cf_users_address WHERE address_id=?", (int(address_id),))
            conn.execute("DELETE FROM cf_addresses WHERE id=?", (int(address_id),))
            if delete_mails:
                conn.execute("DELETE FROM inbound_mails WHERE hme_alias=?", (name,))
        return True

    # ----- users -----
    def create_user(self, email: str, password: str, role: str = "user") -> Dict[str, Any]:
        email = norm_email(email)
        password_hash = normalize_password_secret(password)
        if not email or not password_hash:
            raise ValueError("invalid email or password")
        now = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO cf_users(user_email, password_hash, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (email, password_hash, role or "user", now, now),
            )
            row = conn.execute("SELECT id, user_email, role, created_at, updated_at FROM cf_users WHERE user_email=?", (email,)).fetchone()
        return dict(row)

    def verify_user(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        email = norm_email(email)
        supplied = normalize_password_secret(password)
        if not email or not supplied:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM cf_users WHERE user_email=?", (email,)).fetchone()
        if not row:
            return None
        row = dict(row)
        if not hmac.compare_digest(str(row.get("password_hash") or ""), supplied):
            return None
        return row

    def user_token(self, user: Dict[str, Any]) -> str:
        return self.sign({
            "user_email": user["user_email"],
            "user_id": int(user["id"]),
            "role": user.get("role") or "user",
            "iat": int(time.time()),
            "exp": int(time.time()) + 30 * 24 * 60 * 60,
        })

    def verify_user_token(self, token: str) -> Dict[str, Any]:
        payload = self.verify(token)
        user_id = payload.get("user_id")
        if not user_id:
            raise ValueError("invalid user token")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT id, user_email, role FROM cf_users WHERE id=?", (int(user_id),)).fetchone()
        if not row:
            raise ValueError("user not found")
        payload.update(dict(row))
        return payload

    def bind_user_address(self, user_id: Any, address_id: Any) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO cf_users_address(user_id, address_id, created_at) VALUES (?, ?, ?)",
                (int(user_id), int(address_id), now_iso()),
            )
        return True

    def unbind_user_address(self, user_id: Any, address_id: Any) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM cf_users_address WHERE user_id=? AND address_id=?", (int(user_id), int(address_id)))
        return True

    def list_user_addresses(self, user_id: Any) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.name, a.account_id, a.label, a.created_at, a.updated_at,
                       (SELECT COUNT(*) FROM inbound_mails m WHERE m.hme_alias = a.name) AS mail_count,
                       0 AS send_count
                FROM cf_addresses a
                JOIN cf_users_address ua ON ua.address_id = a.id
                WHERE ua.user_id=?
                ORDER BY a.id DESC
                """,
                (int(user_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_users(self, limit: int = 50, offset: int = 0, query: str = "") -> Dict[str, Any]:
        limit = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))
        query = str(query or "").strip().lower()
        params: List[Any] = []
        where = ""
        if query:
            where = "WHERE user_email LIKE ? OR role LIKE ?"
            params.extend([f"%{query}%", f"%{query}%"])
        with self._lock, self._connect() as conn:
            count = conn.execute(f"SELECT COUNT(*) AS c FROM cf_users {where}", params).fetchone()["c"]
            rows = conn.execute(
                f"""
                SELECT u.id, u.user_email, u.role, u.created_at, u.updated_at,
                       (SELECT COUNT(*) FROM cf_users_address ua WHERE ua.user_id=u.id) AS address_count
                FROM cf_users u {where}
                ORDER BY u.id DESC LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()
        return {"results": [dict(r) for r in rows], "count": count, "limit": limit, "offset": offset}

    def delete_user(self, user_id: Any) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM cf_users_address WHERE user_id=?", (int(user_id),))
            conn.execute("DELETE FROM cf_users WHERE id=?", (int(user_id),))
        return True

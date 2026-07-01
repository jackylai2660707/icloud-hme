#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机入站邮件存储。

用于接收 Cloudflare Email Routing Worker 投递过来的邮件，按 iCloud HME
隐私邮箱/派生邮箱分箱，并为每个邮箱生成只读分享链接。
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from datetime import datetime
from email import policy
from email.parser import Parser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _norm_email(value: Any) -> str:
    value = str(value or "").strip().lower()
    if "@" not in value or any(ch.isspace() for ch in value):
        return ""
    return value


def _base_plus_email(email: str) -> str:
    email = _norm_email(email)
    if "+" not in email or "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    return f"{local.split('+', 1)[0]}@{domain}"


def _decode_header_value(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


class InboundMailStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS inbound_mails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT,
                    source_from TEXT,
                    envelope_to TEXT,
                    hme_alias TEXT,
                    base_alias TEXT,
                    account_id TEXT,
                    subject TEXT,
                    sender_name TEXT,
                    recipient_headers TEXT,
                    text TEXT,
                    html TEXT,
                    raw TEXT,
                    headers_json TEXT,
                    assigned_to TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_inbound_mails_alias ON inbound_mails(hme_alias);
                CREATE INDEX IF NOT EXISTS idx_inbound_mails_base_alias ON inbound_mails(base_alias);
                CREATE INDEX IF NOT EXISTS idx_inbound_mails_account ON inbound_mails(account_id);
                CREATE INDEX IF NOT EXISTS idx_inbound_mails_created ON inbound_mails(created_at);
                CREATE INDEX IF NOT EXISTS idx_inbound_mails_message_id ON inbound_mails(message_id);

                CREATE TABLE IF NOT EXISTS alias_shares (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alias TEXT UNIQUE,
                    base_alias TEXT,
                    account_id TEXT,
                    assignee TEXT,
                    note TEXT,
                    share_token TEXT UNIQUE,
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_alias_shares_token ON alias_shares(share_token);
                CREATE INDEX IF NOT EXISTS idx_alias_shares_assignee ON alias_shares(assignee);
                """
            )

    @staticmethod
    def parse_raw(raw: str) -> Dict[str, Any]:
        raw = raw or ""
        msg = Parser(policy=policy.default).parsestr(raw)
        headers: Dict[str, str] = {}
        for k, v in msg.items():
            headers[str(k)] = _decode_header_value(v)

        text_parts: List[str] = []
        html_parts: List[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.is_multipart():
                    continue
                ctype = part.get_content_type()
                disp = str(part.get_content_disposition() or "").lower()
                if disp == "attachment":
                    continue
                if ctype not in ("text/plain", "text/html"):
                    continue
                try:
                    content = part.get_content()
                except Exception:
                    try:
                        payload = part.get_payload(decode=True) or b""
                        content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    except Exception:
                        content = ""
                if ctype == "text/html":
                    html_parts.append(str(content or ""))
                else:
                    text_parts.append(str(content or ""))
        else:
            ctype = msg.get_content_type()
            if ctype in ("text/plain", "text/html"):
                try:
                    content = msg.get_content()
                except Exception:
                    content = ""
                if ctype == "text/html":
                    html_parts.append(str(content or ""))
                else:
                    text_parts.append(str(content or ""))

        date_value = ""
        if msg.get("Date"):
            try:
                date_value = parsedate_to_datetime(str(msg.get("Date"))).isoformat()
            except Exception:
                date_value = str(msg.get("Date") or "")

        return {
            "message_id": _decode_header_value(msg.get("Message-ID")),
            "subject": _decode_header_value(msg.get("Subject")) or "(无主题)",
            "from": _decode_header_value(msg.get("From")),
            "to": _decode_header_value(msg.get("To")),
            "date": date_value,
            "headers": headers,
            "text": "\n\n".join(x for x in text_parts if x).strip(),
            "html": "\n\n".join(x for x in html_parts if x).strip(),
        }

    @staticmethod
    def detect_alias(payload: Dict[str, Any], parsed: Dict[str, Any], known_aliases: Iterable[str]) -> Dict[str, str]:
        known = []
        seen = set()
        for alias in known_aliases or []:
            alias = _norm_email(alias)
            if alias and alias not in seen:
                known.append(alias)
                seen.add(alias)

        header_values: List[str] = []
        payload_headers = payload.get("headers") or {}
        if isinstance(payload_headers, dict):
            header_values.extend(str(v or "") for v in payload_headers.values())
        parsed_headers = parsed.get("headers") or {}
        if isinstance(parsed_headers, dict):
            # 优先 Apple/转发相关头，再扫全部头。
            preferred = [
                "Delivered-To", "X-Original-To", "X-Forwarded-To", "Envelope-To",
                "Apparently-To", "Resent-To", "To", "Cc", "X-Apple-Original-To",
                "X-Apple-Forwarded-To", "Original-Recipient", "Final-Recipient",
            ]
            for k in preferred:
                if k in parsed_headers:
                    header_values.append(str(parsed_headers.get(k) or ""))
            header_values.extend(str(v or "") for v in parsed_headers.values())

        candidates_text = "\n".join([
            str(payload.get("hme_alias") or ""),
            str(payload.get("to") or ""),
            str(payload.get("envelope_to") or ""),
            str(parsed.get("to") or ""),
            "\n".join(header_values),
            str(payload.get("raw") or "")[:200000],
        ]).lower()

        # 精确匹配已知邮箱，优先匹配更长的 +tag 派生地址。
        for alias in sorted(known, key=len, reverse=True):
            if alias and alias in candidates_text:
                return {"hme_alias": alias, "base_alias": _base_plus_email(alias)}

        # 没有命中已知邮箱时，从头部/正文中兜底找一个 @icloud/@me/@mac 地址。
        for found in EMAIL_RE.findall(candidates_text):
            found = _norm_email(found)
            if found.endswith(("@icloud.com", "@me.com", "@mac.com")):
                return {"hme_alias": found, "base_alias": _base_plus_email(found)}

        fallback = _norm_email(payload.get("to") or parsed.get("to") or "")
        return {"hme_alias": fallback, "base_alias": _base_plus_email(fallback)}

    def ingest(self, payload: Dict[str, Any], known_aliases: Iterable[str] = (), alias_account_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        raw = str(payload.get("raw") or payload.get("rawEmail") or "")
        if not raw:
            raise ValueError("raw 邮件内容不能为空")
        if len(raw.encode("utf-8", errors="ignore")) > 30 * 1024 * 1024:
            raise ValueError("raw 邮件过大，已拒绝")

        parsed = self.parse_raw(raw)
        detected = self.detect_alias(payload, parsed, known_aliases)
        hme_alias = detected.get("hme_alias", "")
        base_alias = detected.get("base_alias", "")
        alias_account_map = alias_account_map or {}
        account_id = (
            alias_account_map.get(hme_alias)
            or alias_account_map.get(base_alias)
            or str(payload.get("account_id") or "")
        )

        source_from = str(payload.get("from") or parsed.get("from") or "").strip()
        envelope_to = _norm_email(payload.get("to") or payload.get("envelope_to") or "")
        message_id = str(payload.get("message_id") or parsed.get("message_id") or "").strip()
        headers_json = json.dumps(payload.get("headers") or parsed.get("headers") or {}, ensure_ascii=False)
        recipient_headers = json.dumps({
            "payload_to": payload.get("to") or "",
            "parsed_to": parsed.get("to") or "",
            "detected_alias": hme_alias,
        }, ensure_ascii=False)

        with self._lock, self._connect() as conn:
            share = conn.execute(
                "SELECT assignee FROM alias_shares WHERE alias=? AND enabled=1",
                (hme_alias,),
            ).fetchone()
            assigned_to = share["assignee"] if share else ""
            cur = conn.execute(
                """
                INSERT INTO inbound_mails
                (message_id, source_from, envelope_to, hme_alias, base_alias, account_id,
                 subject, sender_name, recipient_headers, text, html, raw, headers_json,
                 assigned_to, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id, source_from, envelope_to, hme_alias, base_alias, account_id,
                    parsed.get("subject") or "(无主题)", parsed.get("from") or source_from,
                    recipient_headers, parsed.get("text") or "", parsed.get("html") or "",
                    raw, headers_json, assigned_to, _now_iso(),
                ),
            )
            mail_id = cur.lastrowid
        return {
            "id": mail_id,
            "message_id": message_id,
            "hme_alias": hme_alias,
            "base_alias": base_alias,
            "account_id": account_id,
            "subject": parsed.get("subject") or "(无主题)",
            "assigned_to": assigned_to,
        }

    def list_aliases(self, alias_filter: str = "", assignee: str = "") -> List[Dict[str, Any]]:
        params: List[Any] = []
        where = []
        if alias_filter:
            where.append("(m.hme_alias LIKE ? OR m.base_alias LIKE ?)")
            like = f"%{alias_filter.lower()}%"
            params.extend([like, like])
        if assignee:
            where.append("COALESCE(s.assignee, '') = ?")
            params.append(assignee)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT
              m.hme_alias AS alias,
              m.base_alias AS base_alias,
              COALESCE(MAX(m.account_id), '') AS account_id,
              COUNT(*) AS mail_count,
              MAX(m.created_at) AS latest_at,
              COALESCE(s.assignee, '') AS assignee,
              COALESCE(s.note, '') AS note,
              COALESCE(s.share_token, '') AS share_token,
              COALESCE(s.enabled, 0) AS share_enabled
            FROM inbound_mails m
            LEFT JOIN alias_shares s ON s.alias = m.hme_alias
            {where_sql}
            GROUP BY m.hme_alias
            ORDER BY latest_at DESC
        """
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def list_assignees(self) -> List[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT assignee FROM alias_shares WHERE assignee IS NOT NULL AND assignee<>'' ORDER BY assignee"
            ).fetchall()
            return [r["assignee"] for r in rows]

    def list_shares(self) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM alias_shares ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]

    def list_messages(self, alias: str = "", assignee: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        params: List[Any] = []
        where = []
        if alias:
            where.append("m.hme_alias = ?")
            params.append(_norm_email(alias))
        if assignee:
            where.append("COALESCE(s.assignee, m.assigned_to, '') = ?")
            params.append(assignee)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        with self._lock, self._connect() as conn:
            count = conn.execute(
                f"SELECT COUNT(*) AS c FROM inbound_mails m LEFT JOIN alias_shares s ON s.alias=m.hme_alias {where_sql}",
                params,
            ).fetchone()["c"]
            rows = conn.execute(
                f"""
                SELECT m.id, m.message_id, m.source_from AS "from", m.envelope_to,
                       m.hme_alias, m.base_alias, m.account_id, m.subject,
                       m.sender_name, m.assigned_to, m.created_at,
                       COALESCE(s.assignee, m.assigned_to, '') AS assignee
                FROM inbound_mails m
                LEFT JOIN alias_shares s ON s.alias=m.hme_alias
                {where_sql}
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset],
            ).fetchall()
        return {"messages": [dict(r) for r in rows], "count": count, "limit": limit, "offset": offset}

    def get_message(self, mail_id: int, alias: str = "", assignee: str = "") -> Optional[Dict[str, Any]]:
        params: List[Any] = [int(mail_id)]
        where = ["m.id = ?"]
        if alias:
            where.append("m.hme_alias = ?")
            params.append(_norm_email(alias))
        if assignee:
            where.append("COALESCE(s.assignee, m.assigned_to, '') = ?")
            params.append(assignee)
        where_sql = "WHERE " + " AND ".join(where)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT m.*, COALESCE(s.assignee, m.assigned_to, '') AS assignee
                FROM inbound_mails m
                LEFT JOIN alias_shares s ON s.alias=m.hme_alias
                {where_sql}
                """,
                params,
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["headers"] = json.loads(item.get("headers_json") or "{}")
        except Exception:
            item["headers"] = {}
        return item

    def upsert_share(self, alias: str, account_id: str = "", assignee: str = "", note: str = "", enabled: bool = True, regenerate: bool = False) -> Dict[str, Any]:
        alias = _norm_email(alias)
        if not alias:
            raise ValueError("alias 不能为空")
        base_alias = _base_plus_email(alias)
        now = _now_iso()
        with self._lock, self._connect() as conn:
            old = conn.execute("SELECT * FROM alias_shares WHERE alias=?", (alias,)).fetchone()
            token = old["share_token"] if old and old["share_token"] and not regenerate else secrets.token_urlsafe(32)
            if old:
                conn.execute(
                    """
                    UPDATE alias_shares
                    SET base_alias=?, account_id=?, assignee=?, note=?, share_token=?, enabled=?, updated_at=?
                    WHERE alias=?
                    """,
                    (base_alias, account_id, assignee.strip(), note.strip(), token, 1 if enabled else 0, now, alias),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO alias_shares
                    (alias, base_alias, account_id, assignee, note, share_token, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (alias, base_alias, account_id, assignee.strip(), note.strip(), token, 1 if enabled else 0, now, now),
                )
            if assignee.strip():
                conn.execute(
                    "UPDATE inbound_mails SET assigned_to=? WHERE hme_alias=?",
                    (assignee.strip(), alias),
                )
            row = conn.execute("SELECT * FROM alias_shares WHERE alias=?", (alias,)).fetchone()
        return dict(row)

    def get_share(self, token: str) -> Optional[Dict[str, Any]]:
        token = str(token or "").strip()
        if not token:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM alias_shares WHERE share_token=? AND enabled=1",
                (token,),
            ).fetchone()
        return dict(row) if row else None

    def stats(self) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM inbound_mails").fetchone()["c"]
            aliases = conn.execute("SELECT COUNT(DISTINCT hme_alias) AS c FROM inbound_mails").fetchone()["c"]
            shares = conn.execute("SELECT COUNT(*) AS c FROM alias_shares WHERE enabled=1").fetchone()["c"]
        return {"total_mails": total, "alias_count": aliases, "share_count": shares}

#!/usr/bin/env python3
"""
iCloud Mail — IMAP 收件箱检查模块
===================================
通过 Apple 应用专用密码连接 iCloud Mail IMAP，
查询隐私邮箱别名收到的邮件。

用法:
    from icloud_mail import ICloudMail

    mail = ICloudMail("user@icloud.com", "app-specific-password")
    emails = mail.check_inbox(limit=20)
    # 查找某个隐私别名收到的邮件
    alias_mail = mail.find_by_recipient("alias@icloud.com")

前提:
  - 需要在 appleid.apple.com 生成「App 专用密码」
  - iCloud 邮箱已开启 IMAP 访问
"""

import imaplib
import email
import time
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional, Dict, List

IMAP_SERVER = "imap.mail.me.com"
IMAP_PORT = 993
IMAP_TIMEOUT = 20


class ICloudMail:
    """iCloud Mail IMAP 客户端"""

    def __init__(self, apple_id: str, app_password: str, verbose: bool = False):
        self.apple_id = apple_id
        self.app_password = app_password
        self.verbose = verbose
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    def connect(self) -> bool:
        try:
            import sys
            if sys.version_info >= (3, 9):
                self._conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=IMAP_TIMEOUT)
            else:
                import socket
                old_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(IMAP_TIMEOUT)
                try:
                    self._conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
                finally:
                    socket.setdefaulttimeout(old_timeout)
            self._conn.login(self.apple_id, self.app_password)
            if self.verbose:
                print(f"[IMAP] Connected as {self.apple_id}")
            return True
        except imaplib.IMAP4.error as e:
            msg = str(e)
            if "authentication" in msg.lower() or "login" in msg.lower():
                raise RuntimeError(
                    f"IMAP 登录失败 — 请检查:\n"
                    f"  1. 应用专用密码是否正确\n"
                    f"  2. Apple ID: {self.apple_id}\n"
                    f"  3. 是否已在 appleid.apple.com 生成密码"
                )
            raise RuntimeError(f"IMAP 连接失败: {msg}")
        except Exception as e:
            raise RuntimeError(f"IMAP 连接失败: {e}")

    def disconnect(self):
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and self._conn.state == "SELECTED"

    def _parse_uid_folder(self, uid_or_id: bytes) -> tuple:
        uid_str = uid_or_id.decode() if isinstance(uid_or_id, bytes) else str(uid_or_id)
        if ":" in uid_str:
            folder, real_uid = uid_str.split(":", 1)
        else:
            folder, real_uid = "INBOX", uid_str
        return folder, real_uid.encode()

    def _ensure_connected_folder(self, folder: str):
        if not self._conn:
            self.connect()
        try:
            status, data = self._conn.select(folder, readonly=True)
            if status != "OK":
                self._conn.select("INBOX", readonly=True)
        except Exception:
            try:
                self._conn.select("INBOX", readonly=True)
            except:
                pass

    def _ensure_connected(self):
        self._ensure_connected_folder("INBOX")

    def check_inbox(self, limit: int = 50, days: int = 7) -> List[Dict]:
        inbox_mails = self._search_and_fetch_folder("INBOX", None, limit, days)
        junk_mails = self._search_and_fetch_folder("Junk", None, limit, days)
        all_mails = inbox_mails + junk_mails
        all_mails.sort(key=lambda x: x.get("date", ""), reverse=True)
        return all_mails[:limit]

    def check_unread(self, limit: int = 50, days: int = 7) -> List[Dict]:
        inbox_mails = self._search_and_fetch_folder("INBOX", "UNSEEN", limit, days)
        junk_mails = self._search_and_fetch_folder("Junk", "UNSEEN", limit, days)
        all_mails = inbox_mails + junk_mails
        all_mails.sort(key=lambda x: x.get("date", ""), reverse=True)
        return all_mails[:limit]

    def find_by_recipient(self, recipient: str, limit: int = 20, days: int = 30) -> List[Dict]:
        inbox_mails = self._search_and_fetch_folder("INBOX", f'TO "{recipient}"', limit, days)
        junk_mails = self._search_and_fetch_folder("Junk", f'TO "{recipient}"', limit, days)
        all_mails = inbox_mails + junk_mails
        if not all_mails:
            all_inbox = self._search_and_fetch_folder("INBOX", None, limit * 3, days)
            all_junk = self._search_and_fetch_folder("Junk", None, limit * 3, days)
            all_mails = [m for m in (all_inbox + all_junk) if recipient.lower() in m.get("to", "").lower()]
        all_mails.sort(key=lambda x: x.get("date", ""), reverse=True)
        return all_mails[:limit]

    def stream_inbox(self, limit: int = 50, days: int = 7):
        for m in self._stream_folder("INBOX", limit, days):
            yield m
        for m in self._stream_folder("Junk", limit, days):
            yield m

    def _parse_multiple_headers(self, data, folder: str) -> List[Dict]:
        emails = []
        if not data:
            return emails
        for i in range(len(data)):
            item = data[i]
            if isinstance(item, tuple) and len(item) >= 2:
                header_info = item[0]
                raw_headers = item[1]
                if not isinstance(header_info, bytes) or not isinstance(raw_headers, bytes):
                    continue
                import re
                uid_match = re.search(r'UID\s+(\d+)', header_info.decode(errors='replace'), re.IGNORECASE)
                if uid_match:
                    uid = uid_match.group(1)
                    try:
                        msg = email.message_from_bytes(raw_headers + b"\r\n\r\n")
                        combined_id = f"{folder}:{uid}"
                        emails.append({
                            "id": combined_id,
                            "from": self._decode_header(msg.get("From", "")),
                            "to": self._decode_header(msg.get("To", "")),
                            "subject": self._decode_header(msg.get("Subject", "")),
                            "date": self._safe_date(msg.get("Date", "")),
                            "body_preview": "",
                            "size": len(raw_headers),
                        })
                    except Exception:
                        continue
        return emails

    def _search_and_fetch_folder(self, folder: str, criteria: Optional[str], limit: int, days: int) -> List[Dict]:
        try:
            self._ensure_connected_folder(folder)
        except Exception:
            return []
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        full = f'({criteria} SINCE "{since}")' if criteria else f'(SINCE "{since}")'
        try:
            status, data = self._conn.uid("SEARCH", None, full)
            if status != "OK" or not data[0]:
                return []
            uids = data[0].split()
            recent = uids[-limit:] if len(uids) > limit else uids
            if not recent:
                return []
            uids_str = b",".join(recent)
            status, fetch_data = self._conn.uid("FETCH", uids_str, "(BODY.PEEK[HEADER])")
            if status != "OK" or not fetch_data:
                return []
            emails = self._parse_multiple_headers(fetch_data, folder)
            emails.reverse()
            return emails
        except Exception:
            return []

    def _stream_folder(self, folder: str, limit: int = 50, days: int = 7):
        try:
            self._ensure_connected_folder(folder)
        except Exception:
            return
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        full = f'(SINCE "{since}")'
        try:
            status, data = self._conn.uid("SEARCH", None, full)
            if status != "OK" or not data[0]:
                return
            uids = data[0].split()
            recent = uids[-limit:] if len(uids) > limit else uids
            if not recent:
                return
            uids_str = b",".join(recent)
            status, fetch_data = self._conn.uid("FETCH", uids_str, "(BODY.PEEK[HEADER])")
            if status == "OK" and fetch_data:
                emails = self._parse_multiple_headers(fetch_data, folder)
                emails.reverse()
                for e in emails:
                    yield e
        except Exception:
            return

    def _fetch_headers(self, msg_id: bytes) -> Optional[Dict]:
        status, data = self._conn.fetch(msg_id, "(BODY.PEEK[HEADER])")
        if status != "OK":
            return None
        return self._parse_header_response(data, msg_id, "INBOX")

    def _fetch_headers_uid(self, uid: bytes) -> Optional[Dict]:
        return self._fetch_headers_uid_folder(uid, "INBOX")

    def _fetch_headers_uid_folder(self, uid: bytes, folder: str) -> Optional[Dict]:
        self._ensure_connected_folder(folder)
        status, data = self._conn.uid("FETCH", uid, "(BODY.PEEK[HEADER])")
        if status != "OK":
            return None
        return self._parse_header_response(data, uid, folder)

    def _parse_header_response(self, data, msg_id: bytes, folder: str = "INBOX") -> Optional[Dict]:
        raw = self._extract_body(data)
        if not raw:
            return None
        try:
            msg = email.message_from_bytes(raw + b"\r\n\r\n")
        except Exception:
            return None
        uid_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        combined_id = f"{folder}:{uid_str}"
        return {
            "id": combined_id,
            "from": self._decode_header(msg.get("From", "")),
            "to": self._decode_header(msg.get("To", "")),
            "subject": self._decode_header(msg.get("Subject", "")),
            "date": self._safe_date(msg.get("Date", "")),
            "body_preview": "",
            "size": len(raw),
        }

    def fetch_body(self, msg_id: bytes) -> Optional[str]:
        folder, real_uid = self._parse_uid_folder(msg_id)
        self._ensure_connected_folder(folder)
        status, data = self._conn.uid("FETCH", real_uid, "(BODY.PEEK[TEXT])")
        if status != "OK":
            return None
        raw = self._extract_body(data)
        if not raw:
            return None
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("latin-1", errors="replace")

    def fetch_full(self, msg_id: bytes) -> Optional[Dict]:
        folder, real_uid = self._parse_uid_folder(msg_id)
        self._ensure_connected_folder(folder)
        msg = self._fetch_full_message(real_uid, folder)
        return msg

    def _fetch_full_message(self, msg_id: bytes, folder: str = "INBOX") -> Optional[Dict]:
        status, data = self._conn.uid("FETCH", msg_id, "(BODY.PEEK[])")
        if status != "OK":
            status, data = self._conn.uid("FETCH", msg_id, "(RFC822)")
            if status != "OK":
                return None
        raw = self._extract_body(data)
        if not raw:
            return None
        try:
            em = email.message_from_bytes(raw)
        except Exception:
            return None

        # Extract headers directly from the full message (no second fetch needed)
        uid_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        combined_id = f"{folder}:{uid_str}"
        result = {
            "id": combined_id,
            "from": self._decode_header(em.get("From", "")),
            "to": self._decode_header(em.get("To", "")),
            "subject": self._decode_header(em.get("Subject", "")),
            "date": self._safe_date(em.get("Date", "")),
            "body_preview": "",
            "size": len(raw),
        }

        body = ""
        html_body = ""
        cids = {}
        if em.is_multipart():
            for part in em.walk():
                if part.get_content_maintype() == 'multipart':
                    continue
                ctype = part.get_content_type().split(';')[0].strip().lower()
                cid = part.get('Content-ID')
                try:
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    if cid and ctype.startswith('image/'):
                        import base64
                        cid_clean = cid.strip('<>')
                        b64_data = base64.b64encode(payload).decode('utf-8')
                        cids[cid_clean] = f"data:{ctype};base64,{b64_data}"
                        continue
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace")
                    if ctype == "text/plain" and not body:
                        body = text
                    elif ctype == "text/html" and not html_body:
                        html_body = text
                except Exception:
                    pass
        else:
            try:
                payload = em.get_payload(decode=True)
                if payload:
                    charset = em.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace")
                    ctype = em.get_content_type().split(';')[0].strip().lower()
                    if ctype == "text/plain":
                        body = text
                    elif ctype == "text/html":
                        html_body = text
                    else:
                        body = text
            except Exception:
                pass

        if html_body and cids:
            import re
            for cid_clean, data_uri in cids.items():
                html_body = re.sub(
                    r'src=["\']?cid:' + re.escape(cid_clean) + r'["\']?',
                    f'src="{data_uri}"',
                    html_body,
                    flags=re.IGNORECASE
                )

        if not body and html_body:
            body = _strip_html(html_body)

        result["body"] = body[:5000]
        result["html"] = html_body
        result["content_type"] = em.get_content_type()
        return result

    @staticmethod
    def _extract_body(data: list) -> Optional[bytes]:
        for item in data:
            if isinstance(item, tuple):
                for sub in item:
                    if isinstance(sub, bytes) and len(sub) > 500:
                        return sub
        for item in data:
            if isinstance(item, bytes) and len(item) > 500:
                return item
        best = None
        for item in data:
            if isinstance(item, bytes):
                if best is None or len(item) > len(best):
                    best = item
            elif isinstance(item, tuple):
                for sub in item:
                    if isinstance(sub, bytes):
                        if best is None or len(sub) > len(best):
                            best = sub
        return best if best and len(best) > 100 else None

    @staticmethod
    def _decode_header(value: str) -> str:
        if not value:
            return ""
        parts = decode_header(value)
        result = []
        for part, charset in parts:
            if isinstance(part, bytes):
                try:
                    result.append(part.decode(charset or "utf-8", errors="replace"))
                except Exception:
                    result.append(part.decode("utf-8", errors="replace"))
            else:
                result.append(str(part))
        return " ".join(result)

    def test_connection(self) -> Dict:
        try:
            self.connect()
            status, data = self._conn.select("INBOX", readonly=True)
            if status != "OK":
                return {"ok": False, "error": "无法选中 INBOX"}
            msg_count = int(data[0]) if data else 0
            self.disconnect()
            return {"ok": True, "email": self.apple_id, "inbox_count": msg_count}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @staticmethod
    def _safe_date(date_str: str) -> str:
        try:
            return parsedate_to_datetime(date_str).isoformat()
        except Exception:
            return date_str


def _strip_html(html: str) -> str:
    import re
    html = re.sub(r'<head[^>]*>.*?</head>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</?(div|p|h[1-6]|li|tr|article|section|header|footer|blockquote|pre|table|hr)[^>]*>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r'\2 (\1)', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<li[^>]*>', '• ', html, flags=re.IGNORECASE)
    html = re.sub(r'<[^>]+>', '', html)
    import html as _html
    text = _html.unescape(html)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'^\s+', '', text, flags=re.MULTILINE)
    return text.strip()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("用法: python icloud_mail.py <apple_id> <app_password> [alias_email]")
        sys.exit(1)
    apple_id, app_pwd = sys.argv[1], sys.argv[2]
    alias = sys.argv[3] if len(sys.argv) > 3 else None
    mail = ICloudMail(apple_id, app_pwd, verbose=True)
    result = mail.test_connection()
    if result["ok"]:
        print(f"连接成功! 收件箱: {result['inbox_count']} 封")
    else:
        print(f"连接失败: {result['error']}")
        sys.exit(1)
    if alias:
        msgs = mail.find_by_recipient(alias, limit=10)
        for m in msgs:
            print(f"  [{m['date'][:19]}] {m['from'][:30]} | {m['subject'][:40]}")
    else:
        msgs = mail.check_inbox(limit=10)
        for m in msgs:
            print(f"  [{m['date'][:19]}] {m['from'][:30]} | {m['subject'][:40]}")
    mail.disconnect()
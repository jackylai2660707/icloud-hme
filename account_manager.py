#!/usr/bin/env python3
"""
iCloud HME — 多账号管理器
===========================
管理多组 iCloud 账号及其隐私邮箱别名。

功能:
  - 账号 CRUD (增删改查)
  - Cookie 导入解析 (Header String / JSON)
  - 批量会话校验
  - 别名按账号归属索引
  - 跨账号并发/轮询创建

用法:
    from account_manager import AccountManager

    mgr = AccountManager()
    mgr.add_account("主号", cookie_header_string)
    mgr.create_aliases_batch(["acc_xxx", "acc_yyy"], count_per_account=5)
"""

import json
import time
import uuid
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

HERE = Path(__file__).resolve().parent
ACCOUNTS_FILE = HERE / "accounts.json"
OLD_COOKIES_FILE = HERE / "cookies.json"
RESULTS_DIR = HERE / "results"
LATEST_EMAILS = RESULTS_DIR / "latest_emails.txt"

from mail_cache import get_cache  # noqa: E402


class AccountManager:
    """多账号管理器"""

    def __init__(self):
        self.accounts: Dict[str, Dict] = {}
        self._lock = threading.RLock()
        self._cache = get_cache()
        self._mail_pool: Dict[str, Any] = {}   # acc_id -> ICloudMail (reusable)
        self._pool_lock = threading.Lock()
        self._load()

    def _load(self):
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        if OLD_COOKIES_FILE.exists() and not ACCOUNTS_FILE.exists():
            try:
                self._migrate_old_cookies()
            except Exception:
                pass

        if ACCOUNTS_FILE.exists():
            try:
                data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
                self.accounts = data.get("accounts", {})
            except (json.JSONDecodeError, OSError):
                self.accounts = {}

    def _save(self):
        with self._lock:
            ACCOUNTS_FILE.write_text(
                json.dumps({
                    "accounts": self.accounts,
                    "updated_at": datetime.now().isoformat(),
                }, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def _migrate_old_cookies(self):
        try:
            old = json.loads(OLD_COOKIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(old, dict) or not old:
            return

        acc_id = self._generate_id()
        self.accounts[acc_id] = {
            "id": acc_id,
            "name": "默认账号",
            "real_email": "",
            "cookies": old,
            "host": "icloud.com",
            "status": "active",
            "alias_total": 0,
            "alias_active": 0,
            "last_validated": None,
            "last_error": None,
            "created_at": datetime.now().isoformat(),
        }
        self._save()
        try:
            OLD_COOKIES_FILE.rename(OLD_COOKIES_FILE.with_suffix(".json.bak"))
        except OSError:
            pass

    def _generate_id(self) -> str:
        return "acc_" + uuid.uuid4().hex[:8]

    @staticmethod
    def parse_cookie_input(raw: str) -> Dict[str, str]:
        raw = raw.strip()
        if not raw:
            raise ValueError("空白输入 — 请粘贴 Cookie Header String 或 JSON")

        if raw.startswith("{"):
            try:
                cookies = json.loads(raw)
                if isinstance(cookies, dict):
                    return {k: str(v) for k, v in cookies.items() if v}
            except json.JSONDecodeError:
                pass

        cookies: Dict[str, str] = {}
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                name = name.strip()
                value = value.strip()
                if name:
                    cookies[name] = value

        if not cookies:
            raise ValueError(
                "无法解析 Cookie 输入。\n"
                "请提供 Header String 格式 (name=value; ...) 或 JSON 格式"
            )

        return cookies

    def add_account(
        self, name: str, cookie_input: str, host: str = "icloud.com"
    ) -> Dict:
        from icloud_hme import ICloudHME

        cookies = self.parse_cookie_input(cookie_input)
        acc_id = self._generate_id()

        account: Dict[str, Any] = {
            "id": acc_id,
            "name": name,
            "real_email": "",
            "icloud_email": "",
            "cookies": cookies,
            "host": host,
            "status": "active",
            "alias_total": 0,
            "alias_active": 0,
            "last_validated": None,
            "last_error": None,
            "created_at": datetime.now().isoformat(),
        }

        try:
            client = ICloudHME(cookies, host=host, verbose=False)
            client.validate_session()
            info = client.get_account_info()
            if info:
                account["real_email"] = (
                    info.get("appleId", "")
                    or info.get("primaryEmail", "")
                )
                account["icloud_email"] = self._derive_icloud_email(info)

            try:
                aliases = client.list_aliases()
                account["alias_total"] = len(aliases)
                account["alias_active"] = sum(
                    1 for a in aliases if a.get("active")
                )
            except Exception:
                pass

            account["last_validated"] = datetime.now().isoformat()
            account["last_error"] = None
        except Exception as e:
            account["status"] = "error"
            account["last_error"] = str(e)[:300]

        self.accounts[acc_id] = account
        self._save()
        return account

    def update_account_cookies(
        self, acc_id: str, cookie_input: str, name: Optional[str] = None, host: Optional[str] = None
    ) -> Dict:
        """重新导入指定账号 Cookie，并立即校验会话/同步别名数量。

        校验失败时仍保存新 Cookie，账号状态标记为 error，方便用户看到错误并再次更新。
        """
        from icloud_hme import ICloudHME

        cookies = self.parse_cookie_input(cookie_input)
        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")

        if name is not None and str(name).strip():
            account["name"] = str(name).strip()
        if host is not None and str(host).strip():
            account["host"] = str(host).strip()
        account["cookies"] = cookies
        account["status"] = "active"
        account["last_error"] = None

        try:
            client = ICloudHME(cookies, host=account.get("host", "icloud.com"), verbose=False)
            client.validate_session()
            info = client.get_account_info()
            if info:
                account["real_email"] = (
                    info.get("appleId", "")
                    or info.get("primaryEmail", "")
                )
                account["icloud_email"] = self._derive_icloud_email(info)
            try:
                aliases = client.list_aliases()
                account["alias_total"] = len(aliases)
                account["alias_active"] = sum(
                    1 for a in aliases if a.get("active")
                )
            except Exception:
                pass
            account["last_validated"] = datetime.now().isoformat()
            account["last_error"] = None
            account["status"] = "active"
        except Exception as e:
            account["status"] = "error"
            account["last_error"] = str(e)[:300]

        self._save()
        return account

    def remove_account(self, acc_id: str) -> bool:
        if acc_id in self.accounts:
            del self.accounts[acc_id]
            self._save()
            return True
        return False

    def get_account(self, acc_id: str) -> Optional[Dict]:
        return self.accounts.get(acc_id)

    def list_accounts(self) -> List[Dict]:
        return sorted(
            self.accounts.values(),
            key=lambda a: (a.get("status") != "active", a.get("created_at", "")),
        )

    def update_account(self, acc_id: str, **kwargs) -> Optional[Dict]:
        with self._lock:
            if acc_id in self.accounts:
                self.accounts[acc_id].update(kwargs)
                self._save()
                return dict(self.accounts[acc_id])
            return None

    @staticmethod
    def _derive_icloud_email(info: Dict) -> str:
        primary = str(info.get("primaryEmail", "") or "").strip()
        apple_id = str(info.get("appleId", "") or "").strip()

        if primary and ("@icloud.com" in primary or "@me.com" in primary or "@mac.com" in primary):
            return primary

        if apple_id and ("@icloud.com" in apple_id or "@me.com" in apple_id or "@mac.com" in apple_id):
            return apple_id

        if apple_id and "@" in apple_id:
            local = apple_id.split("@")[0]
            return f"{local}@icloud.com"

        return primary or apple_id

    def validate_account(self, acc_id: str) -> Dict:
        from icloud_hme import ICloudHME

        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")

        try:
            client = ICloudHME(
                account["cookies"],
                host=account.get("host", "icloud.com"),
                verbose=False,
            )
            client.validate_session()
            info = client.get_account_info()
            if info:
                account["real_email"] = (
                    info.get("appleId", "")
                    or info.get("primaryEmail", "")
                )
                existing = account.get("icloud_email", "")
                is_icloud = existing and any(
                    d in existing for d in ("@icloud.com", "@me.com", "@mac.com")
                )
                if not is_icloud:
                    account["icloud_email"] = self._derive_icloud_email(info)

            aliases = client.list_aliases()
            account["alias_total"] = len(aliases)
            account["alias_active"] = sum(
                1 for a in aliases if a.get("active")
            )
            account["status"] = "active"
            account["last_validated"] = datetime.now().isoformat()
            account["last_error"] = None
        except Exception as e:
            account["status"] = "error"
            account["last_error"] = str(e)[:300]

        self._save()
        return account

    def validate_all(self) -> List[Dict]:
        results: List[Dict] = []
        for acc_id in list(self.accounts.keys()):
            try:
                account = self.validate_account(acc_id)
                results.append({
                    "id": acc_id,
                    "ok": account.get("status") == "active",
                    "email": account.get("real_email", ""),
                    "alias_total": account.get("alias_total", 0),
                })
            except Exception as e:
                results.append({
                    "id": acc_id,
                    "ok": False,
                    "error": str(e)[:200],
                })
        return results

    def get_client(self, acc_id: str, verbose: bool = False):
        from icloud_hme import ICloudHME

        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")
        return ICloudHME(
            account["cookies"],
            host=account.get("host", "icloud.com"),
            verbose=verbose,
        )

    def get_forward_options(self) -> Dict[str, Any]:
        """聚合所有 Apple 账号里已绑定/允许的 HME 转发邮箱。

        返回结构:
          {
            "emails": ["a@example.com", ...],
            "selected": "apple 当前默认邮箱",
            "accounts": [
              {"account_id": "...", "emails": [...], "selected": "...", "ok": true}
            ]
          }
        """
        emails: List[str] = []
        seen = set()
        selected = ""
        per_accounts: List[Dict[str, Any]] = []

        for acc_id, account in self.accounts.items():
            if not account.get("cookies"):
                continue
            item: Dict[str, Any] = {
                "account_id": acc_id,
                "account_name": account.get("name", ""),
                "account_email": account.get("real_email", ""),
                "status": account.get("status", ""),
                "emails": [],
                "selected": "",
                "ok": False,
            }
            try:
                client = self.get_client(acc_id, verbose=False)
                data = client.get_forward_options()
                account_emails = [
                    str(e).strip().lower()
                    for e in data.get("emails", [])
                    if str(e).strip() and "@" in str(e)
                ]
                account_selected = str(data.get("selected") or "").strip().lower()
                item["emails"] = account_emails
                item["selected"] = account_selected
                item["ok"] = True

                if account_selected and not selected:
                    selected = account_selected
                for email in account_emails:
                    if email not in seen:
                        seen.add(email)
                        emails.append(email)
            except Exception as e:
                item["error"] = str(e)[:200]
            per_accounts.append(item)

        if selected and selected not in seen:
            emails.insert(0, selected)

        return {
            "emails": emails,
            "selected": selected,
            "accounts": per_accounts,
        }

    def update_forward_to(self, forward_to: str) -> Dict[str, Any]:
        """把所有可用 Apple 账号的 Hide My Email 转发地址切到指定邮箱。"""
        forward_to = str(forward_to or "").strip().lower()
        if not forward_to or "@" not in forward_to:
            raise ValueError("转发地址无效")

        results: List[Dict[str, Any]] = []
        ok_accounts = 0
        failed_accounts = 0

        for acc_id, account in self.accounts.items():
            if not account.get("cookies"):
                continue
            item: Dict[str, Any] = {
                "account_id": acc_id,
                "account_name": account.get("name", ""),
                "account_email": account.get("real_email", ""),
                "ok": False,
            }
            try:
                client = self.get_client(acc_id, verbose=False)
                options = client.get_forward_options()
                allowed = [
                    str(e).strip().lower()
                    for e in options.get("emails", [])
                    if str(e).strip()
                ]
                if allowed and forward_to not in allowed:
                    raise ValueError("该邮箱不在 Apple 账号可选转发地址中")

                before = str(options.get("selected") or "").strip().lower()
                client.update_forward_to(forward_to)
                after = str(client.get_forward_options().get("selected") or "").strip().lower()
                item.update({
                    "ok": after == forward_to,
                    "before": before,
                    "after": after,
                })
                if item["ok"]:
                    ok_accounts += 1
                    account["last_error"] = None
                else:
                    failed_accounts += 1
                    item["error"] = "Apple 返回成功，但读取后的默认转发地址未变化"
            except Exception as e:
                failed_accounts += 1
                item["error"] = str(e)[:200]
                account["last_error"] = str(e)[:300]
            results.append(item)

        if results:
            self._save()

        return {
            "ok": failed_accounts == 0 and ok_accounts > 0,
            "forward_to_email": forward_to,
            "ok_accounts": ok_accounts,
            "failed_accounts": failed_accounts,
            "accounts": results,
        }

    def set_app_password(self, acc_id: str, app_password: str):
        with self._lock:
            if acc_id not in self.accounts:
                raise KeyError(f"账号不存在: {acc_id}")
            self.accounts[acc_id]["app_password"] = app_password
            self._save()

    def get_mail_client(self, acc_id: str, verbose: bool = False):
        from icloud_mail import ICloudMail

        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")
        app_pwd = account.get("app_password", "")
        imap_email = account.get("icloud_email", "")
        if not imap_email:
            real = account.get("real_email", "")
            if real and any(d in real for d in ("@icloud.com", "@me.com", "@mac.com")):
                imap_email = real
            else:
                raise ValueError(
                    "未设置 iCloud 邮箱。\n"
                    "Apple ID ({}) 不是 iCloud 地址，\n"
                    "请点击下方按钮输入你的 @icloud.com 邮箱".format(
                        account.get("real_email", "?")
                    )
                )
        if not app_pwd:
            raise ValueError(
                "未设置 App 专用密码。\n"
                "请点击下方按钮，输入 @icloud.com 邮箱和应用密码"
            )

        # Try to reuse a pooled connection
        with self._pool_lock:
            cached = self._mail_pool.get(acc_id)
            if cached:
                try:
                    # NOOP to verify the connection is still alive
                    if cached._conn and cached._conn.noop()[0] == "OK":
                        return cached
                except Exception:
                    # Connection is dead, remove it
                    try:
                        cached.disconnect()
                    except Exception:
                        pass
                    del self._mail_pool[acc_id]

        # Create a fresh connection and pool it
        client = ICloudMail(imap_email, app_pwd, verbose=verbose)
        client.connect()
        with self._pool_lock:
            # Close any old stale entry before replacing
            old = self._mail_pool.get(acc_id)
            if old:
                try:
                    old.disconnect()
                except Exception:
                    pass
            self._mail_pool[acc_id] = client
        return client

    def check_inbox(self, acc_id: str, limit: int = 50, days: int = 7,
                    force: bool = False) -> List[Dict]:
        cached = self._cache.get_inbox(acc_id)
        age = self._cache.cache_age_seconds(acc_id)

        if not force and cached and age < 300:
            return cached[-limit:]

        try:
            mail = self.get_mail_client(acc_id)
            new_msgs = mail.check_inbox(limit=50, days=days)
        except Exception:
            new_msgs = []

        if new_msgs:
            self._cache.set_inbox(acc_id, new_msgs)

        return self._cache.get_inbox(acc_id)[-limit:]

    def check_alias_mail(self, acc_id: str, alias_email: str,
                         limit: int = 20, days: int = 30,
                         force: bool = False) -> List[Dict]:
        cached = self._cache.get_alias_mail(acc_id, alias_email)
        age = self._cache.cache_age_seconds(acc_id)

        if not force and cached and age < 300:
            return cached[-limit:]

        try:
            mail = self.get_mail_client(acc_id)
            new_msgs = mail.find_by_recipient(alias_email, limit=20, days=days)
        except Exception:
            new_msgs = []

        if new_msgs:
            self._cache.set_alias_mail(acc_id, alias_email, new_msgs)

        return self._cache.get_alias_mail(acc_id, alias_email)[-limit:]

    def check_all_aliases_mail(self, acc_id: str, limit_per: int = 5,
                               days: int = 14,
                               force: bool = False) -> Dict[str, List[Dict]]:
        cached = self._cache.get_all_alias_mail(acc_id)
        age = self._cache.cache_age_seconds(acc_id)

        if not force and cached and age < 300:
            results = {}
            for alias, msgs in cached.items():
                results[alias] = msgs[-limit_per:]
            return results

        from icloud_hme import ICloudHME

        try:
            client = self.get_client(acc_id, verbose=False)
            aliases = client.list_aliases()
        except Exception:
            return cached if cached else {}

        alias_set = {a.get("email", "").lower() for a in aliases if a.get("email")}
        if not alias_set:
            return cached if cached else {}

        try:
            mail = self.get_mail_client(acc_id)
            all_inbox = mail.check_inbox(limit=100, days=days)
        except Exception:
            return cached if cached else {}

        results: Dict[str, List[Dict]] = {}
        for msg in all_inbox:
            to_field = msg.get("to", "").lower()
            for alias in alias_set:
                if alias and alias in to_field:
                    if alias not in results:
                        results[alias] = []
                    if len(results[alias]) < limit_per:
                        results[alias].append(msg)
                    break

        if results:
            self._cache.set_alias_mail_batch(acc_id, results)

        return results

    def test_imap_connection(self, acc_id: str) -> Dict:
        try:
            mail = self.get_mail_client(acc_id)
            result = mail.test_connection()
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def create_aliases_for_account(
        self, acc_id: str, count: int = 1, label: str = "", forward_to: str = ""
    ) -> List[Dict]:
        from icloud_hme import ICloudHME

        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")

        client = ICloudHME(
            account["cookies"],
            host=account.get("host", "icloud.com"),
            verbose=False,
        )

        results: List[Dict] = []
        for i in range(count):
            try:
                alias_label = label or (
                    f"{account.get('name', acc_id)} "
                    f"{datetime.now().strftime('%m%d%H%M')}-{i + 1}"
                )
                result = client.create_alias(label=alias_label, max_retries=3, forward_to=forward_to or None)
                email = result.get("email", "")
                if email:
                    results.append({
                        "email": email,
                        "account_id": acc_id,
                        "ok": True,
                        "forward_to": forward_to or "",
                    })
                    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
                    with open(str(LATEST_EMAILS), "a", encoding="utf-8") as f:
                        f.write(f"{email}\t{acc_id}\n")
                    account["alias_total"] = account.get("alias_total", 0) + 1
                    account["alias_active"] = account.get("alias_active", 0) + 1
                else:
                    results.append({
                        "email": None,
                        "account_id": acc_id,
                        "ok": False,
                        "error": "create_alias 返回空邮箱",
                    })
            except Exception as e:
                err_str = str(e)
                lower = err_str.lower()
                if any(kw in lower for kw in ("http 401", "http 403", "http 421", "trusttokens", "cookie", "session")):
                    account["status"] = "error"
                    account["last_error"] = err_str[:300]
                results.append({
                    "email": None,
                    "account_id": acc_id,
                    "ok": False,
                    "error": err_str[:200],
                })
                if any(kw in lower for kw in (
                    "limit", "exceeded", "maximum", "quota", "429",
                    "too many", "rate", "throttle",
                )):
                    break

        self._save()
        return results

    def create_aliases_batch(
        self,
        account_ids: List[str],
        count_per_account: int = 1,
        interval_sec: float = 3.0,
        label: str = "",
        forward_to: str = "",
    ) -> Dict[str, List[Dict]]:
        all_results: Dict[str, List[Dict]] = {}
        for i, acc_id in enumerate(account_ids):
            if acc_id not in self.accounts:
                all_results[acc_id] = [{
                    "email": None, "account_id": acc_id,
                    "ok": False, "error": "账号不存在",
                }]
                continue
            if self.accounts[acc_id].get("status") != "active":
                all_results[acc_id] = [{
                    "email": None, "account_id": acc_id,
                    "ok": False, "error": "账号不可用",
                }]
                continue

            results = self.create_aliases_for_account(
                acc_id, count_per_account, label, forward_to=forward_to
            )
            all_results[acc_id] = results

            if i < len(account_ids) - 1 and interval_sec > 0:
                time.sleep(interval_sec)

        return all_results

    def get_aliases_for_account(self, acc_id: str) -> List[Dict]:
        try:
            client = self.get_client(acc_id, verbose=False)
            return client.list_aliases()
        except Exception:
            return []

    def get_all_aliases(self) -> List[Dict]:
        all_aliases: List[Dict] = []
        for acc_id, account in self.accounts.items():
            for alias in self.get_aliases_for_account(acc_id):
                alias["account_id"] = acc_id
                alias["account_name"] = account.get("name", "")
                alias["account_email"] = account.get("real_email", "")
                all_aliases.append(alias)
        return all_aliases

    def get_summary(self) -> Dict:
        total_aliases = sum(
            a.get("alias_total", 0) for a in self.accounts.values()
        )
        total_active = sum(
            a.get("alias_active", 0) for a in self.accounts.values()
        )
        active_accounts = sum(
            1 for a in self.accounts.values() if a.get("status") == "active"
        )
        error_accounts = sum(
            1 for a in self.accounts.values() if a.get("status") == "error"
        )
        return {
            "account_count": len(self.accounts),
            "active_accounts": active_accounts,
            "error_accounts": error_accounts,
            "total_aliases": total_aliases,
            "total_active_aliases": total_active,
        }


if __name__ == "__main__":
    print("AccountManager 自测")
    mgr = AccountManager()
    summary = mgr.get_summary()
    print(f"当前账号数: {summary['account_count']}")
    
    header = "X_APPLE_WEB_KB=abc123; SESSION_TOKEN=xyz789"
    parsed = mgr.parse_cookie_input(header)
    print(f"Header String → {len(parsed)} 个 cookie")
    
    json_in = '{"X_APPLE_WEB_KB":"abc123","SESSION_TOKEN":"xyz789"}'
    parsed2 = mgr.parse_cookie_input(json_in)
    print(f"JSON → {len(parsed2)} 个 cookie")
    
    print("自测完成 ✓")

#!/usr/bin/env python3
"""回归测试 — 覆盖核心流程，发现重构中的破坏性变更。"""

import sys
import json
import os
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def test_parse_cookie_header_string():
    """Cookie Header String 格式解析"""
    from account_manager import AccountManager
    mgr = AccountManager()
    
    raw = "X_APPLE_WEB_KB=abc123; SESSION_TOKEN=xyz789"
    cookies = mgr.parse_cookie_input(raw)
    assert len(cookies) == 2
    assert cookies["X_APPLE_WEB_KB"] == "abc123"
    assert cookies["SESSION_TOKEN"] == "xyz789"
    print("  PASS test_parse_cookie_header_string")


def test_parse_cookie_json():
    """JSON 格式 Cookie 解析"""
    from account_manager import AccountManager
    mgr = AccountManager()
    
    raw = '{"X_APPLE_WEB_KB":"abc123","SESSION_TOKEN":"xyz789"}'
    cookies = mgr.parse_cookie_input(raw)
    assert len(cookies) == 2
    assert cookies["X_APPLE_WEB_KB"] == "abc123"
    print("  PASS test_parse_cookie_json")


def test_parse_empty_input():
    """空输入应抛出 ValueError"""
    from account_manager import AccountManager
    mgr = AccountManager()
    
    try:
        mgr.parse_cookie_input("")
        assert False, "应该抛出 ValueError"
    except ValueError:
        pass
    print("  PASS test_parse_empty_input")


def test_derive_icloud_email_primary():
    """dsInfo 有 primaryEmail 时直接使用"""
    from account_manager import AccountManager
    info = {"appleId": "user@qq.com", "primaryEmail": "user@icloud.com"}
    result = AccountManager._derive_icloud_email(info)
    assert result == "user@icloud.com"
    print("  PASS test_derive_icloud_email_primary")


def test_derive_icloud_email_appleid_is_icloud():
    """appleId 本身是 @icloud.com"""
    from account_manager import AccountManager
    info = {"appleId": "user@icloud.com", "primaryEmail": ""}
    result = AccountManager._derive_icloud_email(info)
    assert result == "user@icloud.com"
    print("  PASS test_derive_icloud_email_appleid_is_icloud")


def test_derive_icloud_email_third_party():
    """appleId 是第三方邮箱时推导"""
    from account_manager import AccountManager
    info = {"appleId": "test@gmail.com", "primaryEmail": ""}
    result = AccountManager._derive_icloud_email(info)
    assert result == "test@icloud.com"
    print("  PASS test_derive_icloud_email_third_party")


def test_mail_cache_basic():
    """邮件缓存基本读写"""
    from mail_cache import MailCache
    cache = MailCache()
    
    emails = [
        {"id": "1", "from": "a@b.com", "to": "x@icloud.com", "subject": "Hello", "date": "2025-01-01T00:00:00"},
        {"id": "2", "from": "c@d.com", "to": "y@icloud.com", "subject": "World", "date": "2025-01-02T00:00:00"},
        {"id": "1", "from": "a@b.com", "to": "x@icloud.com", "subject": "Hello Duplicate", "date": "2025-01-03T00:00:00"},
    ]
    
    cache.set_inbox("test_acc", emails)
    cached = cache.get_inbox("test_acc")
    
    # 应该有 2 封（第 3 封 id 重复被去重）
    assert len(cached) == 2, f"期望 2 封，实际 {len(cached)}"
    
    # 清理
    cache.clear_account("test_acc")
    assert len(cache.get_inbox("test_acc")) == 0
    print("  PASS test_mail_cache_basic")


def test_strip_html():
    """HTML 标签剥离"""
    from icloud_mail import _strip_html
    
    html = "<html><body><p>Hello</p><br><div>World</div></body></html>"
    text = _strip_html(html)
    assert "Hello" in text
    assert "World" in text
    assert "<p>" not in text
    assert "<html>" not in text
    print("  PASS test_strip_html")


def test_strip_html_with_link():
    """HTML 链接保留文字"""
    from icloud_mail import _strip_html
    
    html = '<a href="https://example.com">Click here</a>'
    text = _strip_html(html)
    assert "Click here" in text
    assert "example.com" in text
    print("  PASS test_strip_html_with_link")


def test_icloud_hme_account_info():
    """ICloudHME 客户端有 get_account_info 方法"""
    from icloud_hme import ICloudHME
    client = ICloudHME({}, verbose=False)
    assert hasattr(client, "get_account_info")
    # 未校验前应返回 None
    assert client.get_account_info() is None
    print("  PASS test_icloud_hme_account_info")


def test_cf_credential_normalize_and_id_fallback():
    """地址 JWT 支持完整 URL/换行/Bearer，且旧 address_id 失效时按地址兜底。"""
    from cf_compat import CfCompatStore, normalize_jwt_token

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        store = CfCompatStore(base / "mail.db", base / "cf_config.json", base / ".deploy-secrets")
        row = store.ensure_address("old-id-test@icloud.com", source="test")
        token = store.address_token(address_id=row["id"])

        noisy = f'Bearer "https://icloud.example/?credential={token[:20]}\\n{token[20:]}"'
        assert normalize_jwt_token(noisy) == token
        assert store.verify_address_token(noisy)["address"] == "old-id-test@icloud.com"

        # 模拟本机地址表被删除/重建后 id 改变；签名有效且地址仍存在时应继续可用。
        store.delete_address_record(row["id"], delete_mails=False)
        recreated = store.ensure_address("old-id-test@icloud.com", source="test")
        assert recreated["id"] != row["id"]
        payload = store.verify_address_token(token)
        assert payload["address"] == "old-id-test@icloud.com"
        assert payload["address_id"] == recreated["id"]
    print("  PASS test_cf_credential_normalize_and_id_fallback")


def test_inbound_detect_plus_recipient_without_known_alias():
    """即使 known_aliases 里没有 +3，也应从 Received for 恢复真实派生收件人。"""
    from inbound_mail import InboundMailStore

    raw = (
        "Received: from sender.example by mx.example with ESMTP id abc "
        "for <demo-alias+3@icloud.com>; Fri, 03 Jul 2026 00:00:00 +0000\r\n"
        "From: Sender <sender@example.com>\r\n"
        "To: Hide My Email <demo-alias@icloud.com>\r\n"
        "Subject: plus test\r\n"
        "\r\n"
        "hello"
    )
    parsed = InboundMailStore.parse_raw(raw)
    detected = InboundMailStore.detect_alias({"raw": raw, "to": "inbox@mail.example.com"}, parsed, known_aliases=[])
    assert detected["hme_alias"] == "demo-alias+3@icloud.com"
    assert detected["base_alias"] == "demo-alias@icloud.com"
    print("  PASS test_inbound_detect_plus_recipient_without_known_alias")


def test_inbound_family_share_does_not_miss_base_mail():
    """分享/查看 +tag 邮箱时，应同时看到同 base family 的 base 邮件。"""
    from inbound_mail import InboundMailStore

    with tempfile.TemporaryDirectory() as td:
        store = InboundMailStore(Path(td) / "inbound.db")
        raw_base = (
            "From: Sender <sender@example.com>\r\n"
            "To: Hide My Email <family-test@icloud.com>\r\n"
            "Subject: base mail\r\n\r\nbase"
        )
        raw_plus = (
            "Received: from sender.example by mx.example with ESMTP id abc "
            "for <family-test+3@icloud.com>; Fri, 03 Jul 2026 00:00:00 +0000\r\n"
            "From: Sender <sender@example.com>\r\n"
            "To: Hide My Email <family-test@icloud.com>\r\n"
            "Subject: plus mail\r\n\r\nplus"
        )
        base = store.ingest({"raw": raw_base, "to": "inbox@mail.example.com"}, known_aliases=[])
        plus = store.ingest({"raw": raw_plus, "to": "inbox@mail.example.com"}, known_aliases=[])
        assert base["hme_alias"] == "family-test@icloud.com"
        assert plus["hme_alias"] == "family-test+3@icloud.com"
        rows = store.list_messages(alias="family-test+3@icloud.com", limit=10)["messages"]
        aliases = {r["hme_alias"] for r in rows}
        assert "family-test@icloud.com" in aliases
        assert "family-test+3@icloud.com" in aliases
        assert store.get_message(base["id"], alias="family-test+3@icloud.com") is not None
    print("  PASS test_inbound_family_share_does_not_miss_base_mail")


if __name__ == "__main__":
    tests = [
        ("parse_cookie_header_string", test_parse_cookie_header_string),
        ("parse_cookie_json", test_parse_cookie_json),
        ("parse_empty_input", test_parse_empty_input),
        ("derive_icloud_email_primary", test_derive_icloud_email_primary),
        ("derive_icloud_email_appleid_is_icloud", test_derive_icloud_email_appleid_is_icloud),
        ("derive_icloud_email_third_party", test_derive_icloud_email_third_party),
        ("mail_cache_basic", test_mail_cache_basic),
        ("strip_html", test_strip_html),
        ("strip_html_with_link", test_strip_html_with_link),
        ("icloud_hme_account_info", test_icloud_hme_account_info),
        ("cf_credential_normalize_and_id_fallback", test_cf_credential_normalize_and_id_fallback),
        ("inbound_detect_plus_recipient_without_known_alias", test_inbound_detect_plus_recipient_without_known_alias),
        ("inbound_family_share_does_not_miss_base_mail", test_inbound_family_share_does_not_miss_base_mail),
    ]
    
    passed = 0
    failed = 0
    
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL {name}: {e}")
            failed += 1
    
    print(f"\n{'='*40}")
    print(f"结果: {passed} 通过, {failed} 失败")
    
    if failed:
        sys.exit(1)

#!/usr/bin/env python3
"""Analyze iCloud HME local mail through the Admin API.

The script is intentionally read-only. It never exports raw MIME or message bodies
into the report; bodies are fetched transiently only for ChatGPT status heuristics.
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://icloud.armsg.yueseng-ys.com"
CHATGPT_RE = re.compile(r"(?i)\b(chatgpt|openai|codex)\b")
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")
UUID_RE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f-]{16,}\b")
LONG_CODE_RE = re.compile(r"\b(?:\d{4,}|(?=[A-Z0-9]{8,}\b)(?=[A-Z0-9]*\d)[A-Z0-9]{8,})\b", re.I)
SPACE_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")

DEACTIVATED_PATTERNS = [
    r"account\s+(?:has been\s+)?deactivated",
    r"account\s+(?:has been\s+)?disabled",
    r"account\s+suspended",
    r"suspension",
    r"账号已停用",
    r"账户已停用",
    r"账号被停用",
    r"无法访问(?:你的|您的)?账户",
]
PLUS_PATTERNS = [
    r"chatgpt\s*plus",
    r"plus\s+subscription",
    r"subscription\s+(?:is\s+)?active",
    r"manage\s+your\s+subscription",
    r"upgrade\s+(?:to|your)\s+plus",
    r"升级至\s*plus",
    r"plus\s*订阅",
    r"订阅已生效",
]
FREE_SIGNAL_PATTERNS = [
    r"temporary\s+(?:login|verification)\s+code",
    r"verification\s+code",
    r"login\s+code",
    r"临时(?:登录|验证码|认证)代码",
    r"验证码",
    r"welcome\s+to\s+chatgpt",
    r"chatgpt\s+登录",
]


def normalize_base(value: str) -> str:
    return str(value or DEFAULT_BASE_URL).strip().rstrip("/")


def base_alias(address: str) -> str:
    address = str(address or "").strip().lower()
    if "@" not in address:
        return address
    local, domain = address.rsplit("@", 1)
    return f"{local.split('+', 1)[0]}@{domain}"


def parse_time(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def strip_html(value: str) -> str:
    text = TAG_RE.sub(" ", str(value or ""))
    return SPACE_RE.sub(" ", html_lib.unescape(text)).strip()


def request_json(base: str, token: str, path: str) -> Dict[str, Any]:
    req = Request(
        normalize_base(base) + path,
        headers={
            "Accept": "application/json",
            "x-admin-auth": token,
            "User-Agent": "icloud-hme-admin-skill/1.0",
        },
    )
    try:
        with urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} {path}: {body}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"request failed {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected JSON shape from {path}")
    return payload


def list_all_messages(base: str, token: str, page_size: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = request_json(base, token, f"/api/local-inbox/messages?limit={page_size}&offset={offset}")
        current = page.get("messages") or []
        if not isinstance(current, list):
            raise RuntimeError("/api/local-inbox/messages returned invalid messages")
        rows.extend(item for item in current if isinstance(item, dict))
        total = int(page.get("count") or len(rows))
        if not current or len(rows) >= total or len(current) < page_size:
            break
        offset += len(current)
    return rows


def list_known_addresses(base: str, token: str) -> List[str]:
    addresses: List[str] = []
    try:
        data = request_json(base, token, "/api/emails")
        for row in data.get("emails") or []:
            if isinstance(row, dict) and row.get("email"):
                addresses.append(str(row["email"]).strip().lower())
    except RuntimeError:
        # The report can still be produced from message rows.
        pass
    return sorted(set(addresses))


def normalized_subject(subject: str) -> str:
    value = SPACE_RE.sub(" ", str(subject or "").strip().lower())
    value = UUID_RE.sub("<id>", value)
    value = LONG_CODE_RE.sub("<code>", value)
    value = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "<time>", value)
    return value or "(no subject)"


def sender_domain(value: str) -> str:
    matches = EMAIL_RE.findall(str(value or ""))
    if not matches:
        return "unknown"
    return matches[-1].rsplit("@", 1)[-1].lower()


def coarse_category(subject: str, sender: str) -> str:
    text = f"{subject} {sender}".lower()
    if CHATGPT_RE.search(text):
        if re.search(r"(?i)code|验证码|登录|login|verify|认证", text):
            return "ChatGPT 登录/验证码"
        if re.search(r"(?i)plus|subscription|订阅|billing|账单", text):
            return "ChatGPT 订阅/账单"
        if re.search(r"(?i)deactiv|suspend|停用|封禁", text):
            return "ChatGPT 账号状态"
        return "ChatGPT/OpenAI"
    if re.search(r"(?i)code|验证码|verification|login|登录|one[- ]time|otp", text):
        return "登录/验证码"
    if re.search(r"(?i)subscription|billing|invoice|receipt|订阅|账单|付款|支付", text):
        return "订阅/账单"
    if re.search(r"(?i)security|alert|password|安全|密码|警告", text):
        return "安全通知"
    if re.search(r"(?i)invite|invitation|邀请", text):
        return "邀请/协作"
    if re.search(r"(?i)newsletter|digest|marketing|优惠|促销", text):
        return "通知/营销"
    return "其它"


def detect_status(text: str) -> Optional[str]:
    value = str(text or "")
    for pattern in DEACTIVATED_PATTERNS:
        if re.search(pattern, value, re.I):
            return "deactivated"
    for pattern in PLUS_PATTERNS:
        if re.search(pattern, value, re.I):
            return "plus"
    if CHATGPT_RE.search(value) and any(re.search(pattern, value, re.I) for pattern in FREE_SIGNAL_PATTERNS):
        return "free"
    return None


def confidence(status: str, text: str) -> str:
    if status in {"deactivated", "plus"}:
        return "high"
    if status == "free":
        return "medium"
    return "low"


def analyze(base: str, token: str, page_size: int, max_details: int) -> Dict[str, Any]:
    messages = list_all_messages(base, token, page_size)
    known = list_known_addresses(base, token)
    addresses = set(known)
    for row in messages:
        address = str(row.get("hme_alias") or "").strip().lower()
        if address:
            addresses.add(address)

    categories: Dict[str, Dict[str, Any]] = {}
    chat_candidates = []
    for row in messages:
        subject = str(row.get("subject") or "(no subject)")
        sender = str(row.get("from") or row.get("sender_name") or "")
        key = f"{sender_domain(sender)} | {normalized_subject(subject)}"
        item = categories.setdefault(key, {"category": coarse_category(subject, sender), "subject": normalized_subject(subject), "sender_domain": sender_domain(sender), "count": 0, "mailbox_families": set(), "sample_ids": []})
        item["count"] += 1
        family = str(row.get("base_alias") or base_alias(row.get("hme_alias") or ""))
        if family:
            item["mailbox_families"].add(family)
        if len(item["sample_ids"]) < 5 and row.get("id") is not None:
            item["sample_ids"].append(int(row["id"]))
        if CHATGPT_RE.search(f"{subject} {sender}"):
            chat_candidates.append(row)

    evidence_by_family: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    details_limit = max(0, int(max_details))
    detail_count = 0
    detail_errors: List[str] = []
    for row in sorted(chat_candidates, key=lambda x: parse_time(x.get("created_at")), reverse=True):
        if detail_count >= details_limit:
            break
        try:
            detail_response = request_json(base, token, f"/api/local-inbox/messages/{int(row['id'])}")
        except RuntimeError as exc:
            detail_errors.append(f"id={row.get('id')}: {str(exc)[:160]}")
            detail_count += 1
            continue
        detail = detail_response.get("message") if isinstance(detail_response.get("message"), dict) else detail_response
        body = " ".join([
            str(row.get("subject") or ""),
            str(row.get("from") or ""),
            str(detail.get("text") or ""),
            strip_html(str(detail.get("html") or "")),
        ])
        status = detect_status(body)
        if status:
            family = str(row.get("base_alias") or base_alias(row.get("hme_alias") or ""))
            evidence_by_family[family].append({
                "status": status,
                "id": int(row["id"]),
                "created_at": row.get("created_at") or "",
                "alias": row.get("hme_alias") or "",
                "confidence": confidence(status, body),
            })
        detail_count += 1

    family_aliases: Dict[str, set[str]] = defaultdict(set)
    for address in addresses:
        family_aliases[base_alias(address)].add(address)
    for row in messages:
        alias = str(row.get("hme_alias") or "").strip().lower()
        family = str(row.get("base_alias") or base_alias(alias))
        if alias:
            family_aliases[family].add(alias)

    statuses = []
    for family, aliases in sorted(family_aliases.items()):
        evidence = sorted(evidence_by_family.get(family, []), key=lambda x: parse_time(x.get("created_at")), reverse=True)
        latest = evidence[0] if evidence else None
        status = latest["status"] if latest else "unknown"
        note = "family-level evidence; +tag may have been removed upstream" if any("+" in a.split("@", 1)[0] for a in aliases) else ""
        for mailbox in sorted(aliases):
            statuses.append({
                "mailbox": mailbox,
                "family": family,
                "status_scope": "family",
                "status": status,
                "confidence": latest.get("confidence", "low") if latest else "low",
                "evidence_ids": [e["id"] for e in evidence[:10]],
                "latest_evidence_at": latest.get("created_at", "") if latest else "",
                "note": note,
            })

    category_rows = []
    for item in sorted(categories.values(), key=lambda x: (-x["count"], x["subject"])):
        item = dict(item)
        item["mailbox_families"] = sorted(item["mailbox_families"])
        category_rows.append(item)

    status_counts = Counter(item["status"] for item in statuses)
    warnings = [
        "ChatGPT status is heuristic evidence from mail content, not an authoritative subscription API.",
        "+tag aliases use family scope because upstream forwarding may remove the plus tag.",
    ]
    if detail_errors:
        warnings.append(f"{len(detail_errors)} message detail requests failed; affected status evidence may be incomplete.")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "messages_total": len(messages),
        "chatgpt_candidate_messages": len(chat_candidates),
        "chatgpt_details_analyzed": detail_count,
        "categories": category_rows,
        "chatgpt_status": statuses,
        "status_counts": dict(status_counts),
        "warnings": warnings,
    }


def markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# iCloud HME 邮件分析",
        "",
        f"- 生成时间: `{report['generated_at']}`",
        f"- 邮件总数: `{report['messages_total']}`",
        f"- ChatGPT 相关候选邮件: `{report['chatgpt_candidate_messages']}`",
        f"- 已读取正文的候选邮件: `{report['chatgpt_details_analyzed']}`",
        f"- 状态统计: `{report.get('status_counts', {})}`",
        "",
        "## 高频邮件类别",
        "",
        "| 类别 | 归一化主题 | 发件域 | 数量 | 示例 ID |",
        "|---|---|---:|---:|---|",
    ]
    for item in report["categories"][:100]:
        lines.append(f"| {item['category']} | {item['subject']} | {item['sender_domain']} | {item['count']} | {', '.join(map(str, item['sample_ids']))} |")
    lines.extend(["", "## ChatGPT 邮箱状态", "", "| 邮箱 | Family | 状态 | 范围 | 置信度 | 证据 ID |", "|---|---|---|---|---|---|"])
    for item in report["chatgpt_status"]:
        lines.append(f"| {item['mailbox']} | {item['family']} | **{item['status']}** | {item['status_scope']} | {item['confidence']} | {', '.join(map(str, item['evidence_ids'])) or '-'} |")
    lines.extend(["", "## 注意", ""] + [f"- {warning}" for warning in report["warnings"]])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze iCloud HME local inbox via Admin API")
    parser.add_argument("--base-url", default=os.environ.get("ICLOUD_HME_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--admin-token", default=os.environ.get("ICLOUD_HME_ADMIN_TOKEN", ""))
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--max-details", type=int, default=1000)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.admin_token:
        parser.error("set ICLOUD_HME_ADMIN_TOKEN or pass --admin-token; it is never stored by this script")
    try:
        report = analyze(normalize_base(args.base_url), args.admin_token, max(1, min(args.page_size, 200)), max(0, args.max_details))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    output = markdown(report) if args.format == "markdown" else json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(str(args.output))
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

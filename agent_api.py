"""Machine-readable API catalog for administrator automation agents."""

from copy import deepcopy
import re


AGENT_API_VERSION = "2026-07-28"


_ENDPOINTS = [
    {"id": "agent_bootstrap", "method": "GET", "path": "/api/agent/bootstrap", "purpose": "Discover capabilities, workflows, safety rules and live sanitized state", "risk": "read"},
    {"id": "agent_openapi", "method": "GET", "path": "/api/agent/openapi.json", "purpose": "Read the OpenAPI 3.1 description", "risk": "read"},
    {"id": "agent_account_pool", "method": "GET", "path": "/api/agent/account-pool", "purpose": "Joined HME/OpenAI account pool with mother account, mail counts, status and credential availability", "risk": "read", "query": ["status", "account_id", "query", "has_mail", "sort", "order", "limit", "offset", "refresh_status", "sync"]},
    {"id": "agent_account_pool_csv", "method": "GET", "path": "/api/agent/account-pool.csv", "purpose": "Export the filtered HME/OpenAI account pool without credentials", "risk": "sensitive_read", "query": ["status", "account_id", "query", "has_mail", "sort", "order", "refresh_status", "sync"]},
    {"id": "agent_account_pool_credentials", "method": "POST", "path": "/api/agent/account-pool/credentials", "purpose": "Generate login URLs for exact known mailbox addresses, preserving +tag identities", "risk": "secret_response", "confirmation_required": True, "body": {"addresses": ["base@icloud.com", "base+1@icloud.com"], "include_jwt": False}},
    {"id": "agent_messages", "method": "GET", "path": "/api/agent/messages", "purpose": "Search normalized mail metadata by mailbox, status, category, account or text", "risk": "sensitive_read", "query": ["mailbox", "account_id", "status", "category", "query", "since", "limit", "offset", "include_status", "refresh_status"]},
    {"id": "agent_message", "method": "GET", "path": "/api/agent/messages/{mail_id}", "purpose": "Read one parsed mail; raw MIME, headers and account-status analysis are opt-in", "risk": "sensitive_read", "query": ["include_raw", "include_headers", "include_status", "refresh_status"]},
    {"id": "state", "method": "GET", "path": "/api/state", "purpose": "Read service, scheduler, HME-limit and inbox state", "risk": "read"},
    {"id": "accounts", "method": "GET", "path": "/api/accounts", "purpose": "List sanitized Apple mother-account records", "risk": "read"},
    {"id": "account_add", "method": "POST", "path": "/api/accounts/add", "purpose": "Import an Apple account from a user-supplied Cookie", "risk": "high", "confirmation_required": True, "body": {"name": "account label", "cookie_input": "<USER_SUPPLIED_COOKIE>"}},
    {"id": "account_cookie", "method": "POST", "path": "/api/accounts/{account_id}/cookies", "purpose": "Replace one account Cookie and validate it", "risk": "high", "confirmation_required": True, "body": {"cookie_input": "<USER_SUPPLIED_COOKIE>", "name": "optional label"}},
    {"id": "account_validate", "method": "POST", "path": "/api/accounts/{account_id}/validate", "purpose": "Validate one Apple session and refresh HME counts", "risk": "network"},
    {"id": "account_remove", "method": "POST", "path": "/api/accounts/{account_id}/remove", "purpose": "Remove the locally stored mother account; does not delete Apple HME", "risk": "high", "confirmation_required": True},
    {"id": "forward_options", "method": "GET", "path": "/api/forward-options", "purpose": "Read forwarding options for every mother account", "risk": "network"},
    {"id": "account_forward_options", "method": "GET", "path": "/api/accounts/{account_id}/forward-options", "purpose": "Read one account's Apple-allowed forwarding addresses", "risk": "network"},
    {"id": "account_forward_set", "method": "POST", "path": "/api/accounts/{account_id}/forward", "purpose": "Set forwarding for only one mother account", "risk": "high", "confirmation_required": True, "body": {"forward_to_email": "hme1@jackylai.eu.org"}},
    {"id": "emails", "method": "GET", "path": "/api/emails", "purpose": "Fast local HME list including the single +1 variant", "risk": "read", "query": ["limit", "cloud_cache"]},
    {"id": "aliases_sync", "method": "GET", "path": "/api/aliases", "purpose": "Synchronize real HME aliases from Apple; may be slow", "risk": "network"},
    {"id": "hme_create", "method": "POST", "path": "/api/accounts/{account_id}/create", "purpose": "Create HME on one account; blocked locally at 750", "risk": "high", "confirmation_required": True, "body": {"count": 1, "label": "optional"}},
    {"id": "hme_create_batch", "method": "POST", "path": "/api/create-batch", "purpose": "Create HME on selected accounts using per-account forwarding", "risk": "high", "confirmation_required": True, "body": {"account_ids": ["acc_id"], "count_per_account": 1, "label": "optional"}},
    {"id": "hme_delete", "method": "POST", "path": "/api/accounts/{account_id}/alias-delete", "purpose": "Delete one real Apple HME; local +tag variants cannot be deleted", "risk": "destructive", "confirmation_required": True, "body": {"email": "base@icloud.com", "delete_local_mails": False}},
    {"id": "settings_get", "method": "GET", "path": "/api/settings", "purpose": "Read global alias-derivation settings", "risk": "read"},
    {"id": "settings_set", "method": "POST", "path": "/api/settings", "purpose": "Set alias_split_enabled; forwarding is not global", "risk": "write", "body": {"alias_split_enabled": True, "alias_split_count": 1}},
    {"id": "scheduler_get", "method": "GET", "path": "/api/scheduler/config", "purpose": "Read scheduler configuration", "risk": "read"},
    {"id": "scheduler_set", "method": "POST", "path": "/api/scheduler/config", "purpose": "Save scheduler configuration", "risk": "write", "body": {"mode": "interval", "interval_minutes": 60, "count_per_run": 1}},
    {"id": "scheduler_start", "method": "POST", "path": "/api/scheduler/start", "purpose": "Start scheduled HME creation", "risk": "high", "confirmation_required": True},
    {"id": "scheduler_stop", "method": "POST", "path": "/api/scheduler/stop", "purpose": "Stop scheduled HME creation", "risk": "high", "confirmation_required": True},
    {"id": "inbox_summary", "method": "GET", "path": "/api/local-inbox/summary", "purpose": "List mailbox families and message counts", "risk": "read"},
    {"id": "inbox_messages", "method": "GET", "path": "/api/local-inbox/messages", "purpose": "List all or one family of inbound messages", "risk": "sensitive_read", "query": ["alias", "limit", "offset"]},
    {"id": "inbox_message", "method": "GET", "path": "/api/local-inbox/messages/{mail_id}", "purpose": "Read one full inbound message", "risk": "sensitive_read"},
    {"id": "mail_analysis", "method": "GET", "path": "/api/mail-analysis", "purpose": "Read categories and OpenAI status by address/family", "risk": "read", "query": ["refresh"]},
    {"id": "address_list", "method": "GET", "path": "/admin/address", "purpose": "List credential-enabled mailbox addresses", "risk": "read", "query": ["limit", "offset", "query", "sync"]},
    {"id": "address_credential", "method": "GET", "path": "/admin/address_credential", "purpose": "Generate Address JWT and user login URL", "risk": "secret_response", "query": ["address", "account_id", "label"]},
    {"id": "credential_export", "method": "GET", "path": "/admin/export_credentials.csv", "purpose": "Export all mailbox credentials", "risk": "secret_response", "confirmation_required": True},
    {"id": "address_delete_local", "method": "DELETE", "path": "/admin/delete_address/{address_id}", "purpose": "Delete local credential record without deleting Apple HME", "risk": "destructive", "confirmation_required": True},
    {"id": "mail_delete", "method": "DELETE", "path": "/admin/mails/{mail_id}", "purpose": "Delete one locally stored mail", "risk": "destructive", "confirmation_required": True},
    {"id": "users_list", "method": "GET", "path": "/admin/users", "purpose": "List local users and bindings", "risk": "sensitive_read"},
    {"id": "users_create", "method": "POST", "path": "/admin/users", "purpose": "Create a local user", "risk": "high", "confirmation_required": True, "body": {"email": "user@example.com", "password": "<USER_SUPPLIED_PASSWORD>", "role": "user"}},
    {"id": "users_delete", "method": "DELETE", "path": "/admin/users/{user_id}", "purpose": "Delete a local user", "risk": "destructive", "confirmation_required": True},
    {"id": "inbound_config", "method": "GET", "path": "/api/inbound-config", "purpose": "Read Cloudflare inbound configuration", "risk": "secret_response"},
    {"id": "inbound_config_set", "method": "POST", "path": "/api/inbound-config", "purpose": "Change inbound URL or rotate inbound token", "risk": "high", "confirmation_required": True, "body": {"regenerate_token": False, "public_base_url": "https://example"}},
    {"id": "logs", "method": "GET", "path": "/api/logs", "purpose": "Read recent runtime logs", "risk": "sensitive_read"},
    {"id": "skill_download", "method": "GET", "path": "/admin/download_skill", "purpose": "Download the complete administrator Skill ZIP", "risk": "read"},
]


def agent_api_catalog():
    return deepcopy(_ENDPOINTS)


def parse_agent_bool(value, default: bool = False) -> bool:
    """Parse JSON/query booleans without treating the string ``false`` as true."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def parse_agent_pagination(raw_limit, raw_offset, default_limit: int, max_limit: int) -> tuple[int, int]:
    """Validate Agent API pagination without depending on a web framework."""
    try:
        limit = default_limit if raw_limit in (None, "") else int(raw_limit)
        offset = 0 if raw_offset in (None, "") else int(raw_offset)
    except (TypeError, ValueError):
        raise ValueError("limit and offset must be integers")
    if limit < 1 or limit > max_limit:
        raise ValueError(f"limit must be between 1 and {max_limit}")
    if offset < 0:
        raise ValueError("offset must be greater than or equal to 0")
    return limit, offset


def account_pool_mailbox_index(rows: list) -> dict:
    """Index every exact mailbox, preserving +tag addresses as distinct logins."""
    return {
        str(address).strip().lower(): row
        for row in rows or []
        for address in row.get("mailboxes") or []
        if str(address).strip()
    }


def build_agent_openapi(base_url: str) -> dict:
    paths = {}
    for endpoint in _ENDPOINTS:
        operation = {
            "operationId": endpoint["id"],
            "summary": endpoint["purpose"],
            "security": [{"AdminAuth": []}],
            "x-risk": endpoint["risk"],
            "x-confirmation-required": bool(endpoint.get("confirmation_required")),
            "responses": {"200": {"description": "Successful response"}, "401": {"description": "Admin authentication required"}},
        }
        parameters = [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", endpoint["path"])
        ]
        if endpoint.get("query"):
            parameters.extend({"name": name, "in": "query", "required": False, "schema": {"type": "string"}} for name in endpoint["query"])
        if parameters:
            operation["parameters"] = parameters
        if endpoint.get("body") is not None:
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}, "example": endpoint["body"]}},
            }
        paths.setdefault(endpoint["path"], {})[endpoint["method"].lower()] = operation
    return {
        "openapi": "3.1.0",
        "info": {"title": "iCloud HME Administrator Agent API", "version": AGENT_API_VERSION},
        "servers": [{"url": str(base_url).rstrip("/")}],
        "components": {"securitySchemes": {"AdminAuth": {"type": "apiKey", "in": "header", "name": "x-admin-auth"}}},
        "paths": paths,
    }

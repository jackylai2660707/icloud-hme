"""Local HME plus-address derivation rules.

Apple creates the base HME address.  The application exposes exactly one
local plus variant for it (``base+1``); the count is deliberately not
configurable so newly imported accounts cannot reintroduce ``+2``...``+N``.
"""

ALIAS_SPLIT_COUNT = 1


def email_plus_variant(email: str, index: int = ALIAS_SPLIT_COUNT) -> str:
    local, domain = str(email).rsplit("@", 1)
    return f"{local}+{index}@{domain}"


def expand_email_records(records: list, settings: dict | None = None) -> list:
    """Return base records plus their single ``+1`` local variant.

    ``alias_split_count`` is accepted only for backward API/config
    compatibility and intentionally ignored.  Existing historical ``+2`` to
    ``+4`` mail remains in storage and is still available through family
    queries; this function only controls generated address-list records.
    """
    settings = settings or {}
    if not settings.get("alias_split_enabled"):
        return records

    expanded = []
    for rec in records:
        expanded.append(rec)
        email = rec.get("email", "")
        if "@" not in email:
            continue
        variant = dict(rec)
        variant["email"] = email_plus_variant(email)
        variant["base_email"] = email
        variant["variant_index"] = ALIAS_SPLIT_COUNT
        variant["derived"] = True
        expanded.append(variant)
    return expanded

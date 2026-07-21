# Mail Analysis Semantics

## Category normalization

Normalize subjects only for grouping:

1. Decode MIME headers.
2. Lowercase and collapse whitespace.
3. Remove variable verification codes, UUID-like IDs, long numeric IDs, and timestamps.
4. Keep language and meaningful product words.
5. Group by normalized subject plus coarse sender domain.

Never use the normalized subject as proof of account ownership.

## ChatGPT status evidence

Use evidence from subject, sender, body text, and HTML stripped to text. Match case-insensitively and record message IDs.

### `deactivated`

Strong indicators include:

- `deactivated`, `account deactivated`, `account disabled`, `suspended`, `suspension`;
- `账号已停用`, `账户已停用`, `账号被停用`, `无法访问账户`.

### `plus`

Indicators include:

- `chatgpt plus`, `plus subscription`, `subscription active`, `manage your subscription`;
- `升级至 plus`, `plus 订阅`, `订阅已生效`.

### `free`

Use only when a ChatGPT/OpenAI/Codex login, verification, welcome, or free-plan signal exists and no Plus/deactivated evidence exists. Label this as “free / no Plus evidence” in human reports.

### `unknown`

Use when no ChatGPT-related evidence exists or evidence conflicts without a reliable timestamp. Do not infer `free` merely because a mailbox exists.

## Per-mailbox scope

The report should include both:

- `mailbox`: the requested alias;
- `family`: the base alias used for `+tag` grouping;
- `status_scope`: `family` when evidence comes from family aggregation;
- `status`, `confidence`, `evidence_ids`, `latest_evidence_at`.

If exact plus attribution is not present in raw headers, state that the status is family-level and may apply to the base/sibling aliases collectively.

# iCloud HME Admin API Reference

所有以下 Admin API 使用：

```http
x-admin-auth: <Admin API token>
```

`<Admin API token>` 通过环境变量 `ICLOUD_HME_ADMIN_TOKEN` 提供。不要把实际值写入此文件。

## Read-only baseline

```bash
BASE="${ICLOUD_HME_BASE_URL:?set ICLOUD_HME_BASE_URL}"
AUTH="${ICLOUD_HME_ADMIN_TOKEN:?set ICLOUD_HME_ADMIN_TOKEN}"

curl -fsS "$BASE/api/state" -H "x-admin-auth: $AUTH"
curl -fsS "$BASE/api/accounts" -H "x-admin-auth: $AUTH"
curl -fsS "$BASE/api/emails" -H "x-admin-auth: $AUTH"
curl -fsS "$BASE/api/local-inbox/summary" -H "x-admin-auth: $AUTH"
```

## Accounts and HME

| Method | Path | Use |
|---|---|---|
| GET | `/api/accounts` | List account summaries; no Cookie values |
| POST | `/api/accounts/add` | Add and validate user-provided Cookie |
| POST | `/api/accounts/{id}/cookies` | Replace Cookie for an existing account |
| POST | `/api/accounts/{id}/validate` | Revalidate session |
| POST | `/api/accounts/{id}/remove` | Remove locally saved account configuration |
| POST | `/api/accounts/{id}/create` | Create HME addresses; body `{"count":1,"label":"optional"}` |
| POST | `/api/create-batch` | Batch create; body `{"account_ids":["id"],"count_per_account":1}` |
| POST | `/api/accounts/{id}/alias-delete` | Delete a real Apple HME; body `{"email":"xxx@icloud.com"}`; refuses local `+tag` variants |
| GET | `/api/emails` | Fast local/cache address list |
| GET | `/api/aliases` | Cloud sync address list; potentially slow and side-effectful cache update |
| GET | `/admin/address?limit=500&offset=0` | Address credential table |
| GET | `/admin/address_credential?address=...` | Return `{jwt, credential, login_url}` for an address |
| GET | `/admin/export_credentials.csv` | Export address credentials; treat output as secret |

For create/delete operations, confirm exact account/address and count first. `/admin/delete_address/{id}` removes the local credential record; it does not remove the Apple HME address unless the project explicitly adds that behavior.

## Local inbox

| Method | Path | Use |
|---|---|---|
| GET | `/api/local-inbox/summary?q=` | Mailbox counts and latest timestamps |
| GET | `/api/local-inbox/messages?limit=50&offset=0` | All local messages, metadata |
| GET | `/api/local-inbox/messages?alias=xxx%40icloud.com&limit=50&offset=0` | Family mailbox query |
| GET | `/api/local-inbox/messages/{id}` | Full parsed message; fetch only when needed |
| GET | `/api/inbound-config` | Worker config; contains sensitive inbound token, never print |
| GET | `/api/logs` | Recent application logs; summarize only |

Family query semantics: `alias=xxx+3@icloud.com` matches `hme_alias=xxx+3@icloud.com` or `base_alias=xxx@icloud.com`. This is intentional because upstream forwarding can remove the plus tag.

Legacy share routes remain for old links but should not be created for new workflows:

- `POST /api/local-inbox/share`
- `GET /api/shared/{token}/messages`
- `GET /share/{token}`

Use `/admin/address_credential` and `login_url` instead.

## Settings and scheduler

| Method | Path | Use |
|---|---|---|
| GET | `/api/settings` | Read global split/forward settings |
| POST | `/api/settings` | Change `alias_split_enabled`, `alias_split_count`, `forward_to_email` |
| GET | `/api/forward-options` | List Apple-allowed forwarding addresses |
| GET/POST | `/api/scheduler/config` | Read/save schedule |
| POST | `/api/scheduler/start` | Start schedule |
| POST | `/api/scheduler/stop` | Stop schedule |

Do not use `/api/settings` to change forwarding without first reading `/api/forward-options` and confirming the selected address.

## Address JWT user API

These are for the end user, not the Admin token:

```http
Authorization: Bearer <Address JWT>
```

- `GET /api/settings`
- `GET /api/mails`
- `GET /api/parsed_mails`
- `GET /api/parsed_mail/{id}`

They also use family semantics for `+tag` addresses.

# iCloud HME — 多账号聚合管理平台

基于 iCloud Hide My Email 协议，批量创建 `@icloud.com` 隐私邮箱的商用聚合平台。

- 👥 **多账号管理** — 同时管理多组 iCloud 账号，每个独立存储、独立会话
- 🔗 **账号-别名映射** — 自动提取真实 Apple ID，每个隐私邮箱标注归属账号
- ⏱ **定时调度** — 整点自动触发，多账号轮询创建，触达上限自动切下一个
- 🌐 **Web UI** — 暖色面板，仪表盘 + 账号列表 + 别名管理 + 跨账号批量创建

## 前提条件

- **iCloud+ 订阅**（Hide My Email 需要 iCloud+）
- Python 3.10+
- Windows / macOS / Linux

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动 Web UI
python web_ui.py

# 3. 打开 http://127.0.0.1:5050
#    点击左下角「导入 Cookie」添加第一个账号
#    支持粘贴 Cookie Editor 的 Header String 或 JSON
```

## 使用方式

### Web UI（推荐）

```bash
python web_ui.py                    # 启动 Web 界面
python web_ui.py --port 8080        # 指定端口
python web_ui.py --scheduler        # 启动时自动开启调度器
```

界面功能：

| 模块 | 功能 |
|------|------|
| **账号管理** | 添加/切换/删除账号，每个账号独立 Cookie + 会话 |
| **仪表盘** | 账号总数、总别名数、今日创建数，每账号一张状态卡片 |
| **别名列表** | 实时拉取所有别名，标注所属账号 + 真实邮箱 |
| **批量创建** | 勾选目标账号 → 输入数量 → 跨账号轮询创建 |
| **调度器** | 支持两种模式：随机窗口模式；固定间隔模式（如每 30 分钟创建 1 个） |
| **全局邮箱设置** | 可开启隐私邮箱加号派生（如 `name+1@icloud.com` ~ `name+4@icloud.com`），并设置新建别名尝试使用的转发地址 |

计划任务说明：

- **随机窗口模式**：北京时间 `7:00-20:00` 运行，轮次间隔 `60-90` 分钟，每轮每账号随机创建 `3-5` 个
- **固定间隔模式**：按你设置的分钟数循环触发，例如“每 `30` 分钟创建 `1` 个”
- 固定间隔模式会在**所有活跃账号之间轮询**，避免长期只打一个账号
- 计划任务配置会持久化到本地 `scheduler_config.json`

加号派生说明：

- 开启后，本地列表 / 复制 / CSV 会把每个实际创建的隐私邮箱额外展示为 `+1` ~ `+N` 变体
- 例如实际创建 `name@icloud.com`，展示为 `name@icloud.com`、`name+1@icloud.com`、`name+2@icloud.com` ...
- 这些派生地址不额外调用 Apple 创建接口，不消耗 Hide My Email 创建额度
- 转发地址留空时使用 iCloud 当前默认转发地址；填写时会在新建别名时尝试传给 Apple HME API

### 命令行调度器

```bash
# 多账号定时调度（需要先通过 Web UI 添加账号）
python scheduler.py

# 指定账号间间隔
python scheduler.py --interval 5

# 后台守护进程
python scheduler.py -d
```

### CLI 手动操作

```bash
# 列出所有别名
python icloud_hme.py list --cookies cookies.json

# 创建别名
python icloud_hme.py create -n 5 --cookies cookies.json

# 删除别名
python icloud_hme.py delete --email xxx@icloud.com --cookies cookies.json
```

## Cookie 获取

| 方式 | 说明 |
|------|------|
| Web UI 导入 | 点击左下角按钮，粘贴 Cookie Editor 的 Header String |
| Chrome 自动提取 | Windows 下 `python icloud_hme.py export-cookies` |
| 命令行 `--cookies` | 指定 JSON 文件路径 |

支持两种输入格式：
- **Header String**：`name1=value1; name2=value2; ...`
- **JSON**：`{"name1":"value1", "name2":"value2"}`

导入后自动持久化到 `accounts.json`，重启无需重新粘贴。

## 调度逻辑

```
每整点触发一轮
  → 遍历所有活跃账号
  → 每个账号创建到 iCloud 返回上限
  → 账号间间隔 3 秒（可配）
  → 全部完成后等待下一个整点
```

## 文件结构

```
├── icloud_hme.py        # 核心库：Cookie 提取 / HME API / 账号身份提取
├── account_manager.py   # 多账号管理器：CRUD / 批量创建 / 别名索引
├── web_ui.py            # Flask Web 面板 + 内置调度器
├── scheduler.py         # 独立命令行调度器
└── requirements.txt     # pip 依赖
```

运行时生成：

```
accounts.json          # 所有账号及 Cookie（自动持久化）
scheduler_state.json   # 调度器历史状态
scheduler_config.json  # Web UI 调度器配置
app_settings.json      # Web UI 全局邮箱设置（派生开关、转发地址）
inbound_config.json    # Cloudflare Email Worker 入站投递 token
logs/                  # 运行日志
results/               # 创建的邮箱列表、本机收件箱 SQLite 数据库
```

## 本机收件箱

现在可以不用 Apple IMAP 查收邮件，而是让 Apple Hide My Email 把邮件转发到你自己域名的邮箱，再由 Cloudflare Email Routing Worker 投递回本机。

推荐链路：

```
外部发件人
  → xxx@icloud.com 隐私邮箱
  → Apple HME 转发到已绑定邮箱，例如 inbox@mail.armsg.yueseng-ys.com
  → Cloudflare Email Routing Worker
  → POST https://icloud.armsg.yueseng-ys.com/api/inbound-mail
  → 本机 SQLite 保存
  → Web UI「本机收件箱」按隐私邮箱 family 分类展示
```

配置步骤：

1. 在 Apple 账号里添加并验证 `inbox@mail.armsg.yueseng-ys.com` 或你的 catch-all 邮箱。
2. 回到 Web UI「全局邮箱设置」，刷新并选择该转发地址。
3. 打开 Web UI「本机收件箱」→「Worker 配置」，复制：
   - `INBOUND_URL`
   - `INBOUND_TOKEN`
   - Worker 模板地址
4. 在 Cloudflare 创建 Email Worker，代码可用：
   - `https://icloud.armsg.yueseng-ys.com/cloudflare_inbound_worker.js`
5. 在 Worker 变量里设置：
   - `INBOUND_URL=https://icloud.armsg.yueseng-ys.com/api/inbound-mail`
   - `INBOUND_TOKEN=Web UI 中显示的 token`
6. 在 Cloudflare Email Routing 里把目标地址或 catch-all 路由到该 Worker。

相关接口：

| 接口 | 说明 |
|------|------|
| `POST /api/inbound-mail` | Cloudflare Worker 投递原始邮件，Bearer token 认证 |
| `GET /api/local-inbox/summary` | 按隐私邮箱统计本机收到的邮件 |
| `GET /api/local-inbox/messages?alias=xxx@icloud.com` | 查看某个隐私邮箱 family 收件箱 |
| `GET /api/mail-analysis` | 获取邮件分类和每个邮箱/family 的 ChatGPT 状态；加 `?refresh=1` 强制分析 |
| `GET /admin/address_credential?address=xxx@icloud.com` | 生成用户 Address JWT 和自动登录链接 |
| `POST /api/accounts/{id}/alias-delete` | 删除真实 Apple HME；默认保留本机历史邮件；不能删除本地 `+tag` 派生地址 |

外层 Caddy 不再启用 Basic Auth，统一改为应用内入口鉴权：`/admin` 用管理员密码登录，`/user` 用地址凭证或用户账号登录。

「本机收件箱」会合并当前已创建/已云端同步的历史隐私邮箱和派生地址。用户访问统一通过邮箱列表中的自动登录链接；旧的 `/share/{token}` 与 `/api/local-inbox/share` 仅保留后端兼容，不再出现在 Admin UI。

### Agent 管理提示词

Admin 后台提供「Agent 提示词」页面，可一键生成并复制完整系统管理提示词。提示词包含：

- Admin API 认证方式；
- 账号、邮箱、收件箱和登录链接查询；
- HME 创建、Cookie 更新与计划任务管理；
- `+tag` family 收件规则；
- 敏感凭证保护和高影响操作确认规则。

页面可以从当前浏览器登录状态读取 Admin API Token。包含 Token 的提示词等同完整管理员权限，只应交给可信 Agent。

仓库同时提供可下载 Skill：`skills/icloud-hme-admin/`。安装到 Codex：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/icloud-hme-admin "${CODEX_HOME:-$HOME/.codex}/skills/"
export ICLOUD_HME_BASE_URL="https://icloud.armsg.yueseng-ys.com"
export ICLOUD_HME_ADMIN_TOKEN="<Admin API token>"
```

调用 `$icloud-hme-admin` 即可管理账号、真实 Apple HME、凭证、收件箱、计划任务和邮件分析。Skill 不包含任何线上 Token、Cookie 或 JWT。

## Cloudflare Temp Email 兼容模式

项目现在增加了一个兼容 `dreamhunter2333/cloudflare_temp_email` 使用习惯的登录和 API 层。区别只有一个：邮箱地址不是 Cloudflare 自定义域随机生成，而是通过 iCloud Hide My Email 创建 / 同步得到。

### 页面入口

| 页面 | 说明 |
|------|------|
| `/` | 普通用户入口，和 `/user` 相同；支持地址凭证 JWT 登录或用户账号登录 |
| `/admin` | 管理员登录入口。登录后进入原 iCloud HME 管理界面：账号、调度器、隐私邮箱、本机收件箱、凭证导出 |
| `/user` | 普通用户收件入口。支持“地址凭证 JWT 登录”和“用户账号登录 + 绑定地址凭证” |
| `/share/{token}` | 旧版只读分享入口，仅为已有链接保留兼容 |

管理员密码由应用内配置控制。为了兼容 cftempmail 前端，`/open_api/admin_login` 同时接受明文密码或前端 SHA-256 后的密码。

管理后台「隐私邮箱列表」支持按邮箱、标签、账号搜索；普通用户地址列表也支持搜索。地址凭证登录走本机 JWT 校验，不会在登录时全量同步 Apple / 本机地址表。

### 地址凭证 / JWT

管理员在「隐私邮箱列表」点击「导出凭证」即可导出 CSV，字段包括：

| 字段 | 说明 |
|------|------|
| `id` | 本机地址 ID |
| `name` | iCloud 隐私邮箱地址 |
| `jwt` / `credential` | 地址凭证，兼容 cftempmail 的 Address JWT 用法 |
| `login_url` | 自动登录链接，打开后会把凭证写入本地并进入该邮箱收件箱 |
| `account_id` | 归属 iCloud 账号 |
| `mail_count` | 本机收件数量 |

普通用户拿到某个地址的 `jwt` 后，可以：

1. 直接打开 `login_url` 自动进入对应收件箱；
2. 或在 `/user` 的「凭证登录」里粘贴 `jwt` 查看这个地址的收件箱；
3. 注册 / 登录用户账号后，把该 `jwt` 绑定到个人账号，之后一个账号可管理多个隐私邮箱。

> 凭证容错：前端和后端都会自动清洗从 CSV、聊天工具、浏览器地址栏复制出来的凭证，兼容
> `Bearer <jwt>`、完整 `login_url`、换行/空格、URL 编码、引号包裹等格式。地址 JWT 默认不设置过期时间；
> 如果页面提示“凭证过期或无效”，通常是复制内容被截断、浏览器 localStorage 里留有旧坏 token、
> 或该地址已被管理员从本机地址表删除。新版前端会在校验失败后自动清掉本机坏 token，重新打开最新
> `login_url` 即可恢复。

### cftempmail 兼容 API

#### Open API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/open_api/settings` | 站点公开配置，字段兼容 cftempmail |
| `POST` | `/open_api/admin_login` | 管理员登录，Body: `{"password":"<明文或sha256>"}` |
| `POST` | `/open_api/credential_login` | 地址凭证校验，Body: `{"credential":"<Address JWT>"}` |
| `POST` | `/api/address_login` | 地址凭证校验兼容别名，Body 同上 |

#### 地址 JWT API

这些接口使用 `Authorization: Bearer <Address JWT>`：

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/settings` | 当前地址信息：`{ address, address_id, send_balance }` |
| `GET` | `/api/mails?limit=&offset=` | 当前地址邮件列表，返回 raw MIME |
| `GET` | `/api/mail/{id}` | 当前地址单封 raw 邮件 |
| `GET` | `/api/parsed_mails?limit=&offset=` | 当前地址解析后的邮件列表 |
| `GET` | `/api/parsed_mail/{id}` | 当前地址单封解析邮件 |
| `DELETE` | `/api/clear_inbox` | 清空当前地址本机收件箱 |
| `DELETE` | `/api/delete_address` | 删除本机地址凭证 / 绑定记录；不会删除 Apple HME 里的真实隐私邮箱 |

示例：

```bash
BASE=https://icloud.armsg.yueseng-ys.com
JWT='<导出的地址凭证>'

curl -s "$BASE/api/settings" \
  -H "Authorization: Bearer $JWT"

curl -s "$BASE/api/parsed_mails?limit=20&offset=0" \
  -H "Authorization: Bearer $JWT"
```

#### 用户 API

用户接口使用 `x-user-token: <用户JWT>`；绑定地址时额外传 `Authorization: Bearer <Address JWT>`。

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/user_api/register` | 注册用户，Body: `{"email":"u@example.com","password":"<明文或sha256>"}` |
| `POST` | `/user_api/login` | 登录用户，返回 `{ jwt }` |
| `GET` | `/user_api/settings` | 当前用户信息 |
| `GET` | `/user_api/bind_address` | 用户已绑定地址列表 |
| `POST` | `/user_api/bind_address` | 绑定地址凭证到当前用户 |
| `GET` | `/user_api/bind_address_jwt/{address_id}` | 取回已绑定地址的 Address JWT |
| `GET` | `/user_api/mails?address=&limit=&offset=` | 查看用户所有 / 指定地址邮件 |

示例：

```bash
BASE=https://icloud.armsg.yueseng-ys.com

USER_JWT=$(curl -s -X POST "$BASE/user_api/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@example.com","password":"password"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["jwt"])')

curl -s -X POST "$BASE/user_api/bind_address" \
  -H "x-user-token: $USER_JWT" \
  -H "Authorization: Bearer <Address JWT>"

curl -s "$BASE/user_api/mails?limit=20&offset=0" \
  -H "x-user-token: $USER_JWT"
```

#### 管理员 API

管理员接口兼容 cftempmail 的 `x-admin-auth`。可以传管理员密码明文或 SHA-256；也可以先登录 `/admin` 后使用浏览器 cookie。

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/admin/address?limit=&offset=&query=` | 地址列表；传 `sync=1` 时先同步当前已知地址 |
| `POST` | `/admin/new_address` | 创建新的 iCloud HME 隐私邮箱，返回 Address JWT |
| `GET` | `/admin/show_password/{address_id}` | 查看某个地址的 Address JWT 和自动登录链接 |
| `GET` | `/admin/address_credential?address=xxx@icloud.com` | 按邮箱地址生成 / 取回 Address JWT 和自动登录链接 |
| `GET` | `/admin/export_credentials.csv` | 导出全部地址凭证 CSV |
| `GET` | `/admin/export_credentials` | 导出全部地址凭证 JSON |
| `GET` | `/admin/mails?address=&limit=&offset=` | 管理员查看全部 / 指定地址邮件 |
| `DELETE` | `/admin/mails/{id}` | 删除本机邮件 |
| `GET` | `/admin/users` | 用户列表 |
| `POST` | `/admin/users` | 创建用户 |
| `DELETE` | `/admin/users/{id}` | 删除用户 |

注意：`/admin/new_address` 会真实调用 Apple HME 创建隐私邮箱，仍受 Apple 限额影响；失败后请等待下一周期，不要高频重试。

## 依赖

```
requests>=2.25          # HTTP
pycryptodome>=3.15     # Chrome cookie 解密 (Windows)
pywin32>=305           # Windows DPAPI (仅 Windows)
flask>=3.0             # Web UI
```

## License

MIT

---
name: "CTF•超级总控"
description: "面向 CTF 新手与综合题的统一总控 skill。保持原有两种主模式：1) 自动分流，2) 先头脑风暴再分流。分流目标既可以是 ctf-*，也可以在 Web/接口/漏洞验证阶段增强到 strix-*；适合不知道该用哪个 skill、想边做边学、又不想自己先判断何时切换到工具链或漏洞专项的场景;触发名:ctf-super-hub"
---

# CTF 超级总控

## 定位

这是整个仓库的**唯一主入口**。

核心原则：
- **主结构不变**：仍然沿用你原来的 CTF 双模式
- **Strix 只是增强层**：不是另一套主系统
- **用户不需要先理解 Strix**：先从 CTF 入口进，是否切到 `strix-*` 由总控来判断

## 两种主模式

### 模式 1：自动分流

适合：
- 已经有题面、附件、URL、IP、端口、源码、二进制
- 想直接开始，不想自己判断题型

流程：
1. 收集最小必要信息
2. 先判断主类别
3. 决定先进入 `ctf-*` 还是增强到 `strix-*`
4. 给出最小化下一步

### 模式 2：先头脑风暴，再分流

适合：
- 题目描述抽象，看不懂想干什么
- 不知道第一步做什么
- 想先把目标、材料、卡点讲清楚

流程：
1. 先用 `brainstorming` 风格澄清题目与目标
2. 再判断进入 `ctf-*` 还是增强到 `strix-*`
3. 给出后续最小化步骤

## 什么时候分到 `ctf-*`

下面这些情况优先分到原来的 CTF 专项：

- 二进制 / APK / WASM / 固件分析 -> `ctf-reverse`
- 内存破坏 / 控制流劫持 / libc / ROP -> `ctf-pwn`
- RSA / AES / PRNG / 数学构造 -> `ctf-crypto`
- PCAP / 内存 / 磁盘 / 隐写 / 日志 -> `ctf-forensics`
- 社交媒体 / 地理定位 / 公开资料 -> `ctf-osint`
- 样本 / C2 / 恶意脚本 / PE/.NET -> `ctf-malware`
- pyjail / bash jail / 编码 / 游戏 / 杂项 -> `ctf-misc`
- AI/ML 相关题 -> `ctf-ai-ml`
- 解完题以后 -> `ctf-writeup`

## 什么时候增强到 `strix-*`

当题目已经明显进入 **Web / 接口 / 漏洞验证 / 工具链推进** 阶段时，不再只停留在 `ctf-web` 的大类判断，而是增强到 `strix-*`。

典型触发场景：
- 已有 URL，需要先探测、爬取、字典测试、模板扫描
- 已经怀疑是 SQLi / XSS / SSRF / RCE / JWT / IDOR / 上传 / 文件包含
- 已经从 CTF 题面进入真实 Web 测试动作阶段

### 工具链增强
- `strix-httpx`
- `strix-katana`
- `strix-ffuf`
- `strix-nuclei`
- `strix-sqlmap`

### 漏洞专项增强
- `strix-sql-injection`
- `strix-xss`
- `strix-ssrf`
- `strix-rce`
- `strix-authentication-jwt`
- `strix-idor`
- `strix-information-disclosure`
- `strix-insecure-file-uploads`
- `strix-open-redirect`
- `strix-csrf`
- `strix-business-logic`
- `strix-broken-function-level-authorization`
- `strix-path-traversal-lfi-rfi`

### 模式增强
- `strix-quick`
- `strix-standard`

## 路由原则

### 先分主类，再决定要不要增强

不要一上来就把一切都扔给 Strix。

正确顺序：
1. 先判断大类是不是 Web / API / 接口交互问题
2. 如果不是，就走原来的 `ctf-*`
3. 如果是，而且已经进入验证 / 枚举 / 探测 / 工具推进阶段，再切到 `strix-*`

### `ctf-web` 和 `strix-*` 的关系

- `ctf-web`：更像 CTF Web 总类打法
- `strix-*`：更像 Web 安全测试 / 工具使用 / 漏洞专项增强

所以：
- **判断题型** 先看 `ctf-web`
- **开始打具体 Web 测试动作** 再增强到 `strix-*`

## 默认输出格式

统一尽量按这个结构输出：

1. 这题现在更像什么
2. 为什么先走这个方向
3. 现在先做哪 1~3 步
4. 每一步是在干什么
5. 如果没结果，下一步切到哪个 skill
6. 术语用一句人话解释

## 结束条件

当满足下面任意一点时可以结束当前总控回合：
- 主 skill 已明确，并且下一步可执行
- 已经判断需要增强到 `strix-*`
- 题目已解出，并准备转到 `ctf-writeup`

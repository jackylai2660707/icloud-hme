# 新手示例

## 示例 1：有 ELF 文件

输入：
- 附件：`chall`
- 题面：`Can you find the secret?`

推荐：
- 先走自动分流
- 大概率 `ctf-reverse`
- 如果是远程交互且像漏洞利用，再考虑 `ctf-pwn`

## 示例 2：有 URL 和登录框

输入：
- URL：`http://target/`
- 页面：登录、注册、上传头像

推荐：
- 先走自动分流
- 大概率 `ctf-web`
- 如果出现 JWT / token / 签名，再结合 `ctf-crypto`

## 示例 3：只有一句模糊题面

输入：
- 题面：`The truth is hidden in the noise.`

推荐：
- 先走头脑风暴
- 先澄清有没有附件、音频、图片、pcap、二进制
- 再决定去 `ctf-forensics`、`ctf-reverse` 还是 `ctf-misc`

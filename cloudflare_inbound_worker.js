// Cloudflare Email Routing Worker → icloud-hme 本机收件箱
//
// 部署后在 Cloudflare Worker 变量里设置:
//   INBOUND_URL   = __INBOUND_URL__
//   INBOUND_TOKEN = 从 Web UI「本机收件箱 → Worker 配置」复制的 token
//
// 然后在 Email Routing 里把目标地址/ catch-all 路由到这个 Worker。

export default {
  async email(message, env, ctx) {
    const raw = await new Response(message.raw).text();
    const headers = {};
    for (const [key, value] of message.headers) {
      headers[key] = value;
    }

    const payload = {
      from: message.from,
      to: message.to,
      rawSize: message.rawSize,
      raw,
      headers,
    };

    const inboundUrl = env.INBOUND_URL || "__INBOUND_URL__";
    const token = env.INBOUND_TOKEN;
    if (!token) {
      message.setReject("INBOUND_TOKEN is not configured");
      return;
    }

    const resp = await fetch(inboundUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const text = await resp.text();
      message.setReject(`inbound backend failed: ${resp.status} ${text.slice(0, 160)}`);
    }
  },
};

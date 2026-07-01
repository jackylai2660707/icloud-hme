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

    const resp = await fetch(env.INBOUND_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${env.INBOUND_TOKEN}`,
      },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const text = await resp.text();
      message.setReject(`inbound backend failed: ${resp.status} ${text.slice(0, 160)}`);
    }
  },
};

// Vercel serverless function — proxies the Usage Monitor /api/quota-windows endpoint
// with the Bearer USAGE_READ_TOKEN from Vercel env. CORS is locked on usage.jays.services
// (cross-origin-resource-policy: same-origin, no ACAO), so the browser cannot call it
// directly. This proxy is the only safe path.
//
// Env:
//   USAGE_READ_TOKEN  — Bearer token for usage.jays.services (canonical home: Infisical prod)
//
// Private operator data is not cached; the upstream stays the live source.

const UPSTREAM = "https://usage.jays.services/api/quota-windows";

module.exports = async function (req, res) {
  res.setHeader("Cache-Control", "private, no-store");
  const token = process.env.USAGE_READ_TOKEN;
  if (!token) {
    res.status(500).json({
      error: "USAGE_READ_TOKEN not configured on this Vercel project. Set it in the project env (canonical home is Infisical prod).",
    });
    return;
  }

  try {
    const upstream = await fetch(UPSTREAM, {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
        "User-Agent": "home-jays-services/1.0 (+fleet-ops)",
      },
    });

    const body = await upstream.text();
    const upstreamType = String(upstream.headers.get("content-type") || "").trim();
    const trimmed = body.trim();
    const looksJson =
      /(?:^|;)application\/json(?:;|$)/i.test(upstreamType) ||
      trimmed.startsWith("{") ||
      trimmed.startsWith("[");
    if (looksJson) {
      res.setHeader("Content-Type", "application/json; charset=utf-8");
    } else if (upstreamType) {
      res.setHeader("Content-Type", upstreamType);
    }
    res.setHeader("Access-Control-Allow-Origin", "https://home.jays.services");
    res.status(upstream.status).send(body);
  } catch (err) {
    res.status(502).json({
      error: "upstream-fetch-failed",
      message: String((err && err.message) || err),
    });
  }
};

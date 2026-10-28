import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";
import {
  CONFIG_CACHE_TTL_MS,
  INFISICAL_BASE_URL,
  INFISICAL_ENVIRONMENT,
  INFISICAL_SECRET_PATH,
  InfisicalError,
  REQUIRED_SECRET_NAMES,
  _resetConfigCacheForTests,
  resolveInfisicalConfig,
} from "./infisical.js";

const SERVICE_TOKEN = "st.test-service-token";

const SECRETS = {
  GITHUB_WEBHOOK_SECRET: "webhook-secret",
  GITHUB_TOKEN: "github-token",
  ZULIP_SITE: "https://zulip.example.com",
  ZULIP_BOT_EMAIL: "alerts-bot@example.com",
  ZULIP_BOT_API_KEY: "zulip-api-key",
};

const jsonResponse = (body, status = 200, headers = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });

/**
 * A fake fetch that answers the token info call and the listing call.
 *
 * `tokenInfo` and `listing` may be a Response, a function returning one, or
 * a function that throws.  Every call is recorded, so a test can assert how
 * many requests were made and where they went.
 */
function fakeFetch({ tokenInfo, listing } = {}) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url: String(url), init });
    const isTokenInfo = String(url).includes("/api/v2/service-token");
    const responder = isTokenInfo ? tokenInfo : listing;
    if (typeof responder === "function") return responder(String(url), init);
    if (!responder) throw new Error(`unexpected fetch: ${url}`);
    return responder;
  };
  return { impl, calls };
}

/** The token info body a healthy service token answers with. */
function tokenInfoBody(extra = {}) {
  return {
    workspace: "8dae6d03-8f12-48b2-adf3-e5c1b881c8c5",
    scopes: [{ environment: INFISICAL_ENVIRONMENT, secretPath: INFISICAL_SECRET_PATH }],
    permissions: ["read"],
    ...extra,
  };
}

/** The listing body a healthy folder answers with. */
function listingBody(extra = {}) {
  return {
    secrets: Object.entries(SECRETS).map(([secretKey, secretValue]) => ({ secretKey, secretValue })),
    ...extra,
  };
}

/** Capture the console.error lines a test produces. */
function captureErrors() {
  const lines = [];
  console.error = (...args) => lines.push(args.join(" "));
  return lines;
}

const realConsoleError = console.error;

beforeEach(() => {
  _resetConfigCacheForTests();
});

afterEach(() => {
  _resetConfigCacheForTests();
  console.error = realConsoleError;
});

describe("resolveInfisicalConfig", () => {
  it("reads the token info, then the folder, and returns the config", async () => {
    const { impl, calls } = fakeFetch({
      tokenInfo: () => jsonResponse(tokenInfoBody()),
      listing: () => jsonResponse(listingBody()),
    });

    const config = await resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl });

    assert.deepEqual(config, SECRETS);
    assert.equal(calls.length, 2);

    const [infoCall, listCall] = calls;
    assert.equal(infoCall.url, `${INFISICAL_BASE_URL}/api/v2/service-token`);
    assert.equal(infoCall.init.headers.Authorization, `Bearer ${SERVICE_TOKEN}`);
    assert.equal(infoCall.init.redirect, "manual", "a redirect must never be followed");

    assert.equal(
      listCall.url,
      `${INFISICAL_BASE_URL}/api/v3/secrets/raw` +
        "?workspaceId=8dae6d03-8f12-48b2-adf3-e5c1b881c8c5" +
        "&environment=prod" +
        "&secretPath=%2Fpr-conflict-watch"
    );
    assert.equal(listCall.init.headers.Authorization, `Bearer ${SERVICE_TOKEN}`);
    assert.equal(listCall.init.redirect, "manual", "a redirect must never be followed");
  });

  it("takes the project id from workspace", async () => {
    const { impl, calls } = fakeFetch({
      tokenInfo: () => jsonResponse(tokenInfoBody({ workspace: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" })),
      listing: () => jsonResponse(listingBody()),
    });

    await resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl });

    assert.match(calls[1].url, /workspaceId=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/);
  });

  it("uses projectId when workspace is absent", async () => {
    const { impl, calls } = fakeFetch({
      tokenInfo: () =>
        jsonResponse({
          projectId: "11111111-2222-3333-4444-555555555555",
          scopes: [{ environment: "prod", secretPath: "/pr-conflict-watch" }],
        }),
      listing: () => jsonResponse(listingBody()),
    });

    await resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl });

    assert.match(calls[1].url, /workspaceId=11111111-2222-3333-4444-555555555555/);
  });

  it("refuses a token whose scope does not name this folder", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () =>
        jsonResponse(
          tokenInfoBody({ scopes: [{ environment: "prod", secretPath: "/some-other-folder" }] })
        ),
      listing: () => jsonResponse(listingBody()),
    });
    const lines = captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => error instanceof InfisicalError && error.code === "token_scope_mismatch"
    );
    assert.equal(lines.length, 1);
    assert.match(lines[0], /token_scope_mismatch/);
  });

  it("refuses a token whose scope names another environment", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () =>
        jsonResponse(
          tokenInfoBody({ scopes: [{ environment: "dev", secretPath: "/pr-conflict-watch" }] })
        ),
      listing: () => jsonResponse(listingBody()),
    });
    captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => error instanceof InfisicalError && error.code === "token_scope_mismatch"
    );
  });

  it("refuses a token that can also read another folder", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () =>
        jsonResponse(
          tokenInfoBody({
            scopes: [
              { environment: "prod", secretPath: "/pr-conflict-watch" },
              { environment: "prod", secretPath: "/zulip" },
            ],
          })
        ),
      listing: () => jsonResponse(listingBody()),
    });
    captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => error instanceof InfisicalError && error.code === "token_scope_mismatch"
    );
  });

  it("accepts a token without scopes or permissions", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () => jsonResponse({ workspace: "8dae6d03-8f12-48b2-adf3-e5c1b881c8c5" }),
      listing: () => jsonResponse(listingBody()),
    });

    const config = await resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl });

    assert.deepEqual(config, SECRETS);
  });

  it("refuses a token that may write", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () => jsonResponse(tokenInfoBody({ permissions: ["read", "write"] })),
      listing: () => jsonResponse(listingBody()),
    });
    captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => error instanceof InfisicalError && error.code === "token_not_read_only"
    );
  });

  it("refuses a 3xx and never follows it", async () => {
    const { impl, calls } = fakeFetch({
      tokenInfo: () =>
        new Response(null, {
          status: 302,
          headers: { location: "https://elsewhere.example.com/secrets" },
        }),
      listing: () => jsonResponse(listingBody()),
    });
    const lines = captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => error instanceof InfisicalError && error.code === "infisical_status_302"
    );
    assert.equal(calls.length, 1, "a redirect must not be followed");
    assert.ok(!lines.join(" ").includes("elsewhere.example.com"), "a redirect target was logged");
  });

  it("refuses a 3xx on the listing call", async () => {
    const { impl, calls } = fakeFetch({
      tokenInfo: () => jsonResponse(tokenInfoBody()),
      listing: () => new Response(null, { status: 307 }),
    });
    captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => error instanceof InfisicalError && error.code === "infisical_status_307"
    );
    assert.equal(calls.length, 2);
  });

  it("reports a 5xx as a status failure", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () => jsonResponse({ message: "boom" }, 500),
      listing: () => jsonResponse(listingBody()),
    });
    captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) =>
        error instanceof InfisicalError &&
        error.code === "infisical_status_500" &&
        error.status === 500
    );
  });

  it("reports a network failure as unreachable", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () => {
        throw new Error("connection reset");
      },
    });
    captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => error instanceof InfisicalError && error.code === "infisical_unreachable"
    );
  });

  it("reports a body that is not JSON as an invalid response", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () => new Response("<html>nope</html>", { status: 200 }),
      listing: () => jsonResponse(listingBody()),
    });
    captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => error instanceof InfisicalError && error.code === "invalid_response"
    );
  });

  it("rejects a token info body that is not a plain object", async () => {
    for (const body of [[], "workspace", 7, null]) {
      const { impl } = fakeFetch({
        tokenInfo: () => jsonResponse(body),
        listing: () => jsonResponse(listingBody()),
      });
      captureErrors();

      await assert.rejects(
        () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
        (error) => error instanceof InfisicalError && error.code === "invalid_response"
      );
    }
  });

  it("rejects a token info body with no project id", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () => jsonResponse({ scopes: [] }),
      listing: () => jsonResponse(listingBody()),
    });
    captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => error instanceof InfisicalError && error.code === "invalid_response"
    );
  });

  it("rejects scopes that are not an array of objects with string fields", async () => {
    for (const scopes of [["prod"], [{ environment: 1, secretPath: 2 }], "prod"]) {
      const { impl } = fakeFetch({
        tokenInfo: () => jsonResponse(tokenInfoBody({ scopes })),
        listing: () => jsonResponse(listingBody()),
      });
      captureErrors();

      await assert.rejects(
        () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
        (error) => error instanceof InfisicalError && error.code === "invalid_response"
      );
    }
  });

  it("rejects a listing body that is not the shape we expect", async () => {
    const badBodies = [
      { secrets: "nope" },
      { secrets: [{ secretKey: 1, secretValue: 2 }] },
      { secrets: [{ secretKey: "A" }] },
      { secrets: ["A"] },
      {},
      [],
    ];
    for (const body of badBodies) {
      const { impl } = fakeFetch({
        tokenInfo: () => jsonResponse(tokenInfoBody()),
        listing: () => jsonResponse(body),
      });
      captureErrors();

      await assert.rejects(
        () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
        (error) => error instanceof InfisicalError && error.code === "invalid_response"
      );
    }
  });

  it("rejects a body over the size cap", async () => {
    const huge = "x".repeat(64 * 1024 + 1);
    const { impl } = fakeFetch({
      tokenInfo: () => new Response(huge, { status: 200 }),
      listing: () => jsonResponse(listingBody()),
    });
    captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => error instanceof InfisicalError && error.code === "invalid_response"
    );
  });

  it("rejects a body that claims to be over the size cap", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () => jsonResponse(tokenInfoBody(), 200, { "content-length": "99999999" }),
      listing: () => jsonResponse(listingBody()),
    });
    captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => error instanceof InfisicalError && error.code === "invalid_response"
    );
  });

  it("names every secret the folder does not hold", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () => jsonResponse(tokenInfoBody()),
      listing: () =>
        jsonResponse({
          secrets: [
            { secretKey: "GITHUB_WEBHOOK_SECRET", secretValue: "webhook-secret" },
            { secretKey: "ZULIP_SITE", secretValue: "https://zulip.example.com" },
            { secretKey: "ZULIP_BOT_API_KEY", secretValue: "   " },
          ],
        }),
    });
    const lines = captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl }),
      (error) => {
        assert.ok(error instanceof InfisicalError);
        assert.equal(error.code, "missing_secret");
        assert.deepEqual(error.missing, ["GITHUB_TOKEN", "ZULIP_BOT_EMAIL", "ZULIP_BOT_API_KEY"]);
        return true;
      }
    );
    assert.match(lines[0], /GITHUB_TOKEN/);
    assert.match(lines[0], /ZULIP_BOT_EMAIL/);
  });

  it("refuses a config without a service token", async () => {
    const lines = captureErrors();

    await assert.rejects(
      () => resolveInfisicalConfig({ token: "  ", fetchImpl: async () => assert.fail("no call") }),
      (error) => error instanceof InfisicalError && error.code === "not_configured"
    );
    assert.equal(lines.length, 1);
  });

  it("caches the config for a minute", async () => {
    const { impl, calls } = fakeFetch({
      tokenInfo: () => jsonResponse(tokenInfoBody()),
      listing: () => jsonResponse(listingBody()),
    });
    let clock = 1_000_000;

    const first = await resolveInfisicalConfig({
      token: SERVICE_TOKEN,
      fetchImpl: impl,
      now: () => clock,
    });
    clock += CONFIG_CACHE_TTL_MS - 1;
    const second = await resolveInfisicalConfig({
      token: SERVICE_TOKEN,
      fetchImpl: impl,
      now: () => clock,
    });

    assert.deepEqual(first, SECRETS);
    assert.deepEqual(second, SECRETS);
    assert.equal(calls.length, 2, "the second read came from the cache");
  });

  it("reads again once the cache has expired", async () => {
    const { impl, calls } = fakeFetch({
      tokenInfo: () => jsonResponse(tokenInfoBody()),
      listing: () => jsonResponse(listingBody()),
    });
    let clock = 2_000_000;

    await resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl, now: () => clock });
    clock += CONFIG_CACHE_TTL_MS;
    await resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl, now: () => clock });

    assert.equal(calls.length, 4, "an expired cache is read again");
  });

  it("does not cache a failure", async () => {
    let attempt = 0;
    const { impl, calls } = fakeFetch({
      tokenInfo: () => {
        attempt += 1;
        if (attempt === 1) return jsonResponse({ message: "boom" }, 500);
        return jsonResponse(tokenInfoBody());
      },
      listing: () => jsonResponse(listingBody()),
    });
    captureErrors();

    await assert.rejects(() =>
      resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl })
    );
    const config = await resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl });

    assert.deepEqual(config, SECRETS);
    assert.equal(calls.length, 3, "the failed read was not cached");
  });

  it("never logs a secret value or the token", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () => jsonResponse(tokenInfoBody(), 500),
      listing: () => jsonResponse(listingBody()),
    });
    const lines = captureErrors();

    await assert.rejects(() =>
      resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl })
    );

    const logged = lines.join("\n");
    assert.equal(lines.length, 1, "one structured line per failure");
    assert.match(lines[0], /"event":"infisical_failed"/);
    assert.ok(!logged.includes(SERVICE_TOKEN), "the service token was logged");
    for (const value of Object.values(SECRETS)) {
      assert.ok(!logged.includes(value), `a secret value was logged: ${value}`);
    }
  });

  it("logs the missing names and the code, and nothing else", async () => {
    const { impl } = fakeFetch({
      tokenInfo: () => jsonResponse(tokenInfoBody()),
      listing: () => jsonResponse({ secrets: [] }),
    });
    const lines = captureErrors();

    await assert.rejects(() =>
      resolveInfisicalConfig({ token: SERVICE_TOKEN, fetchImpl: impl })
    );

    const line = JSON.parse(lines[0]);
    assert.deepEqual(line.event, "infisical_failed");
    assert.deepEqual(line.code, "missing_secret");
    assert.deepEqual(line.missing, [...REQUIRED_SECRET_NAMES]);
    assert.ok(!lines[0].includes(SERVICE_TOKEN), "the service token was logged");
  });
});

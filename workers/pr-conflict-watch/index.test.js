import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";
import worker, {
  buildMessage,
  handlePr,
  missingSecretNames,
  neutralizeMentions,
  parseZulipResult,
  postZulip,
  topicForRepo,
  zulipMessagesUrl,
} from "./index.js";

const ENV = {
  GITHUB_WEBHOOK_SECRET: "webhook-secret",
  GITHUB_TOKEN: "github-token",
  ZULIP_SITE: "https://zulip.example.com",
  ZULIP_BOT_EMAIL: "alerts-bot@example.com",
  ZULIP_BOT_API_KEY: "zulip-api-key",
};

const PR = {
  number: 123,
  title: "Fix the thing",
  html_url: "https://github.com/Simple-With-Us/BotFleet/pull/123",
  base: { ref: "main" },
  head: { ref: "gb-deployer/afc-123-fix-thing" },
  user: { login: "jaywedgeworth22" },
};

const GAP = "\u00A0 ";

const realFetch = globalThis.fetch;
const realConsoleError = console.error;

afterEach(() => {
  globalThis.fetch = realFetch;
  console.error = realConsoleError;
});

/** Capture one fetch call and answer it with the given Response. */
function stubFetch(response) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return response;
  };
  return calls;
}

const jsonResponse = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

/**
 * Route fetches by host: the GitHub PR lookup and the Zulip post each get
 * their own answer.  A responder is a function, so every call gets a fresh
 * Response body.
 */
function routeFetch({ github, zulip }) {
  const calls = { github: [], zulip: [] };
  globalThis.fetch = async (url, init) => {
    const target = String(url).includes("api.github.com") ? "github" : "zulip";
    calls[target].push({ url, init });
    const responder = target === "github" ? github : zulip;
    return typeof responder === "function" ? responder() : responder;
  };
  return calls;
}

/** A KV namespace stub that records what the Worker wrote. */
function kvStub(initial = {}) {
  const store = new Map(Object.entries(initial));
  return {
    store,
    async get(key) {
      return store.has(key) ? store.get(key) : null;
    },
    async put(key, value) {
      store.set(key, value);
    },
    async delete(key) {
      store.delete(key);
    },
  };
}

describe("postZulip", () => {
  it("posts a form-encoded message to the Alerts stream", async () => {
    const calls = stubFetch(jsonResponse({ result: "success", id: 7 }));

    const ok = await postZulip(ENV, "pr-conflicts BotFleet", "hello");

    assert.equal(ok, true);
    assert.equal(calls.length, 1);
    const { url, init } = calls[0];
    assert.equal(url, "https://zulip.example.com/api/v1/messages");
    assert.equal(init.method, "POST");
    assert.equal(init.headers["Content-Type"], "application/x-www-form-urlencoded");
    assert.equal(init.headers.Authorization, `Basic ${btoa("alerts-bot@example.com:zulip-api-key")}`);

    const fields = new URLSearchParams(init.body);
    assert.equal(fields.get("type"), "stream");
    assert.equal(fields.get("to"), "Alerts");
    assert.equal(fields.get("topic"), "pr-conflicts BotFleet");
    assert.equal(fields.get("content"), "hello");
  });

  it("fails when Zulip answers with an HTTP error", async () => {
    stubFetch(jsonResponse({ result: "error", msg: "Invalid API key" }, 401));
    const errors = [];
    console.error = (...args) => errors.push(args.join(" "));

    const ok = await postZulip(ENV, "pr-conflicts BotFleet", "hello");

    assert.equal(ok, false);
    assert.equal(errors.length, 1);
    assert.match(errors[0], /401/);
    assert.match(errors[0], /Invalid API key/);
    assert.ok(!errors[0].includes("zulip-api-key"), "the api key was logged");
    assert.ok(!errors[0].includes("alerts-bot@example.com"), "the bot email was logged");
  });

  it("fails when Zulip answers HTTP 200 without result success", async () => {
    stubFetch(jsonResponse({ result: "error", msg: "Topic not found" }));
    const errors = [];
    console.error = (...args) => errors.push(args.join(" "));

    const ok = await postZulip(ENV, "pr-conflicts BotFleet", "hello");

    assert.equal(ok, false);
    assert.match(errors[0], /Topic not found/);
  });

  it("does not throw when the response body is not JSON", async () => {
    stubFetch(new Response("gateway timeout", { status: 504 }));
    const errors = [];
    console.error = (...args) => errors.push(args.join(" "));

    const ok = await postZulip(ENV, "pr-conflicts BotFleet", "hello");

    assert.equal(ok, false);
    assert.match(errors[0], /504/);
  });
});

describe("parseZulipResult", () => {
  it("accepts a result of success", () => {
    assert.deepEqual(parseZulipResult({ result: "success", id: 7 }), { ok: true });
  });

  it("reports Zulip's own error message", () => {
    assert.deepEqual(parseZulipResult({ result: "error", msg: "Invalid API key" }), {
      ok: false,
      msg: "Invalid API key",
    });
  });

  it("rejects anything that is not a plain object", () => {
    for (const value of [null, undefined, 7, "success", true, ["result"]]) {
      assert.deepEqual(parseZulipResult(value), {
        ok: false,
        msg: "unparseable Zulip response",
      });
    }
  });

  it("rejects a result that is not a string", () => {
    assert.deepEqual(parseZulipResult({ result: 1 }), {
      ok: false,
      msg: "unparseable Zulip response",
    });
    assert.deepEqual(parseZulipResult({}), {
      ok: false,
      msg: "unparseable Zulip response",
    });
  });

  it("rejects a msg that is not a string", () => {
    assert.deepEqual(parseZulipResult({ result: "error", msg: { text: "boom" } }), {
      ok: false,
      msg: "unparseable Zulip response",
    });
  });

  it("fails the post when the body is not a Zulip result", async () => {
    stubFetch(new Response("<html>gateway</html>", { status: 200 }));
    const errors = [];
    console.error = (...args) => errors.push(args.join(" "));

    const ok = await postZulip(ENV, "pr-conflicts BotFleet", "hello");

    assert.equal(ok, false);
    assert.match(errors[0], /unparseable Zulip response/);
  });
});

describe("zulipMessagesUrl", () => {
  it("builds the messages endpoint and drops a trailing slash", () => {
    assert.equal(zulipMessagesUrl({ ZULIP_SITE: "https://zulip.example.com" }), "https://zulip.example.com/api/v1/messages");
    assert.equal(zulipMessagesUrl({ ZULIP_SITE: "https://zulip.example.com/" }), "https://zulip.example.com/api/v1/messages");
  });
});

describe("topicForRepo", () => {
  it("names the topic after the repository", () => {
    assert.equal(topicForRepo("BotFleet"), "pr-conflicts BotFleet");
  });
});

describe("buildMessage", () => {
  it("mentions the author seat for a new conflict", () => {
    const message = buildMessage({ kind: "conflict", repo: "BotFleet", pr: PR });

    assert.equal(
      message,
      "PR [#123 Fix the thing](https://github.com/Simple-With-Us/BotFleet/pull/123) on `BotFleet` " +
        `has a merge conflict with \`main\`.${GAP}@**GB-Deployer** please rebase it.`
    );
  });

  it("falls back to the author login and branch when the seat is unknown", () => {
    const pr = { ...PR, head: { ref: "someone-else/afc-123-fix-thing" } };
    const message = buildMessage({ kind: "conflict", repo: "BotFleet", pr });

    assert.match(message, /the author \(`jaywedgeworth22`, branch `someone-else\/afc-123-fix-thing`\) please rebase it\./);
    assert.ok(!message.includes("@**"), "an unknown seat must not be mentioned");
  });

  it("escapes backticks and brackets in the title", () => {
    const pr = { ...PR, title: "Fix `code` and [brackets]" };
    const message = buildMessage({ kind: "conflict", repo: "BotFleet", pr });

    assert.ok(
      message.includes("PR [#123 Fix \\`code\\` and \\[brackets\\]]"),
      `title was not escaped: ${message}`
    );
  });

  it("names nobody for a recovered conflict", () => {
    const message = buildMessage({ kind: "recovered", repo: "BotFleet", pr: PR, state: "clean" });

    assert.equal(
      message,
      "PR [#123](https://github.com/Simple-With-Us/BotFleet/pull/123) on `BotFleet` no longer conflicts (now `clean`)."
    );
    assert.ok(!message.includes("@**"), "a recovery must not mention a seat");
  });

  it("never mentions @all or @everyone", () => {
    const prs = [PR, { ...PR, head: { ref: "grokbot/x" } }, { ...PR, head: { ref: "nope/x" } }];
    for (const pr of prs) {
      for (const kind of ["conflict", "recovered"]) {
        const message = buildMessage({ kind, repo: "BotFleet", pr, state: "clean" });
        assert.ok(!/@all\b/i.test(message), message);
        assert.ok(!/@everyone\b/i.test(message), message);
      }
    }
  });

  it("keeps the seat mention it adds itself intact", () => {
    const message = buildMessage({ kind: "conflict", repo: "BotFleet", pr: PR });

    assert.ok(message.includes("@**GB-Deployer**"), message);
  });

  it("neutralizes a mention that the PR title carries", () => {
    const pr = { ...PR, title: "@**all** ship it" };
    const message = buildMessage({ kind: "conflict", repo: "BotFleet", pr });

    assert.ok(!message.includes("@**all**"), message);
    assert.ok(message.includes("@\u200B**all**"), message);
  });

  it("neutralizes mentions in the base ref, the head ref and the login", () => {
    const pr = {
      ...PR,
      base: { ref: "@**all**" },
      head: { ref: "someone-else/@**all**" },
      user: { login: "@**all**" },
    };
    const message = buildMessage({ kind: "conflict", repo: "BotFleet", pr });

    assert.ok(!message.includes("@**all**"), message);
    assert.ok(message.includes("@\u200B**all**"), message);
  });
});

describe("neutralizeMentions", () => {
  const ZWSP = "\u200B";

  it("breaks the mention starters", () => {
    assert.equal(neutralizeMentions("@**all**"), `@${ZWSP}**all**`);
    assert.equal(neutralizeMentions("@_**all**"), `@${ZWSP}_**all**`);
    assert.equal(neutralizeMentions("@*all*"), `@${ZWSP}*all*`);
  });

  it("breaks the bare group mentions", () => {
    for (const group of ["all", "everyone", "channel", "stream", "topic"]) {
      assert.equal(neutralizeMentions(`@${group}`), `@${ZWSP}${group}`);
      assert.equal(neutralizeMentions(`ping @${group} now`), `ping @${ZWSP}${group} now`);
    }
  });

  it("leaves text that is not a mention alone", () => {
    assert.equal(neutralizeMentions("Fix @home the thing"), "Fix @home the thing");
    assert.equal(neutralizeMentions("mail me at a@b.test"), "mail me at a@b.test");
    assert.equal(neutralizeMentions(""), "");
  });
});

describe("handlePr", () => {
  const GITHUB_PR = {
    state: "open",
    mergeable_state: "dirty",
    number: 123,
    title: "Fix the thing",
    html_url: "https://github.com/Simple-With-Us/BotFleet/pull/123",
    base: { ref: "main" },
    head: { ref: "gb-deployer/afc-123-fix-thing" },
    user: { login: "jaywedgeworth22" },
  };
  const KEY = "state:Simple-With-Us/BotFleet#123";

  it("stores the dedupe key only after Zulip accepted the alert", async () => {
    const kv = kvStub();
    routeFetch({
      github: () => jsonResponse(GITHUB_PR),
      zulip: () => jsonResponse({ result: "success", id: 7 }),
    });

    await handlePr("Simple-With-Us", "BotFleet", 123, { ...ENV, PR_STATE: kv });

    assert.equal(kv.store.get(KEY), "dirty");
  });

  it("leaves the KV untouched when the alert post fails", async () => {
    console.error = () => {};
    const kv = kvStub();
    routeFetch({
      github: () => jsonResponse(GITHUB_PR),
      zulip: () => jsonResponse({ result: "error", msg: "Invalid API key" }, 401),
    });

    await handlePr("Simple-With-Us", "BotFleet", 123, { ...ENV, PR_STATE: kv });

    assert.equal(kv.store.size, 0, "a failed alert must not claim the dedupe key");
  });

  it("clears the dedupe key only after Zulip accepted the recovery", async () => {
    const kv = kvStub({ [KEY]: "dirty" });
    routeFetch({
      github: () => jsonResponse({ ...GITHUB_PR, mergeable_state: "clean" }),
      zulip: () => jsonResponse({ result: "success", id: 8 }),
    });

    await handlePr("Simple-With-Us", "BotFleet", 123, { ...ENV, PR_STATE: kv });

    assert.equal(kv.store.size, 0);
  });

  it("keeps the dedupe key when the recovery post fails", async () => {
    console.error = () => {};
    const kv = kvStub({ [KEY]: "dirty" });
    routeFetch({
      github: () => jsonResponse({ ...GITHUB_PR, mergeable_state: "clean" }),
      zulip: () => jsonResponse({ result: "error", msg: "Invalid API key" }, 401),
    });

    await handlePr("Simple-With-Us", "BotFleet", 123, { ...ENV, PR_STATE: kv });

    assert.equal(kv.store.get(KEY), "dirty", "a failed recovery must stay retryable");
  });
});

describe("missingSecretNames", () => {
  it("names every secret that was not provided", () => {
    assert.deepEqual(missingSecretNames(ENV), []);
    assert.deepEqual(missingSecretNames({ ...ENV, ZULIP_BOT_API_KEY: "" }), ["ZULIP_BOT_API_KEY"]);
    assert.deepEqual(missingSecretNames({ ...ENV, GITHUB_TOKEN: undefined }), ["GITHUB_TOKEN"]);
    assert.deepEqual(missingSecretNames({}), [
      "GITHUB_WEBHOOK_SECRET",
      "GITHUB_TOKEN",
      "ZULIP_SITE",
      "ZULIP_BOT_EMAIL",
      "ZULIP_BOT_API_KEY",
    ]);
  });

  it("fails the request with the missing name, never a value", async () => {
    const env = { ...ENV, ZULIP_BOT_EMAIL: undefined };
    const res = await worker.fetch(new Request("https://pr-conflict-watch.example.com/", { method: "POST" }), env, {
      waitUntil() {},
    });

    assert.equal(res.status, 500);
    const body = await res.text();
    assert.equal(body, "pr-conflict-watch misconfigured: missing ZULIP_BOT_EMAIL");
    assert.ok(!body.includes("zulip-api-key"));
  });

  it("answers a health probe before the config check", async () => {
    const res = await worker.fetch(new Request("https://pr-conflict-watch.example.com/"), {}, {
      waitUntil() {},
    });

    assert.equal(res.status, 200);
    assert.equal(await res.text(), "ok");
  });

  it("answers a health probe on a configured Worker too", async () => {
    const res = await worker.fetch(new Request("https://pr-conflict-watch.example.com/"), ENV, {
      waitUntil() {},
    });

    assert.equal(res.status, 200);
    assert.equal(await res.text(), "ok");
  });
});

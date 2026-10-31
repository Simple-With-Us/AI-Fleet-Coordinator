/* Nonsensitive behavioral fixture for mac-protocol.h.  Scripted transport, synthetic token,
 * no Keychain, no network, no credentials.  Compiles and runs on any POSIX C99 toolchain. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include "mac-protocol.h"

#define TOKEN "sbt_echo_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

static int failures;
#define CHECK(name, cond) do { \
    if (!(cond)) { ++failures; printf("FAIL: %s\n", name); } \
    else printf("ok: %s\n", name); \
} while (0)

/* -------------------------------------------------- scripted transport */
#define MAX_REQ 8
static struct { unsigned char frame[MP_MAX_FILE_FRAME + 64]; size_t len; char auth[64];
                const char *session; const char *protocol; int calls; } captured;
static mp_http_response script[MAX_REQ];
static int script_len, script_at, transport_fail_at = -1;
static unsigned char script_body[MP_MAX_BYTES + 65536];

static void script_reset(void) {
    memset(&captured, 0, sizeof(captured));
    script_len = script_at = 0; transport_fail_at = -1;
}

static void script_push(long status, const char *session, const char *type, const char *body) {
    mp_http_response *r = &script[script_len++];
    r->status = status;
    r->session_header = session;
    r->content_type = type;
    size_t n = strlen(body);
    memcpy(script_body, body, n);
    r->body = script_body;
    r->body_len = n;
}

static int fake_post(void *ctx, const unsigned char *frame, size_t len,
                     const mp_headers *headers, mp_http_response *out) {
    (void)ctx;
    ++captured.calls;
    memcpy(captured.frame, frame, len);
    captured.len = len;
    strncpy(captured.auth, headers->authorization, sizeof(captured.auth) - 1);
    captured.session = headers->session;
    captured.protocol = headers->protocol;
    if (transport_fail_at >= 0 && captured.calls == transport_fail_at) return 1;
    *out = script[script_at < script_len ? script_at++ : script_len - 1];
    return 0;
}

static int silent_post(void *ctx, const unsigned char *frame, size_t len,
                       const mp_headers *headers, mp_http_response *out) {
    /* Minimal real transport:  optional fields simply never assigned, so the core's
     * zero-initialized boundary is what makes omission deterministic. */
    return fake_post(ctx, frame, len, headers, out);
}

static int partial_post(void *ctx, const unsigned char *frame, size_t len,
                        const mp_headers *headers, mp_http_response *out) {
    /* Sets status and body but never assigns content_type:  the core boundary must turn
     * that omission into a deterministic NULL and refuse, never a garbage read. */
    (void)ctx; (void)frame; (void)len; (void)headers;
    static unsigned char body[] = "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}";
    out->status = 200;
    out->body = body;
    out->body_len = sizeof(body) - 1;
    return 0;
}

static mp_session sess;
static unsigned char scratch[MP_MAX_FILE_FRAME + 64];
static unsigned char out[MP_MAX_BYTES + 4096];

static size_t handle(const char *frame) {
    size_t len = strlen(frame);
    return mp_handle_frame(&sess, TOKEN, 52, (const unsigned char *)frame, len,
                           fake_post, NULL, scratch, sizeof(scratch), out, sizeof(out));
}

int main(void) {
    /* 1. initialize: session + protocol learned, response verbatim */
    script_reset();
    script_push(200, "sess-abc", "application/json",
        "{\"jsonrpc\":\"2.0\",\"id\":0,\"result\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{}}}");
    memset(&sess, 0, sizeof(sess));
    size_t n = handle("{\"jsonrpc\":\"2.0\",\"id\":0,\"method\":\"initialize\",\"params\":{}}");
    CHECK("initialize returns the result line",
          n && strstr((char *)out, "\"id\":0") && strstr((char *)out, "protocolVersion"));
    CHECK("session learned", sess.session_len && !strcmp(sess.session, "sess-abc"));
    CHECK("protocol learned", sess.has_protocol && !strcmp(sess.protocol, "2025-03-26"));
    CHECK("bearer header assembled", !strcmp(captured.auth, "Bearer " TOKEN));

    /* 2. notification: 202 -> no output line */
    script_reset();
    script_push(202, NULL, NULL, "");
    n = handle("{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}");
    CHECK("notification 202 silent", n == 0);

    /* 3. second request carries session + protocol headers */
    script_reset();
    script_push(200, NULL, "application/json", "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{}}}");
    CHECK("call returns line", n && strstr((char *)out, "\"ok\":true"));
    CHECK("session header sent", captured.session && !strcmp(captured.session, "sess-abc"));
    CHECK("protocol header sent", captured.protocol && !strcmp(captured.protocol, "2025-03-26"));

    /* 4. identity injection: empty args */
    CHECK("empty-args wire valid + injected",
          strstr((char *)captured.frame, "\"arguments\":{\"identity\":\"ECHO\"}") != NULL);

    /* 5. injection into nonempty args: comma placement */
    script_reset();
    script_push(200, NULL, "application/json", "{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"ok\":true}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{\"repo\":\"o/r\"}}}");
    CHECK("nonempty-args injection valid",
          strstr((char *)captured.frame, "{\"identity\":\"ECHO\",\"repo\":\"o/r\"}") != NULL);
    CHECK("rebuilt wire parses", mp_js_canonical(captured.frame, captured.len));

    /* 6. arguments absent: spliced into params */
    script_reset();
    script_push(200, NULL, "application/json", "{\"jsonrpc\":\"2.0\",\"id\":3,\"result\":{\"ok\":true}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\"}}");
    CHECK("missing-args injection valid",
          strstr((char *)captured.frame, "\"arguments\":{\"identity\":\"ECHO\"}") != NULL &&
          mp_js_canonical(captured.frame, captured.len));

    /* 7. empty params object */
    script_reset();
    script_push(200, NULL, "application/json", "{\"jsonrpc\":\"2.0\",\"id\":4,\"result\":{\"ok\":true}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{}}");
    CHECK("empty-params injection valid", mp_js_canonical(captured.frame, captured.len));

    /* 8. explicit ECHO identity passes unmodified */
    script_reset();
    script_push(200, NULL, "application/json", "{\"jsonrpc\":\"2.0\",\"id\":5,\"result\":{\"ok\":true}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{\"identity\":\"ECHO\"}}}");
    CHECK("explicit ECHO unchanged",
          strstr((char *)captured.frame, "\"arguments\":{\"identity\":\"ECHO\"}") != NULL);

    /* 9. explicit WISE identity refused before any POST */
    script_reset();
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":6,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{\"identity\":\"WISE\"}}}");
    CHECK("foreign identity refused", n && strstr((char *)out, "identity_not_allowed"));
    CHECK("no POST for refused identity", captured.calls == 0);

    /* 10. duplicate decoded keys refused at intake (exact host repro) */
    script_reset();
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{\"identity\":\"ECHO\",\"identity\":\"WISE\"}}}");
    CHECK("duplicate identity refused", n && captured.calls == 0);
    /* 11. escaped-twin duplicate refused */
    script_reset();
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":8,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{\"identity\":\"ECHO\",\"identit\\u0079\":\"WISE\"}}}");
    CHECK("escaped duplicate refused", n && captured.calls == 0);

    /* 12. identities tool: no injection */
    script_reset();
    script_push(200, NULL, "application/json", "{\"jsonrpc\":\"2.0\",\"id\":9,\"result\":{\"identities\":[]}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"tools/call\",\"params\":{\"name\":\"identities\",\"arguments\":{}}}");
    CHECK("identities tool not injected",
          strstr((char *)captured.frame, "\"identity\"") == NULL);

    /* 13. non-200 on a call: outcome_unknown shape, no provider prose */
    script_reset();
    script_push(503, NULL, "text/plain", "proxy exploded with details");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":10,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{}}}");
    CHECK("503 -> outcome_unknown", n && strstr((char *)out, "outcome_unknown"));
    CHECK("provider prose scrubbed", !strstr((char *)out, "proxy exploded"));

    /* 14. non-200 on initialize: -32000 */
    script_reset();
    memset(&sess, 0, sizeof(sess));
    script_push(500, NULL, "application/json", "{}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":0,\"method\":\"initialize\",\"params\":{}}");
    CHECK("initialize 500 -> -32000", n && strstr((char *)out, "-32000"));

    /* 15. transport failure: outcome_unknown, no retry (exactly one call) */
    script_reset();
    transport_fail_at = 1;
    script_push(200, NULL, "application/json", "{}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":11,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{}}}");
    CHECK("transport failure -> outcome_unknown", n && strstr((char *)out, "outcome_unknown"));
    CHECK("exactly one attempt", captured.calls == 1);

    /* 16. token echo in response body: failure */
    script_reset();
    script_push(200, NULL, "application/json",
        "{\"jsonrpc\":\"2.0\",\"id\":12,\"result\":{\"leak\":\"" TOKEN "\"}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":12,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{}}}");
    CHECK("token echo refused", n && strstr((char *)out, "outcome_unknown"));

    /* 17. session mismatch mid-stream: failure */
    script_reset();
    script_push(200, "sess-OTHER", "application/json", "{\"jsonrpc\":\"2.0\",\"id\":13,\"result\":{}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":13,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{}}}");
    CHECK("session swap refused", n && strstr((char *)out, "outcome_unknown"));

    /* 18. bad content type: failure */
    script_reset();
    script_push(200, NULL, "text/html", "<html></html>");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":14,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{}}}");
    CHECK("bad content type refused", n && strstr((char *)out, "outcome_unknown"));

    /* 19. SSE: progress event then result (exact host repro) */
    script_reset();
    script_push(200, NULL, "text/event-stream",
        "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\ndata: {\"jsonrpc\":\"2.0\",\"id\":15,\"result\":{\"ok\":true}}\n\n");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":15,\"method\":\"tools/call\",\"params\":{\"name\":\"identities\",\"arguments\":{}}}");
    CHECK("SSE second event found", n && strstr((char *)out, "\"ok\":true"));

    /* 20. SSE: multi-line data joins */
    script_reset();
    script_push(200, NULL, "text/event-stream",
        "data: {\"jsonrpc\":\"2.0\",\"id\":16,\ndata: \"result\":{\"joined\":1}}\n\n");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":16,\"method\":\"tools/call\",\"params\":{\"name\":\"identities\",\"arguments\":{}}}");
    CHECK("SSE multiline join", n && strstr((char *)out, "\"joined\":1"));

    /* 21. SSE: no matching id -> failure */
    script_reset();
    script_push(200, NULL, "text/event-stream",
        "data: {\"jsonrpc\":\"2.0\",\"id\":999,\"result\":{}}\n\n");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":17,\"method\":\"tools/call\",\"params\":{\"name\":\"identities\",\"arguments\":{}}}");
    CHECK("SSE no match refused", n && strstr((char *)out, "outcome_unknown"));

    /* 22. response payload with duplicate keys refused */
    script_reset();
    script_push(200, NULL, "application/json",
        "{\"jsonrpc\":\"2.0\",\"id\":18,\"id\":19,\"result\":{}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":18,\"method\":\"tools/call\",\"params\":{\"name\":\"identities\",\"arguments\":{}}}");
    CHECK("dup-key response refused", n && strstr((char *)out, "outcome_unknown"));

    /* 23. oversized response body: failure without reading it */
    script_reset();
    script_push(200, NULL, "application/json", "{}");
    script[0].body_len = MP_MAX_BYTES + 1;
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":20,\"method\":\"tools/call\",\"params\":{\"name\":\"identities\",\"arguments\":{}}}");
    CHECK("oversize response refused", n && strstr((char *)out, "outcome_unknown"));

    /* 24. malformed frame -> -32700 */
    script_reset();
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":21,\"method\":");
    CHECK("malformed frame -32700", n && strstr((char *)out, "-32700"));
    CHECK("malformed frame never posted", captured.calls == 0);

    /* 25. wrong jsonrpc version -> -32700 */
    n = handle("{\"jsonrpc\":\"1.0\",\"id\":22,\"method\":\"initialize\",\"params\":{}}");
    CHECK("wrong version -32700", n && strstr((char *)out, "-32700"));

    /* 26. oversized ordinary frame -> -32600 (4MiB + 1 of padding) */
    {
        static unsigned char big[MP_MAX_BYTES + 2048];
        static const char prefix[] = "{\"jsonrpc\":\"2.0\",\"id\":23,\"method\":\"x\",\"params\":\"";
        size_t len = sizeof(prefix) - 1;
        memcpy(big, prefix, len);
        memset(big + len, 'a', MP_MAX_BYTES - len + 100); len = MP_MAX_BYTES + 100;
        memcpy(big + len, "\"}", 2); len += 2;
        n = mp_handle_frame(&sess, TOKEN, 52, big, len, fake_post, NULL,
                            scratch, sizeof(scratch), out, sizeof(out));
        CHECK("oversize ordinary frame -32600", n && strstr((char *)out, "-32600"));
    }

    /* 27. initialize with non-printable protocolVersion: failure */
    script_reset();
    memset(&sess, 0, sizeof(sess));
    script_push(200, NULL, "application/json",
        "{\"jsonrpc\":\"2.0\",\"id\":0,\"result\":{\"protocolVersion\":\"bad version!\"}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":0,\"method\":\"initialize\",\"params\":{}}");
    CHECK("bad protocolVersion refused", n && strstr((char *)out, "-32000"));

    /* 28. result+error both present: not a match */
    script_reset();
    script_push(200, NULL, "application/json",
        "{\"jsonrpc\":\"2.0\",\"id\":24,\"result\":{},\"error\":{}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":24,\"method\":\"tools/call\",\"params\":{\"name\":\"identities\",\"arguments\":{}}}");
    CHECK("result+error ambiguous refused", n && strstr((char *)out, "outcome_unknown"));

    /* 29. notification on transport failure: still silent */
    script_reset();
    transport_fail_at = 1;
    n = handle("{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}");
    CHECK("notification failure silent", n == 0);

    /* 31. response fields omitted by the transport:  deterministic absence.
     * silent_post poisons the struct then runs the normal scripted flow;  a 200 JSON
     * result must still come back (content_type set by the script), and a notification
     * with no content_type at all must stay silent rather than read garbage. */
    script_reset();
    script_push(202, NULL, NULL, "");
    static const char notif31[] =
        "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}";
    n = mp_handle_frame(&sess, TOKEN, 52, (const unsigned char *)notif31, sizeof(notif31) - 1,
        silent_post, NULL, scratch, sizeof(scratch), out, sizeof(out));
    CHECK("poisoned-response notification silent", n == 0);

    /* 32. 200 with body but no content_type field:  refusal, never a garbage read */
    memset(&sess, 0, sizeof(sess));
    static const char call32[] =
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"identities\",\"arguments\":{}}}";
    n = mp_handle_frame(&sess, TOKEN, 52, (const unsigned char *)call32, sizeof(call32) - 1,
        partial_post, NULL, scratch, sizeof(scratch), out, sizeof(out));
    CHECK("omitted content-type refused", n && strstr((char *)out, "outcome_unknown"));

    /* 30. redirect policy wired off */
    CHECK("redirects never followed", mp_redirect_allowed() == 0);

    /* 33. PR 1208 review (kody, HIGH):  escaped spellings of protocol constants must
     * classify exactly as their decoded form, so identity binding cannot be bypassed
     * with \u escapes.  Unit level first (mp_js_str_eq decodes like mp_key_decode). */
    {
        static const char esc_call[] = "\"tools\\u002fcall\"";
        CHECK("str_eq decodes \\u escape", mp_js_str_eq((const unsigned char *)esc_call, 0, sizeof(esc_call) - 1, "tools/call"));
        static const char esc_surr[] = "\"\\ud83d\\ude00\"";
        CHECK("str_eq decodes surrogate pair", mp_js_str_eq((const unsigned char *)esc_surr, 0, sizeof(esc_surr) - 1, "\xf0\x9f\x98\x80"));
        static const char esc_bad[] = "\"\\u00zz\"";
        CHECK("str_eq malformed escape unequal", !mp_js_str_eq((const unsigned char *)esc_bad, 0, sizeof(esc_bad) - 1, "x"));
        static const char plain[] = "\"tools/call\"";
        CHECK("str_eq plain still equal", mp_js_str_eq((const unsigned char *)plain, 0, sizeof(plain) - 1, "tools/call"));
        CHECK("str_eq plain still unequal", !mp_js_str_eq((const unsigned char *)plain, 0, sizeof(plain) - 1, "tools/list"));
    }

    /* 34. escaped method VALUE is still a call:  identity injected, frame posted */
    script_reset();
    script_push(200, NULL, "application/json", "{\"jsonrpc\":\"2.0\",\"id\":40,\"result\":{\"ok\":true}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":40,\"method\":\"tools\\u002fcall\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{}}}");
    CHECK("escaped method still a call", n && strstr((char *)out, "\"ok\":true"));
    CHECK("escaped method gets identity injected",
          strstr((char *)captured.frame, "\"identity\":\"ECHO\"") != NULL);

    /* 35. escaped method KEY is still found:  identity injected */
    script_reset();
    script_push(200, NULL, "application/json", "{\"jsonrpc\":\"2.0\",\"id\":41,\"result\":{\"ok\":true}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":41,\"metho\\u0064\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{}}}");
    CHECK("escaped method key still a call", n && strstr((char *)out, "\"ok\":true"));
    CHECK("escaped method key gets identity injected",
          strstr((char *)captured.frame, "\"identity\":\"ECHO\"") != NULL);

    /* 36. escaped ECHO identity value passes (it decodes to the fixed identity) */
    script_reset();
    script_push(200, NULL, "application/json", "{\"jsonrpc\":\"2.0\",\"id\":42,\"result\":{\"ok\":true}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":42,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{\"identity\":\"ECH\\u004f\"}}}");
    CHECK("escaped ECHO identity accepted", n && strstr((char *)out, "\"ok\":true") && captured.calls == 1);

    /* 37. escaped FOREIGN identity still refused before any POST */
    script_reset();
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":43,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"arguments\":{\"identity\":\"WIS\\u0045\"}}}");
    CHECK("escaped foreign identity refused", n && strstr((char *)out, "identity_not_allowed"));
    CHECK("no POST for escaped foreign identity", captured.calls == 0);

    /* 38. escaped arguments KEY does not hide a foreign identity */
    script_reset();
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":44,\"method\":\"tools/call\",\"params\":{\"name\":\"gr_wf_repo_get\",\"argument\\u0073\":{\"identity\":\"WISE\"}}}");
    CHECK("escaped arguments key refused", n && strstr((char *)out, "identity_not_allowed"));
    CHECK("no POST via escaped arguments key", captured.calls == 0);

    /* 39. escaped identities name still skips injection (it IS the identities verb) */
    script_reset();
    script_push(200, NULL, "application/json", "{\"jsonrpc\":\"2.0\",\"id\":45,\"result\":{\"ok\":true}}");
    n = handle("{\"jsonrpc\":\"2.0\",\"id\":45,\"method\":\"tools/call\",\"params\":{\"name\":\"identitie\\u0073\",\"arguments\":{}}}");
    CHECK("escaped identities name not injected",
          n && strstr((char *)captured.frame, "\"identity\"") == NULL);

    /* 40. PR 1208 review (kody, performance):  the duplicate scan decodes each key once.
     * Functional verdicts are unchanged:  large many-key objects pass, an exact
     * duplicate near the end still refuses, an escaped twin still refuses. */
    {
        static unsigned char many[65536];
        size_t m = 0;
        many[m++] = '{';
        for (int k = 0; k < 300; ++k)
            m += (size_t)snprintf((char *)many + m, sizeof(many) - m, "%s\"k%d\":%d", k ? "," : "", k, k);
        many[m++] = '}';
        CHECK("300 distinct keys canonical", mp_js_canonical(many, m));
        /* sizeof-sized tails, grammar asserted valid first:  a refusal below is the
         * duplicate, never a malformed frame. */
        static const char dup_tail[] = ",\"k299\":0}";
        memcpy(many + m - 1, dup_tail, sizeof(dup_tail) - 1);
        size_t dm = m - 1 + sizeof(dup_tail) - 1;
        CHECK("trailing-duplicate frame grammar valid", mp_js_valid(many, dm));
        CHECK("trailing duplicate still refused", !mp_js_canonical(many, dm));
        static const char twin_tail[] = ",\"k29\\u0039\":0}";
        memcpy(many + m - 1, twin_tail, sizeof(twin_tail) - 1);
        size_t tm = m - 1 + sizeof(twin_tail) - 1;
        CHECK("escaped-twin frame grammar valid", mp_js_valid(many, tm));
        CHECK("trailing escaped twin still refused", !mp_js_canonical(many, tm));
    }

    /* 41. Max-key bound:  MP_MAX_KEYS (1024) distinct keys pass;  a 1025th key, or a
     * genuine duplicate scanned at the bound, refuses. */
    {
        static unsigned char wide[65536];
        size_t w = 0;
        wide[w++] = '{';
        for (int k = 0; k < 1024; ++k)
            w += (size_t)snprintf((char *)wide + w, sizeof(wide) - w, "%s\"k%d\":%d", k ? "," : "", k, k);
        wide[w++] = '}';
        CHECK("1024 distinct keys canonical", mp_js_canonical(wide, w));
        static const char over_tail[] = ",\"k1024\":0}";
        memcpy(wide + w - 1, over_tail, sizeof(over_tail) - 1);
        CHECK("1025 keys refused", !mp_js_canonical(wide, w - 1 + sizeof(over_tail) - 1));
        w = 0;
        wide[w++] = '{';
        for (int k = 0; k < 1023; ++k)
            w += (size_t)snprintf((char *)wide + w, sizeof(wide) - w, "%s\"k%d\":%d", k ? "," : "", k, k);
        wide[w++] = '}';
        static const char bound_dup[] = ",\"k0\":0}";
        memcpy(wide + w - 1, bound_dup, sizeof(bound_dup) - 1);
        size_t bm = w - 1 + sizeof(bound_dup) - 1;
        CHECK("bound-duplicate frame grammar valid", mp_js_valid(wide, bm));
        CHECK("duplicate scanned at the 1024-key bound refused", !mp_js_canonical(wide, bm));
    }

    printf(failures ? "\n%d FAILURES\n" : "\nALL FIXTURE CHECKS PASS\n", failures);
    return failures ? 1 : 0;
}

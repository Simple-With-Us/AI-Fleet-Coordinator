/* Assigned ECHO request-owning protocol core.  Pure C99, no frameworks, no secrets at rest:
 * the bearer lives only in caller-owned native memory and every secret-bearing buffer is
 * wiped with mp_wipe.  The SAME source is compiled into the signed Mac helper (URLSession
 * transport) and into the nonsensitive fixture (scripted transport), so fixture results
 * cover the exact protocol bytes the signed build emits. */

#ifndef MAC_PROTOCOL_H
#define MAC_PROTOCOL_H

/* Single-header implementation:  each translation unit includes its own copy, so the
 * public entry points carry per-TU unused annotations;  every helper stays referenced
 * from their bodies.  No broad warning suppression. */
#if defined(__GNUC__)
#define MP_MAYBE_UNUSED __attribute__((unused))
#else
#define MP_MAYBE_UNUSED
#endif

#include <stddef.h>
#include <stdio.h>
#include <string.h>

#define MP_MAX_BYTES (4u * 1024u * 1024u)
#define MP_MAX_FILE_ARGS (6u * 1024u * 1024u)
#define MP_MAX_FILE_FRAME (MP_MAX_FILE_ARGS + 65536u)
#define MP_MAX_SESSION 256u
#define MP_MAX_PROTOCOL 128u
#define MP_FIXED_IDENTITY "ECHO"
#define MP_ENDPOINT "https://agents.simplewithus.com/mcp"

static void mp_wipe(void *pointer, size_t size) {
    volatile unsigned char *bytes = (volatile unsigned char *)pointer;
    while (size--) *bytes++ = 0;
}

/* The helper never follows a redirect: the fixed endpoint answers directly or fails. */
MP_MAYBE_UNUSED
static int mp_redirect_allowed(void) { return 0; }

/* ------------------------------------------------------------- strict JSON */
/* Bounded strict JSON: enough structure to validate frames and locate fields.
 * Depth-capped, no allocation, no number materialization (ids compared raw). */

static int mp_js_ws(const unsigned char *t, size_t n, size_t *p) {
    while (*p < n && (t[*p] == ' ' || t[*p] == '\t' || t[*p] == '\r' || t[*p] == '\n')) ++*p;
    return 1;
}

static int mp_js_string(const unsigned char *t, size_t n, size_t *p) {
    if (*p >= n || t[*p] != '"') return 0;
    ++*p;
    while (*p < n) {
        unsigned char c = t[*p];
        if (c == '"') { ++*p; return 1; }
        if (c == '\\') {
            ++*p;
            if (*p >= n) return 0;
            c = t[*p];
            if (c == 'u') {
                for (int i = 0; i < 4; ++i) {
                    ++*p;
                    if (*p >= n || !((t[*p] >= '0' && t[*p] <= '9') ||
                                     (t[*p] >= 'a' && t[*p] <= 'f') ||
                                     (t[*p] >= 'A' && t[*p] <= 'F'))) return 0;
                }
                ++*p;
            } else if (c == '"' || c == '\\' || c == '/' || c == 'b' || c == 'f' ||
                       c == 'n' || c == 'r' || c == 't') {
                ++*p;
            } else return 0;
        } else if (c < 0x20) return 0;
        else ++*p;
    }
    return 0;
}

static int mp_js_number(const unsigned char *t, size_t n, size_t *p) {
    size_t s = *p;
    if (*p < n && t[*p] == '-') ++*p;
    if (*p >= n) return 0;
    if (t[*p] == '0') ++*p;
    else if (t[*p] >= '1' && t[*p] <= '9') while (*p < n && t[*p] >= '0' && t[*p] <= '9') ++*p;
    else return 0;
    if (*p < n && t[*p] == '.') {
        ++*p;
        if (*p >= n || t[*p] < '0' || t[*p] > '9') return 0;
        while (*p < n && t[*p] >= '0' && t[*p] <= '9') ++*p;
    }
    if (*p < n && (t[*p] == 'e' || t[*p] == 'E')) {
        ++*p;
        if (*p < n && (t[*p] == '+' || t[*p] == '-')) ++*p;
        if (*p >= n || t[*p] < '0' || t[*p] > '9') return 0;
        while (*p < n && t[*p] >= '0' && t[*p] <= '9') ++*p;
    }
    return *p > s;
}

static int mp_js_value(const unsigned char *t, size_t n, size_t *p, int depth) {
    if (depth > 64) return 0;
    mp_js_ws(t, n, p);
    if (*p >= n) return 0;
    unsigned char c = t[*p];
    if (c == '"') return mp_js_string(t, n, p);
    if (c == '-' || (c >= '0' && c <= '9')) return mp_js_number(t, n, p);
    if (c == 't') { if (n - *p >= 4 && !memcmp(t + *p, "true", 4)) { *p += 4; return 1; } return 0; }
    if (c == 'f') { if (n - *p >= 5 && !memcmp(t + *p, "false", 5)) { *p += 5; return 1; } return 0; }
    if (c == 'n') { if (n - *p >= 4 && !memcmp(t + *p, "null", 4)) { *p += 4; return 1; } return 0; }
    if (c == '[') {
        ++*p; mp_js_ws(t, n, p);
        if (*p < n && t[*p] == ']') { ++*p; return 1; }
        for (;;) {
            if (!mp_js_value(t, n, p, depth + 1)) return 0;
            mp_js_ws(t, n, p);
            if (*p < n && t[*p] == ',') { ++*p; continue; }
            if (*p < n && t[*p] == ']') { ++*p; return 1; }
            return 0;
        }
    }
    if (c == '{') {
        ++*p; mp_js_ws(t, n, p);
        if (*p < n && t[*p] == '}') { ++*p; return 1; }
        for (;;) {
            mp_js_ws(t, n, p);
            if (!mp_js_string(t, n, p)) return 0;
            mp_js_ws(t, n, p);
            if (*p >= n || t[*p] != ':') return 0;
            ++*p;
            if (!mp_js_value(t, n, p, depth + 1)) return 0;
            mp_js_ws(t, n, p);
            if (*p < n && t[*p] == ',') { ++*p; continue; }
            if (*p < n && t[*p] == '}') { ++*p; return 1; }
            return 0;
        }
    }
    return 0;
}

/* Canonical strictness:  no duplicate DECODED object keys anywhere (a server or caller must
 * not smuggle "identity":"ECHO","identity":"WISE" or an escaped "identit\u0079" twin past a
 * first-key lookup).  Keys decode fully (\uXXXX with surrogate pairs, UTF-8 encoded);  an
 * object over 1024 keys or a key over 4096 decoded bytes is non-canonical and refused. */
#define MP_MAX_KEYS 1024u
#define MP_MAX_KEY_BYTES 4096u

static int mp_hex4(const unsigned char *t, size_t *p, size_t n, unsigned *out) {
    unsigned v = 0;
    for (int i = 0; i < 4; ++i) {
        ++*p;
        if (*p >= n) return 0;
        unsigned char c = t[*p];
        v <<= 4;
        if (c >= '0' && c <= '9') v |= c - '0';
        else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
        else return 0;
    }
    *out = v;
    return 1;
}

static size_t mp_utf8(unsigned cp, unsigned char *out) {
    if (cp < 0x80) { out[0] = (unsigned char)cp; return 1; }
    if (cp < 0x800) {
        out[0] = 0xc0 | (cp >> 6); out[1] = 0x80 | (cp & 0x3f); return 2;
    }
    if (cp < 0x10000) {
        out[0] = 0xe0 | (cp >> 12); out[1] = 0x80 | ((cp >> 6) & 0x3f);
        out[2] = 0x80 | (cp & 0x3f); return 3;
    }
    out[0] = 0xf0 | (cp >> 18); out[1] = 0x80 | ((cp >> 12) & 0x3f);
    out[2] = 0x80 | ((cp >> 6) & 0x3f); out[3] = 0x80 | (cp & 0x3f); return 4;
}

/* Decode a key string span (including quotes) into out;  returns decoded length or -1. */
static long mp_key_decode(const unsigned char *t, size_t span, size_t slen,
                          unsigned char *out, size_t cap) {
    size_t i = span + 1, end = span + slen - 1, n = 0;
    while (i < end) {
        unsigned char c = t[i];
        if (c == '\\') {
            ++i;
            if (i >= end) return -1;
            c = t[i];
            if (c == 'u') {
                unsigned cp;
                size_t p = i;
                if (!mp_hex4(t, &p, end, &cp)) return -1;
                i = p;
                if (cp >= 0xd800 && cp <= 0xdbff) {
                    unsigned lo;
                    if (i + 1 < end && t[i + 1] == '\\' && i + 2 < end && t[i + 2] == 'u') {
                        p = i + 2;
                        if (!mp_hex4(t, &p, end, &lo)) return -1;
                        if (lo < 0xdc00 || lo > 0xdfff) return -1;
                        cp = 0x10000 + ((cp - 0xd800) << 10) + (lo - 0xdc00);
                        i = p;
                    } else return -1;
                } else if (cp >= 0xdc00 && cp <= 0xdfff) return -1;
                if (n + 4 > cap) return -1;
                n += mp_utf8(cp, out + n);
            } else {
                if (c == 'n') c = '\n';
                else if (c == 't') c = '\t';
                else if (c == 'r') c = '\r';
                else if (c == 'b') c = '\b';
                else if (c == 'f') c = '\f';
                if (n + 1 > cap) return -1;
                out[n++] = c;
            }
        } else {
            if (n + 1 > cap) return -1;
            out[n++] = c;
        }
        ++i;
    }
    return (long)n;
}

static int mp_js_value_c(const unsigned char *t, size_t n, size_t *p, int depth);

/* 64-bit FNV-1a over a decoded key:  narrows duplicate candidates;  equality is always
 * confirmed byte-exact, so a collision can only cost work, never a wrong verdict. */
static unsigned long long mp_key_hash(const unsigned char *p, size_t n) {
    unsigned long long h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; ++i) { h ^= p[i]; h *= 1099511628211ULL; }
    return h;
}

static int mp_js_object_c(const unsigned char *t, size_t n, size_t *p, int depth) {
    ++*p;  /* at '{' */
    mp_js_ws(t, n, p);
    if (*p < n && t[*p] == '}') { ++*p; return 1; }
    size_t spans[MP_MAX_KEYS][2];
    unsigned long long hashes[MP_MAX_KEYS];
    size_t count = 0;
    for (;;) {
        mp_js_ws(t, n, p);
        size_t ks = *p;
        if (!mp_js_string(t, n, p)) return 0;
        if (count >= MP_MAX_KEYS) return 0;
        unsigned char a[MP_MAX_KEY_BYTES + 4], b[MP_MAX_KEY_BYTES + 4];
        long la = mp_key_decode(t, ks, *p - ks, a, MP_MAX_KEY_BYTES);
        if (la < 0) return 0;
        /* Each key decodes once;  an equal hash re-decodes that prior key for the exact
         * byte comparison.  Hash comparisons stay O(k^2) on k <= MP_MAX_KEYS 64-bit
         * values (bounded and cheap);  the quadratic key DECODING is avoided unless
         * hashes collide, and a collision can only cost work, never change a verdict. */
        unsigned long long ha = mp_key_hash(a, (size_t)la);
        for (size_t j = 0; j < count; ++j) {
            if (hashes[j] != ha) continue;
            long lb = mp_key_decode(t, spans[j][0], spans[j][1], b, MP_MAX_KEY_BYTES);
            if (lb < 0) return 0;
            if (la == lb && !memcmp(a, b, (size_t)la)) return 0;  /* duplicate decoded key */
        }
        spans[count][0] = ks; spans[count][1] = *p - ks;
        hashes[count] = ha;
        ++count;
        mp_js_ws(t, n, p);
        if (*p >= n || t[*p] != ':') return 0;
        ++*p;
        if (!mp_js_value_c(t, n, p, depth + 1)) return 0;
        mp_js_ws(t, n, p);
        if (*p < n && t[*p] == ',') { ++*p; continue; }
        if (*p < n && t[*p] == '}') { ++*p; return 1; }
        return 0;
    }
}

static int mp_js_value_c(const unsigned char *t, size_t n, size_t *p, int depth) {
    if (depth > 64) return 0;
    mp_js_ws(t, n, p);
    if (*p >= n) return 0;
    unsigned char c = t[*p];
    if (c == '{') return mp_js_object_c(t, n, p, depth);
    if (c == '[') {
        ++*p; mp_js_ws(t, n, p);
        if (*p < n && t[*p] == ']') { ++*p; return 1; }
        for (;;) {
            if (!mp_js_value_c(t, n, p, depth + 1)) return 0;
            mp_js_ws(t, n, p);
            if (*p < n && t[*p] == ',') { ++*p; continue; }
            if (*p < n && t[*p] == ']') { ++*p; return 1; }
            return 0;
        }
    }
    if (c == '"') return mp_js_string(t, n, p);
    if (c == '-' || (c >= '0' && c <= '9')) return mp_js_number(t, n, p);
    if (c == 't') { if (n - *p >= 4 && !memcmp(t + *p, "true", 4)) { *p += 4; return 1; } return 0; }
    if (c == 'f') { if (n - *p >= 5 && !memcmp(t + *p, "false", 5)) { *p += 5; return 1; } return 0; }
    if (c == 'n') { if (n - *p >= 4 && !memcmp(t + *p, "null", 4)) { *p += 4; return 1; } return 0; }
    return 0;
}

/* Validate the whole buffer is exactly one canonical JSON value (no duplicate decoded keys). */
static int mp_js_canonical(const unsigned char *t, size_t n) {
    size_t p = 0;
    if (!mp_js_value_c(t, n, &p, 0)) return 0;
    mp_js_ws(t, n, &p);
    return p == n;
}

/* Validate the whole buffer is exactly one JSON value (non-canonical;  exported for probes). */
MP_MAYBE_UNUSED
static int mp_js_valid(const unsigned char *t, size_t n) {
    size_t p = 0;
    if (!mp_js_value(t, n, &p, 0)) return 0;
    mp_js_ws(t, n, &p);
    return p == n;
}

/* Decoded-string equality for a string value spanning [span, span+slen) including quotes.
 * The span decodes exactly as keys decode (\uXXXX with surrogate pairs, UTF-8 encoded),
 * so an escaped spelling of a protocol constant still classifies as that constant:  an
 * escaped "tools\u002fcall" method or identity must NOT slip past classification and
 * identity binding as merely "not equal".  Malformed escapes, or a decoded value beyond
 * the key bound, are simply not equal (such a value never equals a short constant). */
static int mp_js_str_eq(const unsigned char *t, size_t span, size_t slen, const char *expect) {
    unsigned char decoded[MP_MAX_KEY_BYTES + 4];
    long n = mp_key_decode(t, span, slen, decoded, MP_MAX_KEY_BYTES);
    if (n < 0) return 0;
    size_t want = strlen(expect);
    return (size_t)n == want && !memcmp(decoded, expect, want);
}

/* Find a top-level key in a validated JSON object at obj (position of '{').
 * On hit, the value-span out-parameters receive the raw value span and the return is 1. */
static int mp_js_find(const unsigned char *t, size_t n, size_t obj, const char *key,
                      size_t *vs, size_t *vl) {
    size_t p = obj + 1;
    mp_js_ws(t, n, &p);
    if (p < n && t[p] == '}') return 0;
    for (;;) {
        size_t ks;
        mp_js_ws(t, n, &p);
        ks = p;
        if (!mp_js_string(t, n, &p)) return 0;
        size_t kl = p - ks;
        mp_js_ws(t, n, &p);
        if (p >= n || t[p] != ':') return 0;
        ++p;
        mp_js_ws(t, n, &p);
        size_t start = p;
        if (!mp_js_value(t, n, &p, 1)) return 0;
        if (mp_js_str_eq(t, ks, kl, key)) { *vs = start; *vl = p - start; return 1; }
        mp_js_ws(t, n, &p);
        if (p < n && t[p] == ',') { ++p; continue; }
        return 0;
    }
}


/* ------------------------------------------------------- session and I/O */
/* Session state learned from the initialize exchange;  mirrored on every later request. */
typedef struct {
    char session[MP_MAX_SESSION + 1];
    size_t session_len;          /* 0 = no session negotiated */
    char protocol[MP_MAX_PROTOCOL + 1];
    int has_protocol;
} mp_session;

typedef struct {
    long status;                 /* HTTP status;  < 0 means transport failure */
    const char *session_header;  /* printable, <= MP_MAX_SESSION, or NULL (transport-owned) */
    const char *content_type;    /* or NULL (transport-owned) */
    const unsigned char *body;   /* transport-owned response bytes */
    size_t body_len;
} mp_http_response;

/* Header block the core assembles for exactly one POST.  authorization holds the bearer:
 * it is core-owned native memory, wiped by the core after the call returns. */
typedef struct {
    char authorization[8 + 52 + 1];  /* "Bearer " + fixed-shape assigned token */
    const char *session;             /* NULL when no session negotiated */
    const char *protocol;            /* NULL before initialize completes */
} mp_headers;

/* One POST to the fixed endpoint.  Returns 0 on an HTTP response, nonzero on any
 * transport failure (redirects are rejected by the transport, never followed). */
typedef int (*mp_transport_post)(void *ctx, const unsigned char *frame, size_t frame_len,
                                 const mp_headers *headers, mp_http_response *out);

/* Fixed failure shapes (mirror the reviewed adapter):  provider prose, headers, redirect
 * locations and credential bytes are never reflected. */
static size_t mp_failure(unsigned char *out, size_t cap, const unsigned char *id_raw,
                         size_t id_len, int is_call, const char *code) {
    static const char unknown_msg[] =
        "The write outcome is unknown. Do not retry blindly; check target state first.";
    static const char identity_msg[] = "That identity is not assigned to this connection.";
    static const char transport_msg[] =
        "Assigned hosted transport unavailable; no automatic retry.";
    const char *message = code ? (code[0] == 'i' ? identity_msg : unknown_msg) : transport_msg;
    if (is_call) {
        /* {"jsonrpc":"2.0","id":<id>,"result":{"isError":true,"content":[{"type":"text",
         *  "text":"{\"code\":\"<code>\",\"retryable\":false,\"message\":\"...\"}"}]}} */
        int n = snprintf((char *)out, cap,
            "{\"jsonrpc\":\"2.0\",\"id\":%.*s,\"result\":{\"isError\":true,"
            "\"content\":[{\"type\":\"text\",\"text\":\"{\\\"code\\\":\\\"%s\\\","
            "\\\"retryable\\\":false,\\\"message\\\":\\\"%s\\\"}\"}]}}",
            (int)id_len, (const char *)id_raw, code ? code : "outcome_unknown", message);
        return (n > 0 && (size_t)n < cap) ? (size_t)n : 0;
    }
    int n = snprintf((char *)out, cap,
        "{\"jsonrpc\":\"2.0\",\"id\":%.*s,\"error\":{\"code\":-32000,\"message\":\"%s\"}}",
        (int)id_len, (const char *)id_raw, transport_msg);
    return (n > 0 && (size_t)n < cap) ? (size_t)n : 0;
}

/* Fixed input-rejection lines (never provider-derived). */
MP_MAYBE_UNUSED
static size_t mp_line_error(unsigned char *out, size_t cap, long code, const char *message) {
    int n = snprintf((char *)out, cap,
        "{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":%ld,\"message\":\"%s\"}}",
        code, message);
    return (n > 0 && (size_t)n < cap) ? (size_t)n : 0;
}

/* method == "tools/call": 1 yes, 0 no, -1 malformed */
static int t_frame_method_is_call(const unsigned char *frame, size_t len) {
    size_t vs, vl;
    if (!mp_js_find(frame, len, 0, "method", &vs, &vl) || frame[vs] != '"') return -1;
    return mp_js_str_eq(frame, vs, vl, "tools/call") ? 1 : 0;
}

/* Frame size policy (mirrors the reviewed adapter):  ordinary frames cap at MP_MAX_BYTES;
 * only the two file-write verbs may exceed it, with arguments <= MP_MAX_FILE_ARGS and
 * framing overhead <= 64 KiB. */
MP_MAYBE_UNUSED
static int mp_frame_allowed(const unsigned char *frame, size_t len) {
    if (len <= MP_MAX_BYTES) return 1;
    if (len > MP_MAX_FILE_FRAME || !mp_js_canonical(frame, len)) return 0;
    size_t vs, vl, ps, pl, as, al;
    if (t_frame_method_is_call(frame, len) != 1) return 0;
    if (!mp_js_find(frame, len, 0, "params", &ps, &pl) || frame[ps] != '{') return 0;
    if (!mp_js_find(frame, len, ps, "name", &vs, &vl)) return 0;
    if (!mp_js_str_eq(frame, vs, vl, "gr_wf_file_put") &&
        !mp_js_str_eq(frame, vs, vl, "gr_wf_files_put")) return 0;
    if (!mp_js_find(frame, len, ps, "arguments", &as, &al) || frame[as] != '{') return 0;
    return al <= MP_MAX_FILE_ARGS && len - al <= 65536;
}


/* -------------------------------------------------------- frame handling */

static int mp_token_echo(const unsigned char *body, size_t body_len,
                         const char *token, size_t token_len) {
    if (body_len < token_len) return 0;
    for (size_t i = 0; i + token_len <= body_len; ++i)
        if (!memcmp(body + i, token, token_len)) return 1;
    return 0;
}

static int mp_printable(const char *text, size_t len, size_t cap) {
    if (!len || len > cap) return 0;
    for (size_t i = 0; i < len; ++i)
        if (text[i] < 0x21 || text[i] > 0x7e) return 0;
    return 1;
}

/* Decode a JSON string span (including quotes) into out; printable ASCII only. */
static int mp_js_decode(const unsigned char *t, size_t span, size_t slen,
                        char *out, size_t cap, size_t *out_len) {
    size_t i = span + 1, end = span + slen - 1, n = 0;
    while (i < end) {
        unsigned char c = t[i];
        if (c == '\\') {
            ++i;
            if (i >= end) return 0;
            c = t[i];
            if (c == 'u') return 0;
            if (c == 'n') c = '\n';
            else if (c == 't') c = '\t';
            else if (c == 'r') c = '\r';
            else if (c == 'b') c = '\b';
            else if (c == 'f') c = '\f';
        }
        if (n + 1 >= cap) return 0;
        out[n++] = (char)c;
        ++i;
    }
    out[n] = 0;
    *out_len = n;
    return 1;
}

/* One input frame -> one optional output line.  Returns the output length, 0 when the
 * protocol owes no line (notifications), with the line written into out (cap bytes).
 * scratch (scratch_cap) is workspace for an identity-bound frame rebuild. */
MP_MAYBE_UNUSED
static size_t mp_handle_frame(mp_session *sess, const char *token, size_t token_len,
                              const unsigned char *frame, size_t len,
                              mp_transport_post post, void *ctx,
                              unsigned char *scratch, size_t scratch_cap,
                              unsigned char *out, size_t cap) {
    if (!mp_frame_allowed(frame, len))
        return mp_line_error(out, cap, -32600, "Input frame too large.");
    if (!mp_js_canonical(frame, len) || frame[0] != '{')
        return mp_line_error(out, cap, -32700, "Invalid JSON-RPC input.");
    size_t vs = 0, vl = 0, ms, ml, ps, pl;
    int has_id = mp_js_find(frame, len, 0, "id", &vs, &vl);
    int is_call = t_frame_method_is_call(frame, len);
    if (is_call < 0 ||
        !mp_js_find(frame, len, 0, "jsonrpc", &ms, &ml) ||
        !mp_js_str_eq(frame, ms, ml, "2.0"))
        return mp_line_error(out, cap, -32700, "Invalid JSON-RPC input.");
    size_t id_s = vs, id_l = vl;
    /* A message without an id (notification) is owed NO line, even on failure - the
     * reviewed adapter's failure() returns None for it. */
#define MP_FAIL(code) do { \
    if (!has_id) return 0; \
    return mp_failure(out, cap, frame + id_s, id_l, is_call, (code)); \
} while (0)

    /* Fixed-seat binding:  the credential proves ECHO;  a different explicit identity is a
     * genuine refusal, never sent, and no selector exists. */
    const unsigned char *wire = frame;
    size_t wire_len = len;
    if (is_call && mp_js_find(frame, len, 0, "params", &ps, &pl) && frame[ps] == '{') {
        size_t ns, nl;
        int named_identities =
            mp_js_find(frame, len, ps, "name", &ns, &nl) &&
            mp_js_str_eq(frame, ns, nl, "identities");
        if (!named_identities) {
            size_t as, al;
            if (mp_js_find(frame, len, ps, "arguments", &as, &al) && frame[as] == '{') {
                size_t is, il;
                if (mp_js_find(frame, len, as, "identity", &is, &il)) {
                    if (!mp_js_str_eq(frame, is, il, MP_FIXED_IDENTITY)) {
                        is_call = 1;
                        MP_FAIL("identity_not_allowed");
                    }
                } else {
                    /* Comma only when the object already has a member:  {} gets a bare entry. */
                    size_t look = as + 1;
                    mp_js_ws(frame, len, &look);
                    int nonempty = look < len && frame[look] != '}';
                    static const char add[] = "\"identity\":\"" MP_FIXED_IDENTITY "\"";
                    size_t add_len = sizeof(add) - 1 + (nonempty ? 1 : 0);
                    if ((size_t)len + add_len > scratch_cap)
                        return mp_line_error(out, cap, -32600, "Input frame too large.");
                    memcpy(scratch, frame, as + 1);
                    memcpy(scratch + as + 1, add, sizeof(add) - 1);
                    if (nonempty) scratch[as + 1 + sizeof(add) - 1] = ',';
                    memcpy(scratch + as + 1 + add_len, frame + as + 1, len - as - 1);
                    wire = scratch;
                    wire_len = len + add_len;
                }
            } else {
                size_t look = ps + 1;
                mp_js_ws(frame, len, &look);
                int nonempty = look < len && frame[look] != '}';
                static const char add[] =
                    "\"arguments\":{\"identity\":\"" MP_FIXED_IDENTITY "\"}";
                size_t add_len = sizeof(add) - 1 + (nonempty ? 1 : 0);
                if ((size_t)len + add_len > scratch_cap)
                    return mp_line_error(out, cap, -32600, "Input frame too large.");
                memcpy(scratch, frame, ps + 1);
                memcpy(scratch + ps + 1, add, sizeof(add) - 1);
                if (nonempty) scratch[ps + 1 + sizeof(add) - 1] = ',';
                memcpy(scratch + ps + 1 + add_len, frame + ps + 1, len - ps - 1);
                wire = scratch;
                wire_len = len + add_len;
            }
            if (!mp_js_canonical(wire, wire_len))
                return mp_line_error(out, cap, -32600, "Request rebuild failed.");
        }
    }

    mp_headers headers;
    memcpy(headers.authorization, "Bearer ", 7);
    memcpy(headers.authorization + 7, token, token_len);
    headers.authorization[7 + token_len] = 0;
    headers.session = sess->session_len ? sess->session : NULL;
    headers.protocol = sess->has_protocol ? sess->protocol : NULL;

    mp_http_response response;
    memset(&response, 0, sizeof(response));  /* absent transport fields stay NULL */
    int transport_failed = post(ctx, wire, wire_len, &headers, &response);
    mp_wipe(headers.authorization, sizeof(headers.authorization));
    if (transport_failed)
        MP_FAIL(NULL);
    if (response.status == 202 && !has_id) return 0;
    if (response.status != 200)
        MP_FAIL(NULL);

    int is_initialize = 0;
    {
        size_t xs, xl;
        is_initialize = mp_js_find(frame, len, 0, "method", &xs, &xl) &&
                        mp_js_str_eq(frame, xs, xl, "initialize");
    }
    if (response.session_header) {
        size_t slen = strlen(response.session_header);
        if (!mp_printable(response.session_header, slen, MP_MAX_SESSION))
            MP_FAIL(NULL);
        if (sess->session_len && strcmp(sess->session, response.session_header) &&
            !is_initialize)
            MP_FAIL(NULL);
    }
    if (response.body_len > MP_MAX_BYTES ||
        mp_token_echo(response.body, response.body_len, token, token_len))
        MP_FAIL(NULL);

    /* Response payloads:  one JSON document, or SSE data events. */
    const unsigned char *payloads[64];
    size_t payload_lens[64];
    size_t payload_count = 0;
    const char *type = response.content_type ? response.content_type : "";
    if (!strncmp(type, "application/json", 16)) {
        if (!mp_js_canonical(response.body, response.body_len))
            MP_FAIL(NULL);
        payloads[0] = response.body;
        payload_lens[0] = response.body_len;
        payload_count = 1;
    } else if (!strncmp(type, "text/event-stream", 17)) {
        /* Bounded event assembly:  data lines join with \n inside one event, a blank line
         * dispatches it, scanning CONTINUES past every delimiter.  Each event payload is
         * validated as canonical JSON;  overflow or a broken event fails the response. */
        static unsigned char sse_buf[MP_MAX_BYTES + 1];
        size_t used = 0, i = 0, data_start = 0;
        int has_data = 0, broken = 0;
        while (i < response.body_len && !broken) {
            size_t line_end = i;
            while (line_end < response.body_len && response.body[line_end] != '\n') ++line_end;
            size_t stop = line_end;
            if (stop > i && response.body[stop - 1] == '\r') --stop;
            if (stop == i) {  /* blank line: dispatch the assembled event */
                if (has_data) {
                    if (payload_count >= 64) { broken = 1; break; }
                    payloads[payload_count] = sse_buf + data_start;
                    payload_lens[payload_count] = used - data_start;
                    ++payload_count;
                    has_data = 0;
                }
            } else if (stop - i > 5 && !memcmp(response.body + i, "data:", 5)) {
                size_t ds = i + 5;
                if (ds < stop && response.body[ds] == ' ') ++ds;
                size_t need = stop - ds + (has_data ? 1 : 0);
                if (used + need > MP_MAX_BYTES) { broken = 1; break; }
                if (has_data) sse_buf[used++] = '\n';
                else data_start = used;
                memcpy(sse_buf + used, response.body + ds, stop - ds);
                used += stop - ds;
                has_data = 1;
            }
            i = line_end + 1;
        }
        if (has_data && !broken) {  /* final event without a trailing blank line */
            if (payload_count >= 64) broken = 1;
            else {
                payloads[payload_count] = sse_buf + data_start;
                payload_lens[payload_count] = used - data_start;
                ++payload_count;
            }
        }
        if (broken || !payload_count)
            MP_FAIL(NULL);
        for (size_t k = 0; k < payload_count; ++k)
            if (!mp_js_canonical(payloads[k], payload_lens[k]))
                MP_FAIL(NULL);
    } else {
        MP_FAIL(NULL);
    }
    if (!has_id) return 0;

    const unsigned char *match = NULL;
    size_t match_len = 0, matches = 0, ms2, ml2, rs, rl;
    for (size_t k = 0; k < payload_count; ++k) {
        if (payload_lens[k] == 0 || payloads[k][0] != '{') continue;
        if (!mp_js_find(payloads[k], payload_lens[k], 0, "id", &ms2, &ml2)) continue;
        if (ml2 != id_l || memcmp(payloads[k] + ms2, frame + id_s, id_l)) continue;
        int has_result = mp_js_find(payloads[k], payload_lens[k], 0, "result", &rs, &rl);
        int has_error = mp_js_find(payloads[k], payload_lens[k], 0, "error", &rs, &rl);
        if (has_result == has_error) continue;
        match = payloads[k];
        match_len = payload_lens[k];
        ++matches;
    }
    if (matches != 1)
        MP_FAIL(NULL);

    if (is_initialize) {
        size_t rs2, rl2, pvs, pvl;
        if (!mp_js_find(match, match_len, 0, "result", &rs2, &rl2) || match[rs2] != '{')
            MP_FAIL(NULL);
        if (!mp_js_find(match, match_len, rs2, "protocolVersion", &pvs, &pvl) ||
            match[pvs] != '"')
            MP_FAIL(NULL);
        size_t plen;
        if (!mp_js_decode(match, pvs, pvl, sess->protocol, sizeof(sess->protocol), &plen) ||
            !mp_printable(sess->protocol, plen, MP_MAX_PROTOCOL))
            MP_FAIL(NULL);
        sess->has_protocol = 1;
        if (response.session_header) {
            memcpy(sess->session, response.session_header, strlen(response.session_header) + 1);
            sess->session_len = strlen(response.session_header);
        }
    }
    if (match_len + 1 > cap)
        MP_FAIL(NULL);
    memcpy(out, match, match_len);
    return match_len;
#undef MP_FAIL
}

#endif

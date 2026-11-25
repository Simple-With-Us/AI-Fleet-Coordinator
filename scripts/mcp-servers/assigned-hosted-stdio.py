#!/usr/bin/env -S python3 -I
"""Assigned-bearer stdio transport. No secret reader, token minting or retries."""

import base64
import hashlib
import http.client
import json
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request


def native_origin_claims(env, identity, meta=None):
    """Same-seat claims from runtime-owned per-call context and a private local note.

    No startup-session fallback, registry call, signing key or post-open mutation.
    Operator independently binds the protected relay receipt before effects.
    """
    if not isinstance(meta, dict):
        return {}
    if identity == "CODEX":
        platform = "codex"
        candidates = []
        if "threadId" in meta:
            candidates.append(meta["threadId"])
        turn = meta.get("x-codex-turn-metadata")
        if isinstance(turn, dict) and "thread_id" in turn:
            candidates.append(turn["thread_id"])
        if not candidates or any(not isinstance(x, str) for x in candidates):
            return {}
        sid = candidates[0]
        if any(x != sid for x in candidates):
            return {}
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", sid):
            return {}
    elif identity == "MM":
        platform = "minimax"
        context = meta.get("com.simplewithus/native-origin/1")
        if (
            not isinstance(context, dict)
            or set(context) != {"version", "platform", "session_id", "turn_id", "tool_call_id"}
            or type(context.get("version")) is not int
            or context["version"] != 1
            or context.get("platform") != platform
            or not isinstance(context.get("session_id"), str)
            or not re.fullmatch(r"mvs_[0-9a-f]{32}", context["session_id"])
            or any(
                not isinstance(context.get(name), str)
                or not context[name].strip()
                or len(context[name]) > 256
                for name in ("turn_id", "tool_call_id")
            )
            or "threadId" in meta
            or "x-codex-turn-metadata" in meta
        ):
            return {}
        sid = context["session_id"]
    else:
        return {}
    home = env.get("HOME", "")
    if not os.path.isabs(home):
        return {}
    fd = None
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in (".simple-tools", identity, "letters"):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                return {}
        note_fd = os.open(sid + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(note_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                return {}
            raw = os.read(note_fd, 262145)
        finally:
            os.close(note_fd)
        if len(raw) > 262144:
            return {}
        note = json.loads(raw)
        if (
            not isinstance(note, dict)
            or note.get("session_id") != sid
            or note.get("platform") != platform
            or note.get("state") != "open"
            or not re.fullmatch(r"[A-Z]{2}", str(note.get("label") or ""))
            or type(note.get("generation")) is not int
            or note["generation"] < 1
        ):
            return {}
        return {
            "origin_session_claim": sid,
            "origin_letter_claim": note["label"],
            "origin_generation_claim": note["generation"],
        }
    except (OSError, ValueError, TypeError):
        return {}
    finally:
        if fd is not None:
            os.close(fd)


#: Per-call metadata keys a native client can authenticate.  Only these names
#: are ever reported by the capability probe; unknown keys are counted, never
#: named, and no value of any key is emitted.
KNOWN_META_KEYS = (
    "com.simplewithus/native-origin/1",
    "com.zcode/request-context",
    "threadId",
    "x-codex-turn-metadata",
)

#: The only tool names the probe may emit.  A caller-controlled name can carry
#: secret text or an unbounded body, so anything else reports as "other".
PROBE_TOOL_LABELS = ("identities", "gr_open_pr", "open_pr")


def call_shape_once(env, identity, params):
    """Default-off, host-armed one-shot capability probe for any identity.

    An empty private <identity>/origin-call-shape.once marker is consumed
    atomically, then one JSON line reports the next tool call's metadata
    SHAPE:  the tool label (allowlisted names only), the _meta type, the type
    of each known context key, and the count of other keys.  Never a value,
    never an unknown key name, never a request or response body.

    Arm at most one marker per identity at a time and never rearm while a
    consumption may still be in flight:  open/fstat/unlink is not
    inode-bound, so a racing rearm could be consumed by an earlier reader.
    One armed generation emits at most one line, even across processes."""
    if (
        not isinstance(identity, str)
        or not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,31}", identity)
        or not isinstance(params, dict)
    ):
        return
    home = env.get("HOME", "")
    if not os.path.isabs(home):
        return
    directory = None
    marker = None
    try:
        directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in (".simple-tools", identity):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                return
        name = "origin-call-shape.once"
        marker = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(marker)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size != 0
        ):
            return
        # Only the unlink winner emits, even across adapter processes.
        os.unlink(name, dir_fd=directory)
        meta = params.get("_meta")

        def kind(value):
            if value is None:
                return "missing-or-null"
            if type(value) is bool:
                return "boolean"
            if type(value) is int:
                return "integer"
            if isinstance(value, str):
                return "string"
            if isinstance(value, dict):
                return "object"
            if isinstance(value, list):
                return "array"
            return "other"

        keys = meta.keys() if isinstance(meta, dict) else ()
        tool = params.get("name")
        shape = {
            "diagnostic": "origin-call-shape/1",
            "tool": tool
            if tool in PROBE_TOOL_LABELS
            else ("other" if isinstance(tool, str) else None),
            "meta_type": kind(meta),
            "known_keys": {
                key: kind(meta.get(key)) if isinstance(meta, dict) else "missing-or-null"
                for key in KNOWN_META_KEYS
            },
            "other_key_count": sum(1 for key in keys if key not in KNOWN_META_KEYS),
        }
        print(json.dumps(shape, sort_keys=True), file=sys.stderr)
    except (OSError, ValueError, TypeError):
        return
    finally:
        if marker is not None:
            os.close(marker)
        if directory is not None:
            os.close(directory)


def origin_shape_once(env, identity, params):
    """Default-off, host-armed one-shot for identities only. Never emits values.

    An empty private MM/origin-identities-shape.once marker is consumed atomically
    before fixed-field types/validity are printed. No request/response/body dump.
    """
    if identity != "MM" or not isinstance(params, dict) or params.get("name") != "identities":
        return
    home = env.get("HOME", "")
    if not os.path.isabs(home):
        return
    directory = None
    marker = None
    try:
        directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in (".simple-tools", "MM"):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                return
        name = "origin-identities-shape.once"
        marker = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(marker)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size != 0
        ):
            return
        # Only the unlink winner emits, even across adapter processes.
        os.unlink(name, dir_fd=directory)
        meta = params.get("_meta")
        context = meta.get("com.simplewithus/native-origin/1") if isinstance(meta, dict) else None

        def kind(value):
            if value is None:
                return "missing-or-null"
            if type(value) is bool:
                return "boolean"
            if type(value) is int:
                return "integer"
            if isinstance(value, str):
                return "string"
            if isinstance(value, dict):
                return "object"
            if isinstance(value, list):
                return "array"
            return "other"

        fields = ("version", "platform", "session_id", "turn_id", "tool_call_id")
        shape = {
            "diagnostic": "mm-origin-identities-shape/1",
            "tool": "identities",
            "meta_type": kind(meta),
            "context_type": kind(context),
            "field_types": {
                key: kind(context.get(key) if isinstance(context, dict) else None) for key in fields
            },
            "exact_context_keys": isinstance(context, dict) and set(context) == set(fields),
            "private_note_claims_valid": bool(native_origin_claims(env, identity, meta)),
        }
        print(json.dumps(shape, sort_keys=True), file=sys.stderr)
    except (OSError, ValueError, TypeError):
        return
    finally:
        if marker is not None:
            os.close(marker)
        if directory is not None:
            os.close(directory)


ENDPOINT = "https://agents.simplewithus.com/mcp"
MAX_BYTES = 4 * 1024 * 1024
MAX_FILE_ARGS_BYTES = 6 * 1024 * 1024
MAX_FILE_FRAME_BYTES = MAX_FILE_ARGS_BYTES + 65536
# Each Worker request counts against a daily cap, so discovery is answered from
# disk for this long after a real answer.  A new tool appears within this window.
CACHE_TTL_S = 600
# Sent when this process has no negotiated version, as simple-chat-remote does.
DEFAULT_PROTOCOL = "2025-06-18"
PROTOCOL_RE = re.compile(r"[!-~]{1,128}")
# A modern request names its own version here and needs no handshake.  The server refuses
# a header that disagrees with it, so it outranks both of the versions above.
META_VERSION = "io.modelcontextprotocol/protocolVersion"
# Such a request must also say what its body says in the standard headers (SEP-2243), or the
# server answers 400, code -32020, "the request headers and body disagree":  Mcp-Method on every
# request, and Mcp-Name on the methods below, carrying the body field named here.  This is the
# table and the encoding the SDK's own client uses (MCP_NAME_HEADER_SOURCE, encodeMcpParamValue).
NAME_FIELD = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
    "tasks/get": "taskId",
    "tasks/update": "taskId",
    "tasks/cancel": "taskId",
}
BASE64_PREFIX = "=?base64?"
BASE64_SUFFIX = "?="
METHOD_RE = re.compile(r"[!-~]{1,128}")
BEARER_REFUSED_MESSAGE = (
    "The hosted server refused this connection's assigned bearer: it is invalid, expired, "
    "revoked, paused or not accepted for MCP, or the server could not load its credentials. "
    "The call did not run. Startup may have been answered from a local cache, so this can be "
    "the first sign. Ask the host owner to check this seat's bearer; do not use another seat's."
)
TOKEN_RE = re.compile(
    r"^(?:sbt_([a-z][a-z0-9-]{1,19})|gbb_([a-z][a-z0-9]{1,19}))_[A-Za-z0-9_-]{43}$"
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TransportFailure(Exception):
    pass


def assigned_identity(token):
    match = TOKEN_RE.fullmatch(token)
    if not match:
        raise TransportFailure("assigned bearer is missing or malformed")
    return match[1].upper() if match[1] else "GB-" + match[2].upper()


def default_cache_dir(env):
    """The cache folder, or None.  A host that sets neither variable runs uncached."""
    base = env.get("XDG_CACHE_HOME", "")
    if not os.path.isabs(base):
        home = env.get("HOME", "")
        if not os.path.isabs(home):
            return None
        base = os.path.join(home, ".cache")
    return os.path.join(base, "assigned-hosted-stdio")


class DiskCache:
    """Hosted discovery answers, one set per bearer.  Holds no bearer and never raises.

    Answers are per seat (instructions, identity enum), so the file names come from
    the bearer, domain-separated from the hash the hosted server stores.  Anything
    that is not private to this user is ignored: a planted file must not become the
    tool catalog.  An unwritable folder (a sandboxed host) only means no caching.
    """

    def __init__(self, root, token):
        self.root = root
        seed = b"assigned-hosted-stdio-cache/1\0" + token.encode() + b"\0" + ENDPOINT.encode()
        self.prefix = hashlib.sha256(seed).hexdigest()[:32]

    def _private_root(self):
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        info = os.lstat(self.root)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError("unsafe cache folder")
        os.chmod(self.root, 0o700)  # An inherited default ACL can widen the mkdir mode.

    def _path(self, name):
        return os.path.join(self.root, f"{self.prefix}.{name}.json")

    def load(self, name):
        try:
            self._private_root()
            fd = os.open(self._path(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    return None
                raw = source.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            entry = json.loads(raw)
            age = time.time() - entry["at"]
            result = entry["result"]
            return result if 0 <= age < CACHE_TTL_S and isinstance(result, dict) else None
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError, RecursionError):
            return None

    def store(self, name, result):
        temp = None
        try:
            raw = json.dumps({"at": time.time(), "result": result}).encode()
            if len(raw) > MAX_BYTES:
                return
            self._private_root()
            fd, temp = tempfile.mkstemp(prefix=self.prefix + ".", suffix=".tmp", dir=self.root)
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(raw)
            os.replace(temp, self._path(name))
            temp = None
        except (OSError, ValueError, TypeError, RecursionError):
            pass
        finally:
            if temp:
                try:
                    os.unlink(temp)
                except OSError:
                    pass

    def clear(self):
        try:
            names = os.listdir(self.root)
        except OSError:
            return
        for name in names:
            if name.startswith(self.prefix + "."):
                try:  # A second adapter refused at the same moment may have got there first.
                    os.unlink(os.path.join(self.root, name))
                except OSError:
                    pass


def initialize_key(message):
    """The server negotiates against the version asked for, so answers are kept per version."""
    params = message.get("params")
    asked = params.get("protocolVersion") if isinstance(params, dict) else None
    return "init-" + hashlib.sha256(str(asked).encode()).hexdigest()[:16]


def usable_initialize(result):
    return (
        isinstance(result, dict)
        and isinstance(result.get("capabilities"), dict)
        and isinstance(result.get("protocolVersion"), str)
        and PROTOCOL_RE.fullmatch(result["protocolVersion"]) is not None
    )


def tools_key(version):
    """A modern answer has a different shape, and its decoder refuses a legacy one."""
    return "tools-" + hashlib.sha256(version.encode()).hexdigest()[:16]


def first_page_of_tools(message):
    """Only a request with no cursor at all.  The server answers a malformed one."""
    params = message.get("params", {})
    return isinstance(params, dict) and "cursor" not in params


def names_envelope(message):
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return isinstance(meta, dict) and META_VERSION in meta


def needs_base64(value):
    """True when a header value is not safe plain ASCII (RFC 9110 5.5):  empty, edge space or tab,
    a byte outside 0x20 to 0x7E and tab, or already shaped like the Base64 form."""
    if not value or value != value.strip(" \t"):
        return True
    if value.startswith(BASE64_PREFIX) and value.endswith(BASE64_SUFFIX):
        return True
    return any(ord(char) != 9 and not 32 <= ord(char) <= 126 for char in value)


def encode_header_value(value):
    """Plain when safe, else =?base64?<UTF-8 in Base64>?=.  A lone surrogate becomes U+FFFD,
    as a JavaScript TextEncoder makes it."""
    if not needs_base64(value):
        return value
    text = value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return BASE64_PREFIX + base64.b64encode(text.encode("utf-8")).decode("ascii") + BASE64_SUFFIX


def name_header(message):
    """The Mcp-Name value for this request's method, or None when the method has none or the
    body field is not a string (the server then judges the body)."""
    method = message.get("method")
    params = message.get("params")
    field = NAME_FIELD.get(method) if isinstance(method, str) else None
    value = params.get(field) if field and isinstance(params, dict) else None
    return encode_header_value(value) if isinstance(value, str) else None


def is_bearer_refusal(error):
    """Only the hosted server's own invalid_token answer, which it sends before any tool runs."""
    if error.code != 401:
        return False
    try:
        return json.loads(error.read(4096)).get("error") == "invalid_token"
    except (OSError, ValueError, AttributeError, RecursionError, http.client.HTTPException):
        return False


def bearer_refused(message):
    if "id" not in message:
        return None
    if message.get("method") == "tools/call":
        text = json.dumps(
            {"code": "bearer_refused", "retryable": False, "message": BEARER_REFUSED_MESSAGE}
        )
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"isError": True, "content": [{"type": "text", "text": text}]},
        }
    return {
        "jsonrpc": "2.0",
        "id": message["id"],
        "error": {"code": -32000, "message": BEARER_REFUSED_MESSAGE},
    }


def failure(message, code="outcome_unknown"):
    if "id" not in message:
        return None
    if message.get("method") == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "code": code,
                                "retryable": False,
                                "message": "The write outcome is unknown. Do not retry blindly; check target state first."
                                if code == "outcome_unknown"
                                else "That identity is not assigned to this connection.",
                            }
                        ),
                    }
                ],
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": message["id"],
        "error": {
            "code": -32000,
            "message": "Assigned hosted transport unavailable; no automatic retry.",
        },
    }


class Adapter:
    def __init__(self, token, opener=None, cache_dir=None, env=None):
        self.env = dict(os.environ if env is None else env)
        self.identity = assigned_identity(token)  # A local guard, not server authentication.
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open
        self.session = None
        self.protocol = None
        self.cache = DiskCache(cache_dir, token) if cache_dir else None

    def request_version(self, message):
        """The version this request speaks, or None for an envelope only the server can refuse."""
        if names_envelope(message):
            version = message["params"]["_meta"][META_VERSION]
            return version if isinstance(version, str) and PROTOCOL_RE.fullmatch(version) else None
        return self.protocol or DEFAULT_PROTOCOL

    def answer_from_cache(self, message):
        if self.cache is None or "id" not in message or self.session:
            return None
        method = message.get("method")
        if method == "initialize" and not names_envelope(message):
            result = self.cache.load(initialize_key(message))
            if not usable_initialize(result):
                return None
            self.protocol = result["protocolVersion"]
        elif method == "tools/list" and first_page_of_tools(message):
            version = self.request_version(message)
            result = self.cache.load(tools_key(version)) if version else None
            if result is None or not isinstance(result.get("tools"), list):
                return None
        else:
            return None
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    def remember(self, message, reply, session):
        if self.cache is None or not isinstance(reply.get("result"), dict):
            return
        # A server that issues sessions is stateful, so nothing it said may be replayed:
        # it must see every initialize again, and its catalog is per session.  An empty
        # session header is no session, as it is everywhere else in this adapter.
        if session or self.session:
            self.cache.clear()
            return
        result = reply["result"]
        method = message.get("method")
        if method == "initialize":
            # The key is the legacy slot, which answer_from_cache replays to legacy clients.
            if not names_envelope(message) and usable_initialize(result):
                self.cache.store(initialize_key(message), result)
        elif (
            method == "tools/list"
            and first_page_of_tools(message)
            and isinstance(result.get("tools"), list)
            and not result.get("nextCursor")
            and self.request_version(message)
        ):
            self.cache.store(tools_key(self.request_version(message)), result)

    def call(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise TransportFailure("invalid JSON-RPC message")
        method = message.get("method")
        # The hosted handler is stateless (createMcpHandler, no session generator), so
        # nothing it does depends on a notification, and each one is a counted request.
        if isinstance(method, str) and method.startswith("notifications/") and "id" not in message:
            return None
        local = self.answer_from_cache(message)
        if local is not None:
            return local
        params = message.get("params")
        if message.get("method") == "tools/call" and isinstance(params, dict):
            args = params.get("arguments")
            # No alias selection: the hosted server independently enforces its binding.
            if isinstance(args, dict) and "identity" in args and args["identity"] != self.identity:
                return failure(message, "identity_not_allowed")
        if message.get("method") == "tools/call" and isinstance(params, dict):
            args = params.get("arguments", {})
            if isinstance(args, dict) and params.get("name") != "identities":
                # Assigned by the credential, never a configurable seat selector.
                message = {
                    **message,
                    "params": {**params, "arguments": {**args, "identity": self.identity}},
                }
        if (
            message.get("method") == "tools/call"
            and isinstance(params, dict)
            and params.get("name") in ("gr_open_pr", "open_pr")
        ):
            args = message["params"].get("arguments")
            if isinstance(args, dict):
                claims = native_origin_claims(self.env, self.identity, params.get("_meta"))
                args = {k: v for k, v in args.items() if not k.startswith("origin_")}
                message = {
                    **message,
                    "params": {**message["params"], "arguments": {**args, **claims}},
                }
                if not claims:
                    print(
                        "assigned-hosted-stdio: review origin unavailable for this native context; alert held",
                        file=sys.stderr,
                    )
        if method == "tools/call":
            origin_shape_once(self.env, self.identity, message.get("params"))
            call_shape_once(self.env, self.identity, message.get("params"))
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "assigned-hosted-stdio/1.0",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        version = self.request_version(message)
        # A request that names its own version in its envelope is served on the modern path, which
        # wants the version, the method and, for some methods, the name in headers too.  The
        # envelope's own version outranks everything, an initialize included.
        modern = "id" in message and names_envelope(message) and version is not None
        # An initialize that names no version is what negotiates one, so it sends none.
        if modern or (method != "initialize" and version):
            headers["MCP-Protocol-Version"] = version
        if modern and isinstance(method, str) and METHOD_RE.fullmatch(method):
            headers["Mcp-Method"] = method
            name = name_header(message)
            if name is not None:
                headers["Mcp-Name"] = name
        encoded = json.dumps(message).encode()
        if not file_frame_allowed(message, len(encoded)):
            raise TransportFailure("Input frame too large.")
        request = urllib.request.Request(ENDPOINT, data=encoded, headers=headers, method="POST")
        try:
            with self.opener(request, timeout=120) as response:
                status = response.status
                if status == 202 and "id" not in message:
                    return None
                if status != 200:
                    return failure(message)
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    if len(session) > 256 or not re.fullmatch(r"[!-~]+", session):
                        return failure(message)
                    if (
                        self.session
                        and session != self.session
                        and message.get("method") != "initialize"
                    ):
                        return failure(message)
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES or self.token.encode() in raw:
                    return failure(message)
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                if content_type == "text/event-stream":
                    payloads = []
                    data = []
                    for line in raw.decode("utf-8").replace("\r\n", "\n").split("\n") + [""]:
                        if not line:
                            if data:
                                payloads.append(json.loads("\n".join(data)))
                                data = []
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip(" "))
                elif content_type == "application/json":
                    payloads = [json.loads(raw)]
                else:
                    return failure(message)
                matching = [
                    p
                    for p in payloads
                    if isinstance(p, dict)
                    and p.get("jsonrpc") == "2.0"
                    and "id" in p
                    and p["id"] == message.get("id")
                    and (("result" in p) != ("error" in p))
                ]
                if "id" not in message:
                    return None
                if len(matching) != 1:
                    return failure(message)
                result = matching[0]
                if message.get("method") == "initialize" and isinstance(result.get("result"), dict):
                    protocol = result["result"].get("protocolVersion")
                    if not isinstance(protocol, str) or not re.fullmatch(r"[!-~]{1,128}", protocol):
                        return failure(message)
                    # Only a legacy initialize negotiates the version that later legacy
                    # frames send.  A modern one names its own version in every request.
                    if not names_envelope(message):
                        self.protocol = protocol
                    self.session = session
                self.remember(message, result, session)
                return result
        except urllib.error.HTTPError as error:
            if is_bearer_refusal(error):
                if self.cache is not None:
                    self.cache.clear()
                return bearer_refused(message)
            return failure(message)
        except (OSError, ValueError, http.client.HTTPException):
            # Never reflect provider prose, headers, credentials or redirect locations.
            # An accepted write can lose its response even on a non-JSON 4xx/5xx.
            return failure(message)


def file_frame_allowed(message, frame_bytes):
    if frame_bytes <= MAX_BYTES:
        return True
    if frame_bytes > MAX_FILE_FRAME_BYTES or not isinstance(message, dict):
        return False
    params = message.get("params")
    if (
        message.get("jsonrpc") != "2.0"
        or message.get("method") != "tools/call"
        or not isinstance(params, dict)
        or params.get("name") not in {"gr_wf_file_put", "gr_wf_files_put"}
        or set(message) - {"jsonrpc", "id", "method", "params"}
        or set(params) - {"name", "arguments", "_meta"}
        or not isinstance(params.get("arguments"), dict)
    ):
        return False
    size = len(json.dumps(params["arguments"]).encode())
    return size <= MAX_FILE_ARGS_BYTES and frame_bytes - size <= 65536


def main():
    token = os.environ.pop("ASSIGNED_MCP_BEARER", "").strip()
    try:
        adapter = Adapter(token, cache_dir=default_cache_dir(os.environ))
    except TransportFailure:
        print("assigned-hosted-stdio: assigned bearer is missing or malformed", file=sys.stderr)
        return 3
    while True:
        raw = sys.stdin.buffer.readline(MAX_FILE_FRAME_BYTES + 1)
        if not raw:
            return 0
        if len(raw) > MAX_FILE_FRAME_BYTES:
            # Drain the rejected frame; its tail must not become another request.
            while raw and not raw.endswith(b"\n"):
                raw = sys.stdin.buffer.readline(MAX_FILE_FRAME_BYTES + 1)
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Input frame too large."},
                    }
                ),
                flush=True,
            )
            continue
        try:
            message = json.loads(raw)
            if not file_frame_allowed(message, len(raw)):
                raise TransportFailure("Input frame too large.")
            result = adapter.call(message)
        except (ValueError, TransportFailure):
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Invalid JSON-RPC input."},
                    }
                ),
                flush=True,
            )
            continue
        if result is not None:
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    sys.exit(main())

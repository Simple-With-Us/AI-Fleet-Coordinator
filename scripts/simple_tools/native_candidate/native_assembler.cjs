'use strict';
const z=require('./offline_node_modules/zod');
var PROTOCOL_V4_LIMITS = {
  maxFrameBytes: 1024 * 1024,
  logicalFrameAssemblyMaxBytes: 16 * 1024 * 1024,
  logicalFrameAssemblyMaxFragments: 1024,
  logicalFrameAssemblyMaxConcurrent: 32,
  logicalFrameAssemblyMaxStagedBytes: 32 * 1024 * 1024,
  logicalFrameAssemblyTimeoutMs: 3e4,
  transportEnvelopeIdMaxChars: 256,
  subscriberBufferMaxOps: 500,
  subscriberBufferMaxBytes: 1024 * 1024,
  eventRetentionPerSession: 2e3,
  snapshotTailWindowRows: 60,
  rowsRangeMaxLimit: 200,
  toolOutputFinalHeadBytes: 32 * 1024,
  toolOutputFinalTailBytes: 32 * 1024,
  goalVerificationsRetained: 20,
  pendingCommandsDisplayMax: 32,
  commandPendingTtlMs: 24 * 60 * 60 * 1e3,
  idempotencyTablePerSession: 512,
  conversationQueryTimeoutMs: 1e4,
  attachmentMaxBytes: 20 * 1024 * 1024,
  attachmentChunkMaxBytes: 512 * 1024,
  attachmentPreviewMaxBytes: (30*1024*1024),
  // share 选择阶段的 metadata-only stat 曾复用 attachmentPreviewMaxBytes
  // （30MiB）作为 totalBytes 上限，于是超过该值的附件在 schema 校验就抛错，
  // 「容量超限」这个本应确定阻断的分类反而被降级成 deferred 并静默丢内容。
  // stat 不搬运字节，只需要一个足够表达真实文件大小的上界。
  attachmentStatMaxBytes: 2 * 1024 * 1024 * 1024,
  attachmentPreviewMaxChunks: (30*1024*1024) / (512 * 1024),
  attachmentReadCacheMaxBytes: (30*1024*1024),
  attachmentReadCacheTtlMs: 3e4,
  attachmentUploadMaxChunks: 64,
  attachmentUploadMaxConcurrent: 16,
  attachmentUploadMaxStagedBytes: 64 * 1024 * 1024,
  attachmentUploadTtlMs: 5 * 6e4,
  attachmentUnreferencedTtlMs: 24 * 60 * 60 * 1e3
};
var topicWireBase64Schema = z.string().min(4).regex(/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/u);
function crc32WireBytes(bytes) {
  let crc = 4294967295;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = crc >>> 1 ^ (crc & 1 ? 3988292384 : 0);
    }
  }
  return ((crc ^ 4294967295) >>> 0).toString(16).padStart(8, "0");
}
var BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
function encodeWireBytesBase64(bytes) {
  let result = "";
  let block = [];
  for (let offset = 0; offset < bytes.byteLength; offset += 3) {
    const a = bytes[offset] ?? 0;
    const hasB = offset + 1 < bytes.byteLength;
    const hasC = offset + 2 < bytes.byteLength;
    const b2 = hasB ? bytes[offset + 1] ?? 0 : 0;
    const c = hasC ? bytes[offset + 2] ?? 0 : 0;
    block.push(
      BASE64_ALPHABET[a >>> 2],
      BASE64_ALPHABET[(a & 3) << 4 | b2 >>> 4],
      hasB ? BASE64_ALPHABET[(b2 & 15) << 2 | c >>> 6] : "=",
      hasC ? BASE64_ALPHABET[c & 63] : "="
    );
    if (block.length >= 16384) {
      result += block.join("");
      block = [];
    }
  }
  return result + block.join("");
}
function decodeWireBase64(value2) {
  if (!topicWireBase64Schema.safeParse(value2).success) return null;
  const padding = value2.endsWith("==") ? 2 : value2.endsWith("=") ? 1 : 0;
  const output = new Uint8Array(value2.length / 4 * 3 - padding);
  let writeOffset = 0;
  for (let offset = 0; offset < value2.length; offset += 4) {
    const a = BASE64_ALPHABET.indexOf(value2[offset]);
    const b2 = BASE64_ALPHABET.indexOf(value2[offset + 1]);
    const c = value2[offset + 2] === "=" ? 0 : BASE64_ALPHABET.indexOf(value2[offset + 2]);
    const d = value2[offset + 3] === "=" ? 0 : BASE64_ALPHABET.indexOf(value2[offset + 3]);
    if (a < 0 || b2 < 0 || c < 0 || d < 0) return null;
    const combined = a << 18 | b2 << 12 | c << 6 | d;
    if (writeOffset < output.length) output[writeOffset++] = combined >>> 16;
    if (writeOffset < output.length) {
      output[writeOffset++] = combined >>> 8 & 255;
    }
    if (writeOffset < output.length) output[writeOffset++] = combined & 255;
  }
  return output;
}

var CHANNEL_EVENT_RESPONSE_TYPE = 204;
var SOCKET_PROTOCOL_HEADER_BYTES = 13;
function vqlByteLength(value2) {
  let bytes = 1;
  for (let remaining = value2 >>> 7; remaining > 0; remaining >>>= 7) bytes += 1;
  return bytes;
}
function measureTopicNotificationEnvelopeBytes(wire) {
  const cliNdjsonBytes = utf8JsonByteLength({
    method: "v4/conversation/frame",
    params: wire
  }) + 1;
  const wireJsonBytes = utf8JsonByteLength(wire);
  const maxEventIdJsonBytes = String(Number.MAX_SAFE_INTEGER).length;
  const maxEventIdSerializedBytes = 1 + vqlByteLength(maxEventIdJsonBytes) + maxEventIdJsonBytes;
  const channelHeaderBytes = 1 + vqlByteLength(2) + 1 + vqlByteLength(CHANNEL_EVENT_RESPONSE_TYPE) + maxEventIdSerializedBytes;
  const channelPayloadBytes = channelHeaderBytes + 1 + vqlByteLength(wireJsonBytes) + wireJsonBytes;
  const channelSocketBytes = channelPayloadBytes + SOCKET_PROTOCOL_HEADER_BYTES;
  const dataBase64Bytes = 4 * Math.ceil(channelPayloadBytes / 3);
  const transportId = "x".repeat(PROTOCOL_V4_LIMITS.transportEnvelopeIdMaxChars);
  const mobileRelayFixedBytes = utf8JsonByteLength({
    type: "data",
    payload: {
      zcode_type: "rpc-frame",
      bridgeSessionId: transportId,
      bridgeGeneration: Number.MAX_SAFE_INTEGER,
      recoveryId: transportId,
      seq: Number.MAX_SAFE_INTEGER,
      dataBase64: ""
    },
    client_ts: Number.MAX_SAFE_INTEGER,
    server_ts: Number.MAX_SAFE_INTEGER
  });
  const mobileRelayBytes = mobileRelayFixedBytes + dataBase64Bytes;
  return {
    cliNdjsonBytes,
    channelSocketBytes,
    mobileRelayBytes,
    maxBytes: Math.max(cliNdjsonBytes, channelSocketBytes, mobileRelayBytes)
  };
}
function utf8JsonByteLength(value2) {
  return new TextEncoder().encode(JSON.stringify(value2)).byteLength;
}

const WIRE_FAULT_INVALID_PAYLOAD="proto.frameAssemblyInvalidPayload";
// ../shared/src/zcode-protocol-v4/wire-assembler.ts

var COMPLETE_WIRE_KEYS = /* @__PURE__ */ new Set([
  "wireVersion",
  "kind",
  "deliveryKind",
  "logicalFrameId",
  "logicalFrameOrdinal",
  "topic",
  "subscriptionId",
  "frame"
]);
var FRAGMENT_WIRE_KEYS = /* @__PURE__ */ new Set([
  "wireVersion",
  "kind",
  "deliveryKind",
  "logicalFrameId",
  "logicalFrameOrdinal",
  "topic",
  "subscriptionId",
  "fragmentIndex",
  "fragmentCount",
  "logicalBytes",
  "checksum",
  "dataBase64"
]);
var CHECKSUM_KEYS = /* @__PURE__ */ new Set(["algorithm", "value"]);
function hasOnlyKeys(value2, allowed) {
  return Object.keys(value2).every((key2) => allowed.has(key2));
}
function parseDeliveryKind(value2) {
  return value2 === "initial" || value2 === "online" || value2 === "recovery" ? value2 : null;
}
function hasValidFragmentInner(wire) {
  if (!hasOnlyKeys(wire, FRAGMENT_WIRE_KEYS) || typeof wire.fragmentIndex !== "number" || typeof wire.fragmentCount !== "number" || typeof wire.logicalBytes !== "number" || typeof wire.dataBase64 !== "string" || typeof wire.checksum !== "object" || wire.checksum === null || Array.isArray(wire.checksum) || !hasOnlyKeys(wire.checksum, CHECKSUM_KEYS)) {
    return false;
  }
  const checksum2 = wire.checksum;
  return typeof checksum2.algorithm === "string" && typeof checksum2.value === "string";
}
function routeKey(topic, subscriptionId) {
  return `${topic}\0${subscriptionId}`;
}
function bytesEqual(left, right) {
  if (left.byteLength !== right.byteLength) return false;
  for (let index = 0; index < left.byteLength; index += 1) {
    if (left[index] !== right[index]) return false;
  }
  return true;
}
function frameMatchesEnvelope(frame, wire) {
  if (typeof frame !== "object" || frame === null) return false;
  const value2 = frame;
  return value2.topic === wire.topic && value2.subscriptionId === wire.subscriptionId;
}
function hardBound2(value2, maximum, name) {
  const resolved = value2 ?? maximum;
  if (!Number.isFinite(resolved) || resolved <= 0) {
    throw new RangeError(`${name} must be a positive finite number`);
  }
  return Math.min(Math.floor(resolved), maximum);
}
var TopicWireFrameAssembler = class {
  constructor(frameSchema, options2 = {}) {
    this.frameSchema = frameSchema;
    this.maxAssemblyBytes = hardBound2(
      options2.maxAssemblyBytes,
      PROTOCOL_V4_LIMITS.logicalFrameAssemblyMaxBytes,
      "maxAssemblyBytes"
    );
    this.maxFragments = hardBound2(
      options2.maxFragments,
      PROTOCOL_V4_LIMITS.logicalFrameAssemblyMaxFragments,
      "maxFragments"
    );
    this.maxConcurrentAssemblies = hardBound2(
      options2.maxConcurrentAssemblies,
      PROTOCOL_V4_LIMITS.logicalFrameAssemblyMaxConcurrent,
      "maxConcurrentAssemblies"
    );
    this.maxStagedDecodedBytes = hardBound2(
      options2.maxStagedDecodedBytes,
      PROTOCOL_V4_LIMITS.logicalFrameAssemblyMaxStagedBytes,
      "maxStagedDecodedBytes"
    );
    this.timeoutMs = hardBound2(
      options2.timeoutMs,
      PROTOCOL_V4_LIMITS.logicalFrameAssemblyTimeoutMs,
      "timeoutMs"
    );
    this.maxPhysicalFrameBytes = hardBound2(
      options2.maxPhysicalFrameBytes,
      PROTOCOL_V4_LIMITS.maxFrameBytes,
      "maxPhysicalFrameBytes"
    );
  }
  frameSchema;
  assemblies = /* @__PURE__ */ new Map();
  /** 每 route 的单调 ordinal tombstone；不会像 bounded id LRU 一样淘汰后复活旧帧。 */
  settledByRoute = /* @__PURE__ */ new Map();
  stagedDecodedBytes = 0;
  maxAssemblyBytes;
  maxFragments;
  maxConcurrentAssemblies;
  maxStagedDecodedBytes;
  timeoutMs;
  maxPhysicalFrameBytes;
  accept(wire, now = Date.now()) {
    const events = this.expire(now);
    const key2 = routeKey(wire.topic, wire.subscriptionId);
    if (!Number.isSafeInteger(wire.logicalFrameOrdinal) || wire.logicalFrameOrdinal < 1) {
      events.push(this.fault(wire, "proto.frameAssemblyMetadataMismatch"));
      return events;
    }
    const settled = this.settledByRoute.get(key2);
    if (settled) {
      if (wire.logicalFrameOrdinal < settled.logicalFrameOrdinal) return events;
      if (wire.logicalFrameOrdinal === settled.logicalFrameOrdinal) {
        if (wire.logicalFrameId !== settled.logicalFrameId) {
          events.push(this.fault(wire, "proto.frameAssemblyOrdinalConflict"));
        }
        return events;
      }
    }
    const deliveryKind = parseDeliveryKind(wire.deliveryKind);
    if (deliveryKind === "recovery") {
      for (let index = events.length - 1; index >= 0; index -= 1) {
        const event = events[index];
        if (event?.kind === "fault" && event.fault.topic === wire.topic && event.fault.subscriptionId === wire.subscriptionId && event.fault.logicalFrameOrdinal < wire.logicalFrameOrdinal) {
          events.splice(index, 1);
        }
      }
    }
    const current = this.assemblies.get(key2);
    if (current) {
      if (wire.logicalFrameOrdinal < current.logicalFrameOrdinal) return events;
      if (wire.logicalFrameOrdinal === current.logicalFrameOrdinal) {
        if (wire.logicalFrameId !== current.logicalFrameId) {
          this.release(key2, current);
          this.settle(key2, current);
          events.push(this.fault(wire, "proto.frameAssemblyOrdinalConflict"));
          return events;
        }
      } else {
        this.release(key2, current);
        this.settle(key2, current);
        if (deliveryKind !== "recovery") {
          events.push(this.fault(current, "proto.frameAssemblySuperseded"));
        }
      }
    }
    if (deliveryKind === null) {
      this.releaseAndSettle(key2, wire);
      events.push(this.fault(wire, "proto.frameAssemblyMetadataMismatch"));
      return events;
    }
    if (wire.kind === "complete") {
      if (!hasOnlyKeys(wire, COMPLETE_WIRE_KEYS) || !Object.prototype.hasOwnProperty.call(wire, "frame")) {
        this.releaseAndSettle(key2, wire);
        events.push(this.fault(wire, "proto.frameAssemblyMetadataMismatch"));
        return events;
      }
      if (measureTopicNotificationEnvelopeBytes(wire).maxBytes > this.maxPhysicalFrameBytes) {
        this.releaseAndSettle(key2, wire);
        events.push(this.fault(wire, "proto.frameEnvelopeTooLarge"));
        return events;
      }
      const active = this.assemblies.get(key2);
      if (active) {
        this.release(key2, active);
        this.settle(key2, active);
        events.push(this.fault(active, "proto.frameAssemblyMetadataMismatch"));
        return events;
      }
      const logicalBytes = new TextEncoder().encode(JSON.stringify(wire.frame)).byteLength;
      if (logicalBytes > this.maxAssemblyBytes) {
        this.settle(key2, wire);
        events.push(this.fault(wire, "proto.frameAssemblyTooLarge"));
        return events;
      }
      if (!frameMatchesEnvelope(wire.frame, wire)) {
        this.settle(key2, wire);
        events.push(this.fault(wire, "proto.frameAssemblyMetadataMismatch"));
        return events;
      }
      const parsed2 = this.frameSchema.safeParse(wire.frame);
      if (!parsed2.success) {
        this.settle(key2, wire);
        events.push(this.fault(wire, WIRE_FAULT_INVALID_PAYLOAD));
        return events;
      }
      this.settle(key2, wire);
      events.push({ kind: "complete", frame: parsed2.data, deliveryKind });
      return events;
    }
    if (!hasValidFragmentInner(wire)) {
      this.releaseAndSettle(key2, wire);
      events.push(this.fault(wire, "proto.frameAssemblyMetadataMismatch"));
      return events;
    }
    if (measureTopicNotificationEnvelopeBytes(wire).maxBytes > this.maxPhysicalFrameBytes) {
      this.releaseAndSettle(key2, wire);
      events.push(this.fault(wire, "proto.frameEnvelopeTooLarge"));
      return events;
    }
    if (wire.fragmentCount > this.maxFragments) {
      this.releaseAndSettle(key2, wire);
      events.push(this.fault(wire, "proto.frameFragmentCountExceeded"));
      return events;
    }
    if (wire.logicalBytes > this.maxAssemblyBytes) {
      this.releaseAndSettle(key2, wire);
      events.push(this.fault(wire, "proto.frameAssemblyTooLarge"));
      return events;
    }
    if (!Number.isInteger(wire.fragmentCount) || wire.fragmentCount < 1 || !Number.isInteger(wire.fragmentIndex) || wire.fragmentIndex < 0 || wire.fragmentIndex >= wire.fragmentCount || !Number.isInteger(wire.logicalBytes) || wire.logicalBytes < 1 || wire.fragmentCount > wire.logicalBytes || wire.checksum.algorithm !== "crc32" || !/^[0-9a-f]{8}$/u.test(wire.checksum.value)) {
      this.releaseAndSettle(key2, wire);
      events.push(this.fault(wire, "proto.frameAssemblyMetadataMismatch"));
      return events;
    }
    const decoded = decodeWireBase64(wire.dataBase64);
    if (!decoded) {
      this.releaseAndSettle(key2, wire);
      events.push(this.fault(wire, "proto.frameAssemblyInvalidBase64"));
      return events;
    }
    let assembly = this.assemblies.get(key2);
    if (assembly) {
      if (assembly.fragmentCount !== wire.fragmentCount || assembly.deliveryKind !== deliveryKind || assembly.logicalBytes !== wire.logicalBytes || assembly.checksum.algorithm !== wire.checksum.algorithm || assembly.checksum.value !== wire.checksum.value) {
        this.release(key2, assembly);
        this.settle(key2, assembly);
        events.push(this.fault(wire, "proto.frameAssemblyMetadataMismatch"));
        return events;
      }
    } else {
      if (this.assemblies.size >= this.maxConcurrentAssemblies) {
        this.settle(key2, wire);
        events.push(this.fault(wire, "proto.frameAssemblyConcurrentLimit"));
        return events;
      }
      if (this.stagedDecodedBytes + decoded.byteLength > this.maxStagedDecodedBytes) {
        this.settle(key2, wire);
        events.push(this.fault(wire, "proto.frameAssemblyBudgetExceeded"));
        return events;
      }
      assembly = {
        deliveryKind,
        logicalFrameId: wire.logicalFrameId,
        logicalFrameOrdinal: wire.logicalFrameOrdinal,
        topic: wire.topic,
        subscriptionId: wire.subscriptionId,
        fragmentCount: wire.fragmentCount,
        logicalBytes: wire.logicalBytes,
        checksum: { algorithm: "crc32", value: wire.checksum.value },
        fragments: Array.from(
          { length: wire.fragmentCount },
          () => void 0
        ),
        receivedCount: 0,
        decodedBytes: 0,
        firstSeenAt: now
      };
      this.assemblies.set(key2, assembly);
    }
    const previous = assembly.fragments[wire.fragmentIndex];
    if (previous) {
      if (!bytesEqual(previous, decoded)) {
        this.release(key2, assembly);
        this.settle(key2, assembly);
        events.push(this.fault(wire, "proto.frameAssemblyFragmentConflict"));
      }
      return events;
    }
    if (this.stagedDecodedBytes + decoded.byteLength > this.maxStagedDecodedBytes) {
      this.release(key2, assembly);
      this.settle(key2, assembly);
      events.push(this.fault(wire, "proto.frameAssemblyBudgetExceeded"));
      return events;
    }
    if (assembly.decodedBytes + decoded.byteLength > assembly.logicalBytes) {
      this.release(key2, assembly);
      this.settle(key2, assembly);
      events.push(this.fault(wire, "proto.frameAssemblyLengthMismatch"));
      return events;
    }
    assembly.fragments[wire.fragmentIndex] = decoded;
    assembly.receivedCount += 1;
    assembly.decodedBytes += decoded.byteLength;
    this.stagedDecodedBytes += decoded.byteLength;
    if (assembly.receivedCount !== assembly.fragmentCount) return events;
    this.release(key2, assembly);
    if (assembly.decodedBytes !== assembly.logicalBytes) {
      this.settle(key2, assembly);
      events.push(this.fault(assembly, "proto.frameAssemblyLengthMismatch"));
      return events;
    }
    const logical = new Uint8Array(assembly.decodedBytes);
    let offset = 0;
    for (const fragment of assembly.fragments) {
      if (!fragment) {
        this.settle(key2, assembly);
        events.push(this.fault(assembly, "proto.frameAssemblyLengthMismatch"));
        return events;
      }
      logical.set(fragment, offset);
      offset += fragment.byteLength;
    }
    if (crc32WireBytes(logical) !== assembly.checksum.value) {
      this.settle(key2, assembly);
      events.push(this.fault(assembly, "proto.frameAssemblyChecksumMismatch"));
      return events;
    }
    let json;
    try {
      json = new TextDecoder("utf-8", { fatal: true }).decode(logical);
    } catch {
      this.settle(key2, assembly);
      events.push(this.fault(assembly, "proto.frameAssemblyInvalidUtf8"));
      return events;
    }
    let value2;
    try {
      value2 = JSON.parse(json);
    } catch {
      this.settle(key2, assembly);
      events.push(this.fault(assembly, "proto.frameAssemblyInvalidJson"));
      return events;
    }
    if (!frameMatchesEnvelope(value2, assembly)) {
      this.settle(key2, assembly);
      events.push(this.fault(assembly, "proto.frameAssemblyMetadataMismatch"));
      return events;
    }
    const parsed = this.frameSchema.safeParse(value2);
    if (!parsed.success) {
      this.settle(key2, assembly);
      events.push(this.fault(assembly, WIRE_FAULT_INVALID_PAYLOAD));
      return events;
    }
    this.settle(key2, assembly);
    events.push({ kind: "complete", frame: parsed.data, deliveryKind: assembly.deliveryKind });
    return events;
  }
  expire(now = Date.now()) {
    const events = [];
    for (const [key2, assembly] of this.assemblies) {
      if (now - assembly.firstSeenAt < this.timeoutMs) continue;
      this.release(key2, assembly);
      this.settle(key2, assembly);
      events.push(this.fault(assembly, "proto.frameAssemblyTimedOut"));
    }
    return events;
  }
  discard(topic, subscriptionId) {
    const key2 = routeKey(topic, subscriptionId);
    const assembly = this.assemblies.get(key2);
    if (assembly) this.release(key2, assembly);
    this.settledByRoute.delete(key2);
  }
  /** fault 后释放该 route 的 active bytes，但保留 ordinal tombstone 防旧 replay 复活。 */
  abort(topic, subscriptionId) {
    const key2 = routeKey(topic, subscriptionId);
    const assembly = this.assemblies.get(key2);
    if (!assembly) return;
    this.release(key2, assembly);
    this.settle(key2, assembly);
  }
  clear() {
    this.assemblies.clear();
    this.settledByRoute.clear();
    this.stagedDecodedBytes = 0;
  }
  getStats() {
    return { assemblies: this.assemblies.size, stagedDecodedBytes: this.stagedDecodedBytes };
  }
  get nextExpiryAt() {
    let next = null;
    for (const assembly of this.assemblies.values()) {
      const expiresAt = assembly.firstSeenAt + this.timeoutMs;
      if (next === null || expiresAt < next) next = expiresAt;
    }
    return next;
  }
  release(key2, assembly) {
    if (this.assemblies.get(key2) !== assembly) return;
    this.assemblies.delete(key2);
    this.stagedDecodedBytes -= assembly.decodedBytes;
  }
  settle(key2, frame) {
    const previous = this.settledByRoute.get(key2);
    if (previous && previous.logicalFrameOrdinal > frame.logicalFrameOrdinal) return;
    this.settledByRoute.set(key2, {
      logicalFrameId: frame.logicalFrameId,
      logicalFrameOrdinal: frame.logicalFrameOrdinal
    });
  }
  releaseAndSettle(key2, frame) {
    const active = this.assemblies.get(key2);
    if (active && active.logicalFrameOrdinal === frame.logicalFrameOrdinal && active.logicalFrameId === frame.logicalFrameId) {
      this.release(key2, active);
    }
    this.settle(key2, frame);
  }
  fault(source, reasonCode) {
    const deliveryKind = parseDeliveryKind(source.deliveryKind);
    return {
      kind: "fault",
      fault: {
        ...deliveryKind === null ? {} : { deliveryKind },
        reasonCode,
        logicalFrameId: source.logicalFrameId,
        logicalFrameOrdinal: source.logicalFrameOrdinal,
        topic: source.topic,
        subscriptionId: source.subscriptionId
      }
    };
  }
};


module.exports={TopicWireFrameAssembler,PROTOCOL_V4_LIMITS,crc32WireBytes,encodeWireBytesBase64,measureTopicNotificationEnvelopeBytes};

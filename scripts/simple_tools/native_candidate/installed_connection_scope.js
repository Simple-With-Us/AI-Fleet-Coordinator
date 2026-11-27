// Exact pinned source declarations. Fixture injects external dependencies.
var TRUSTED_CONNECTION_FIELD = "__zcodeTrustedV4Connection";
var TRUSTED_UNSUBSCRIBE_ROUTE_FIELD = "__zcodeTrustedV4UnsubscribeRoute";
function readTrustedZCodeAgentV4UnsubscribeRoute(value2) {
  if (typeof value2 !== "object" || value2 === null) return null;
  const route = value2[TRUSTED_UNSUBSCRIBE_ROUTE_FIELD];
  if (!route || typeof route.topic !== "string" || typeof route.connectionId !== "string") {
    return null;
  }
  return route;
}
function readTrustedZCodeAgentV4Connection(value2) {
  if (typeof value2 !== "object" || value2 === null) return null;
  const context = value2[TRUSTED_CONNECTION_FIELD];
  if (!context || typeof context.connectionId !== "string") return null;
  if (context.clientMode !== "desktop-continuous" && context.clientMode !== "web-remote-replayable") {
    return null;
  }
  return {
    connectionId: context.connectionId,
    clientMode: context.clientMode,
    // 只认 true：缺席与 false 都是「旧消费者」，键在场与否是下游读端的判据。
    ...context.workflowRunDeltas === true ? { workflowRunDeltas: true } : {},
    ...context.pluginDiscoveryUi === true ? { pluginDiscoveryUi: true } : {},
    ...context.historyFirst === true ? { historyFirst: true } : {}
  };
}
function withTrustedConnection(value2, context) {
  const forwarded = {
    ...value2
  };
  delete forwarded[TRUSTED_CONNECTION_FIELD];
  delete forwarded["connectionId"];
  delete forwarded["clientMode"];
  delete forwarded["deliveryProfile"];
  delete forwarded["subscriberScope"];
  delete forwarded["workflowRunDeltas"];
  delete forwarded["pluginDiscoveryUi"];
  delete forwarded["historyFirst"];
  forwarded[TRUSTED_CONNECTION_FIELD] = context;
  return forwarded;
}
function withTrustedUnsubscribeRoute(value2, route) {
  const forwarded = {
    ...value2
  };
  delete forwarded[TRUSTED_UNSUBSCRIBE_ROUTE_FIELD];
  forwarded[TRUSTED_UNSUBSCRIBE_ROUTE_FIELD] = route;
  return forwarded;
}
function carriesExtensionInputSource(envelope2) {
  const payload = envelope2.payload && typeof envelope2.payload === "object" ? envelope2.payload : void 0;
  if (envelope2.type === "sendText") return payload?.source?.kind === "extension";
  if (envelope2.type === "createSession") return payload?.firstInput?.source?.kind === "extension";
  return false;
}
function workspaceKey(target2) {
  return target2.workspaceIdentity?.trim() || target2.workspacePath;
}
function workspaceTarget(target2) {
  return {
    workspacePath: target2.workspacePath,
    ...target2.workspaceIdentity ? { workspaceIdentity: target2.workspaceIdentity } : {}
  };
}
var MAX_PENDING_OWNERSHIP_FRAMES = 1024;
var MAX_PENDING_OWNERSHIP_BYTES = 32 * 1024 * 1024;
var MAX_DOWNSTREAM_CONNECTION_ID_LENGTH = 256;
function assertConnectionId(connectionId) {
  if (connectionId.trim().length === 0 || connectionId.length > MAX_DOWNSTREAM_CONNECTION_ID_LENGTH) {
    throw new Error("fault.connection.invalidConnectionId");
  }
}
function namespaceRelayConnectionId(upstreamConnectionId, downstreamConnectionId) {
  return `relay:${upstreamConnectionId.length}:${upstreamConnectionId}${downstreamConnectionId.length}:${downstreamConnectionId}`;
}
function frameBytes(frame) {
  return new TextEncoder().encode(JSON.stringify(frame)).byteLength;
}
function deliveryProfileFor(clientMode) {
  return clientMode === "desktop-continuous" ? "continuous" : "replayable";
}
function createZCodeAgentConnectionScope(base2, context) {
  assertConnectionId(context.connectionId);
  const role2 = context.role ?? "terminal-client";
  const owned = /* @__PURE__ */ new Map();
  const routeKeyByOwnership = /* @__PURE__ */ new Map();
  const pendingByOwnership = /* @__PURE__ */ new Map();
  const routedFrameEvents = /* @__PURE__ */ new Map();
  const runtimeGenerationByWorkspaceKey = /* @__PURE__ */ new Map();
  let disposed = false;
  let handshakeComplete = role2 === "trusted-host-relay";
  let helloIssued = role2 === "trusted-host-relay";
  let boundClientId = null;
  let clientWorkflowRunDeltas = false;
  let pluginDiscoveryUiSupported = false;
  let clientHistoryFirst = false;
  let commandQueryWorkspaceKey = null;
  let currentTransportFlowState = "drained";
  let flowClosed = false;
  let flowUpdateChain = Promise.resolve();
  const forwardedFlowStateByRoute = /* @__PURE__ */ new Map();
  const attachmentFlowRoutes = /* @__PURE__ */ new Map();
  const forwardedConnection = (params) => {
    const downstream = readTrustedZCodeAgentV4Connection(params);
    if (role2 === "trusted-host-relay" && downstream) {
      assertConnectionId(downstream.connectionId);
      return {
        connectionId: namespaceRelayConnectionId(context.connectionId, downstream.connectionId),
        clientMode: downstream.clientMode,
        // 增量位属于**下游那一端**：relay 自己不消费帧，只把下游 clientHello 的声明带上去。
        ...downstream.workflowRunDeltas === true ? { workflowRunDeltas: true } : {},
        ...downstream.pluginDiscoveryUi === true ? { pluginDiscoveryUi: true } : {},
        ...downstream.historyFirst === true ? { historyFirst: true } : {}
      };
    }
    return {
      connectionId: context.connectionId,
      clientMode: context.clientMode,
      ...clientWorkflowRunDeltas ? { workflowRunDeltas: true } : {},
      ...role2 === "terminal-client" && pluginDiscoveryUiSupported ? { pluginDiscoveryUi: true } : {},
      ...clientHistoryFirst ? { historyFirst: true } : {}
    };
  };
  const assertOpen = () => {
    if (disposed) throw new Error("fault.connection.closed");
  };
  const assertReady = () => {
    assertOpen();
    if (!handshakeComplete) throw new Error("fault.connection.handshakeRequired");
  };
  const routeKey2 = (kind, target2, topic, subscriptionId, connectionId) => `${kind}\0${workspaceKey(target2)}\0${topic}\0${subscriptionId}\0${connectionId}`;
  const ownershipKey = (kind, target2, topic) => `${kind}\0${workspaceKey(target2)}\0${topic}`;
  const routedEventKey = (kind, target2) => `${kind}\0${workspaceKey(target2)}`;
  const flowRouteKey = (target2, connectionId) => `${workspaceKey(target2)}\0${connectionId}`;
  const flowRouteForEntry = (entry2) => ({
    key: flowRouteKey(entry2.target, entry2.connectionId),
    target: entry2.target,
    connection: {
      connectionId: entry2.connectionId,
      clientMode: entry2.clientMode
    }
  });
  const currentFlowRoutes = () => {
    const routes = new Map(attachmentFlowRoutes);
    for (const entry2 of owned.values()) {
      const route = flowRouteForEntry(entry2);
      routes.set(route.key, route);
    }
    return routes;
  };
  const forwardFlowRoute = async (route, state) => {
    if (forwardedFlowStateByRoute.get(route.key) === state) return;
    await base2.setConnectionFlowStateV4(
      withTrustedConnection(
        {
          ...workspaceTarget(route.target),
          state
        },
        route.connection
      )
    );
    forwardedFlowStateByRoute.set(route.key, state);
  };
  const applyTransportFlowState = async (state) => {
    if (flowClosed && state !== "closed") return;
    currentTransportFlowState = state;
    if (state === "closed") flowClosed = true;
    for (const route of currentFlowRoutes().values()) {
      await forwardFlowRoute(route, state);
    }
  };
  const enqueueTransportFlowState = (state) => {
    if (disposed || flowClosed && state !== "closed") return Promise.resolve();
    const update = flowUpdateChain.then(() => applyTransportFlowState(state));
    flowUpdateChain = update.catch(() => {
    });
    return update;
  };
  const syncCurrentFlowForEntry = async (entry2) => {
    if (currentTransportFlowState !== "saturated") return;
    await forwardFlowRoute(flowRouteForEntry(entry2), "saturated");
  };
  const closeUnusedFlowRoute = async (entry2) => {
    const route = flowRouteForEntry(entry2);
    if (currentFlowRoutes().has(route.key)) return;
    if (forwardedFlowStateByRoute.has(route.key)) {
      await forwardFlowRoute(route, "closed");
      forwardedFlowStateByRoute.delete(route.key);
    }
  };
  const remember = (subscriptionId, entry2) => {
    const key2 = routeKey2(entry2.kind, entry2.target, entry2.topic, subscriptionId, entry2.connectionId);
    const previous = routeKeyByOwnership.get(entry2.ownershipKey);
    if (previous) owned.delete(previous);
    routeKeyByOwnership.set(entry2.ownershipKey, key2);
    owned.set(key2, entry2);
  };
  const findBySubscription = (kind, target2, subscriptionId, expectedRoute) => {
    const expectedWorkspaceKey = workspaceKey(target2);
    const matches = [...owned.values()].filter(
      (entry2) => entry2.kind === kind && entry2.subscriptionId === subscriptionId && workspaceKey(entry2.target) === expectedWorkspaceKey && (!expectedRoute || entry2.topic === expectedRoute.topic && entry2.connectionId === expectedRoute.connectionId)
    );
    return matches.length === 1 ? matches[0] : null;
  };
  const ownsFrame = (kind, target2, frame) => {
    const expectedWorkspaceKey = workspaceKey(target2);
    return [...owned.values()].some(
      (entry2) => entry2.kind === kind && entry2.subscriptionId === frame.subscriptionId && entry2.topic === frame.topic && workspaceKey(entry2.target) === expectedWorkspaceKey
    );
  };
  const forwardedUnsubscribeRoute = (params) => {
    if (role2 !== "trusted-host-relay") return null;
    const downstream = readTrustedZCodeAgentV4UnsubscribeRoute(params);
    if (!downstream) return null;
    assertConnectionId(downstream.connectionId);
    return {
      topic: downstream.topic,
      connectionId: namespaceRelayConnectionId(context.connectionId, downstream.connectionId)
    };
  };
  const beginPendingOwnership = (kind, target2, topic) => {
    const pending = {
      kind,
      target: workspaceTarget(target2),
      topic,
      frames: [],
      stagedBytes: 0,
      overflowReason: null,
      runtimeGeneration: runtimeGenerationByWorkspaceKey.get(workspaceKey(target2)) ?? 0,
      invalidatedByRuntimeRestart: false
    };
    const key2 = ownershipKey(kind, target2, topic);
    const group = pendingByOwnership.get(key2) ?? /* @__PURE__ */ new Set();
    group.add(pending);
    pendingByOwnership.set(key2, group);
    return pending;
  };
  const removePendingOwnership = (pending) => {
    const key2 = ownershipKey(pending.kind, pending.target, pending.topic);
    const group = pendingByOwnership.get(key2);
    group?.delete(pending);
    if (group?.size === 0) pendingByOwnership.delete(key2);
  };
  const discardPendingOwnership = (pending) => {
    removePendingOwnership(pending);
    pending.frames.length = 0;
    pending.stagedBytes = 0;
    pending.overflowReason = null;
  };
  const stageFrame = (pending, frame) => {
    if (pending.overflowReason || pending.invalidatedByRuntimeRestart) return;
    const bytes = frameBytes(frame);
    if (pending.frames.length + 1 > MAX_PENDING_OWNERSHIP_FRAMES || pending.stagedBytes + bytes > MAX_PENDING_OWNERSHIP_BYTES) {
      pending.frames.length = 0;
      pending.stagedBytes = 0;
      pending.overflowReason = "fault.subscription.initialFrameStagingOverflow";
      return;
    }
    pending.frames.push(frame);
    pending.stagedBytes += bytes;
  };
  const routeIncomingFrame = (kind, target2, frame, emitter) => {
    if (ownsFrame(kind, target2, frame)) {
      emitter.fire(frame);
      return;
    }
    const group = pendingByOwnership.get(ownershipKey(kind, target2, frame.topic));
    if (!group) return;
    for (const pending of group) stageFrame(pending, frame);
  };
  const routedEvent = (kind, target2, source) => {
    const key2 = routedEventKey(kind, target2);
    let route = routedFrameEvents.get(key2);
    if (!route) {
      const emitter = new Emitter();
      route = {
        emitter,
        upstream: source((frame) => routeIncomingFrame(kind, target2, frame, emitter))
      };
      routedFrameEvents.set(key2, route);
    }
    return route.emitter.event;
  };
  const forget = (entry2) => {
    const key2 = routeKey2(
      entry2.kind,
      entry2.target,
      entry2.topic,
      entry2.subscriptionId,
      entry2.connectionId
    );
    owned.delete(key2);
    if (routeKeyByOwnership.get(entry2.ownershipKey) === key2) {
      routeKeyByOwnership.delete(entry2.ownershipKey);
    }
  };
  const unsubscribeBase = (entry2, runtimePolicy) => {
    const params = {
      ...entry2.target,
      subscriptionId: entry2.subscriptionId,
      ...runtimePolicy ? { runtimePolicy } : {}
    };
    const forwarded = withTrustedUnsubscribeRoute(params, {
      topic: entry2.topic,
      connectionId: entry2.connectionId
    });
    switch (entry2.kind) {
      case "conversation":
        return base2.unsubscribeConversationV4(forwarded);
      case "sessions-index":
        return base2.unsubscribeSessionsIndexV4(forwarded);
      case "workspace-config":
        return base2.unsubscribeWorkspaceConfigV4(forwarded);
    }
  };
  const unsubscribeOwnedEntry = async (entry2, runtimePolicy) => {
    forget(entry2);
    try {
      await closeUnusedFlowRoute(entry2);
    } finally {
      await unsubscribeBase(entry2, runtimePolicy);
    }
  };
  const resyncBase = (entry2, params) => {
    const forwarded = withTrustedUnsubscribeRoute(params, {
      topic: entry2.topic,
      connectionId: entry2.connectionId
    });
    switch (entry2.kind) {
      case "conversation":
        return base2.resyncConversationV4(forwarded);
      case "sessions-index":
        return base2.resyncSessionsIndexV4(forwarded);
      case "workspace-config":
        return base2.resyncWorkspaceConfigV4(forwarded);
    }
  };
  const resyncOwned = (kind, params) => {
    assertReady();
    const entry2 = findBySubscription(
      kind,
      params,
      params.subscriptionId,
      forwardedUnsubscribeRoute(params)
    );
    if (!entry2) return Promise.reject(new Error("fault.subscription.notOwned"));
    return resyncBase(entry2, params);
  };
  const rememberAfterSubscribe = async (subscriptionId, entry2, pending) => {
    removePendingOwnership(pending);
    const complete2 = { ...entry2, subscriptionId };
    if (pending.invalidatedByRuntimeRestart || pending.runtimeGeneration !== (runtimeGenerationByWorkspaceKey.get(workspaceKey(entry2.target)) ?? 0)) {
      discardPendingOwnership(pending);
      throw new Error("fault.subscription.runtimeRestarted");
    }
    if (disposed) {
      discardPendingOwnership(pending);
      await unsubscribeBase(complete2).catch(() => {
      });
      throw new Error("fault.connection.closed");
    }
    if (pending.overflowReason) {
      const reason = pending.overflowReason;
      discardPendingOwnership(pending);
      await unsubscribeBase(complete2).catch(() => {
      });
      throw new Error(reason);
    }
    remember(subscriptionId, complete2);
    try {
      await syncCurrentFlowForEntry(complete2);
    } catch (error) {
      forget(complete2);
      await unsubscribeBase(complete2).catch(() => {
      });
      throw error;
    }
    const route = routedFrameEvents.get(routedEventKey(entry2.kind, entry2.target));
    if (!route) return;
    for (const frame of pending.frames) {
      if (frame.subscriptionId === subscriptionId && ownsFrame(entry2.kind, entry2.target, frame)) {
        route.emitter.fire(frame);
      }
    }
    pending.frames.length = 0;
    pending.stagedBytes = 0;
  };
  const invalidateWorkspaceRuntime = (invalidatedWorkspaceKey, generation2) => {
    runtimeGenerationByWorkspaceKey.set(invalidatedWorkspaceKey, generation2);
    for (const entry2 of owned.values()) {
      if (workspaceKey(entry2.target) === invalidatedWorkspaceKey) forget(entry2);
    }
    for (const key2 of forwardedFlowStateByRoute.keys()) {
      if (key2.startsWith(`${invalidatedWorkspaceKey}\0`)) {
        forwardedFlowStateByRoute.delete(key2);
      }
    }
    const invalidatedPending = [...pendingByOwnership.values()].flatMap((group) => [...group]).filter((pending) => workspaceKey(pending.target) === invalidatedWorkspaceKey);
    for (const pending of invalidatedPending) {
      pending.invalidatedByRuntimeRestart = true;
      removePendingOwnership(pending);
      pending.frames.length = 0;
      pending.stagedBytes = 0;
    }
  };
  const hasRuntimeLifecycle = Boolean(base2.onAgentRuntimeLifecycle);
  const runtimeLifecycleDisposable = base2.onAgentRuntimeLifecycle?.((event) => {
    if (event.state === "available") {
      return;
    }
    invalidateWorkspaceRuntime(event.workspaceKey, event.runtimeIdentity.generation);
  });
  const runtimeRestartDisposable = hasRuntimeLifecycle ? void 0 : base2.onAgentRuntimeRestarted?.(({ workspaceKey: restarted }) => {
    const nextGeneration = (runtimeGenerationByWorkspaceKey.get(restarted) ?? 0) + 1;
    invalidateWorkspaceRuntime(restarted, nextGeneration);
  });
  const overrides = {
    onDynamicSessionLifecycle(params) {
      return (listener) => base2.onDynamicSessionLifecycle(params)((snapshot) => {
        if (snapshot.state === "stopping") {
          const topic = conversationTopic(snapshot.sessionId);
          const key2 = workspaceKey(params);
          for (const entry2 of owned.values()) {
            if (workspaceKey(entry2.target) === key2 && entry2.topic === topic) forget(entry2);
          }
          for (const group of [...pendingByOwnership.values()]) {
            for (const pending of [...group]) {
              if (workspaceKey(pending.target) !== key2 || pending.topic !== topic) continue;
              pending.invalidatedByRuntimeRestart = true;
              removePendingOwnership(pending);
              pending.frames.length = 0;
              pending.stagedBytes = 0;
            }
          }
        }
        listener(snapshot);
      });
    },
    async helloConversationV4() {
      assertOpen();
      helloIssued = true;
      return createHello(context);
    },
    async initializeConversationV4(clientHello) {
      assertOpen();
      if (!helloIssued) throw new Error("fault.connection.helloRequired");
      const parsed = clientHelloSchema.parse(clientHello);
      if (boundClientId !== null && boundClientId !== parsed.clientId) {
        throw new Error("fault.connection.clientChanged");
      }
      boundClientId = parsed.clientId;
      clientWorkflowRunDeltas = clientSupportsWorkflowRunDeltas(parsed);
      pluginDiscoveryUiSupported = parsed.capabilities?.pluginDiscoveryUi === true;
      clientHistoryFirst = parsed.capabilities?.historyFirst === true;
      handshakeComplete = true;
    },
    async setConnectionFlowStateV4(params) {
      assertOpen();
      if (role2 !== "trusted-host-relay") {
        throw new Error("fault.connection.flowControlForbidden");
      }
      const downstream = readTrustedZCodeAgentV4Connection(params);
      if (!downstream) throw new Error("fault.connection.flowControlUntrusted");
      const forwarded = forwardedConnection(params);
      const ownsRoute = attachmentFlowRoutes.has(flowRouteKey(params, forwarded.connectionId)) || [...owned.values()].some(
        (entry2) => workspaceKey(entry2.target) === workspaceKey(params) && entry2.connectionId === forwarded.connectionId
      );
      if (!ownsRoute) throw new Error("fault.subscription.notOwned");
      await base2.setConnectionFlowStateV4(withTrustedConnection(params, forwarded));
    },
    async sendConversationCommandV4(params) {
      assertOpen();
      if (carriesExtensionInputSource(params.envelope)) {
        throw new Error("fault.command.extensionSourceUntrusted");
      }
      if (role2 === "terminal-client") {
        assertReady();
        if (params.envelope.clientId !== boundClientId) {
          throw new Error("fault.command.clientMismatch");
        }
      }
      return base2.sendConversationCommandV4(
        withTrustedConnection(params, forwardedConnection(params))
      );
    },
    async queryConversationCommandsV4(params) {
      assertReady();
      if (params.clock)
        return base2.queryConversationCommandsV4(
          withTrustedConnection(params, forwardedConnection(params))
        );
      const requestedWorkspaceKey = workspaceKey(params);
      if (commandQueryWorkspaceKey !== null && commandQueryWorkspaceKey !== requestedWorkspaceKey) {
        throw new Error("fault.command.queryForeignWorkspace");
      }
      commandQueryWorkspaceKey = requestedWorkspaceKey;
      return base2.queryConversationCommandsV4(
        withTrustedConnection(params, forwardedConnection(params))
      );
    },
    async backgroundBashOutputV4(params) {
      assertReady();
      return base2.backgroundBashOutputV4(
        withTrustedConnection(params, forwardedConnection(params))
      );
    },
    async conversationRowsRangeV4(params) {
      assertReady();
      return base2.conversationRowsRangeV4(
        withTrustedConnection(params, forwardedConnection(params))
      );
    },
    async attachmentBeginV4(params) {
      assertReady();
      const forwarded = forwardedConnection(params);
      const result = await base2.attachmentBeginV4(withTrustedConnection(params, forwarded));
      if (result.state === "staging") {
        if (disposed || flowClosed) {
          await base2.attachmentAbortV4(withTrustedConnection(params, forwarded)).catch(() => {
          });
          throw new Error("fault.connection.closed");
        }
        const route = {
          key: flowRouteKey(params, forwarded.connectionId),
          target: workspaceTarget(params),
          connection: forwarded
        };
        attachmentFlowRoutes.set(route.key, route);
        if (currentTransportFlowState === "saturated") {
          try {
            await forwardFlowRoute(route, "saturated");
          } catch (error) {
            attachmentFlowRoutes.delete(route.key);
            await base2.attachmentAbortV4(withTrustedConnection(params, forwarded)).catch(() => {
            });
            throw error;
          }
        }
      }
      return result;
    },
    async attachmentReadV4(params) {
      assertReady();
      return base2.attachmentReadV4(withTrustedConnection(params, forwardedConnection(params)));
    },
    async conversationAttachmentReadV4(params) {
      assertReady();
      return base2.conversationAttachmentReadV4(
        withTrustedConnection(params, forwardedConnection(params))
      );
    },
    async conversationAttachmentStatV4(params) {
      assertReady();
      return base2.conversationAttachmentStatV4(
        withTrustedConnection(params, forwardedConnection(params))
      );
    },
    async attachmentPreviewSourceV4(params) {
      assertReady();
      return base2.attachmentPreviewSourceV4(
        withTrustedConnection(params, forwardedConnection(params))
      );
    },
    async attachmentChunkV4(params) {
      assertReady();
      return base2.attachmentChunkV4(withTrustedConnection(params, forwardedConnection(params)));
    },
    async attachmentCommitV4(params) {
      assertReady();
      const forwarded = forwardedConnection(params);
      return base2.attachmentCommitV4(withTrustedConnection(params, forwarded));
    },
    async attachmentAbortV4(params) {
      assertReady();
      const forwarded = forwardedConnection(params);
      await base2.attachmentAbortV4(withTrustedConnection(params, forwarded));
    },
    async subscribeConversationV4(params) {
      assertReady();
      const forwarded = forwardedConnection(params);
      const topic = conversationTopic(params.sessionId);
      const pending = beginPendingOwnership("conversation", params, topic);
      try {
        const result = await base2.subscribeConversationV4(withTrustedConnection(params, forwarded));
        await rememberAfterSubscribe(
          result.ack.subscriptionId,
          {
            kind: "conversation",
            ownershipKey: `conversation\0${workspaceKey(params)}\0${topic}\0${forwarded.connectionId}`,
            topic,
            connectionId: forwarded.connectionId,
            clientMode: forwarded.clientMode,
            target: workspaceTarget(params)
          },
          pending
        );
        return result;
      } catch (error) {
        discardPendingOwnership(pending);
        throw error;
      }
    },
    async unsubscribeConversationV4(params) {
      const entry2 = findBySubscription(
        "conversation",
        params,
        params.subscriptionId,
        forwardedUnsubscribeRoute(params)
      );
      if (!entry2) return;
      await unsubscribeOwnedEntry(entry2, params.runtimePolicy);
    },
    async resyncConversationV4(params) {
      return resyncOwned("conversation", params);
    },
    onDynamicConversationFrame(params) {
      return routedEvent(
        "conversation",
        params,
        base2.onDynamicConversationFrame(params)
      );
    },
    onDynamicLocalTtftFacts(params) {
      assertOpen();
      if (role2 !== "terminal-client" || context.clientMode !== "desktop-continuous" || params.workspaceIdentity?.trim() || params.remoteSessionId)
        return Event.None;
      return base2.onDynamicLocalTtftFacts(workspaceTarget(params));
    },
    onDynamicConversationTelemetryFact(params) {
      assertOpen();
      const downstream = readTrustedZCodeAgentV4Connection(params);
      const relayDesktopDownstream = role2 === "trusted-host-relay" && downstream?.clientMode === "desktop-continuous";
      if (context.clientMode !== "desktop-continuous" || role2 !== "terminal-client" && !relayDesktopDownstream) {
        return Event.None;
      }
      return base2.onDynamicConversationTelemetryFact(
        withTrustedConnection(workspaceTarget(params), forwardedConnection(params))
      );
    },
    onDynamicPluginUiToolResult() {
      assertOpen();
      if (role2 !== "terminal-client" || context.clientMode !== "desktop-continuous")
        return Event.None;
      return base2.onDynamicPluginUiToolResult();
    },
    onDynamicCuaPermissionObservation() {
      assertOpen();
      if (role2 !== "terminal-client" || context.clientMode !== "desktop-continuous") {
        return Event.None;
      }
      return base2.onDynamicCuaPermissionObservation();
    },
    onDynamicProcessResourceSample() {
      assertOpen();
      if (role2 !== "trusted-host-relay") {
        return Event.None;
      }
      return base2.onDynamicProcessResourceSample();
    },
    onDynamicToolExecResource() {
      if (disposed || role2 !== "trusted-host-relay") return Event.None;
      return base2.onDynamicToolExecResource();
    },
    onDynamicMcpResourceSamples() {
      if (disposed || role2 !== "trusted-host-relay") return Event.None;
      return base2.onDynamicMcpResourceSamples();
    },
    onDynamicRemoteConversationTelemetry() {
      if (disposed || role2 !== "trusted-host-relay") return Event.None;
      return base2.onDynamicRemoteConversationTelemetry();
    },
    onDynamicMcpTelemetry() {
      assertOpen();
      if (role2 !== "trusted-host-relay") {
        return Event.None;
      }
      return base2.onDynamicMcpTelemetry();
    },
    async subscribeSessionsIndexV4(params) {
      assertReady();
      const forwarded = forwardedConnection(params);
      const topic = sessionsIndexTopic(workspaceKey(params));
      const pending = beginPendingOwnership("sessions-index", params, topic);
      try {
        const result = await base2.subscribeSessionsIndexV4(
          withTrustedConnection(params, forwarded)
        );
        await rememberAfterSubscribe(
          result.ack.subscriptionId,
          {
            kind: "sessions-index",
            ownershipKey: `sessions-index\0${topic}\0${forwarded.connectionId}`,
            topic,
            connectionId: forwarded.connectionId,
            clientMode: forwarded.clientMode,
            target: workspaceTarget(params)
          },
          pending
        );
        return result;
      } catch (error) {
        discardPendingOwnership(pending);
        throw error;
      }
    },
    async unsubscribeSessionsIndexV4(params) {
      const entry2 = findBySubscription(
        "sessions-index",
        params,
        params.subscriptionId,
        forwardedUnsubscribeRoute(params)
      );
      if (!entry2) return;
      await unsubscribeOwnedEntry(entry2, params.runtimePolicy);
    },
    async resyncSessionsIndexV4(params) {
      return resyncOwned("sessions-index", params);
    },
    onDynamicSessionsIndexFrame(params) {
      return routedEvent(
        "sessions-index",
        params,
        base2.onDynamicSessionsIndexFrame(params)
      );
    },
    async subscribeWorkspaceConfigV4(params) {
      assertReady();
      const forwarded = forwardedConnection(params);
      const topic = workspaceConfigTopic(workspaceKey(params));
      const pending = beginPendingOwnership("workspace-config", params, topic);
      try {
        const result = await base2.subscribeWorkspaceConfigV4(
          withTrustedConnection(params, forwarded)
        );
        await rememberAfterSubscribe(
          result.ack.subscriptionId,
          {
            kind: "workspace-config",
            ownershipKey: `workspace-config\0${topic}\0${forwarded.connectionId}`,
            topic,
            connectionId: forwarded.connectionId,
            clientMode: forwarded.clientMode,
            target: workspaceTarget(params)
          },
          pending
        );
        return result;
      } catch (error) {
        discardPendingOwnership(pending);
        throw error;
      }
    },
    async unsubscribeWorkspaceConfigV4(params) {
      const entry2 = findBySubscription(
        "workspace-config",
        params,
        params.subscriptionId,
        forwardedUnsubscribeRoute(params)
      );
      if (!entry2) return;
      await unsubscribeOwnedEntry(entry2, params.runtimePolicy);
    },
    async resyncWorkspaceConfigV4(params) {
      return resyncOwned("workspace-config", params);
    },
    onDynamicWorkspaceConfigFrame(params) {
      return routedEvent(
        "workspace-config",
        params,
        base2.onDynamicWorkspaceConfigFrame(params)
      );
    }
  };
  const service = new Proxy(base2, {
    get(target2, property, receiver) {
      const override = Reflect.get(overrides, property, receiver);
      if (override !== void 0) return override;
      const value2 = Reflect.get(target2, property, target2);
      return typeof value2 === "function" ? value2.bind(target2) : value2;
    }
  });
  return {
    service,
    setTransportFlowState(state) {
      return enqueueTransportFlowState(state);
    },
    async dispose() {
      if (disposed) return;
      await enqueueTransportFlowState("closed").catch(() => {
      });
      if (disposed) return;
      disposed = true;
      handshakeComplete = false;
      helloIssued = false;
      boundClientId = null;
      clientWorkflowRunDeltas = false;
      const entries = Array.from(owned.values());
      owned.clear();
      routeKeyByOwnership.clear();
      for (const group of pendingByOwnership.values()) {
        for (const pending of group) {
          pending.frames.length = 0;
          pending.stagedBytes = 0;
          pending.overflowReason = null;
        }
      }
      pendingByOwnership.clear();
      for (const route of routedFrameEvents.values()) {
        route.upstream.dispose();
        route.emitter.dispose();
      }
      routedFrameEvents.clear();
      runtimeLifecycleDisposable?.dispose();
      runtimeRestartDisposable?.dispose();
      runtimeGenerationByWorkspaceKey.clear();
      attachmentFlowRoutes.clear();
      forwardedFlowStateByRoute.clear();
      await Promise.allSettled(entries.map((entry2) => unsubscribeBase(entry2)));
    }
  };
}
globalThis.makeScope=createZCodeAgentConnectionScope;

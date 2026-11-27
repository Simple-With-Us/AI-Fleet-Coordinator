// Exact pinned source declarations. Fixture injects external dependencies.
function exposeWebSocket(ws, services, clientMode, workspaceIdentityMapper, resourcePeers) {
  const socket = wrapWebSocket(ws);
  const protocol = new SocketProtocol(socket);
  const rawServer = new ChannelServer(protocol, "server");
  const loggedServer = new LoggingChannelServer(
    rawServer,
    (...args) => log28.debug(void 0, ...args)
  );
  const server = workspaceIdentityMapper ? new SourceServerWorkspaceIdentityChannelServer(loggedServer, workspaceIdentityMapper) : loggedServer;
  const agentService = services.getOptional(IZCodeAgentService);
  const reverseClient = clientMode === "desktop-continuous" && resourcePeers ? new ChannelClient(protocol) : void 0;
  const rawResourceChannel = reverseClient?.getChannel(TOPIC_RESOURCE_RELAY_CHANNEL);
  const resourceChannel = rawResourceChannel && workspaceIdentityMapper ? {
    async call(command, args, token2) {
      const result = await rawResourceChannel.call(
        command,
        workspaceIdentityMapper.mapOutgoing(args),
        token2
      );
      return workspaceIdentityMapper.mapIncoming(result);
    },
    listen(event, args) {
      return Event.map(
        rawResourceChannel.listen(event, workspaceIdentityMapper.mapOutgoing(args)),
        (value2) => workspaceIdentityMapper.mapIncoming(value2)
      );
    }
  } : rawResourceChannel;
  const peer = resourceChannel ? resourcePeers?.attach(resourceChannel) : void 0;
  const connectionContext = {
    connectionId: `server-core-ws-${randomUUID43()}`,
    clientMode,
    role: clientMode === "desktop-continuous" ? "trusted-host-relay" : "terminal-client"
  };
  const scope = agentService ? createZCodeAgentConnectionScope(
    peer ? peer.wrapAgent(agentService) : agentService,
    connectionContext
  ) : void 0;
  const overrides = /* @__PURE__ */ new Map();
  if (scope) {
    overrides.set(IZCodeAgentService.channelName, scope.service);
  }
  const shareService = services.getOptional(IConversationShareService);
  if (shareService) {
    overrides.set(
      IConversationShareService.channelName,
      scopeConversationShareServiceForAttachment(
        shareService,
        clientMode,
        scope?.service,
        connectionContext
      )
    );
  }
  if (clientMode !== "desktop-continuous" && services.getOptional(IProviderProvisioningTargetService)) {
    overrides.set(IProviderProvisioningTargetService.channelName, {
      apply: async () => {
        throw new Error("Provider Provisioning \u4EC5\u652F\u6301\u53D7\u4FE1 Desktop Host");
      }
    });
  }
  services.exposeOnChannelServer(server, overrides);
  socket.onClose(() => {
    peer?.dispose();
    reverseClient?.dispose();
    void scope?.dispose();
    rawServer.dispose();
  });
}
globalThis.expose=exposeWebSocket;

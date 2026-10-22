The MiniMax app-server socket is a WebSocket, not HTTP, and it speaks JSON-RPC.  This probe does the handshake and prints the server's own method catalogue, so the protocol does not have to be derived again.

What it does, in order:  it opens the Unix socket at ~/.minimax/app-server-control/app-server.sock, performs the HTTP/1.1 upgrade with a WebSocket key, sends the `initialize` request with the transport protocol version, reads the reply, and prints the server's name, its version, its transports and every method it offers.  It then sends the `initialized` notification and closes.

What it sends is the initialize and the notification, and nothing else, so it changes no state and enqueues nothing.

The app-server has to be running for it to answer:

  mcode app-server daemon start
  python3 scripts/probe-minimax-app-server.py
  mcode app-server daemon stop

What it proves, and what it does not.  It proves the channel is reachable from outside a MiniMax process and that the DesktopService contract rides inside one JSON-RPC method, `desktopService/transport`, whose request frame is a RuntimeTransportWireRequestFrame.  It does not yet send a DesktopService request, so the exact field names of that frame are still open, and so is the question of whether the app-server runtime can take over a session the GUI runtime owns.  Those are the next two steps, and the finding is written up on AFC-412.

The server's own method catalogue, for reference:  account/read, account/login/start, account/login/cancel, account/logout, server/prepareRestart and desktopService/transport.

The MiniMax app-server socket is a WebSocket, not HTTP, and it speaks JSON-RPC.  This probe does the handshake and prints the server's own method catalogue, so the protocol does not have to be derived again.

What it does, in order:  it opens the Unix socket at ~/.minimax/app-server-control/app-server.sock, performs the HTTP/1.1 upgrade with a WebSocket key, sends the `initialize` request with the transport protocol version, reads the reply, and prints the server's name, its version, its transports and every method it offers.  It then sends the `initialized` notification and closes.

What it sends is the initialize and the notification, and nothing else, so it changes no state and enqueues nothing.

The app-server has to be running for it to answer:

  mcode app-server daemon start
  python3 scripts/probe-minimax-app-server.py
  mcode app-server daemon stop

## The frame shape, and who may drive a session

Both open questions from the first probe are now answered from the installed package's own runtime validators, which are authoritative because the server rejects anything that fails them.

A request frame is a plain object:

    {
      "type": "request",
      "id": "<non-empty string>",
      "method": "POST",
      "url": "/minimax-desktop/api/v1/session/mvs_abc/message",
      "headers": { "content-type": "application/json" },
      "body": "{\"content\":\"hi\"}",
      "streamWindow": 16
    }

`id` is the client's correlation id, `method` is an HTTP verb, `url` is a path, `headers` is an object of string to string, and `body` is either a UTF-8 string or a base64 payload object.  `streamWindow` is optional and is an integer from 1 to 256; the contract's default is 16.

The body travels inside a JSON-RPC request as `{"method":"desktopService/transport","params":{"frame":{...}}}`, and the server answers `{"result":{"accepted":true,"requestId":"<frame.id>"}}`.  The server's replies come back as JSON-RPC notifications carrying the same method, with one of four frame types:  `response-start` (an HTTP status and headers), `stream-chunk` (a payload), `stream-done`, or `stream-error` (a message and an optional code).  A `seq` number on each frame counts them up.

Flow control is carried by frame type, not by a header:  a `stream-credit` frame grants more credits, and an `abort` frame cancels the request.  A second request under the same stream scope supersedes the first, and the first is aborted.

The routes the contract offers are `session/:id/message`, `session/:id/queue`, `session/:id/steer` and `session/:id/resume`, all POST, all under `/minimax-desktop/api/v1/`.  The resume body carries `drain_queued`.  A message body carries `content`, and optionally `model`, `attachments`, `turn_id`, `client_intent`, `enable_team`, `review_request`, `computer_use` and `base_req`; the session id is a path binding, not a body field.

## The app-server can drive a GUI session, but not a GUI turn

Session ownership is not exclusive between runtimes.  The app-server and the desktop app share one store under `~/.minimax/v2/`, one session id namespace, and one session table, so a `mvs_...` id the desktop app issued is a valid argument for the app-server's routes.

The one exclusion is a per-turn lease in the shared database, and it is held per turn rather than per session.  While the desktop app holds a turn, the app-server's send is refused with HTTP 409 and the key `local_session_busy` and the message "Session already has an active Turn.  Use queue send to deliver the message after it."  It does not queue implicitly and it does not take the turn over.  The sanctioned way to deliver anyway is the queue route, whose rows are in the same shared database, so the message runs after the turn ends.

So the transport is buildable, and its shape is a 409-then-queue fallback rather than a takeover.

What is still not known:  whether a turn started by the app-server shows up in the desktop app's own view, and whether the app-server's abort route can stop a turn the desktop app is running.  Neither is needed to send a message.

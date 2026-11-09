# 2026-09-14 — THE BOARD remembers collab login

Board `7d3605e2`.  Branch `fx/board-remember-token`.

Owner: login password then collab token on every load of mac.jays.services/board.

`fetch()` does not reuse the browser Basic login, and the page stored the token in `sessionStorage` (gone when the tab closes).

Fix: after a successful `/board` Basic (or cookie) login, set HttpOnly `mac_collab_session` for 30 days.  API calls send that cookie.  The in-page token bar only appears on 401.  localStorage is a fallback Bearer.  Rotating `MAC_COLLAB_TOKEN` invalidates cookies.

Live: copy `scripts/mac-collab/mac-collab-server.py` to `~/apps/mac-collab/` and `pm2 restart mac-collab`.

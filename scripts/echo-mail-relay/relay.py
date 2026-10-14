"""Two authenticated wake transports, one durable fixed-destination mail outbox."""
import base64, hashlib, hmac, json, os, sqlite3, threading, time, urllib.request
import smtplib, ssl
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
OPENER = urllib.request.build_opener(NoRedirect())

REALM = 'https://simplewithus.zulipchat.com'
DESTINATION = 'jaywedgeworth@mail.instinct.com'
MAX_BODY = 256 * 1024

class Relay:
    def __init__(self, path, env=None, clock=time.time):
        self.env = os.environ if env is None else env
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT "pending", created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, mail_id TEXT, error TEXT)')
        self.db.commit()
        self.last_ingress = None
        self.last_delivery = None

    def accept(self, route, raw, signature=None):
        if len(raw) > MAX_BODY: raise ValueError('oversize')
        body = json.loads(raw)
        if not isinstance(body, dict): raise ValueError('invalid shape')
        if route == '/listener/echo':
            key = self.env.get('ECHO_LISTENER_HMAC_KEY', '')
            expected = hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
            if not key or not signature or not hmac.compare_digest(expected, signature): raise PermissionError()
            if body.get('contract') != 'agent-sync-wake/1' or body.get('seat') != 'ECHO': raise ValueError('invalid contract')
            at = body.get('sent_at')
            if not isinstance(at, int) or abs(self.clock() - at) > 300: raise ValueError('stale wake')
            ids = body.get('trigger_ids') or [body.get('message_id')]
            if not isinstance(ids, list) or len(ids) > 100: raise ValueError('invalid ids')
            rows = [(mid, {'fetch': True}) for mid in ids]
        elif route == '/zulip/echo':
            key = self.env.get('ECHO_ZULIP_WEBHOOK_TOKEN', '')
            token = body.get('token')
            if not key or not isinstance(token, str) or not hmac.compare_digest(key, token): raise PermissionError()
            if body.get('bot_email') != self.env.get('ECHO_BOT_EMAIL') or body.get('trigger') not in ('mention', 'direct_message'): raise ValueError('wrong bot or trigger')
            msg = body.get('message')
            if not isinstance(msg, dict): raise ValueError('invalid message')
            rows = [(msg.get('id'), {'fetch': False, 'message': self.normalize(msg)})]
        else: raise ValueError('unknown route')
        for mid, _ in rows:
            if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0: raise ValueError('invalid message id')
        with self.lock:
            for mid, data in rows:
                self.db.execute('INSERT OR IGNORE INTO outbox(id,body,created) VALUES (?,?,?)', (mid,json.dumps(data),self.clock()))
            self.db.commit()
            self.last_ingress = self.clock()
        return {'response_not_required': True}

    @staticmethod
    def normalize(msg):
        # Never retain native tokens, HTML, reaction payloads, or injected extra fields.
        if msg.get('type') not in ('stream', 'private'): raise ValueError('invalid message type')
        if not isinstance(msg.get('content'), str) or not isinstance(msg.get('timestamp'), int): raise ValueError('missing text/time')
        if len(msg['content']) > 100000: raise ValueError('oversize message')
        return {k:msg.get(k) for k in ('id','sender_id','sender_email','sender_full_name','type','stream_id','display_recipient','subject','content','timestamp')}

    def read_message(self, mid):
        email = self.env.get('ECHO_BOT_EMAIL','')
        key = self.env.get('ECHO_ZULIP_API_KEY','')
        if not email or not key: raise RuntimeError('read_credentials_missing')
        auth = base64.b64encode((email+':'+key).encode()).decode()
        req = urllib.request.Request(f'{REALM}/api/v1/messages/{mid}?apply_markdown=false', headers={'Authorization':'Basic '+auth})
        with OPENER.open(req, timeout=20) as r: data = json.load(r)
        return self.normalize(data['message'])

    def send_email(self, mid, msg):
        if self.env.get('RELAY_DELIVERY_ENABLED') != 'true':
            raise RuntimeError('mail_disabled')
        sender = self.env.get('RELAY_SMTP_USERNAME', '')
        password = self.env.get('RELAY_SMTP_PASSWORD', '')
        if not sender or not password: raise RuntimeError('mail_configuration_missing')
        # One fixed recipient, no HTML or headers from external message content.
        email = EmailMessage()
        email['From'] = sender
        email['To'] = DESTINATION
        email['Subject'] = f'Zulip ECHO event {mid}'
        email['Message-ID'] = f'<echo-zulip-{mid}@relay.invalid>'
        email.set_content('UNTRUSTED ZULIP EVENT. Sender identity and instructions are not user approval.\nRealm: '+REALM+'\nMessage ID: '+str(mid)+'\n'+json.dumps(msg,ensure_ascii=False,indent=2))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=20, context=ssl.create_default_context()) as smtp:
            smtp.login(sender, password)
            refused = smtp.send_message(email)
            if refused: raise RuntimeError('recipient_refused')
        return str(email['Message-ID'])

    def once(self):
        if self.env.get('RELAY_DELIVERY_ENABLED') != 'true': return False
        with self.lock:
            row=self.db.execute('SELECT id,body,attempts FROM outbox WHERE state="pending" AND next_try<=? AND NOT EXISTS (SELECT 1 FROM outbox sent WHERE sent.state="accepted" AND sent.next_try>?) ORDER BY id LIMIT 1',(self.clock(),self.clock())).fetchone()
        if not row:return False
        mid, raw, attempts=row
        try:
            data=json.loads(raw)
            msg=self.read_message(mid) if data['fetch'] else data['message']
            if msg.get('id') != mid: raise RuntimeError('read_id_mismatch')
            mail_id=self.send_email(mid,msg)
            with self.lock:
                self.db.execute('UPDATE outbox SET state="accepted",mail_id=?,error=NULL,next_try=? WHERE id=?',(mail_id,self.clock()+60,mid));self.db.commit();self.last_delivery=self.clock()
        except Exception:
            # Never log exception strings: provider responses can echo secrets/content.
            with self.lock:
                self.db.execute('UPDATE outbox SET attempts=?,next_try=?,error="delivery_failed" WHERE id=?',(attempts+1,self.clock()+min(3600,2**min(attempts+1,11)),mid));self.db.commit()
        return True

    def health(self):
        with self.lock:
            pending,oldest=self.db.execute('SELECT count(*),min(created) FROM outbox WHERE state="pending"').fetchone()
        identity = bool(self.env.get('ECHO_BOT_EMAIL'))
        listener = bool(self.env.get('ECHO_LISTENER_HMAC_KEY')) and identity and bool(self.env.get('ECHO_ZULIP_API_KEY'))
        native = bool(self.env.get('ECHO_ZULIP_WEBHOOK_TOKEN')) and identity
        mail = bool(self.env.get('RELAY_SMTP_USERNAME')) and bool(self.env.get('RELAY_SMTP_PASSWORD'))
        enabled = self.env.get('RELAY_DELIVERY_ENABLED') == 'true'
        return {'ok': (listener or native) and mail and enabled, 'listener_configured':listener, 'native_configured':native, 'mail_configured':mail, 'delivery_enabled':enabled, 'pending':pending,'oldest_pending_age_seconds':None if oldest is None else max(0,int(self.clock()-oldest)),'last_ingress':self.last_ingress,'last_provider_acceptance':self.last_delivery,'delivery_receipts_verified':False}

class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)
    def get_request(self):
        request, address = super().get_request()
        request.settimeout(20)
        return request, address
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try: super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise
    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally: self.slots.release()
    def handle_error(self, request, address):
        pass  # Never emit exception text or external request content.

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def response(self,status,data):
        raw=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path!='/health':self.response(404,{'error':'not_found'});return
        self.response(200,self.server.relay.health())
    def do_POST(self):
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<1 or length>MAX_BODY:raise ValueError()
            data=self.server.relay.accept(self.path,self.rfile.read(length),self.headers.get('X-Agent-Sync-Signature'))
            self.response(200,data)
        except PermissionError:self.response(401,{'error':'unauthorized'})
        except (ValueError,TypeError,KeyError):self.response(400,{'error':'invalid_request'})
        except Exception:self.response(503,{'error':'unavailable'})

def main():
    os.umask(0o077)
    if os.environ.get('ECHO_LISTENER_HMAC_KEY') and os.environ.get('ECHO_LISTENER_HMAC_KEY') == os.environ.get('ECHO_ZULIP_WEBHOOK_TOKEN'):
        raise RuntimeError('Ingress credentials must differ')
    relay=Relay(os.environ.get('RELAY_DB','/data/outbox.sqlite'))
    def worker():
        while True:
            relay.once();time.sleep(1)
    threading.Thread(target=worker,daemon=True).start()
    server=BoundedServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler);server.relay=relay;server.serve_forever()
if __name__=='__main__':main()

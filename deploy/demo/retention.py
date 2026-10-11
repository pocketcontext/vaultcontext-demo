#!/usr/bin/env python3
"""Private demo contact/preferences service. Does not send messages."""
import argparse
import calendar
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import re
import signal
import threading
import sqlite3
import stat
import sys
import time
from urllib.parse import parse_qs, urlsplit

DAY = 86400


class Rejected(Exception):
    def __init__(self, status=400):
        self.status = status


def after_year(timestamp):
    current = datetime.fromtimestamp(timestamp, timezone.utc)
    day = min(current.day, calendar.monthrange(current.year + 1, current.month)[1])
    return int(current.replace(year=current.year + 1, day=day).timestamp())


class Store:
    def __init__(self, path):
        self.path = Path(path).absolute()
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError('unsafe contact store path')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ValueError('contact store directory must be private and owned')
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('contact store file must be private and owned')
        else:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS contacts (
                subject TEXT PRIMARY KEY, email TEXT NOT NULL,
                sales INTEGER NOT NULL, newsletter INTEGER NOT NULL,
                revision INTEGER NOT NULL, last_used INTEGER NOT NULL,
                review_due INTEGER NOT NULL, terms TEXT NOT NULL, consent TEXT NOT NULL,
                token_hash TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS unsubscribe_tokens (token_hash TEXT PRIMARY KEY, subject TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS consent_events (
                id INTEGER PRIMARY KEY, subject TEXT NOT NULL, at INTEGER NOT NULL,
                sales INTEGER NOT NULL, newsletter INTEGER NOT NULL,
                terms TEXT NOT NULL, consent TEXT NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS suppression (
                email TEXT PRIMARY KEY, sales INTEGER NOT NULL, newsletter INTEGER NOT NULL,
                updated INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS review_events (
                id INTEGER PRIMARY KEY, subject TEXT NOT NULL, at INTEGER NOT NULL,
                decision TEXT NOT NULL, reason TEXT NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS security_events (
                id INTEGER PRIMARY KEY, at INTEGER NOT NULL, event TEXT NOT NULL,
                ip TEXT NOT NULL DEFAULT '', incident_until INTEGER NOT NULL DEFAULT 0);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(security_events)')}
            if 'ip' not in columns:
                db.execute("ALTER TABLE security_events ADD COLUMN ip TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        if (self.path.parent/'migration-fenced.json').exists():
            raise Rejected(503)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            # WAL is required for Litestream. Private sidecars and replica history
            # have independent retention; secure_delete cannot erase old replicas.
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            if (self.path.parent/'migration-fenced.json').exists():
                raise Rejected(503)
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def subject(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 255 or any(ord(c) < 33 for c in value):
            raise Rejected()
        return value

    @staticmethod
    def public(row):
        return {'salesContact': bool(row['sales']), 'newsletter': bool(row['newsletter']),
                'revision': row['revision'], 'reviewDue': row['review_due']}

    def preferences(self, subject):
        subject = self.subject(subject)
        with self.connect() as db:
            row = db.execute('SELECT * FROM contacts WHERE subject=?', (subject,)).fetchone()
            return self.public(row) if row else {'salesContact': False, 'newsletter': False, 'revision': 0, 'reviewDue': None}

    def enroll(self, body, now=None):
        now = int(time.time() if now is None else now)
        allowed = {'subject', 'email', 'salesContact', 'newsletter', 'termsVersion', 'consentVersion', 'expectedRevision'}
        if not isinstance(body, dict) or set(body) not in (allowed, allowed | {'security'}):
            raise Rejected()
        security = self.security_observation(body['security'], now) if 'security' in body else None
        if security and security['event'] != 'enrollment_request':
            raise Rejected()
        subject = self.subject(body['subject'])
        email = body['email']
        if not isinstance(email, str) or not 3 <= len(email) <= 254 or email.count('@') != 1 or any(c.isspace() for c in email):
            raise Rejected()
        email = email.strip().lower()
        for field in ('salesContact', 'newsletter'):
            if type(body[field]) is not bool:
                raise Rejected()
        for field in ('termsVersion', 'consentVersion'):
            if not isinstance(body[field], str) or not 1 <= len(body[field]) <= 100:
                raise Rejected()
        if type(body['expectedRevision']) is not int or body['expectedRevision'] < 0:
            raise Rejected()
        sales, newsletter = int(body['salesContact']), int(body['newsletter'])
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            old = db.execute('SELECT * FROM contacts WHERE subject=?', (subject,)).fetchone()
            revision = old['revision'] if old else 0
            if revision != body['expectedRevision']:
                raise Rejected(409)
            # Repeating enrollment is an explicit, revision-checked preference decision.
            # /touch alone never modifies consent or a suppression record.
            revision += 1
            review = after_year(now)  # Explicit current opt-in renews marketing engagement; /touch never does.
            db.execute('''INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(subject) DO UPDATE SET email=excluded.email,sales=excluded.sales,
                newsletter=excluded.newsletter,revision=excluded.revision,last_used=excluded.last_used,
                review_due=excluded.review_due,terms=excluded.terms,consent=excluded.consent,
                token_hash=excluded.token_hash''',
                (subject, email, sales, newsletter, revision, now, review,
                 body['termsVersion'], body['consentVersion'], hashlib.sha256(token.encode()).hexdigest()))
            # Keep every issued withdrawal capability until contact deletion/expiry.
            # Rotating preferences must not silently break an earlier link.
            db.execute('INSERT INTO unsubscribe_tokens VALUES (?,?)', (hashlib.sha256(token.encode()).hexdigest(), subject))
            db.execute('INSERT INTO consent_events(subject,at,sales,newsletter,terms,consent,revision) VALUES (?,?,?,?,?,?,?)',
                       (subject, now, sales, newsletter, body['termsVersion'], body['consentVersion'], revision))
            suppressed = db.execute('SELECT * FROM suppression WHERE email=?', (email,)).fetchone()
            block_sales = 0 if sales else int(bool((old and old['sales']) or (suppressed and suppressed['sales'])))
            block_news = 0 if newsletter else int(bool((old and old['newsletter']) or (suppressed and suppressed['newsletter'])))
            if block_sales or block_news:
                db.execute('''INSERT INTO suppression VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET
                    sales=excluded.sales,newsletter=excluded.newsletter,updated=excluded.updated''',
                    (email, block_sales, block_news, now))
            else:
                db.execute('DELETE FROM suppression WHERE email=?', (email,))
            if security:
                self.record_security(db, security, now)
            return {'salesContact': bool(sales), 'newsletter': bool(newsletter), 'revision': revision,
                    'reviewDue': review, 'unsubscribeToken': token}

    def touch(self, subject, now=None):
        with self.connect() as db:
            db.execute('UPDATE contacts SET last_used=? WHERE subject=?',
                       (int(time.time() if now is None else now), self.subject(subject)))
        return {'ok': True}

    def unsubscribe(self, token, now=None):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
            raise Rejected()
        now = int(time.time() if now is None else now)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute('SELECT contacts.* FROM contacts JOIN unsubscribe_tokens USING(subject) WHERE unsubscribe_tokens.token_hash=?', (digest,)).fetchone()
            if row:
                revision = row['revision'] + 1
                db.execute('UPDATE contacts SET sales=0,newsletter=0,revision=? WHERE subject=?', (revision, row['subject']))
                db.execute('''INSERT INTO suppression VALUES (?,1,1,?) ON CONFLICT(email) DO UPDATE SET
                    sales=1,newsletter=1,updated=excluded.updated''', (row['email'], now))
                db.execute('INSERT INTO consent_events(subject,at,sales,newsletter,terms,consent,revision) VALUES (?,?,0,0,?,?,?)',
                           (row['subject'], now, row['terms'], row['consent'], revision))
        # Same response for absent/expired tokens; capabilities do not enumerate addresses.
        return {'ok': True}

    @staticmethod
    def security_observation(body, now):
        if not isinstance(body, dict) or set(body) != {'event', 'ip', 'timestamp'}:
            raise Rejected()
        # Historical enrollment observations remain accepted by maintenance clients.
        if body['event'] not in ('enrollment', 'enrollment_request', 'auth_success', 'auth_failure'):
            raise Rejected()
        if type(body['timestamp']) is not int or abs(now-body['timestamp']) > 300:
            raise Rejected()
        try:
            if not isinstance(body['ip'], str) or '%' in body['ip']:
                raise ValueError()
            address = str(ipaddress.ip_address(body['ip']))
        except ValueError:
            raise Rejected() from None
        return {**body, 'ip': address}

    @staticmethod
    def record_security(db, body, now):
        # Bound repeated identical observations in a one-minute interval.
        duplicate = db.execute('SELECT 1 FROM security_events WHERE event=? AND ip=? AND at>=?',
                               (body['event'], body['ip'], now-60)).fetchone()
        if not duplicate:
            db.execute('INSERT INTO security_events(at,event,ip) VALUES (?,?,?)',
                       (body['timestamp'], body['event'], body['ip']))

    def security(self, body, now=None):
        now = int(time.time() if now is None else now)
        observation = self.security_observation(body, now)
        with self.connect() as db:
            self.record_security(db, observation, now)
        return {'ok': True}

    def review_queue(self, body, now=None):
        now = int(time.time() if now is None else now)
        if not isinstance(body, dict) or set(body) != {'after', 'limit'}:
            raise Rejected()
        if not isinstance(body['after'], str) or len(body['after']) > 255 or type(body['limit']) is not int or not 1 <= body['limit'] <= 100:
            raise Rejected()
        with self.connect() as db:
            rows = db.execute('SELECT subject,revision,review_due FROM contacts WHERE (sales=1 OR newsletter=1) AND review_due<=? AND subject>? ORDER BY subject LIMIT ?',
                              (now, body['after'], body['limit'])).fetchall()
        return {'items':[{'subject':r['subject'],'revision':r['revision'],'reviewDue':r['review_due']} for r in rows],
                'nextAfter':rows[-1]['subject'] if len(rows) == body['limit'] else None}

    def review(self, body, now=None):
        now = int(time.time() if now is None else now)
        if not isinstance(body, dict) or set(body) != {'subject','expectedRevision','decision','reason'}:
            raise Rejected()
        subject = self.subject(body['subject'])
        if type(body['expectedRevision']) is not int or body['expectedRevision'] < 1 or body['decision'] not in ('retain','delete'):
            raise Rejected()
        reason = body['reason']
        if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 240 or any(ord(c) < 32 for c in reason):
            raise Rejected()
        with self.connect() as db:
            row = db.execute('SELECT * FROM contacts WHERE subject=?',(subject,)).fetchone()
            if not row:
                raise Rejected(404)
            if row['revision'] != body['expectedRevision']:
                raise Rejected(409)
            if body['decision'] == 'retain':
                if not row['sales'] and not row['newsletter']:
                    raise Rejected()  # Review cannot manufacture consent or retain an opted-out profile.
                revision = row['revision'] + 1
                due = after_year(now)
                db.execute('UPDATE contacts SET revision=?,review_due=? WHERE subject=?',(revision,due,subject))
                db.execute('INSERT INTO review_events(subject,at,decision,reason,revision) VALUES (?,?,?,?,?)',
                           (subject,now,'retain',reason.strip(),revision))
                return {'retained':True,'revision':revision,'reviewDue':due}
            # Preserve a complete existing suppression unchanged; strengthen any
            # partial withdrawal so a deleted profile cannot be re-imported.
            db.execute('''INSERT INTO suppression VALUES (?,1,1,?) ON CONFLICT(email) DO UPDATE SET
                sales=1,newsletter=1,updated=excluded.updated WHERE suppression.sales=0 OR suppression.newsletter=0''',
                (row['email'],now))
            for table in ('consent_events','unsubscribe_tokens','review_events','contacts'):
                db.execute('DELETE FROM '+table+' WHERE subject=?',(subject,))
            # A deletion reason is validated but not retained as another personal record.
            return {'deleted':True}

    def maintenance(self, now=None):
        now = int(time.time() if now is None else now)
        with self.connect() as db:
            expired = [r[0] for r in db.execute('SELECT subject FROM contacts WHERE sales=0 AND newsletter=0 AND last_used<=?', (now-30*DAY,))]
            for subject in expired:
                db.execute('DELETE FROM consent_events WHERE subject=?', (subject,))
                db.execute('DELETE FROM unsubscribe_tokens WHERE subject=?', (subject,))
                db.execute('DELETE FROM review_events WHERE subject=?', (subject,))
                db.execute('DELETE FROM contacts WHERE subject=?', (subject,))
            db.execute('DELETE FROM security_events WHERE at<=? AND incident_until<=?', (now-30*DAY, now))
            due = db.execute('SELECT COUNT(*) FROM contacts WHERE (sales=1 OR newsletter=1) AND review_due<=?', (now,)).fetchone()[0]
        return {'expiredNonmarketing': len(expired), 'marketingReviewDue': due}


def handler(store, secret):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'DemoContacts'
        def log_message(self, *_args):
            pass  # Never log URLs, capabilities, identity data, or request bodies.

        def reply(self, status, payload):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def dispatch(self, method):
            try:
                parsed = urlsplit(self.path)
                public_unsubscribe = method == 'POST' and parsed.path == '/unsubscribe' and not parsed.query
                if not public_unsubscribe and not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + secret):
                    raise Rejected(401)
                if method == 'GET' and parsed.path == '/health' and not parsed.query:
                    with store.connect() as db:
                        db.execute('SELECT 1').fetchone()
                    result = {'ready':True}
                elif method == 'GET' and parsed.path == '/preferences':
                    query = parse_qs(parsed.query, strict_parsing=True)
                    if set(query) != {'subject'} or len(query['subject']) != 1:
                        raise Rejected()
                    result = store.preferences(query['subject'][0])
                elif method == 'POST' and not parsed.query:
                    if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise Rejected()
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 8192:
                        raise Rejected(413)
                    body = json.loads(self.rfile.read(length))
                    if parsed.path in ('/enroll', '/preferences'):
                        result = store.enroll(body)
                    elif parsed.path == '/touch' and isinstance(body, dict) and set(body) == {'subject'}:
                        result = store.touch(body['subject'])
                    elif public_unsubscribe and isinstance(body, dict) and set(body) == {'token'}:
                        result = store.unsubscribe(body['token'])
                    elif parsed.path == '/security':
                        result = store.security(body)
                    elif parsed.path == '/maintenance' and body == {}:
                        result = store.maintenance()
                    else:
                        raise Rejected(404)
                else:
                    raise Rejected(404)
                self.reply(200, result)
            except Rejected as exc:
                self.reply(exc.status, {'error': 'request rejected'})
            except (ValueError, TypeError, UnicodeError):
                self.reply(400, {'error': 'request rejected'})
            except Exception:
                self.reply(503, {'error': 'service unavailable'})

        def do_GET(self):
            self.dispatch('GET')
        def do_POST(self):
            self.dispatch('POST')

        def setup(self):
            super().setup()
            self.connection.settimeout(10)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    parser.add_argument('--port', type=int, default=8781)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument('--maintenance', action='store_true')
    operation.add_argument('--review', action='store_true', help='operator only: read one review decision as JSON from stdin')
    operation.add_argument('--review-queue', action='store_true', help='operator only: read {after,limit} from stdin; no email export')
    args = parser.parse_args()
    os.umask(0o077)
    store = Store(args.database)
    if args.review or args.review_queue:
        try:
            raw = sys.stdin.buffer.read(8193)
            if len(raw) > 8192:
                raise Rejected(413)
            body = json.loads(raw)
            print(json.dumps(store.review(body) if args.review else store.review_queue(body)))
        except Rejected as exc:
            print(json.dumps({'error':'review rejected','status':exc.status}), file=sys.stderr)
            raise SystemExit(1) from None
        except (ValueError, TypeError):
            print('invalid private review request', file=sys.stderr)
            raise SystemExit(1) from None
        return
    if args.maintenance:
        print(json.dumps(store.maintenance()))
        return
    secret = os.environ.get('VAULTCONTEXT_DEMO_CONTACT_TOKEN', '')
    if len(secret) < 32:
        raise SystemExit('contact service requires a strong configured token')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(store, secret))
    server.daemon_threads = False
    stopping = threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda _s,_f:stopping.set())
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    stopping.wait()
    server.shutdown()
    server.server_close()  # Join in-flight requests before supervisor performs final sync.
    thread.join()


if __name__ == '__main__':
    main()

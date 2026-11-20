#!/usr/bin/env python3
import base64
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get('PORT', '8899'))
UP = (sys.argv[2] if len(sys.argv) > 2 else
      os.environ.get('UPSTREAM', 'https://inference-api.nousresearch.com')).rstrip('/')
HERMES_HOME = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))).expanduser()
HERMES_REPO = os.environ.get('HERMES_REPO', str(Path.home() / '.hermes' / 'hermes-agent'))
AUTH_DIR = Path(os.environ.get('AUTH_DIR', str(SCRIPT_DIR / 'auths'))).expanduser()
LOG = os.environ.get('LOG', str(SCRIPT_DIR / 'nous_proxy.log'))
DUMP = os.environ.get('DUMP_DIR')
LOCAL_KEY = os.environ.get('LOCAL_KEY', '').strip()
NO_REFRESH = os.environ.get('NO_REFRESH') == '1'
STRIP = os.environ.get('STRIP_RESPONSES') == '1'
INCLUDE_MAIN = os.environ.get('INCLUDE_MAIN') == '1'
BACKEND_MODE = 'readonly' if NO_REFRESH else os.environ.get('BACKEND_MODE', 'auto').strip().lower()
COOLDOWN_429 = int(os.environ.get('COOLDOWN_429', '60'))
COOLDOWN_401 = int(os.environ.get('COOLDOWN_401', '120'))
# Batasi failover saat 429: satu request tidak boleh membakar seluruh pool.
MAX_429_FAILOVER = int(os.environ.get('MAX_429_FAILOVER', '2'))
# Batasi request paralel ke upstream (burst 3x200k token nabrak limit apa pun).
MAX_CONCURRENT = int(os.environ.get('MAX_CONCURRENT', '2'))
UP_SEM = threading.BoundedSemaphore(MAX_CONCURRENT)
KEEPALIVE = os.environ.get('KEEPALIVE', '1') != '0'
KEEPALIVE_HORIZON = int(os.environ.get('KEEPALIVE_HORIZON', '0'))
SKEW = 120
REFRESH_COOLDOWN = 20
SKIP_HDRS = {'host', 'content-length', 'connection', 'transfer-encoding',
             'accept-encoding', 'authorization'}

_pool_lock = threading.Lock()
_rr = [0]
N = [0]


def _now():
    return datetime.now(timezone.utc)


def _parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _jwt_iat(token):
    try:
        seg = token.split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(seg + '=' * (-len(seg) % 4)))
        return float(payload.get('iat') or 0)
    except Exception:
        return 0.0


def _epoch(dt):
    return dt.timestamp() if dt else 0.0


def _valid(exp):
    return bool(exp) and _now().timestamp() + SKEW < exp.timestamp()


def _log(line):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a') as fh:
            fh.write('%s %s\n' % (time.strftime('%F %T'), line))
    except Exception:
        pass


def _states_from_store(store):
    out = []
    if isinstance(store, dict):
        prov = (store.get('providers') or {}).get('nous')
        if isinstance(prov, dict):
            out.append(prov)
        for entry in (store.get('credential_pool') or {}).get('nous') or []:
            if isinstance(entry, dict):
                out.append(entry)
        if not out and (store.get('access_token') or store.get('agent_key')):
            out.append(store)
    return out


def _freshest_in(states):
    best = None
    for state in states:
        tok = state.get('agent_key') or state.get('access_token')
        if not tok or not isinstance(tok, str):
            continue
        exp = _parse_iso(state.get('agent_key_expires_at') or state.get('expires_at'))
        obtained = _parse_iso(state.get('obtained_at'))
        order = (_jwt_iat(tok) or _epoch(obtained), _epoch(exp))
        if best is None or order > best['order']:
            best = {'token': tok, 'exp': exp, 'order': order,
                    'pool_id': state.get('id')}
    return best


def _read_file_member(path):
    try:
        store = json.loads(path.read_text())
    except Exception:
        return None, None
    return _freshest_in(_states_from_store(store)), store


_resolve_official = None
_import_err = None
if BACKEND_MODE in ('auto', 'inprocess'):
    try:
        if HERMES_REPO not in sys.path:
            sys.path.insert(0, HERMES_REPO)
        from hermes_cli.auth import resolve_nous_runtime_credentials
        try:
            from hermes_cli.anon_challenge import background_caller
        except Exception:
            import contextlib

            @contextlib.contextmanager
            def background_caller():
                yield

        def _resolve_official(force=False, stale=None):
            kw = {'force_refresh': force, 'stale_access_token': stale,
                  'timeout_seconds': 30}
            with background_caller():
                return resolve_nous_runtime_credentials(**kw)
    except Exception as _e:
        _import_err = '%s: %s' % (type(_e).__name__, _e)
        _resolve_official = None

if BACKEND_MODE == 'inprocess' and _resolve_official is None:
    raise SystemExit('BACKEND_MODE=inprocess tapi import gagal: %s' % _import_err)
if _resolve_official is not None:
    LIVE_BACKEND = 'official-inprocess'
elif BACKEND_MODE in ('auto', 'cli') and (shutil.which('hermes') or
                                          Path.home().joinpath('.local/bin/hermes').exists()):
    LIVE_BACKEND = 'cli'
else:
    LIVE_BACKEND = 'readonly'


def _hermes_bin():
    return shutil.which('hermes') or str(Path.home() / '.local' / 'bin' / 'hermes')


class Member:
    def __init__(self, name, kind, path=None):
        self.name = name
        self.kind = kind
        self.path = path
        self.lock = threading.Lock()
        self.cache = {'token': None, 'exp': None}
        self.cooldown_until = 0.0
        self.last_refresh = 0.0
        self.last_grant_warn = 0.0
        self.file_mtime = 0.0
        self.stats = {'req': 0, 'ok': 0, 'fail': 0, 'last_status': None}

    def _live_bearer(self, force, stale):
        if LIVE_BACKEND == 'official-inprocess':
            cred = _resolve_official(force=force, stale=stale)
            tok = cred.get('api_key')
            if not tok:
                raise RuntimeError('resolver resmi tidak mengembalikan api_key')
            return tok, _parse_iso(cred.get('expires_at'))
        best, _ = _read_file_member(HERMES_HOME / 'auth.json')
        if best and _valid(best['exp']):
            return best['token'], best['exp']
        if LIVE_BACKEND == 'cli' and self._cli_refresh(HERMES_HOME / 'auth.json', None):
            best, _ = _read_file_member(HERMES_HOME / 'auth.json')
            if best and _valid(best['exp']):
                return best['token'], best['exp']
        raise RuntimeError('credential Hermes live expired dan refresh gagal '
                           '(backend=%s); jalankan: hermes auth refresh nous' % LIVE_BACKEND)

    def _cli_refresh(self, source_path, pool_id):
        if NO_REFRESH:
            return False
        if time.monotonic() - self.last_refresh < REFRESH_COOLDOWN:
            return False
        self.last_refresh = time.monotonic()
        if self.kind == 'live':
            env = dict(os.environ)
            cmd = [_hermes_bin(), 'auth', 'refresh', 'nous']
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=90, env=env)
                _log('[%s] refresh cli exit=%d' % (self.name, r.returncode))
                return r.returncode == 0
            except Exception as e:
                _log('[%s] refresh cli gagal: %s' % (self.name, type(e).__name__))
                return False
        sb = AUTH_DIR / '.state' / self.name
        try:
            (sb / 'shared').mkdir(parents=True, exist_ok=True)
            store = json.loads(source_path.read_text())
            if not isinstance(store, dict) or 'providers' not in store:
                state = store if isinstance(store, dict) else {}
                store = {'version': 1, 'providers': {'nous': state},
                         'active_provider': 'nous'}
            (sb / 'auth.json').write_text(json.dumps(store))
            env = dict(os.environ)
            env['HERMES_HOME'] = str(sb)
            env['HERMES_SHARED_AUTH_DIR'] = str(sb / 'shared')
            cmd = [_hermes_bin(), 'auth', 'refresh', 'nous']
            r = subprocess.run(cmd, capture_output=True, timeout=90, env=env)
            if r.returncode != 0 and b'pass an index' in (r.stdout + r.stderr) and pool_id:
                cmd = [_hermes_bin(), 'auth', 'refresh', 'nous', str(pool_id)]
                r = subprocess.run(cmd, capture_output=True, timeout=90, env=env)
            _log('[%s] refresh sandbox exit=%d' % (self.name, r.returncode))
            if r.returncode != 0:
                return False
            new = (sb / 'auth.json').read_bytes()
            tmp = source_path.with_suffix('.tmp')
            tmp.write_bytes(new)
            os.chmod(tmp, 0o600)
            os.replace(tmp, source_path)
            return True
        except Exception as e:
            _log('[%s] refresh sandbox gagal: %s' % (self.name, type(e).__name__))
            return False

    def bearer(self, force=False, stale=None):
        with self.lock:
            if self.kind == 'live':
                if not force and self.cache['token'] and _valid(self.cache['exp']):
                    return self.cache['token']
                tok, exp = self._live_bearer(force, stale)
                self.cache.update(token=tok, exp=exp)
                return tok
            try:
                mtime = self.path.stat().st_mtime
            except Exception:
                mtime = 0.0
            if mtime != self.file_mtime:
                self.file_mtime = mtime
                self.cache.update(token=None, exp=None)
            if not force and self.cache['token'] and _valid(self.cache['exp']):
                return self.cache['token']
            best, _ = _read_file_member(self.path)
            if best and _valid(best['exp']):
                self.cache.update(token=best['token'], exp=best['exp'])
                return best['token']
            if self._cli_refresh(self.path, best['pool_id'] if best else None):
                best, _ = _read_file_member(self.path)
                if best and _valid(best['exp']):
                    self.file_mtime = self.path.stat().st_mtime
                    self.cache.update(token=best['token'], exp=best['exp'])
                    return best['token']
            exp_str = best['exp'].isoformat() if best and best['exp'] else 'tidak ada'
            raise RuntimeError('token expired (%s) dan refresh gagal' % exp_str)

    def refresh_now(self):
        with self.lock:
            if self.kind == 'live' and LIVE_BACKEND == 'official-inprocess':
                cred = _resolve_official(force=True, stale=None)
                tok = cred.get('api_key')
                if not tok:
                    raise RuntimeError('resolver resmi tidak mengembalikan api_key')
                exp = _parse_iso(cred.get('expires_at'))
                self.cache.update(token=tok, exp=exp)
                return exp
            path = HERMES_HOME / 'auth.json' if self.kind == 'live' else self.path
            best, _ = _read_file_member(path)
            if not self._cli_refresh(path, best['pool_id'] if best else None):
                raise RuntimeError('refresh resmi gagal')
            best, _ = _read_file_member(path)
            if not best:
                raise RuntimeError('tidak ada token setelah refresh')
            if self.kind == 'file':
                self.file_mtime = path.stat().st_mtime
            self.cache.update(token=best['token'], exp=best['exp'])
            return best['exp']

    def lifetime(self):
        path = HERMES_HOME / 'auth.json' if self.kind == 'live' else self.path
        try:
            store = json.loads(path.read_text())
            st = (store.get('providers') or {}).get('nous') or {}
            vals = [int(float(st[k])) for k in ('expires_in', 'agent_key_expires_in')
                    if st.get(k)]
            return min(v for v in vals if v > 0) if vals else 3600
        except Exception:
            return 3600

    def info(self):
        exp = self.cache['exp']
        if self.kind == 'file' and not exp:
            best, _ = _read_file_member(self.path)
            exp = best['exp'] if best else None
        grant = None
        days_left = None
        try:
            path = HERMES_HOME / 'auth.json' if self.kind == 'live' else self.path
            store = json.loads(path.read_text())
            g = ((store.get('providers') or {}).get('nous') or {}).get('grant_approved_at')
            gd = _parse_iso(g)
            if gd:
                grant = gd.isoformat()
                days_left = round(30.0 - (_now().timestamp() - gd.timestamp()) / 86400.0, 2)
        except Exception:
            pass
        return {
            'name': self.name, 'kind': self.kind,
            'backend': LIVE_BACKEND if self.kind == 'live' else 'file+cli-sandbox',
            'req': self.stats['req'], 'ok': self.stats['ok'], 'fail': self.stats['fail'],
            'last_status': self.stats['last_status'],
            'cooldown_s': max(0, round(self.cooldown_until - time.monotonic())),
            'token_expires_at': exp.isoformat() if exp else None,
            'grant_approved_at': grant,
            'grant_days_left': days_left,
        }


def build_pool():
    members = []
    try:
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(AUTH_DIR, 0o700)
        state_dir = AUTH_DIR / '.state'
        state_dir.mkdir(exist_ok=True)
        os.chmod(state_dir, 0o700)
    except Exception:
        pass
    for p in sorted(AUTH_DIR.glob('*.json')):
        members.append(Member(p.stem, 'file', p))
    if not members or INCLUDE_MAIN:
        members.append(Member('hermes-live', 'live'))
    return members


MEMBERS = build_pool()


def pick_member(exclude=()):
    with _pool_lock:
        n = len(MEMBERS)
        for _ in range(n):
            m = MEMBERS[_rr[0] % n]
            _rr[0] += 1
            if m.name in exclude:
                continue
            if time.monotonic() >= m.cooldown_until:
                return m
        # Semua member masih cooldown: jangan paksa tembak upstream.
        # Menembak akun yang lagi kena 429 cuma me-reset cooldown-nya dan
        # bikin pool terjebak badai rate-limit. Biarkan cooldown habis dulu.
        return None


def sanitize(body, path):
    stripped = 0
    if not STRIP or '/responses' not in path:
        return body, stripped
    try:
        j = json.loads(body)
    except Exception:
        return body, stripped
    if not isinstance(j, dict):
        return body, stripped
    if 'previous_response_id' in j:
        j.pop('previous_response_id', None)
        stripped += 1
    inp = j.get('input')
    if isinstance(inp, list):
        for it in inp:
            if isinstance(it, dict):
                if 'id' in it:
                    it.pop('id')
                    stripped += 1
                for k in [k for k in it if k.startswith('encrypted')]:
                    it.pop(k)
                    stripped += 1
    return json.dumps(j, ensure_ascii=False).encode(), stripped


def _upstream_url(path):
    if UP.endswith('/v1') and path.startswith('/v1/'):
        return UP[:-3] + path
    return UP + path


def _client_ok(headers):
    if not LOCAL_KEY:
        return True
    return headers.get('Authorization', '') == 'Bearer ' + LOCAL_KEY


def _cooldown(m, code, rinfo, body=''):
    if code == 429:
        wait = COOLDOWN_429
        try:
            ra = int(rinfo.get('Retry-After', ''))
            wait = max(wait, min(ra, 3600))
        except Exception:
            pass
        # Nous juga kirim retry_after di body JSON, bukan cuma header.
        try:
            j = json.loads(body)
            err = j.get('error') if isinstance(j.get('error'), dict) else {}
            ra = int(err.get('retry_after') or j.get('retry_after'))
            wait = max(wait, min(ra, 3600))
        except Exception:
            pass
    else:
        wait = COOLDOWN_401
    m.cooldown_until = time.monotonic() + wait
    _log('[%s] cooldown %ds (http=%s)' % (m.name, wait, code))


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send_json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = self.path.split('?')[0]
        if p in ('/health', '/v1/health'):
            return self._send_json(200, {
                'status': 'ok', 'upstream': UP,
                'members': [m.info() for m in MEMBERS],
                'strip_responses': STRIP, 'no_refresh': NO_REFRESH})
        if p in ('/stats', '/v1/stats'):
            return self._send_json(200, {'members': [m.info() for m in MEMBERS],
                                         'rr_pointer': _rr[0]})
        if not _client_ok(self.headers):
            return self._send_json(401, {'error': {'message': 'local key salah'}})
        m = pick_member()
        if m is None:
            return self._send_json(503, {'error': {'message': 'semua member cooldown'}})
        try:
            bearer = m.bearer()
        except Exception as e:
            m.stats['fail'] += 1
            m.cooldown_until = time.monotonic() + COOLDOWN_401
            return self._send_json(401, {'error': {'message': '[%s] %s' % (m.name, e)}})
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in SKIP_HDRS}
        hdrs['Authorization'] = 'Bearer ' + bearer
        req = urllib.request.Request(_upstream_url(self.path), headers=hdrs, method='GET')
        try:
            r = urllib.request.urlopen(req, timeout=60)
            code, raw, ct = r.status, r.read(), r.headers.get('Content-Type') or 'application/json'
        except urllib.error.HTTPError as e:
            code, raw, ct = e.code, e.read(), e.headers.get('Content-Type') or 'application/json'
        except Exception as e:
            code, raw, ct = 599, str(e).encode(), 'text/plain'
        m.stats['req'] += 1
        m.stats['last_status'] = code
        if 200 <= code < 300:
            m.stats['ok'] += 1
        else:
            m.stats['fail'] += 1
        _log('[%s] GET %s http=%s resp=%dB' % (m.name, self.path, code, len(raw)))
        self.send_response(code)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        with UP_SEM:
            self._do_post()

    def _do_post(self):
        if not _client_ok(self.headers):
            return self._send_json(401, {'error': {'message': 'local key salah'}})
        ln = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(ln)
        body, stripped = sanitize(raw, self.path)
        N[0] += 1
        if DUMP:
            try:
                os.makedirs(DUMP, exist_ok=True)
                open(os.path.join(DUMP, 'req_%03d.json' % N[0]), 'wb').write(body)
            except Exception:
                pass
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in SKIP_HDRS}

        tried = set()
        code = rinfo = stream = None
        n429 = 0
        for _ in range(len(MEMBERS) + 1):
            m = pick_member(exclude=tried)
            if m is None:
                break
            tried.add(m.name)
            m.stats['req'] += 1
            bearer = None
            for attempt in (1, 2):
                try:
                    bearer = m.bearer(force=(attempt == 2), stale=bearer)
                except Exception as e:
                    _log('[%s] bearer gagal: %s' % (m.name, e))
                    code = 401
                    break
                hdrs['Authorization'] = 'Bearer ' + bearer
                req = urllib.request.Request(_upstream_url(self.path), data=body,
                                             headers=hdrs, method='POST')
                try:
                    r = urllib.request.urlopen(req, timeout=600)
                    code, rinfo, stream = r.status, r.headers, r
                except urllib.error.HTTPError as e:
                    code, rinfo, stream = e.code, e.headers, e
                except Exception as e:
                    code = 599
                    _log('[%s] upstream gagal: %s' % (m.name, type(e).__name__))
                    break
                if code == 401 and attempt == 1:
                    try:
                        stream.read()
                    except Exception:
                        pass
                    _log('[%s] 401 -> retry jalur resmi' % m.name)
                    continue
                break
            m.stats['last_status'] = code
            if code is not None and 200 <= code < 300:
                m.stats['ok'] += 1
                m.cooldown_until = 0.0
                _log('[%s] POST %s http=%s stripped=%d req=%dB' %
                     (m.name, self.path, code, stripped, len(raw)))
                break
            m.stats['fail'] += 1
            preview = ''
            if stream is not None:
                try:
                    preview = stream.read(2000).decode('utf-8', 'replace')
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
                stream = None
            if code in (401, 429):
                _cooldown(m, code, rinfo, preview)
            if code in (401, 429, 503):
                rl = {k: v for k, v in (rinfo.items() if rinfo else [])
                      if k.lower() == 'retry-after' or k.lower().startswith('x-ratelimit')}
                _log('[%s] POST %s http=%s hdr=%s body=%s' %
                     (m.name, self.path, code, rl, preview[:500]))
            if code == 429:
                n429 += 1
                if n429 >= MAX_429_FAILOVER:
                    _log('[%s] cap failover 429 tercapai (%d) -> stop, sisa pool diamankan'
                         % (m.name, n429))
                    break
            _log('[%s] POST %s gagal http=%s -> failover' % (m.name, self.path, code))

        if stream is None:
            if code is None:
                # Tidak ada member yang bisa dicoba (semua cooldown).
                # Kasih tau klien kapan boleh retry, jangan tembak upstream.
                wait = max(1, int(min(m.cooldown_until for m in MEMBERS)
                                  - time.monotonic()) + 1)
                return self._send_json(503, {
                    'error': {'message': 'semua member cooldown',
                              'reason': 'rate_limited', 'retry_after': wait}})
            if code == 429:
                # Cap failover tercapai: masih ada member segar, tapi request
                # ini berhenti biar tidak membakar pool. Beri retry_after.
                wait = max(1, int(min(m.cooldown_until for m in MEMBERS)
                                  - time.monotonic()) + 1)
                return self._send_json(503, {
                    'error': {'message': 'rate limited (failover cap)',
                              'reason': 'rate_limited', 'retry_after': wait}})
            return self._send_json(503 if code == 401 else 599,
                                   {'error': {'message': 'semua member gagal '
                                              '(last http=%s)' % code}})
        self.send_response(code)
        self.send_header('Content-Type', rinfo.get('Content-Type') or 'application/json')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                self.wfile.write(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except Exception:
            pass


def _keepalive_loop():
    while True:
        time.sleep(30)
        if NO_REFRESH:
            return
        for m in MEMBERS:
            if time.monotonic() < m.cooldown_until:
                continue
            if m.kind == 'live' and LIVE_BACKEND == 'readonly':
                continue
            try:
                exp = m.cache['exp']
                if exp is None:
                    if m.kind == 'file':
                        best, _ = _read_file_member(m.path)
                        exp = best['exp'] if best else None
                    else:
                        best, _ = _read_file_member(HERMES_HOME / 'auth.json')
                        exp = best['exp'] if best else None
                tick = max(60, min(900, m.lifetime() // 4))
                horizon = KEEPALIVE_HORIZON if KEEPALIVE_HORIZON > 0 else tick + SKEW
                if exp is None or exp.timestamp() - _now().timestamp() < horizon:
                    m.refresh_now()
                    _log('[%s] keepalive refresh OK' % m.name)
            except Exception as e:
                _log('[%s] keepalive gagal: %s' % (m.name, e))
                m.cooldown_until = time.monotonic() + COOLDOWN_401
            try:
                left = m.info().get('grant_days_left')
                if left is not None and left <= 5 and \
                        time.monotonic() - m.last_grant_warn > 21600:
                    m.last_grant_warn = time.monotonic()
                    _log('[%s] WARNING: grant device-code mati dalam %.1f hari '
                         '(hard limit 30 hari dari approval, refresh TIDAK '
                         'memperpanjang). Login ulang: nous_login.py --force %s'
                         % (m.name, left, m.name))
            except Exception:
                pass


class Server(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


if __name__ == '__main__':
    if os.environ.get('FORCE_REFRESH') == '1':
        for m in MEMBERS:
            if m.kind == 'live':
                try:
                    m.bearer(force=True)
                    _log('[%s] force refresh OK' % m.name)
                except Exception as e:
                    _log('[%s] force refresh gagal: %s' % (m.name, e))
    if KEEPALIVE and not NO_REFRESH:
        threading.Thread(target=_keepalive_loop, daemon=True,
                         name='nous-proxy-keepalive').start()
    print('nous proxy: 127.0.0.1:%d -> %s' % (PORT, UP), flush=True)
    print('members: %s' % ', '.join('%s(%s)' % (m.name, m.kind) for m in MEMBERS), flush=True)
    print('keepalive: %s | log: %s' % ('on' if KEEPALIVE and not NO_REFRESH else 'off', LOG), flush=True)
    Server(('127.0.0.1', PORT), H).serve_forever()

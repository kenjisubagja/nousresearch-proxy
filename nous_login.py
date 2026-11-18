#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
HERMES_REPO = os.environ.get('HERMES_REPO', str(Path.home() / '.hermes' / 'hermes-agent'))
PORTAL = (os.environ.get('HERMES_PORTAL_BASE_URL') or
          os.environ.get('NOUS_PORTAL_BASE_URL') or
          'https://portal.nousresearch.com').rstrip('/')
INFERENCE = 'https://inference-api.nousresearch.com/v1'
CLIENT_ID = 'hermes-cli'
SCOPE = 'inference:invoke'
GRANT_DEVICE = 'urn:ietf:params:oauth:grant-type:device_code'
POLL_CAP = 1
EMPTY_AGENT_KEYS = {'agent_key': None, 'agent_key_id': None,
                    'agent_key_expires_at': None, 'agent_key_expires_in': None,
                    'agent_key_reused': None, 'agent_key_obtained_at': None}


def _now():
    return datetime.now(timezone.utc)


def _iso_after(now, ttl):
    return datetime.fromtimestamp(now.timestamp() + int(ttl), tz=timezone.utc).isoformat()


def _jwt_claims(token):
    try:
        seg = str(token).split('.')[1]
        return json.loads(base64.urlsafe_b64decode(seg + '=' * (-len(seg) % 4)))
    except Exception:
        return {}


def label_from_token(token, fallback='nous-account'):
    claims = _jwt_claims(token)
    for key in ('email', 'preferred_username', 'upn'):
        val = claims.get(key)
        if val:
            return str(val)
    return fallback


def _post_form(url, fields, timeout=30):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    req.add_header('Accept', 'application/json')
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = {}
        return e.code, dict(e.headers), payload


def _login_stdlib(open_browser=True):
    code, _, dev = _post_form(PORTAL + '/api/oauth/device/code',
                              {'client_id': CLIENT_ID, 'scope': SCOPE})
    if code != 200 or 'device_code' not in dev:
        raise SystemExit('Device code request gagal (http=%s): %s' %
                         (code, json.dumps(dev)[:200]))
    url = dev['verification_uri_complete']
    print()
    print('To continue:')
    print('  1. Open: %s' % url)
    print('  2. If prompted, enter code: %s' % dev['user_code'])
    if open_browser:
        try:
            if webbrowser.open(url):
                print('  (Opened browser for verification)')
        except Exception:
            pass
    interval = max(1, min(int(dev['interval']), POLL_CAP))
    deadline = time.monotonic() + max(1, int(dev['expires_in']))
    print('Waiting for approval (polling every %ss)...' % interval, flush=True)
    edge_backoff = 0.0
    while time.monotonic() < deadline:
        code, hdrs, payload = _post_form(
            PORTAL + '/api/oauth/token',
            {'grant_type': GRANT_DEVICE, 'client_id': CLIENT_ID,
             'device_code': dev['device_code']})
        if code == 200 and payload.get('access_token'):
            now = _now()
            ttl = int(payload.get('expires_in') or 3600)
            state = {
                'portal_base_url': PORTAL,
                'inference_base_url': payload.get('inference_base_url') or INFERENCE,
                'client_id': CLIENT_ID,
                'scope': payload.get('scope') or SCOPE,
                'token_type': payload.get('token_type') or 'Bearer',
                'access_token': payload['access_token'],
                'refresh_token': payload.get('refresh_token'),
                'obtained_at': now.isoformat(),
                'expires_at': _iso_after(now, ttl),
                'expires_in': ttl,
                'tls': {'insecure': False, 'ca_bundle': None},
                **EMPTY_AGENT_KEYS}
            return state
        err = str(payload.get('error') or '')
        if err == 'authorization_pending':
            edge_backoff = 0.0
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            continue
        if err == 'slow_down':
            interval = min(interval + 1, 30)
            time.sleep(interval)
            continue
        if code in (408, 429) or code >= 500 or (code == 403 and hdrs.get('x-vercel-mitigated')):
            try:
                ra = int(hdrs.get('Retry-After', ''))
                edge_backoff = min(max(interval, ra), 60)
            except Exception:
                edge_backoff = min(max(edge_backoff * 2, interval * 2, 5), 60)
            time.sleep(max(0.0, min(edge_backoff, deadline - time.monotonic())))
            continue
        raise SystemExit('Login gagal: %s — %s' %
                         (err or ('http %s' % code),
                          payload.get('error_description') or json.dumps(payload)[:200]))
    raise SystemExit('Device code expired sebelum di-approve. Ulangi login; '
                     'pastikan sudah sign-in di %s/login' % PORTAL)


def login_official(open_browser=True):
    if HERMES_REPO not in sys.path:
        sys.path.insert(0, HERMES_REPO)
    from hermes_cli.auth_nous import _nous_device_code_login
    return _nous_device_code_login(open_browser=open_browser)


def build_store(state, label=None):
    state = dict(state)
    lbl = label or str(state.get('label') or '').strip() or \
        label_from_token(state.get('access_token', ''))
    state.setdefault('label', lbl)
    entry = {
        'id': uuid.uuid4().hex[:6],
        'label': lbl,
        'auth_type': 'oauth',
        'priority': 0,
        'source': 'device_code',
        'access_token': state.get('access_token'),
        'refresh_token': state.get('refresh_token'),
        'last_status': 'ok',
        'last_status_at': None,
        'last_error_code': None,
        'last_error_reason': None,
        'last_error_message': None,
        'last_error_reset_at': None,
        'expires_at': state.get('expires_at'),
        'inference_base_url': state.get('inference_base_url'),
        'agent_key': state.get('agent_key'),
        'agent_key_expires_at': state.get('agent_key_expires_at'),
        'request_count': 0,
        'token_type': state.get('token_type'),
        'scope': state.get('scope'),
        'client_id': state.get('client_id'),
        'portal_base_url': state.get('portal_base_url'),
        'obtained_at': state.get('obtained_at'),
        'expires_in': state.get('expires_in'),
        'agent_key_id': state.get('agent_key_id'),
        'agent_key_expires_in': state.get('agent_key_expires_in'),
        'agent_key_reused': state.get('agent_key_reused'),
        'agent_key_obtained_at': state.get('agent_key_obtained_at'),
        'grant_approved_at': state.get('grant_approved_at'),
        'tls': state.get('tls'),
    }
    return {'version': 1, 'providers': {'nous': state},
            'active_provider': 'nous', 'credential_pool': {'nous': [entry]}}


def existing_accounts(auth_dir):
    out = {}
    for p in sorted(auth_dir.glob('*.json')):
        try:
            store = json.loads(p.read_text())
            st = (store.get('providers') or {}).get('nous') or {}
            tok = st.get('agent_key') or st.get('access_token') or ''
            email = _jwt_claims(tok).get('email')
            if email:
                out[str(email)] = p.name
        except Exception:
            continue
    return out


def next_name(auth_dir):
    nums = []
    for p in auth_dir.glob('auth*.json'):
        m = re.fullmatch(r'auth(\d+)', p.stem)
        if m:
            nums.append(int(m.group(1)))
    return 'auth%d' % (max(nums) + 1 if nums else 1)


def verify_inference(state):
    tok = state.get('agent_key') or state.get('access_token')
    base = str(state.get('inference_base_url') or INFERENCE).rstrip('/')
    req = urllib.request.Request(base + '/models')
    req.add_header('Authorization', 'Bearer ' + tok)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        data = json.loads(r.read())
        return len(data.get('data', []))
    except Exception as e:
        return 'gagal: %s' % type(e).__name__


def main():
    ap = argparse.ArgumentParser(description='Login Nous Portal ala Hermes -> auths/<nama>.json')
    ap.add_argument('name', nargs='?', help='nama file tanpa .json (default: authN urut)')
    ap.add_argument('--auth-dir', default=os.environ.get('AUTH_DIR', str(SCRIPT_DIR / 'auths')))
    ap.add_argument('--force', action='store_true', help='izinkan akun yang sudah ada')
    ap.add_argument('--no-browser', action='store_true', help='jangan buka browser otomatis')
    args = ap.parse_args()

    auth_dir = Path(args.auth_dir).expanduser()
    auth_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(auth_dir, 0o700)
    name = re.sub(r'[^A-Za-z0-9_-]', '', args.name) if args.name else next_name(auth_dir)
    if not name:
        raise SystemExit('Nama file tidak valid.')
    target = auth_dir / (name + '.json')
    if target.exists() and not args.force:
        raise SystemExit('%s sudah ada. Pakai nama lain atau --force.' % target)

    print('Login Nous Portal (jalur resmi Hermes device-code)')
    print('Portal: %s' % PORTAL)
    try:
        state = login_official(open_browser=not args.no_browser)
        print('(selesai via fungsi resmi Hermes in-process)')
    except SystemExit:
        raise
    except Exception as e:
        print('Jalur resmi tidak tersedia (%s: %s) — fallback stdlib.' %
              (type(e).__name__, e))
        state = _login_stdlib(open_browser=not args.no_browser)

    state['grant_approved_at'] = _now().isoformat()

    label = label_from_token(state.get('access_token', ''))
    accounts = existing_accounts(auth_dir)
    if label in accounts and not args.force:
        raise SystemExit(
            'Akun %s sudah ada di %s. Login ulang akun yang sama membuat chain '
            'terpisah (boleh, seperti device baru), tapi default diblok biar '
            'tidak dobel. Ulangi dengan --force kalau memang mau.' %
            (label, accounts[label]))

    store = build_store(state, label)
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps(store, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)

    print()
    print('Login berhasil!')
    print('  Akun   : %s' % label)
    print('  File   : %s' % target)
    print('  Expires: %s' % state.get('expires_at'))
    n = verify_inference(state)
    print('  Cek /v1/models: %s model' % n if isinstance(n, int) else '  Cek /v1/models: %s' % n)
    print()
    print('Restart nous_proxy.py biar file baru ke-load jadi member pool.')


if __name__ == '__main__':
    main()

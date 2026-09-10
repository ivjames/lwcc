#!/usr/bin/env python3
"""Accounts, invite links and sessions for the lwcc app — stdlib only.

Everything lives in users.json next to the app (mode 0600, gitignored, never
committed):

    {"users":    {"<email>": {role, pw, created, passwordSet}},
     "invites":  {"<token>": {email, role, reset, created, expires}},
     "sessions": {"<id>":    {email, created}}}

Two rules shape the design:

  * **Nobody types someone else's password.** An account exists only once an
    invite link has been redeemed, and the person redeeming it chooses the
    password on the spot — so whoever issued the invite never sees it. The
    same mechanism, pinned to an existing email, is the password reset.
  * **Sessions are server-side.** The cookie carries a random id and nothing
    else, so signing out, resetting a password or removing a user revokes
    access immediately instead of waiting for a cookie to expire.

Passwords are scrypt (n=16384, r=8, p=1) over a 16-byte salt.

The app and this file's CLI both write users.json; writes are atomic
(temp file + os.replace) and the file is small, so the loser of a race
between a `lwcc invite` and a sign-in loses one write, not the file.

CLI:

    lwccauth.py invite [--admin] [--for EMAIL]   one-time sign-up link
    lwccauth.py invite --reset --for EMAIL       password-reset link
    lwccauth.py users                            accounts + pending links
    lwccauth.py user-remove EMAIL
    lwccauth.py invite-revoke TOKEN
"""
import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(ROOT, 'users.json')
ROLES = ('admin', 'staff')
INVITE_TTL = 7 * 24 * 3600          # one week to redeem a link
SESSION_TTL = 180 * 24 * 3600       # matches the cookie's Max-Age
MIN_PASSWORD = 10
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$')

# scrypt work factor. 16384/8/1 needs ~16 MB per hash — comfortably under
# OpenSSL's 32 MB default ceiling, and slow enough to make an offline guess
# at a stolen users.json expensive.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 16384, 8, 1

_LOCK = threading.Lock()


class AuthError(Exception):
    """Something the caller should show the person, with an HTTP status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def _epoch():
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def site_url():
    """Where invite links point. SITE_URL in .env wins; the live host is the
    default so a link minted on the droplet is clickable as printed."""
    path = os.path.join(ROOT, '.env')
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line.startswith('SITE_URL=') and line.split('=', 1)[1].strip():
                    return line.split('=', 1)[1].strip().rstrip('/')
    except OSError:
        pass
    return 'https://lwcc.lab980.com'


def norm_email(email):
    return str(email or '').strip().lower()


# -- storage ----------------------------------------------------------------

def _blank():
    return {'users': {}, 'invites': {}, 'sessions': {}}


def load():
    """The whole store, with expired invites and sessions dropped. Read fresh
    every time: the CLI writes this file behind the running app's back, and a
    cached copy would answer with accounts that no longer exist."""
    try:
        with open(USERS_FILE, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    for key in ('users', 'invites', 'sessions'):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    now = _epoch()
    data['invites'] = {t: i for t, i in data['invites'].items()
                       if float(i.get('expires') or 0) > now}
    data['sessions'] = {s: v for s, v in data['sessions'].items()
                        if float(v.get('created') or 0) + SESSION_TTL > now
                        and v.get('email') in data['users']}
    return data


def save(data):
    """Atomic, 0600. The temp file is created in the app dir (same filesystem)
    so os.replace is a rename, never a copy."""
    fd, tmp = tempfile.mkstemp(prefix='.users.json.', dir=ROOT)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write('\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, USERS_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _mutate(fn):
    """Read-modify-write under the process lock; returns whatever fn returns."""
    with _LOCK:
        data = load()
        result = fn(data)
        save(data)
        return result


# -- passwords --------------------------------------------------------------

def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=SCRYPT_N,
                        r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f'scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}'


def check_password(password, stored):
    try:
        scheme, n, r, p, salt, want = str(stored).split('$')
        if scheme != 'scrypt':
            return False
        dk = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt),
                            n=int(n), r=int(r), p=int(p), dklen=len(want) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


_DUMMY = None


def _burn_a_hash():
    """Hash something on an unknown email so a missing account costs the same
    as a wrong password — otherwise the reply time says who has an account."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password('a password that is nobody{s'.replace('{', "'"))
    check_password('wrong password', _DUMMY)


def password_problem(password, confirm=None):
    """None when the password is acceptable, else why not."""
    password = password or ''
    if len(password) < MIN_PASSWORD:
        return f'Password must be at least {MIN_PASSWORD} characters.'
    if confirm is not None and password != confirm:
        return 'The two passwords do not match.'
    return None


# -- accounts ---------------------------------------------------------------

def any_users():
    return bool(load()['users'])


def get_user(email):
    return load()['users'].get(norm_email(email))


def list_users():
    data = load()
    counts = {}
    for s in data['sessions'].values():
        counts[s.get('email')] = counts.get(s.get('email'), 0) + 1
    out = []
    for email, u in sorted(data['users'].items()):
        out.append({**u, 'email': email, 'sessions': counts.get(email, 0)})
    return out


def list_invites():
    data = load()
    return sorted(({**i, 'token': t} for t, i in data['invites'].items()),
                  key=lambda i: i.get('created') or '')


def verify(email, password):
    """The account when the password is right, else None (constant-ish work
    either way)."""
    email = norm_email(email)
    user = load()['users'].get(email)
    if not user:
        _burn_a_hash()
        return None
    if not check_password(password or '', user.get('pw') or ''):
        return None
    return {**user, 'email': email}


def remove_user(email, by=None):
    """Delete an account and every session it holds. Refuses self-removal:
    the last admin deleting themselves locks everyone out of /admin/users."""
    email = norm_email(email)
    if by and norm_email(by) == email:
        raise AuthError('You cannot remove your own account.', 400)

    def go(data):
        if email not in data['users']:
            raise AuthError(f'No account for {email}.', 404)
        del data['users'][email]
        data['sessions'] = {s: v for s, v in data['sessions'].items()
                            if v.get('email') != email}
        data['invites'] = {t: i for t, i in data['invites'].items()
                           if norm_email(i.get('email')) != email}
        return True

    return _mutate(go)


# -- invite / reset links ---------------------------------------------------

def create_invite(email=None, role='staff', reset=False):
    """Mint a one-time link. An invite creates an account on redemption; a
    reset re-keys an existing one. Both expire after INVITE_TTL."""
    email = norm_email(email)
    role = role if role in ROLES else 'staff'
    if email and not EMAIL_RE.match(email):
        raise AuthError(f'{email!r} does not look like an email address.', 400)
    if reset and not email:
        raise AuthError('A password reset needs the account email.', 400)
    token = secrets.token_urlsafe(32)

    def go(data):
        if reset:
            if email not in data['users']:
                raise AuthError(f'No account for {email}.', 404)
        elif email and email in data['users']:
            raise AuthError(f'{email} already has an account — send a '
                            f'password reset instead.', 409)
        if email:
            # One live link per address, so an accidental double-invite
            # doesn't leave a second working link behind.
            data['invites'] = {t: i for t, i in data['invites'].items()
                               if norm_email(i.get('email')) != email}
        data['invites'][token] = {
            'email': email or None,
            'role': data['users'].get(email, {}).get('role', role) if reset else role,
            'reset': bool(reset),
            'created': now_iso(),
            'expires': _epoch() + INVITE_TTL,
        }
        return token

    _mutate(go)
    return {'token': token, 'email': email or None, 'reset': bool(reset),
            'role': role, 'url': invite_url(token)}


def invite_url(token):
    return f'{site_url()}/invite/{token}'


def get_invite(token):
    """The live invite, or None when it is unknown, spent or expired."""
    return load()['invites'].get(str(token or ''))


def revoke_invite(token):
    def go(data):
        if str(token) not in data['invites']:
            raise AuthError('No such pending link.', 404)
        del data['invites'][str(token)]
        return True

    return _mutate(go)


def redeem_invite(token, password, confirm=None, email=None):
    """Set the password the link is for and sign that account in. Returns
    (session_id, user). The link is spent whether it created the account or
    reset it, and a reset drops the account's other sessions."""
    token = str(token or '')
    problem = password_problem(password, confirm)
    if problem:
        raise AuthError(problem, 400)
    sid = secrets.token_urlsafe(32)
    pw = hash_password(password)

    def go(data):
        invite = data['invites'].get(token)
        if not invite or float(invite.get('expires') or 0) <= _epoch():
            raise AuthError('This link has already been used or has expired. '
                            'Ask for a new one.', 410)
        addr = norm_email(invite.get('email') or email)
        if not addr or not EMAIL_RE.match(addr):
            raise AuthError('That link needs an email address.', 400)
        if invite.get('reset'):
            if addr not in data['users']:
                raise AuthError(f'No account for {addr}.', 404)
            data['users'][addr]['pw'] = pw
            data['users'][addr]['passwordSet'] = now_iso()
            # A reset means the old password may be in the wrong hands —
            # every other browser it signed in is signed out.
            data['sessions'] = {s: v for s, v in data['sessions'].items()
                                if v.get('email') != addr}
        else:
            if addr in data['users']:
                raise AuthError(f'{addr} already has an account.', 409)
            data['users'][addr] = {
                'role': invite.get('role') if invite.get('role') in ROLES else 'staff',
                'pw': pw, 'created': now_iso(), 'passwordSet': now_iso()}
        del data['invites'][token]
        data['sessions'][sid] = {'email': addr, 'created': _epoch()}
        return {**data['users'][addr], 'email': addr}

    return sid, _mutate(go)


# -- sessions ---------------------------------------------------------------

def create_session(email):
    email = norm_email(email)
    sid = secrets.token_urlsafe(32)

    def go(data):
        if email not in data['users']:
            raise AuthError(f'No account for {email}.', 404)
        data['sessions'][sid] = {'email': email, 'created': _epoch()}
        return sid

    return _mutate(go)


def session_user(sid):
    """The signed-in account for a cookie value, or None."""
    sid = str(sid or '')
    if not sid:
        return None
    data = load()
    s = data['sessions'].get(sid)
    if not s:
        return None
    user = data['users'].get(s.get('email'))
    if not user:
        return None
    return {**user, 'email': s['email']}


def destroy_session(sid):
    sid = str(sid or '')
    if not sid:
        return False

    def go(data):
        return data['sessions'].pop(sid, None) is not None

    return _mutate(go)


# -- CLI --------------------------------------------------------------------

def _cmd_invite(args):
    inv = create_invite(email=args.email, role='admin' if args.admin else 'staff',
                        reset=args.reset)
    who = inv['email'] or 'anyone with the link'
    kind = 'Password reset' if inv['reset'] else f'Invite ({inv["role"]})'
    print(f'{kind} for {who} — expires in 7 days, usable once:\n\n  {inv["url"]}\n')
    if not inv['email']:
        print('This link has no email pinned: whoever opens it chooses both '
              'the address and the password.')
    return 0


def _cmd_users(args):
    users = list_users()
    if not users:
        print('No accounts yet. Mint the first one with:\n'
              '  lwcc invite --admin --for you@example.com')
    else:
        print(f'{len(users)} account(s):')
        for u in users:
            print(f'  {u["email"]:<34} {u["role"]:<6} '
                  f'created {u.get("created", "?")}  '
                  f'{u["sessions"]} active session(s)')
    invites = list_invites()
    if invites:
        print(f'\n{len(invites)} pending link(s):')
        for i in invites:
            kind = 'reset' if i.get('reset') else f'invite/{i.get("role")}'
            print(f'  {i.get("email") or "(open)":<34} {kind:<13} '
                  f'{invite_url(i["token"])}')
    return 0


def _cmd_user_remove(args):
    remove_user(args.email)
    print(f'Removed {norm_email(args.email)} — any browser it was signed in '
          f'on is signed out.')
    return 0


def _cmd_invite_revoke(args):
    revoke_invite(args.token)
    print('Link revoked.')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='lwccauth', description='lwcc accounts, invite links and sessions.')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('invite', help='mint a one-time sign-up or reset link')
    p.add_argument('--admin', action='store_true',
                   help='the account gets the admin role (maintenance tools)')
    p.add_argument('--for', dest='email', default=None,
                   help='pin the link to this email address')
    p.add_argument('--reset', action='store_true',
                   help='password reset for an existing account (needs --for)')
    p.set_defaults(fn=_cmd_invite)

    p = sub.add_parser('users', help='list accounts and pending links')
    p.set_defaults(fn=_cmd_users)

    p = sub.add_parser('user-remove', help='delete an account and its sessions')
    p.add_argument('email')
    p.set_defaults(fn=_cmd_user_remove)

    p = sub.add_parser('invite-revoke', help='cancel a pending link')
    p.add_argument('token')
    p.set_defaults(fn=_cmd_invite_revoke)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except AuthError as e:
        sys.stderr.write(f'error: {e}\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())

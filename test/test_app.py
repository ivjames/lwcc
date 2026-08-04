"""End-to-end test of the site app: start it on a scratch public/ dir, upload
the sample PDF through /api/upload, and check routing + publishing.

Run:  python3 test/test_app.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
SAMPLE = os.path.join(ROOT, 'samples', 'WG_2026_08_02.pdf')

# Filename-date corroboration (clears the OCR verify warning): every naming
# style in the backlog, plus the reconvert source path.
sys.path.insert(0, ROOT)
import app as app_mod  # noqa: E402
assert app_mod.filename_matches_date('WG 010823.pdf', '2023-01-08')
assert app_mod.filename_matches_date('WG_2025_04_13.pdf', '2025-04-13')
assert app_mod.filename_matches_date('WG 4.16.23 PDF.pdf', '2023-04-16')
assert app_mod.filename_matches_date('2023-10-01/source.pdf', '2023-10-01')
assert not app_mod.filename_matches_date('WG 010823.pdf', '2023-01-15')
assert not app_mod.filename_matches_date(None, '2023-01-08')
PORT = 8972
BASE = f'http://127.0.0.1:{PORT}'
TOKEN = 'test-token-123'

# Run the app from a scratch copy so the repo's public/ and .env are untouched.
scratch = tempfile.mkdtemp(prefix='lwcc-app-test-')
for name in ('app.py', 'wgconvert', 'config', 'assets', 'template'):
    src = os.path.join(ROOT, name)
    dst = os.path.join(scratch, name)
    if os.path.isdir(src):
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__'))
    else:
        shutil.copyfile(src, dst)
with open(os.path.join(scratch, '.env'), 'w') as fh:
    fh.write(f'UPLOAD_TOKEN={TOKEN}\n')

proc = subprocess.Popen(
    [sys.executable, os.path.join(scratch, 'app.py'), '--port', str(PORT)],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                      # keep 3xx visible (login/logout flows)


OPENER = urllib.request.build_opener(NoRedirect)
LAST = {}                                # headers of the most recent response


def req(path, data=None, headers=None, method=None):
    r = urllib.request.Request(BASE + path, data=data, headers=headers or {}, method=method)
    try:
        with OPENER.open(r, timeout=60) as resp:
            LAST['headers'] = resp.headers
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        LAST['headers'] = e.headers
        return e.code, e.read()


def login(token, nxt='/admin'):
    return req('/admin/login', method='POST',
               data=urllib.parse.urlencode({'token': token, 'next': nxt}).encode(),
               headers={'Content-Type': 'application/x-www-form-urlencoded'})


try:
    for _ in range(50):                      # wait for startup
        try:
            status, _ = req('/healthz')
            if status == 200:
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise AssertionError('app did not start')

    # Nothing published yet: / falls through to 404, archive is empty.
    status, body = req('/archive')
    assert status == 200 and b'Nothing published yet' in body

    # The whole admin area is behind a sign-in: pages answer 401 with the
    # login form until the session cookie is set.
    status, body = req('/admin')
    assert status == 401 and b'Admin Sign-in' in body and b'name="token"' in body
    assert b'Publish Worship Guides' not in body
    assert 'no-store' in (LAST['headers'].get('Cache-Control') or '')

    status, body = login('wrong')
    assert status == 401 and b'Wrong token' in body

    # Right token: 303 + long-lived HttpOnly cookie; an off-site "next" is
    # ignored in favor of /admin.
    status, body = login(TOKEN, nxt='https://evil.example/')
    assert status == 303, (status, body)
    setc = LAST['headers'].get('Set-Cookie') or ''
    assert 'wg_token=' in setc and 'HttpOnly' in setc and 'Max-Age=15552000' in setc, setc
    assert LAST['headers'].get('Location') == '/admin'
    COOKIE = {'Cookie': setc.split(';', 1)[0]}

    status, body = req('/admin', headers=COOKIE)
    assert status == 200 and b'Publish Worship Guides' in body and b'multiple' in body
    assert b'id="token"' not in body, 'no per-action token field anymore'
    assert 'no-store' in (LAST['headers'].get('Cache-Control') or '')

    with open(SAMPLE, 'rb') as fh:
        pdf = fh.read()

    # Fails closed / bad token.
    status, body = req('/api/upload', data=pdf,
                       headers={'X-Upload-Token': 'wrong', 'Content-Type': 'application/pdf'})
    assert status == 401, (status, body)

    status, body = req('/api/upload?sync=1', data=b'%PDF-not really',
                       headers={'X-Upload-Token': TOKEN, 'Content-Type': 'application/pdf'})
    assert status == 422, (status, body)

    status, body = req('/api/upload', data=b'hello',
                       headers={'X-Upload-Token': TOKEN, 'Content-Type': 'application/pdf'})
    assert status == 400, (status, body)

    # The real upload converts and publishes (?sync=1: result inline).
    status, body = req('/api/upload?sync=1', data=pdf,
                       headers={'X-Upload-Token': TOKEN, 'Content-Type': 'application/pdf'})
    assert status == 200, (status, body)
    data = json.loads(body)
    assert data['ok'] and data['dateISO'] == '2026-08-02' and data['warnings'] == [], data
    assert data.get('notes') == [], 'responses carry the informational tier too'
    assert data['replaced'] is False, data

    # Re-uploading the same Sunday overwrites in place and says so. The
    # session cookie authenticates API calls just like the header does.
    status, body = req('/api/upload?sync=1', data=pdf,
                       headers={**COOKIE, 'Content-Type': 'application/pdf'})
    assert status == 200, (status, body)
    assert json.loads(body)['replaced'] is True

    # Default (no ?sync=1): the bytes are accepted immediately, conversion
    # runs from the server-side queue, progress is pollable, and the result
    # still reaches uploads.log. The status endpoint is gated like uploads.
    status, body = req('/api/upload', data=pdf,
                       headers={**COOKIE, 'Content-Type': 'application/pdf',
                                'X-Filename': 'WG_async.pdf'})
    assert status == 200, (status, body)
    j = json.loads(body)
    assert j['ok'] and j.get('queued') and j.get('id'), j
    jid = j['id']
    status, body = req('/api/status?ids=' + jid)
    assert status == 401, 'status endpoint gated'
    js = None
    for _ in range(240):
        status, body = req('/api/status?ids=' + jid, headers=COOKIE)
        assert status == 200, (status, body)
        js = json.loads(body)['jobs'][jid]
        if js['status'] in ('ok', 'warned', 'failed'):
            break
        time.sleep(0.5)
    assert js and js['status'] == 'ok' and js['dateISO'] == '2026-08-02', js
    assert js['replaced'] is True and js['warnings'] == [], js
    assert not os.listdir(os.path.join(scratch, 'queue')), 'spool emptied after success'
    snap = json.loads(body)['queue']
    assert snap['waiting'] == 0 and snap['converting'] is None, \
        'status responses carry a live queue snapshot'

    # An explicit ?date= pins publishing regardless of what the PDF says
    # (memorial programs whose printed dates are not the service date).
    status, body = req('/api/upload?sync=1&date=2020-01-05', data=pdf,
                       headers={**COOKIE, 'Content-Type': 'application/pdf'})
    assert status == 200, (status, body)
    d = json.loads(body)
    assert d['ok'] and d['dateISO'] == '2020-01-05', d
    assert os.path.exists(os.path.join(scratch, 'public', '2020-01-05', 'index.html'))
    status, body = req('/api/upload?sync=1&date=Jan-5', data=pdf,
                       headers={**COOKIE, 'Content-Type': 'application/pdf'})
    assert status == 400, 'malformed override rejected'

    out_dir = os.path.join(scratch, 'public', '2026-08-02')
    for f in ('index.html', 'guide.json', 'cover.jpg'):
        assert os.path.exists(os.path.join(out_dir, f)), f

    # The uploaded PDF is retained next to its output, and served.
    src = os.path.join(out_dir, 'source.pdf')
    assert os.path.exists(src) and open(src, 'rb').read() == pdf, 'source retained'
    status, body = req('/2026-08-02/source.pdf')
    assert status == 200 and body.startswith(b'%PDF-')

    # The newest Sunday is now the front page, and stays reachable by date.
    status, body = req('/')
    assert status == 200 and b'Love Unleashed' in body
    status, body = req('/2026-08-02/')
    assert status == 200 and b'Love Unleashed' in body
    assert b'class="weeknav"' in body and b'All Sundays' in body, 'week nav injected'
    status, body = req('/archive')
    assert status == 200 and b'/2026-08-02/' in body
    assert b'Love Unleashed' in body, 'archive shows sermon metadata'

    # A second published Sunday wires up prev/next both ways.
    shutil.copytree(os.path.join(scratch, 'public', '2026-08-02'),
                    os.path.join(scratch, 'public', '2026-08-09'))
    status, body = req('/2026-08-02/')
    assert b'href="/2026-08-09/"' in body and b'rel="next"' in body, 'next link'
    status, body = req('/')          # front page is now Aug 9
    assert b'href="/2026-08-02/"' in body and b'rel="prev"' in body, 'prev link'
    status, body = req('/2026-08-02')
    assert status == 301 or b'Love Unleashed' in body   # bare date redirects

    # Warnings persist and surface: uploads.log audit line, no review panel
    # while everything is clean, then badge + panel once a guide has warnings.
    log_path = os.path.join(scratch, 'uploads.log')
    assert os.path.exists(log_path)
    entries = [json.loads(l) for l in open(log_path)]
    assert any(e.get('ok') and e.get('dateISO') == '2026-08-02' for e in entries)
    assert any(not e.get('ok') for e in entries), 'failed attempts audited too'
    assert any(e.get('ok') and e.get('file') == 'WG_async.pdf' for e in entries), \
        'queued conversions audited like sync ones'
    assert any(e.get('action') == 'login' and not e.get('ok') for e in entries), \
        'failed sign-ins audited'
    status, body = req('/admin', headers=COOKIE)
    assert b'Needs review' not in body

    # Upload history: per-file batch results survive leaving the page —
    # browsable at /admin/history (gated), filterable by outcome, with the
    # original filename when the uploader sends X-Filename (the admin JS does,
    # percent-encoded).
    status, body = req('/api/upload?sync=1', data=pdf,
                       headers={**COOKIE, 'Content-Type': 'application/pdf',
                                'X-Filename': 'WG%202026%2008%2002.pdf'})
    assert status == 200 and json.loads(body)['ok'], (status, body)
    status, body = req('/admin/history')
    assert status == 401 and b'Admin Sign-in' in body, 'history gated like admin'
    status, body = req('/admin/history', headers=COOKIE)
    assert status == 200 and b'Upload History' in body
    assert 'no-store' in (LAST['headers'].get('Cache-Control') or '')
    assert b'&amp;nbsp;' not in body, 'timestamp nbsp must not be double-escaped'
    assert b'WG 2026 08 02.pdf' in body, 'filename recorded from X-Filename'
    assert b'/2026-08-02/' in body and '✔ published'.encode() in body
    assert '✖ failed'.encode() in body, 'failed conversions are in the history too'
    page = body.decode()
    assert page.index('WG 2026 08 02.pdf') < page.index('✖ failed'), 'newest first'
    status, body = req('/admin/history?status=failed', headers=COOKIE)
    assert status == 200 and '✖ failed'.encode() in body
    assert b'WG 2026 08 02.pdf' not in body, 'outcome filter hides published uploads'
    status, body = req('/admin/history?status=ok&limit=1', headers=COOKIE)
    assert status == 200 and b'show all' in body, 'limit paginates with a show-all link'
    status, body = req('/admin', headers=COOKIE)
    assert b'Recent uploads' in body and b'/admin/history' in body, \
        'admin page shows the recent-history card'

    # Failed conversions keep their PDF server-side; /api/retry re-enqueues
    # them without a re-upload, carrying any pinned date forward.
    status, body = req('/api/upload?date=2030-05-05', data=b'%PDF-garbage',
                       headers={**COOKIE, 'Content-Type': 'application/pdf',
                                'X-Filename': 'broken.pdf'})
    assert status == 200 and json.loads(body)['queued'], (status, body)
    gid = json.loads(body)['id']
    for _ in range(240):
        status, body = req('/api/status?ids=' + gid, headers=COOKIE)
        js = json.loads(body)['jobs'][gid]
        if js['status'] in ('ok', 'warned', 'failed'):
            break
        time.sleep(0.5)
    assert js['status'] == 'failed', js
    failed_dir = os.path.join(scratch, 'queue', 'failed')
    kept = [f for f in os.listdir(failed_dir) if not f.endswith('.meta')]
    assert len(kept) == 1 and kept[0].endswith('__broken.pdf'), kept
    assert os.path.exists(os.path.join(failed_dir, kept[0] + '.meta')), \
        'pinned date survives the failure'
    status, body = req('/admin', headers=COOKIE)
    assert b'Failed conversions' in body and b'broken.pdf' in body \
        and b'retryFailed' in body, 'failed card with retry UI'
    status, body = req('/api/retry', data=json.dumps({'name': 'nope.pdf'}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 404, 'unknown failed upload -> not found'
    status, body = req('/api/retry', data=json.dumps({'name': kept[0]}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200 and json.loads(body)['ok'], body
    rid = json.loads(body)['id']
    for _ in range(240):
        status, body = req('/api/status?ids=' + rid, headers=COOKIE)
        js = json.loads(body)['jobs'][rid]
        if js['status'] in ('ok', 'warned', 'failed'):
            break
        time.sleep(0.5)
    assert js['status'] == 'failed', 'garbage still fails, via the retry path'
    kept2 = [f for f in os.listdir(failed_dir) if not f.endswith('.meta')]
    assert len(kept2) == 1 and kept2[0].endswith('__broken.pdf') and kept2 != kept, \
        'kept again under the new job id'
    entries = [json.loads(l) for l in open(log_path)]
    assert any(e.get('action') == 'retry' and e.get('dateOverride') == '2030-05-05'
               for e in entries), 'retry audited with the carried-over date'

    gj = os.path.join(scratch, 'public', '2026-08-09', 'guide.json')
    g = json.load(open(gj))
    g['warnings'] = ['page 7: content without a label kept as an untitled item']
    json.dump(g, open(gj, 'w'))
    status, body = req('/archive')
    assert b'needs review' in body, 'archive badge for warned guide'
    status, body = req('/admin', headers=COOKIE)
    assert b'Needs review' in body and b'untitled item' in body, 'review panel lists warnings'
    assert b'Published Sundays' in body and b'adminAction' in body, 'management panel present'
    assert b'id="reconvertall"' in body and b'2026-08-09' in body, \
        'bulk re-convert offered for flagged Sundays with stored sources'
    assert b'id="reviewall"' in body, 'bulk mark-reviewed offered too'

    # Mark reviewed: warnings move to reviewedWarnings, panel and badge clear.
    def action(name, date, token=TOKEN):
        return req(f'/api/{name}', data=json.dumps({'date': date}).encode(),
                   headers={'X-Upload-Token': token, 'Content-Type': 'application/json'})
    status, body = action('review', '2026-08-09', token='wrong')
    assert status == 401
    status, body = action('review', '2026-08-09')
    assert status == 200 and json.loads(body)['ok'], body
    g = json.load(open(gj))
    assert g['warnings'] == [] and 'untitled item' in g['reviewedWarnings'][0]
    status, body = req('/admin', headers=COOKIE)
    assert b'Needs review' not in body
    status, body = req('/archive')
    assert b'needs review' not in body

    # Re-convert rebuilds a Sunday from its stored source.pdf — server-side
    # parser passes without a re-upload — and lands in the history record.
    status, body = req('/admin', headers=COOKIE)
    assert b'Re-convert' in body, 'admin lists the re-convert action'
    with open(os.path.join(out_dir, 'index.html'), 'w') as fh:
        fh.write('stale')
    status, body = action('reconvert', '2026-08-02')
    assert status == 200 and json.loads(body)['ok'], body
    assert b'Love Unleashed' in open(os.path.join(out_dir, 'index.html'), 'rb').read(), \
        're-convert regenerated the page from the stored PDF'
    entries = [json.loads(l) for l in open(log_path)]
    assert any(e.get('reconvert') and e.get('ok') for e in entries), \
        're-conversion audited as a conversion'
    status, body = req('/admin/history', headers=COOKIE)
    assert b'source.pdf' in body, 're-conversions appear in upload history'
    os.remove(os.path.join(scratch, 'public', '2026-08-09', 'source.pdf'))
    status, body = action('reconvert', '2026-08-09')
    assert status == 404, 'no stored source -> not found, with guidance'

    # Form editor: gated like the rest of admin; page loads with embedded
    # data; save sanitizes server-side, preserves protected fields, re-renders.
    status, body = req('/admin/edit/2026-08-09')
    assert status == 401 and b'Admin Sign-in' in body, 'editor gated too'
    status, body = req('/admin/edit/2026-08-09', headers=COOKIE)
    assert status == 200 and b'guide-data' in body and b'Love Unleashed' in body
    status, body = req('/admin/edit/1999-01-01', headers=COOKIE)
    assert status == 404

    g = json.load(open(gj))
    g['welcome']['body'][0]['text'] = 'Edited welcome <b>kept</b> <script>alert(1)</script>'
    g['series']['title'] = 'Edited <i>Title</i>'          # plain field: tags stripped
    g['dateISO'] = '1999-01-01'                            # protected: ignored
    g['warnings'] = ['forged']                             # protected: ignored
    g['bogusKey'] = {'x': 1}                               # unknown: dropped
    status, body = req('/api/save',
                       data=json.dumps({'date': '2026-08-09', 'guide': g}).encode(),
                       headers={'X-Upload-Token': TOKEN, 'Content-Type': 'application/json'})
    assert status == 200 and json.loads(body)['ok'], body
    saved = json.load(open(gj))
    assert '<b>kept</b>' in saved['welcome']['body'][0]['text']
    assert '<script>' not in saved['welcome']['body'][0]['text'], 'script neutralized'
    assert saved['series']['title'] == 'Edited Title', 'plain field stripped of tags'
    assert saved['dateISO'] == '2026-08-02', 'dateISO from file (fixture is a copy), forge ignored'
    assert saved['warnings'] == [], 'warnings come from the file, not the form'
    assert 'bogusKey' not in saved
    page = open(os.path.join(scratch, 'public', '2026-08-09', 'index.html')).read()
    assert 'Edited welcome <b>kept</b>' in page, 'save re-rendered the page'
    assert 'alert(1)' not in page or '<script>alert' not in page

    # Re-render rebuilds index.html from guide.json.
    idx = os.path.join(scratch, 'public', '2026-08-09', 'index.html')
    before = os.path.getmtime(idx)
    status, body = action('rerender', '2026-08-09')
    assert status == 200 and json.loads(body)['ok'], body
    assert os.path.getmtime(idx) >= before
    assert b'Love Unleashed' in open(idx, 'rb').read()

    # Unpublish sets the folder aside (dot-prefixed, unserved) and the front
    # page falls back to the previous newest Sunday.
    status, body = action('unpublish', '2026-08-09')
    assert status == 200 and json.loads(body)['ok'], body
    aside = [f for f in os.listdir(os.path.join(scratch, 'public'))
             if f.startswith('.unpublished-2026-08-09')]
    assert aside, 'folder renamed aside, not deleted'
    status, body = req('/archive')
    assert b'/2026-08-09/' not in body
    status, body = req('/')
    assert b'2026-08-02' in body or b'August 2, 2026' in body
    status, body = req(f'/{aside[0]}/index.html')
    assert status == 404, 'unpublished folder is not served'
    status, body = action('unpublish', '2026-08-09')
    assert status == 404, 'acting on a gone date reports not found'

    # Sign out clears the cookie and the browser lands back on the login gate.
    status, body = req('/admin/logout', headers=COOKIE)
    assert status == 303 and LAST['headers'].get('Location') == '/'
    assert 'Max-Age=0' in (LAST['headers'].get('Set-Cookie') or '')
    status, body = req('/admin', headers={'Cookie': 'wg_token='})
    assert status == 401, 'cleared cookie no longer signs in'

    # Sermon search: title, scripture, and no-hit cases.
    status, body = req('/search?q=Unleashed')
    assert status == 200 and b'/2026-08-02/' in body and b'<mark>' in body
    status, body = req('/search?q=Matthew')
    assert status == 200 and b'/2026-08-02/' in body
    status, body = req('/search?q=zzzqqqxyzzy')
    assert status == 200 and b'No results' in body

    print('all app tests passed')
finally:
    proc.terminate()
    proc.wait(timeout=5)
    shutil.rmtree(scratch, ignore_errors=True)

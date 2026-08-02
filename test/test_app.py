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
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
SAMPLE = os.path.join(ROOT, 'samples', 'WG_2026_08_02.pdf')
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


def req(path, data=None, headers=None, method=None):
    r = urllib.request.Request(BASE + path, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


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

    status, body = req('/admin')
    assert status == 200 and b'Publish Worship Guides' in body and b'multiple' in body

    with open(SAMPLE, 'rb') as fh:
        pdf = fh.read()

    # Fails closed / bad token.
    status, body = req('/api/upload', data=pdf,
                       headers={'X-Upload-Token': 'wrong', 'Content-Type': 'application/pdf'})
    assert status == 401, (status, body)

    status, body = req('/api/upload', data=b'%PDF-not really',
                       headers={'X-Upload-Token': TOKEN, 'Content-Type': 'application/pdf'})
    assert status == 422, (status, body)

    status, body = req('/api/upload', data=b'hello',
                       headers={'X-Upload-Token': TOKEN, 'Content-Type': 'application/pdf'})
    assert status == 400, (status, body)

    # The real upload converts and publishes.
    status, body = req('/api/upload', data=pdf,
                       headers={'X-Upload-Token': TOKEN, 'Content-Type': 'application/pdf'})
    assert status == 200, (status, body)
    data = json.loads(body)
    assert data['ok'] and data['dateISO'] == '2026-08-02' and data['warnings'] == [], data
    assert data['replaced'] is False, data

    # Re-uploading the same Sunday overwrites in place and says so.
    status, body = req('/api/upload', data=pdf,
                       headers={'X-Upload-Token': TOKEN, 'Content-Type': 'application/pdf'})
    assert status == 200, (status, body)
    assert json.loads(body)['replaced'] is True

    out_dir = os.path.join(scratch, 'public', '2026-08-02')
    for f in ('index.html', 'guide.json', 'cover.jpg'):
        assert os.path.exists(os.path.join(out_dir, f)), f

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
    status, body = req('/admin')
    assert b'Needs review' not in body

    gj = os.path.join(scratch, 'public', '2026-08-09', 'guide.json')
    g = json.load(open(gj))
    g['warnings'] = ['page 7: content without a label kept as an untitled item']
    json.dump(g, open(gj, 'w'))
    status, body = req('/archive')
    assert b'needs review' in body, 'archive badge for warned guide'
    status, body = req('/admin')
    assert b'Needs review' in body and b'untitled item' in body, 'review panel lists warnings'
    assert b'Published Sundays' in body and b'adminAction' in body, 'management panel present'

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
    status, body = req('/admin')
    assert b'Needs review' not in body
    status, body = req('/archive')
    assert b'needs review' not in body

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

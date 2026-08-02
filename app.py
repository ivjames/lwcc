#!/usr/bin/env python3
"""lwcc.lab980.com — worship guide site + converter app.

Serves the published worship guides from public/ (one directory per Sunday,
newest at /) and converts newly uploaded worship-guide PDFs in place:

    GET  /            current (newest) Sunday's guide
    GET  /YYYY-MM-DD/ any published Sunday
    GET  /archive     list of every published Sunday
    GET  /admin       upload page (the POST is what's protected, not the page)
    POST /api/upload  raw PDF body -> convert -> publish; X-Upload-Token header
                      must match UPLOAD_TOKEN from .env (fails closed if unset)
    GET  /healthz     liveness for the platform health-check sweep

Stdlib only, runs under pm2 behind the site's nginx vhost per lab980
conventions. Uploads run the wgconvert pipeline synchronously (a few seconds);
warnings from the parser are returned to the uploader so odd content is seen,
not silently dropped.
"""
import argparse
import hmac
import http.server
import json
import os
import re
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, 'public')
DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MAX_UPLOAD = 40 * 1024 * 1024

sys.path.insert(0, ROOT)
from wgconvert import extract, parse, render  # noqa: E402


def load_env():
    """KEY=VALUE pairs from .env in the app dir, per platform convention."""
    env = {}
    path = os.path.join(ROOT, '.env')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    return env


ENV = load_env()


def published_dates():
    if not os.path.isdir(PUBLIC):
        return []
    return sorted(
        (d for d in os.listdir(PUBLIC)
         if DATE_DIR_RE.match(d) and os.path.exists(os.path.join(PUBLIC, d, 'index.html'))),
        reverse=True)


def convert_pdf(pdf_path):
    """Run the wgconvert pipeline and publish into public/<dateISO>/."""
    with open(os.path.join(ROOT, 'config', 'church.json'), encoding='utf-8') as fh:
        church = json.load(fh)
    work_dir = tempfile.mkdtemp(prefix='wg-upload-')
    try:
        extracted = extract(pdf_path, work_dir)
        guide = parse(extracted)
        if not guide['dateISO']:
            raise ValueError('no service date found in the PDF — convert it '
                             'manually with bin/wg-convert and hand-edit guide.json')
        out_dir = os.path.join(PUBLIC, guide['dateISO'])
        os.makedirs(out_dir, exist_ok=True)
        cover_dest = None
        if extracted.cover_path:
            cover_dest = os.path.join(out_dir, 'cover' + os.path.splitext(extracted.cover_path)[1])
            shutil.copyfile(extracted.cover_path, cover_dest)
        with open(os.path.join(out_dir, 'guide.json'), 'w', encoding='utf-8') as fh:
            json.dump(guide, fh, indent=2, ensure_ascii=False)
            fh.write('\n')
        html = render(guide, church,
                      banner_path=os.path.join(ROOT, 'assets', 'banner.png'),
                      cover_path=cover_dest)
        with open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8') as fh:
            fh.write(html)
        return guide
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


PAGE_STYLE = """
  body{font-family:Georgia,'Times New Roman',serif;background:#fbfaf5;color:#26241d;
    max-width:680px;margin:0 auto;padding:40px 20px;line-height:1.6}
  h1{font-family:Arial,Helvetica,sans-serif;font-size:1.2rem;letter-spacing:2px;
    text-transform:uppercase;color:#054253;border-bottom:3px solid #0a5a6e;
    display:inline-block;padding-bottom:4px}
  a{color:#a20816}
  .card{background:#fff;border:1px solid #d8d6c7;border-left:4px solid #0a5a6e;
    border-radius:10px;padding:18px 20px;margin:14px 0}
  input,button{font:inherit;padding:10px 14px;border-radius:8px;border:1px solid #d8d6c7}
  button{background:#054253;color:#fff;border:none;cursor:pointer}
  button:disabled{opacity:.5;cursor:default}
  .warn{color:#8a6410}.err{color:#a20816}.ok{color:#1f7a44}
  code{background:#f1efe6;padding:2px 6px;border-radius:4px;font-size:.85em}
"""


def archive_page():
    dates = published_dates()
    items = '\n'.join(
        f'    <li><a href="/{d}/">{d}</a></li>' for d in dates) or '    <li>Nothing published yet.</li>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Worship Guide Archive</title><style>{PAGE_STYLE}</style></head>
<body>
<h1>Worship Guide Archive</h1>
<div class="card"><ul>
{items}
</ul></div>
<p><a href="/">Current guide</a></p>
</body></html>
"""


ADMIN_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Publish a Worship Guide</title><style>{PAGE_STYLE}</style></head>
<body>
<h1>Publish a Worship Guide</h1>
<div class="card">
  <p>Drop in the week's worship-guide PDF. It is converted and published
  immediately; the newest Sunday becomes the front page.</p>
  <p><label>Upload token<br><input type="password" id="token" size="28"></label></p>
  <p><input type="file" id="pdf" accept="application/pdf,.pdf"></p>
  <p><button id="go">Convert &amp; publish</button></p>
  <div id="result"></div>
</div>
<div class="card">
  <p>Batch (backlog) from a terminal:</p>
  <p><code>curl -X POST --data-binary @WG.pdf -H "X-Upload-Token: $TOKEN" -H "Content-Type: application/pdf" https://lwcc.lab980.com/api/upload</code></p>
</div>
<p><a href="/">Current guide</a> · <a href="/archive">Archive</a></p>
<script>
const $ = id => document.getElementById(id);
$('token').value = localStorage.getItem('wgToken') || '';
$('go').addEventListener('click', async () => {{
  const file = $('pdf').files[0], token = $('token').value.trim();
  const out = $('result');
  if (!file) {{ out.innerHTML = '<p class="err">Choose a PDF first.</p>'; return; }}
  localStorage.setItem('wgToken', token);
  $('go').disabled = true;
  out.innerHTML = '<p>Converting…</p>';
  try {{
    const res = await fetch('/api/upload', {{
      method: 'POST',
      headers: {{'X-Upload-Token': token, 'Content-Type': 'application/pdf'}},
      body: file,
    }});
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || res.statusText);
    const warns = (data.warnings || []).map(w => `<li class="warn">⚠ ${{w}}</li>`).join('');
    out.innerHTML = `<p class="ok">Published <a href="${{data.url}}">${{data.date}}</a>.</p>` +
      (warns ? `<p>Check these before sharing:</p><ul>${{warns}}</ul>` : '');
  }} catch (e) {{
    out.innerHTML = `<p class="err">${{e.message}}</p>`;
  }} finally {{
    $('go').disabled = false;
  }}
}});
</script>
</body></html>
"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC, **kwargs)

    # -- helpers ------------------------------------------------------------

    def send_page(self, body, status=200, ctype='text/html; charset=utf-8'):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_page(json.dumps(obj), status=status, ctype='application/json')

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/healthz':
            self.send_page('ok\n', ctype='text/plain')
            return
        if path == '/archive':
            self.send_page(archive_page())
            return
        if path == '/admin':
            self.send_page(ADMIN_PAGE)
            return
        if path == '/':
            dates = published_dates()
            if dates:
                with open(os.path.join(PUBLIC, dates[0], 'index.html'), 'rb') as fh:
                    self.send_page(fh.read())
                return
        super().do_GET()

    def do_POST(self):
        # One-shot handling: read the (bounded) body before any error reply,
        # otherwise the client hits a broken pipe mid-upload and never sees it.
        self.close_connection = True
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if length < 0 or length > MAX_UPLOAD:
            self.send_json({'ok': False, 'error': f'body must be 1..{MAX_UPLOAD} bytes'},
                           status=413)
            return
        body = self.rfile.read(length)
        if self.path.split('?', 1)[0] != '/api/upload':
            self.send_json({'ok': False, 'error': 'not found'}, status=404)
            return
        expected = ENV.get('UPLOAD_TOKEN', '')
        if not expected:
            self.send_json({'ok': False, 'error':
                            'uploads disabled: set UPLOAD_TOKEN in .env and restart'},
                           status=503)
            return
        got = self.headers.get('X-Upload-Token', '')
        if not hmac.compare_digest(got, expected):
            self.send_json({'ok': False, 'error': 'bad upload token'}, status=401)
            return
        if not body.startswith(b'%PDF-'):
            self.send_json({'ok': False, 'error': 'not a PDF'}, status=400)
            return
        tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        try:
            tmp.write(body)
            tmp.close()
            guide = convert_pdf(tmp.name)
            self.send_json({
                'ok': True,
                'date': guide['date'],
                'dateISO': guide['dateISO'],
                'url': f"/{guide['dateISO']}/",
                'warnings': guide['warnings'],
            })
        except Exception as e:                        # surface, don't 500-blank
            traceback.print_exc()
            self.send_json({'ok': False, 'error': str(e)}, status=422)
        finally:
            os.unlink(tmp.name)

    # -- policy -------------------------------------------------------------

    def end_headers(self):
        # Guides are replaced in place when re-rendered; keep caching short.
        self.send_header('Cache-Control', 'public, max-age=300')
        super().end_headers()

    def list_directory(self, path):  # no directory listings
        self.send_error(404, 'Not Found')
        return None

    def log_message(self, fmt, *args):
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8061)))
    ap.add_argument('--host', default='127.0.0.1')
    args = ap.parse_args()
    os.makedirs(PUBLIC, exist_ok=True)
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    token = 'set' if ENV.get('UPLOAD_TOKEN') else 'NOT SET (uploads disabled)'
    print(f'lwcc serving {PUBLIC} on http://{args.host}:{args.port} — upload token {token}',
          flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()

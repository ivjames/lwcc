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
import datetime
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


def audit_log(entry):
    """Append one JSON line per upload to uploads.log — the durable record of
    every conversion, including failures that publish nothing."""
    entry = {'at': datetime.datetime.now().isoformat(timespec='seconds'), **entry}
    try:
        with open(os.path.join(ROOT, 'uploads.log'), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        pass


def missing_deps():
    """Converter binaries the app shells out to; empty list = all present."""
    return [b for b in ('pdftohtml', 'pdfimages', 'pdftoppm', 'tesseract')
            if shutil.which(b) is None]


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


MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
               'August', 'September', 'October', 'November', 'December']


def date_label(d):
    """'2026-07-26' -> 'July 26, 2026'"""
    dt = datetime.date.fromisoformat(d)
    return f'{MONTH_NAMES[dt.month - 1]} {dt.day}, {dt.year}'


_meta_cache = {}


def guide_meta(d):
    """Sermon metadata + searchable text for a published Sunday, from its
    guide.json (cached by mtime). None when the JSON is absent/unreadable."""
    path = os.path.join(PUBLIC, d, 'guide.json')
    if not os.path.exists(path):
        return None
    mtime = os.path.getmtime(path)
    hit = _meta_cache.get(d)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding='utf-8') as fh:
            g = json.load(fh)
    except (OSError, ValueError):
        return None

    def strip(s):
        return re.sub(r'<[^>]+>', '', s or '')

    series = g.get('series') or {}
    refs, parts = [], []
    for o in g.get('order') or []:
        if o.get('kind') != 'item':
            continue
        if o.get('title'):
            parts.append(o['title'])
        if o.get('who'):
            parts.append(o['who'])
        for b in o.get('body') or []:
            if b.get('type') == 'ref':
                refs.append(b['text'])
            parts.append(strip(b.get('text')))
    if g.get('welcome'):
        parts += [strip(b.get('text')) for b in g['welcome'].get('body') or []]
    for a in g.get('announcements') or []:
        parts += [a.get('heading') or '', strip(a.get('text'))]
    for pr in g.get('prayerRequests') or []:
        parts += [pr.get('name') or '', strip(pr.get('text'))]
    for ev in g.get('specialEvents') or []:
        parts += [ev.get('heading') or ''] + [strip(p) for p in ev.get('paragraphs') or []]
    blob = ' '.join(p for p in ([series.get('title'), series.get('by'),
                                 g.get('season')] + refs + parts) if p)
    meta = {
        'title': series.get('title'),
        'by': series.get('by'),
        'season': g.get('season'),
        'refs': refs,
        'warnings': g.get('warnings') or [],
        'blob': re.sub(r'\s+', ' ', blob),
    }
    _meta_cache[d] = (mtime, meta)
    return meta


def weeknav_html(d):
    """Prev/next-Sunday strip injected into served guide pages. Computed at
    request time so links stay correct as the backlog fills in."""
    dates = published_dates()          # newest first
    older = newer = None
    if d in dates:
        i = dates.index(d)
        newer = dates[i - 1] if i > 0 else None
        older = dates[i + 1] if i + 1 < len(dates) else None
    parts = []
    if older:
        parts.append(f'<a rel="prev" href="/{older}/">&larr; {date_label(older)}</a>')
    parts.append('<a href="/archive">All Sundays</a>')
    parts.append('<a href="/search">Search</a>')
    if newer:
        parts.append(f'<a rel="next" href="/{newer}/">{date_label(newer)} &rarr;</a>')
    return ('<div class="weeknav" style="font-family:Arial,Helvetica,sans-serif;'
            'font-size:.9rem;display:flex;gap:8px 22px;justify-content:center;'
            'flex-wrap:wrap;padding:10px 20px;background:#f4f2ea;'
            'border-bottom:1px solid #d8d6c7">' + '\n  '.join(parts) + '</div>')


def guide_with_nav(d):
    """The published page with the week-nav strip under the sticky section nav
    and again above the footer."""
    with open(os.path.join(PUBLIC, d, 'index.html'), encoding='utf-8') as fh:
        html = fh.read()
    nav = weeknav_html(d)
    if '</nav>' in html:
        html = html.replace('</nav>', '</nav>\n' + nav, 1)
    else:
        html = html.replace('<body>', '<body>\n' + nav, 1)
    html = html.replace('<footer>', nav + '\n<footer>', 1)
    return html


def convert_pdf(pdf_path):
    """Run the wgconvert pipeline and publish into public/<dateISO>/."""
    church = load_church()
    work_dir = tempfile.mkdtemp(prefix='wg-upload-')
    try:
        extracted = extract(pdf_path, work_dir)
        guide = parse(extracted)
        if not guide['dateISO']:
            raise ValueError('no service date found in the PDF — convert it '
                             'manually with bin/wg-convert and hand-edit guide.json')
        out_dir = os.path.join(PUBLIC, guide['dateISO'])
        replaced = os.path.exists(os.path.join(out_dir, 'index.html'))
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
        return guide, replaced
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def load_church():
    with open(os.path.join(ROOT, 'config', 'church.json'), encoding='utf-8') as fh:
        return json.load(fh)


def write_guide_json(path, g):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(g, fh, indent=2, ensure_ascii=False)
        fh.write('\n')


def mark_reviewed(d):
    """Move a Sunday's parser warnings to reviewedWarnings — the operator has
    checked the page and accepts it. Clears the review panel and badge while
    keeping the history in guide.json."""
    path = os.path.join(PUBLIC, d, 'guide.json')
    with open(path, encoding='utf-8') as fh:
        g = json.load(fh)
    if g.get('warnings'):
        g['reviewedWarnings'] = (g.get('reviewedWarnings') or []) + g['warnings']
        g['warnings'] = []
        write_guide_json(path, g)


def rerender_date(d):
    """Rebuild index.html from the stored guide.json (after hand-edits)."""
    out_dir = os.path.join(PUBLIC, d)
    with open(os.path.join(out_dir, 'guide.json'), encoding='utf-8') as fh:
        g = json.load(fh)
    cover = next((f for f in os.listdir(out_dir)
                  if re.fullmatch(r'cover\.(jpe?g|png|webp)', f)), None)
    html = render(g, load_church(),
                  banner_path=os.path.join(ROOT, 'assets', 'banner.png'),
                  cover_path=os.path.join(out_dir, cover) if cover else None)
    with open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8') as fh:
        fh.write(html)


def unpublish_date(d):
    """Take a Sunday off the site without destroying it: the folder is renamed
    aside (restore by renaming it back and re-uploading is never needed)."""
    ts = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    os.rename(os.path.join(PUBLIC, d),
              os.path.join(PUBLIC, f'.unpublished-{d}-{ts}'))


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
    rows = []
    for d in dates:
        meta = guide_meta(d)
        line = f'<a href="/{d}/">{date_label(d)}</a>'
        if meta and meta['title']:
            line += f" — <b>{meta['title']}</b>"
            if meta['by']:
                line += f" <span style=\"color:#54574a\">({meta['by']})</span>"
        if meta and meta['refs']:
            line += ('<br><span style="color:#54574a;font-size:.9em">'
                     + ' · '.join(meta['refs']) + '</span>')
        if meta and meta['warnings']:
            n = len(meta['warnings'])
            line += (f'<br><span class="warn" style="font-size:.9em">⚠ needs review '
                     f'({n} parser warning{"s" if n > 1 else ""})</span>')
        rows.append(f'    <li style="margin:8px 0">{line}</li>')
    items = '\n'.join(rows) or '    <li>Nothing published yet.</li>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Worship Guide Archive</title><style>{PAGE_STYLE}</style></head>
<body>
<h1>Worship Guide Archive</h1>
<form action="/search" style="margin:14px 0"><input type="search" name="q"
  placeholder="Search sermons…" size="28"> <button>Search</button></form>
<div class="card"><ul style="list-style:none;padding-left:0">
{items}
</ul></div>
<p><a href="/">Current guide</a></p>
</body></html>
"""


def search_page(query):
    q = (query or '').strip()
    words = [w for w in q.lower().split() if w]
    results = []
    if words:
        for d in published_dates():
            meta = guide_meta(d)
            hay = (meta['blob'].lower() + ' ' + d) if meta else d
            if not all(w in hay for w in words):
                continue
            title = (meta or {}).get('title')
            by = (meta or {}).get('by')
            refs = (meta or {}).get('refs') or []
            snippet = ''
            if meta:
                low = meta['blob'].lower()
                # center the snippet on the most meaningful (longest) word
                pos = -1
                for w in sorted(words, key=len, reverse=True):
                    pos = low.find(w)
                    if pos >= 0:
                        break
                if pos >= 0:
                    start = max(0, pos - 80)
                    end = min(len(meta['blob']), pos + 160)
                    snippet = ('…' if start else '') + meta['blob'][start:end] + \
                              ('…' if end < len(meta['blob']) else '')
                    snippet = (snippet.replace('&', '&amp;').replace('<', '&lt;')
                               .replace('>', '&gt;'))
                    for w in words:
                        if len(w) < 3:      # don't blanket-highlight "a"/"of"/"an"
                            continue
                        snippet = re.sub(f'({re.escape(w)})', r'<mark>\1</mark>',
                                         snippet, flags=re.I)
            head = f'<a href="/{d}/">{date_label(d)}</a>'
            if title:
                head += f' — <b>{title}</b>'
            if by:
                head += f' <span style="color:#54574a">({by})</span>'
            if refs:
                head += ('<br><span style="color:#54574a;font-size:.9em">'
                         + ' · '.join(refs) + '</span>')
            body = f'<br><span style="font-size:.92em">{snippet}</span>' if snippet else ''
            results.append(f'    <li style="margin:12px 0">{head}{body}</li>')
    if q and not results:
        listing = '<p>No results for <b>' + (q.replace('&', '&amp;')
                  .replace('<', '&lt;').replace('>', '&gt;')) + '</b>.</p>'
    elif results:
        joined = '\n'.join(results)
        listing = f'<ul style="list-style:none;padding-left:0">\n{joined}\n</ul>'
    else:
        listing = '<p>Search sermon titles, scripture, speakers, or any text from the guides.</p>'
    q_attr = q.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sermon Search</title><style>{PAGE_STYLE}</style></head>
<body>
<h1>Sermon Search</h1>
<form action="/search" style="margin:14px 0"><input type="search" name="q"
  value="{q_attr}" placeholder="Search sermons…" size="28" autofocus>
  <button>Search</button></form>
<div class="card">
{listing}
</div>
<p><a href="/">Current guide</a> · <a href="/archive">Archive</a></p>
</body></html>
"""


ADMIN_PAGE = ("""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Publish Worship Guides</title><style>__STYLE__
  #drop{border:2px dashed #76a2bf;border-radius:10px;padding:26px;text-align:center;
    color:#54574a;margin:10px 0}
  #drop.over{background:#eaf1f5;border-color:#054253}
  table{width:100%;border-collapse:collapse;font-size:.95em}
  td,th{padding:6px 8px;border-bottom:1px solid #d8d6c7;text-align:left;vertical-align:top}
  .st{white-space:nowrap}
  ul.warns{margin:4px 0 0;padding-left:18px;color:#8a6410}
  #summary{font-weight:700;margin-top:10px}
  button.mini{padding:4px 12px;font-size:.82em;margin-left:8px;background:#3f6b82}
</style></head>
<body>
<h1>Publish Worship Guides</h1>
<div class="card">
  <p>Add one PDF or a whole backlog. Files are converted and published one at
  a time — the newest Sunday always ends up as the front page, and every
  Sunday gets its permanent <code>/YYYY-MM-DD/</code> URL.</p>
  <p><label>Upload token<br><input type="password" id="token" size="28"></label></p>
  <div id="drop">Drag PDFs here, or
    <input type="file" id="pdf" accept="application/pdf,.pdf" multiple></div>
  <p><button id="go" disabled>Convert &amp; publish</button>
     <button id="clear" disabled>Clear list</button></p>
  <table id="queue" hidden><thead>
    <tr><th>File</th><th class="st">Status</th><th>Result</th></tr>
  </thead><tbody></tbody></table>
  <div id="summary"></div>
</div>
__REVIEW__
<p><a href="/">Current guide</a> · <a href="/archive">Archive</a></p>
<script>
const $ = id => document.getElementById(id);
let queue = [];   // {file, status, data, error}
let running = false;

$('token').value = localStorage.getItem('wgToken') || '';

function addFiles(list) {
  for (const f of list) {
    if (!/\.pdf$/i.test(f.name)) continue;
    if (queue.some(q => q.file.name === f.name && q.status !== 'failed')) continue;
    queue.push({file: f, status: 'queued', data: null, error: null});
  }
  queue.sort((a, b) => a.file.name.localeCompare(b.file.name));
  renderQueue();
}

function statusCell(q) {
  return {queued: '·', converting: '⏳ converting', ok: '<span class="ok">✔ published</span>',
          warned: '<span class="warn">⚠ published</span>',
          failed: '<span class="err">✖ failed</span>'}[q.status];
}

function resultCell(q) {
  if (q.status === 'failed') return '<span class="err">' + q.error + '</span>';
  if (!q.data) return '';
  let html = '<a href="' + q.data.url + '">' + q.data.date + '</a>';
  if (q.data.replaced) html += ' <span class="warn">(replaced existing)</span>';
  if (q.data.warnings.length) {
    html += '<ul class="warns">' +
      q.data.warnings.map(w => '<li>' + w + '</li>').join('') + '</ul>';
  }
  return html;
}

function renderQueue() {
  const tb = $('queue').querySelector('tbody');
  tb.innerHTML = queue.map(q =>
    '<tr><td>' + q.file.name + '</td><td class="st">' + statusCell(q) +
    '</td><td>' + resultCell(q) + '</td></tr>').join('');
  $('queue').hidden = !queue.length;
  $('go').disabled = running || !queue.some(q => q.status === 'queued');
  $('clear').disabled = running || !queue.length;
  const done = queue.filter(q => ['ok', 'warned', 'failed'].includes(q.status));
  if (done.length && !running) {
    const ok = done.filter(q => q.status === 'ok').length;
    const warned = done.filter(q => q.status === 'warned').length;
    const failed = done.filter(q => q.status === 'failed').length;
    $('summary').textContent = ok + ' clean, ' + warned + ' with warnings, ' + failed + ' failed.';
  } else {
    $('summary').textContent = '';
  }
}

$('pdf').addEventListener('change', e => { addFiles(e.target.files); e.target.value = ''; });
$('drop').addEventListener('dragover', e => { e.preventDefault(); $('drop').classList.add('over'); });
$('drop').addEventListener('dragleave', () => $('drop').classList.remove('over'));
$('drop').addEventListener('drop', e => {
  e.preventDefault();
  $('drop').classList.remove('over');
  addFiles(e.dataTransfer.files);
});
$('clear').addEventListener('click', () => { queue = []; renderQueue(); });

async function adminAction(action, date) {
  const token = $('token').value.trim();
  if (!token) { alert('Enter the upload token first.'); return; }
  if (action === 'unpublish' && !confirm('Unpublish ' + date + '? The folder is set aside, not deleted.')) return;
  localStorage.setItem('wgToken', token);
  const res = await fetch('/api/' + action, {
    method: 'POST',
    headers: {'X-Upload-Token': token, 'Content-Type': 'application/json'},
    body: JSON.stringify({date}),
  });
  const data = await res.json().catch(() => ({ok: false, error: res.statusText}));
  if (!data.ok) { alert(data.error || 'failed'); return; }
  location.reload();
}

$('go').addEventListener('click', async () => {
  const token = $('token').value.trim();
  localStorage.setItem('wgToken', token);
  running = true;
  renderQueue();
  for (const q of queue) {
    if (q.status !== 'queued') continue;
    q.status = 'converting';
    renderQueue();
    try {
      const res = await fetch('/api/upload', {
        method: 'POST',
        headers: {'X-Upload-Token': token, 'Content-Type': 'application/pdf'},
        body: q.file,
      });
      const text = await res.text();
      let data;
      try { data = JSON.parse(text); }
      catch {
        throw new Error('HTTP ' + res.status + ' ' + res.statusText +
          ' — reply was not from the app (nginx limit? needs client_max_body_size/'+
          'proxy_read_timeout in the vhost — see README): ' +
          text.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 100));
      }
      if (!data.ok) throw new Error(data.error || res.statusText);
      q.data = data;
      q.status = data.warnings.length ? 'warned' : 'ok';
    } catch (e) {
      q.status = 'failed';
      q.error = e.message;
    }
    renderQueue();
  }
  running = false;
  renderQueue();
});
</script>
</body></html>
""").replace('__STYLE__', PAGE_STYLE)


def manage_html():
    """Server-rendered management panel for /admin: Sundays needing review
    (with their warnings and a Mark-reviewed action) plus a compact list of
    everything published with re-render/unpublish actions."""
    dates = published_dates()
    if not dates:
        return ''
    metas = {d: guide_meta(d) for d in dates}
    out = []

    flagged = [(d, m) for d, m in metas.items() if m and m['warnings']]
    if flagged:
        items = []
        for d, m in sorted(flagged, reverse=True):
            warns = ''.join(f'<li class="warn">{w}</li>' for w in m['warnings'])
            items.append(
                f'<li style="margin:10px 0"><a href="/{d}/">{date_label(d)}</a> '
                f'<button class="mini" onclick="adminAction(\'review\', \'{d}\')">'
                f'Mark reviewed</button>'
                f'<ul style="margin:4px 0 0;padding-left:18px">{warns}</ul></li>')
        out.append('<div class="card"><p><b>Needs review</b> — published with '
                   'parser warnings. Check the page; if it reads right, mark it '
                   'reviewed (warnings are kept in guide.json under '
                   'reviewedWarnings). Or fix and re-upload the PDF.</p>'
                   '<ul style="list-style:none;padding-left:0">'
                   + ''.join(items) + '</ul></div>')

    rows = []
    for d in dates:
        m = metas.get(d)
        title = f' — {m["title"]}' if m and m['title'] else ''
        rows.append(
            f'<li style="margin:7px 0"><a href="/{d}/">{d}</a>{title} '
            f'<button class="mini" onclick="adminAction(\'rerender\', \'{d}\')">'
            f'Re-render</button>'
            f'<button class="mini" onclick="adminAction(\'unpublish\', \'{d}\')">'
            f'Unpublish</button></li>')
    out.append('<div class="card"><p><b>Published Sundays</b> — re-render '
               'rebuilds the page from its guide.json (after hand-edits); '
               'unpublish sets the folder aside without deleting it.</p>'
               '<ul style="list-style:none;padding-left:0">'
               + ''.join(rows) + '</ul></div>')
    return '\n'.join(out)


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
        path, _, query = self.path.partition('?')
        if path == '/healthz':
            missing = missing_deps()
            body = 'ok\n' if not missing else \
                'degraded: missing ' + ', '.join(missing) + \
                ' — apt-get install -y poppler-utils tesseract-ocr\n'
            self.send_page(body, ctype='text/plain')
            return
        if path == '/archive':
            self.send_page(archive_page())
            return
        if path == '/search':
            import urllib.parse
            q = urllib.parse.parse_qs(query).get('q', [''])[0]
            self.send_page(search_page(q))
            return
        if path == '/admin':
            self.send_page(ADMIN_PAGE.replace('__REVIEW__', manage_html()))
            return
        if path == '/':
            dates = published_dates()
            if dates:
                self.send_page(guide_with_nav(dates[0]))
                return
        if '/.' in path:
            self.send_error(404, 'Not Found')
            return
        m = re.fullmatch(r'/(\d{4}-\d{2}-\d{2})', path)
        if m:
            self.send_response(301)
            self.send_header('Location', path + '/')
            self.end_headers()
            return
        m = re.fullmatch(r'/(\d{4}-\d{2}-\d{2})/(?:index\.html)?', path)
        if m and os.path.exists(os.path.join(PUBLIC, m.group(1), 'index.html')):
            self.send_page(guide_with_nav(m.group(1)))
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
        path = self.path.split('?', 1)[0]
        if path not in ('/api/upload', '/api/review', '/api/rerender', '/api/unpublish'):
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
        if path != '/api/upload':
            self.handle_action(path.rsplit('/', 1)[1], body)
            return
        if not body.startswith(b'%PDF-'):
            self.send_json({'ok': False, 'error': 'not a PDF'}, status=400)
            return
        tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        try:
            tmp.write(body)
            tmp.close()
            guide, replaced = convert_pdf(tmp.name)
            audit_log({'ok': True, 'dateISO': guide['dateISO'],
                       'replaced': replaced, 'warnings': guide['warnings']})
            self.send_json({
                'ok': True,
                'date': guide['date'],
                'dateISO': guide['dateISO'],
                'url': f"/{guide['dateISO']}/",
                'replaced': replaced,
                'warnings': guide['warnings'],
            })
        except Exception as e:                        # surface, don't 500-blank
            traceback.print_exc()
            audit_log({'ok': False, 'error': str(e)})
            self.send_json({'ok': False, 'error': str(e)}, status=422)
        finally:
            os.unlink(tmp.name)

    def handle_action(self, action, body):
        try:
            data = json.loads(body or b'{}')
            date = data.get('date', '')
        except ValueError:
            self.send_json({'ok': False, 'error': 'invalid JSON body'}, status=400)
            return
        if not DATE_DIR_RE.match(date) or \
                not os.path.exists(os.path.join(PUBLIC, date, 'guide.json')):
            self.send_json({'ok': False, 'error': f'no published guide for {date!r}'},
                           status=404)
            return
        try:
            if action == 'review':
                mark_reviewed(date)
            elif action == 'rerender':
                rerender_date(date)
            elif action == 'unpublish':
                unpublish_date(date)
            audit_log({'action': action, 'date': date, 'ok': True})
            self.send_json({'ok': True, 'date': date})
        except Exception as e:
            traceback.print_exc()
            audit_log({'action': action, 'date': date, 'ok': False, 'error': str(e)})
            self.send_json({'ok': False, 'error': str(e)}, status=500)

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
    ap.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8069)))
    ap.add_argument('--host', default='127.0.0.1')
    args = ap.parse_args()
    os.makedirs(PUBLIC, exist_ok=True)
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    token = 'set' if ENV.get('UPLOAD_TOKEN') else 'NOT SET (uploads disabled)'
    print(f'lwcc serving {PUBLIC} on http://{args.host}:{args.port} — upload token {token}',
          flush=True)
    missing = missing_deps()
    if missing:
        print(f"WARNING: missing converter deps: {', '.join(missing)} — "
              'uploads will fail until: apt-get install -y poppler-utils tesseract-ocr',
              flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()

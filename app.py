#!/usr/bin/env python3
"""lwcc.lab980.com — worship guide site + converter app.

Serves the published worship guides from public/ (one directory per Sunday,
newest at /) and converts newly uploaded worship-guide PDFs in place:

    GET  /            current (newest) Sunday's guide
    GET  /YYYY-MM-DD/ any published Sunday
    GET  /archive     list of every published Sunday
    GET  /admin       admin area (upload, review, edit) — every page is gated
                      by a sign-in cookie; POST /admin/login sets it (long-
                      lived, HttpOnly) after checking the upload token once
    POST /api/upload  raw PDF body -> convert -> publish; the admin cookie or
                      an X-Upload-Token header must match UPLOAD_TOKEN from
                      .env (fails closed if unset)
    GET  /healthz     liveness for the platform health-check sweep

Stdlib only, runs under pm2 behind the site's nginx vhost per lab980
conventions. Uploads run the wgconvert pipeline synchronously (a few seconds);
warnings from the parser are returned to the uploader so odd content is seen,
not silently dropped.
"""
import argparse
import datetime
import hmac
import http.cookies
import http.server
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, 'public')
DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MAX_UPLOAD = 40 * 1024 * 1024
COOKIE_NAME = 'wg_token'
COOKIE_MAX_AGE = 180 * 24 * 3600
# where /admin/login may redirect after sign-in; anything else falls back
# to /admin so the token can't be used to bounce visitors off-site
ADMIN_NEXT_RE = re.compile(r'/admin(/edit/\d{4}-\d{2}-\d{2})?')

sys.path.insert(0, ROOT)
from wgconvert import extract, parse, render  # noqa: E402
from wgconvert.extract import render_page_image  # noqa: E402


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
        for fl in guide.get('flyers') or []:
            fl['image'] = f"flyer-{fl['page']}.jpg"
            render_page_image(pdf_path, fl['page'], os.path.join(out_dir, fl['image']))
        with open(os.path.join(out_dir, 'guide.json'), 'w', encoding='utf-8') as fh:
            json.dump(guide, fh, indent=2, ensure_ascii=False)
            fh.write('\n')
        html = render(guide, church,
                      banner_path=os.path.join(ROOT, 'assets', 'banner.png'),
                      cover_path=cover_dest, flyer_dir=out_dir)
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
                  cover_path=os.path.join(out_dir, cover) if cover else None,
                  flyer_dir=out_dir)
    with open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8') as fh:
        fh.write(html)


def unpublish_date(d):
    """Take a Sunday off the site without destroying it: the folder is renamed
    aside (restore by renaming it back and re-uploading is never needed)."""
    ts = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    os.rename(os.path.join(PUBLIC, d),
              os.path.join(PUBLIC, f'.unpublished-{d}-{ts}'))


ALLOWED_TAG_RE = re.compile(r'</?(b|i)>|<sup>|</sup>', re.I)
BLOCK_TYPES = ('para', 'prayer', 'refrain', 'ref', 'verse')
ITEM_TYPES = ('music', 'prayer', 'litany', 'scripture', 'message', 'plain')


def clean_plain(s):
    """Plain-text field: strip all markup; the renderer escapes it."""
    return re.sub(r'<[^>]*>', '', str(s or '')).strip()


def clean_rich(s):
    """Rich field: keep only the trusted <b>/<i>/<sup> vocabulary, escape
    everything else (existing entities pass through untouched)."""
    s = str(s or '')
    out, pos = [], 0

    def esc_frag(t):
        t = re.sub(r'&(?![a-zA-Z]+;|#\d+;)', '&amp;', t)
        return t.replace('<', '&lt;').replace('>', '&gt;')

    for m in ALLOWED_TAG_RE.finditer(s):
        out.append(esc_frag(s[pos:m.start()]))
        out.append(m.group(0).lower())
        pos = m.end()
    out.append(esc_frag(s[pos:]))
    return ''.join(out).strip()


def _clean_blocks(blocks):
    out = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        btype = b.get('type') if b.get('type') in BLOCK_TYPES else 'para'
        text = clean_rich(b.get('text'))
        if text:
            out.append({'type': btype, 'text': text})
    return out


def sanitize_guide(sub, existing):
    """Rebuild a guide dict from an edit-form submission: known fields only,
    types coerced, markup constrained. Protected fields (dateISO, warnings,
    reviewedWarnings, journal.fromOcr) always come from the existing file."""
    if not isinstance(sub, dict):
        sub = {}
    g = {}
    g['date'] = clean_plain(sub.get('date')) or existing.get('date')
    g['dateISO'] = existing.get('dateISO')
    g['season'] = clean_plain(sub.get('season')) or None
    series = sub.get('series') if isinstance(sub.get('series'), dict) else {}
    title = clean_plain(series.get('title'))
    g['series'] = {'title': title, 'by': clean_plain(series.get('by')) or None} if title else None
    g['coverAlt'] = clean_plain(sub.get('coverAlt')) or None

    w = sub.get('welcome') if isinstance(sub.get('welcome'), dict) else None
    g['welcome'] = None
    if w:
        body = _clean_blocks(w.get('body'))
        if body or clean_plain(w.get('heading')):
            g['welcome'] = {'heading': clean_plain(w.get('heading')) or 'Welcome',
                            'who': clean_plain(w.get('who')) or None, 'body': body}

    order = []
    for o in sub.get('order') or []:
        if not isinstance(o, dict):
            continue
        if o.get('kind') == 'stage':
            text = clean_plain(o.get('text'))
            if text:
                order.append({'kind': 'stage', 'text': text})
            continue
        item = {
            'kind': 'item',
            'type': o.get('type') if o.get('type') in ITEM_TYPES else 'plain',
            'label': clean_plain(o.get('label')) or None,
            'title': clean_plain(o.get('title')) or None,
            'titleQuoted': bool(o.get('titleQuoted')),
            'who': clean_plain(o.get('who')) or None,
            'note': clean_plain(o.get('note')) or None,
            'body': _clean_blocks(o.get('body')),
        }
        if item['label'] or item['title'] or item['body']:
            order.append(item)
    g['order'] = order

    g['musicTeam'] = []
    for m in sub.get('musicTeam') or []:
        if isinstance(m, dict) and clean_plain(m.get('name')):
            g['musicTeam'].append({'name': clean_plain(m.get('name')),
                                   'role': clean_plain(m.get('role')) or None})
    g['prayerRequests'] = []
    for pr in sub.get('prayerRequests') or []:
        if isinstance(pr, dict) and clean_rich(pr.get('text')):
            g['prayerRequests'].append({'name': clean_plain(pr.get('name')) or None,
                                        'text': clean_rich(pr.get('text'))})
    g['announcements'] = []
    for a in sub.get('announcements') or []:
        if isinstance(a, dict) and clean_rich(a.get('text')):
            g['announcements'].append({
                'heading': clean_plain(a.get('heading')) or None,
                'text': clean_rich(a.get('text')),
                'kind': 'attendance' if a.get('kind') == 'attendance' else 'note'})
    g['specialEvents'] = []
    for ev in sub.get('specialEvents') or []:
        if not isinstance(ev, dict) or not clean_plain(ev.get('heading')):
            continue
        g['specialEvents'].append({
            'heading': clean_plain(ev.get('heading')),
            'paragraphs': [clean_rich(p) for p in ev.get('paragraphs') or [] if clean_rich(p)],
            'note': clean_plain(ev.get('note')) or None,
            'sectionTitle': clean_plain(ev.get('sectionTitle')) or 'Coming Up'})

    j = sub.get('journal') if isinstance(sub.get('journal'), dict) else None
    g['journal'] = None
    has_sections = any(isinstance(s, dict) and clean_plain(s.get('text'))
                       for s in (j.get('sections') or [])) if j else False
    if j and (clean_plain(j.get('morning')) or clean_plain(j.get('evening')) or has_sections):
        g['journal'] = {'subtitle': clean_plain(j.get('subtitle')) or None,
                        'morning': clean_plain(j.get('morning')) or None,
                        'midday': clean_plain(j.get('midday')) or None,
                        'evening': clean_plain(j.get('evening')) or None,
                        'sections': [
                            {'heading': clean_plain(s.get('heading')),
                             'text': clean_plain(s.get('text')),
                             'attribution': clean_plain(s.get('attribution')) or None}
                            for s in j.get('sections') or []
                            if isinstance(s, dict) and clean_plain(s.get('text'))]}
        if (existing.get('journal') or {}).get('fromOcr'):
            g['journal']['fromOcr'] = True

    g['flyers'] = existing.get('flyers') or []
    g['warnings'] = existing.get('warnings') or []
    if existing.get('reviewedWarnings'):
        g['reviewedWarnings'] = existing['reviewedWarnings']
    return g


def save_guide(d, submitted):
    path = os.path.join(PUBLIC, d, 'guide.json')
    with open(path, encoding='utf-8') as fh:
        existing = json.load(fh)
    g = sanitize_guide(submitted, existing)
    write_guide_json(path, g)
    rerender_date(d)


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


def login_page(next_path, error=None):
    err = f'<p class="err">{error}</p>\n' if error else ''
    next_attr = next_path.replace('&', '&amp;').replace('"', '&quot;')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Sign-in</title><style>{PAGE_STYLE}</style></head>
<body>
<h1>Admin Sign-in</h1>
<div class="card">
{err}<form method="POST" action="/admin/login">
  <input type="hidden" name="next" value="{next_attr}">
  <p><label>Upload token<br>
    <input type="password" name="token" size="28" autofocus
           autocomplete="current-password"></label></p>
  <p><button>Sign in</button></p>
  <p><small style="color:#54574a">One sign-in unlocks uploading, reviewing,
  and editing on this browser for about six months.</small></p>
</form>
</div>
<p><a href="/">Current guide</a></p>
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
  a.minilink{background:#3f6b82;color:#fff;text-decoration:none;border-radius:8px;
    padding:4px 12px;font-size:.82em;margin-left:8px;font-family:Arial,Helvetica,sans-serif}
</style></head>
<body>
<h1>Publish Worship Guides</h1>
<div class="card">
  <p>Add one PDF or a whole backlog. Files are converted and published one at
  a time — the newest Sunday always ends up as the front page, and every
  Sunday gets its permanent <code>/YYYY-MM-DD/</code> URL.</p>
  <div id="drop">Drag PDFs here, or
    <input type="file" id="pdf" accept="application/pdf,.pdf" multiple></div>
  <p><button id="go" disabled>Convert &amp; publish</button>
     <button id="clear" disabled>Clear list</button></p>
  <table id="queue" hidden><thead>
    <tr><th>File</th><th class="st">Status</th><th>Result</th></tr>
  </thead><tbody></tbody></table>
  <div id="summary"></div>
</div>
__HISTORY__
__REVIEW__
<p><a href="/">Current guide</a> · <a href="/archive">Archive</a> ·
   <a href="/admin/logout">Sign out</a></p>
<script>
const $ = id => document.getElementById(id);
let queue = [];   // {file, status, data, error}
let running = false;

localStorage.removeItem('wgToken');  // pre-cookie versions left the secret here

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
  if (action === 'unpublish' && !confirm('Unpublish ' + date + '? The folder is set aside, not deleted.')) return;
  const res = await fetch('/api/' + action, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({date}),
  });
  if (res.status === 401) { location.reload(); return; }  // cookie expired -> sign-in page
  const data = await res.json().catch(() => ({ok: false, error: res.statusText}));
  if (!data.ok) { alert(data.error || 'failed'); return; }
  location.reload();
}

$('go').addEventListener('click', async () => {
  running = true;
  renderQueue();
  for (const q of queue) {
    if (q.status !== 'queued') continue;
    q.status = 'converting';
    renderQueue();
    try {
      const res = await fetch('/api/upload', {
        method: 'POST',
        headers: {'Content-Type': 'application/pdf',
                  'X-Filename': encodeURIComponent(q.file.name)},
        body: q.file,
      });
      if (res.status === 401) throw new Error('signed out — reload this page to sign in again');
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
            f'<a class="minilink" href="/admin/edit/{d}">Edit</a>'
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


def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def upload_status(e):
    """Outcome bucket for an uploads.log entry: ok | warned | failed."""
    if not e.get('ok'):
        return 'failed'
    return 'warned' if e.get('warnings') else 'ok'


def upload_history(limit=None, status=None):
    """Upload entries from uploads.log (admin actions and logins excluded),
    newest first — the durable record behind the admin results table."""
    entries = []
    try:
        with open(os.path.join(ROOT, 'uploads.log'), encoding='utf-8') as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if 'action' not in e:
                    entries.append(e)
    except OSError:
        return []
    entries.reverse()
    if status:
        entries = [e for e in entries if upload_status(e) == status]
    return entries[:limit] if limit else entries


def history_rows(entries):
    rows = []
    for e in entries:
        when = esc((e.get('at') or '').replace('T', '&nbsp;'))
        d = e.get('dateISO')
        sunday = f'<a href="/{esc(d)}/">{esc(d)}</a>' if d else '—'
        st = upload_status(e)
        if st == 'failed':
            status_html = '<span class="err">✖ failed</span>'
            detail = f'<span class="err">{esc(e.get("error") or "")}</span>'
        elif st == 'warned':
            n = len(e['warnings'])
            status_html = (f'<span class="warn">⚠ published, {n} '
                           f'warning{"s" if n > 1 else ""}</span>')
            detail = ('<ul class="warns">'
                      + ''.join(f'<li>{esc(w)}</li>' for w in e['warnings'])
                      + '</ul>')
        else:
            status_html = '<span class="ok">✔ published</span>'
            detail = ''
        if e.get('replaced'):
            status_html += ' <span class="warn">(replaced existing)</span>'
        rows.append(f'<tr><td class="st">{when}</td><td>{esc(e.get("file") or "—")}</td>'
                    f'<td class="st">{sunday}</td><td class="st">{status_html}</td>'
                    f'<td>{detail}</td></tr>')
    return '\n'.join(rows)


HISTORY_TABLE_HEAD = ('<table><thead><tr><th>When</th><th>File</th>'
                      '<th>Sunday</th><th>Status</th><th>Details</th></tr>'
                      '</thead><tbody>')


def recent_uploads_html():
    """Compact last-few-uploads card for /admin — the batch results table
    above is per-visit, this one survives leaving the page."""
    entries = upload_history(limit=8)
    if not entries:
        return ''
    return ('<div class="card"><p><b>Recent uploads</b> — results are kept, '
            'so closing this page loses nothing. '
            '<a href="/admin/history">Browse the full upload history</a>.</p>'
            + HISTORY_TABLE_HEAD + history_rows(entries) + '</tbody></table></div>')


def history_page(query):
    """/admin/history — every upload ever recorded, newest first, filterable
    by outcome. Reads uploads.log so it includes failures that published
    nothing."""
    params = urllib.parse.parse_qs(query or '')
    status = (params.get('status') or [''])[0]
    if status not in ('ok', 'warned', 'failed'):
        status = ''
    try:
        limit = max(1, min(int((params.get('limit') or ['200'])[0]), 5000))
    except ValueError:
        limit = 200
    everything = upload_history()
    counts = {'ok': 0, 'warned': 0, 'failed': 0}
    for e in everything:
        counts[upload_status(e)] += 1
    matched = [e for e in everything if not status or upload_status(e) == status]
    entries = matched[:limit]

    def flink(label, st):
        href = '/admin/history' + (f'?status={st}' if st else '')
        cur = ' style="font-weight:700"' if st == status else ''
        return f'<a href="{href}"{cur}>{label}</a>'

    filters = ' · '.join([
        flink(f'All ({len(everything)})', ''),
        flink(f'Clean ({counts["ok"]})', 'ok'),
        flink(f'With warnings ({counts["warned"]})', 'warned'),
        flink(f'Failed ({counts["failed"]})', 'failed')])
    truncated = ''
    if len(matched) > limit:
        truncated = (f'<p><small style="color:#54574a">Showing the newest '
                     f'{limit} of {len(matched)} — '
                     f'<a href="/admin/history?status={status}&amp;limit={len(matched)}">'
                     f'show all</a>.</small></p>')
    rows = history_rows(entries) or ('<tr><td colspan="5">No uploads '
                                     + ('with this outcome ' if status else '')
                                     + 'recorded yet.</td></tr>')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Upload History</title><style>{PAGE_STYLE}
  table{{width:100%;border-collapse:collapse;font-size:.95em}}
  td,th{{padding:6px 8px;border-bottom:1px solid #d8d6c7;text-align:left;vertical-align:top}}
  .st{{white-space:nowrap}}
  ul.warns{{margin:4px 0 0;padding-left:18px;color:#8a6410}}
</style></head>
<body>
<h1>Upload History</h1>
<div class="card">
  <p>Every conversion this app has run — the per-file results from batch
  uploads, kept for review long after the upload page is closed. Failures that
  published nothing are here too.</p>
  <p>{filters}</p>
  {HISTORY_TABLE_HEAD}
{rows}
</tbody></table>
{truncated}
</div>
<p><a href="/admin">Back to admin</a> · <a href="/archive">Archive</a></p>
</body></html>
"""


EDIT_PAGE = (r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Edit __DATE__</title><style>__STYLE__
  fieldset{border:1px solid #d8d6c7;border-radius:10px;margin:14px 0;padding:12px 14px}
  legend{font-family:Arial,Helvetica,sans-serif;font-weight:700;color:#054253;padding:0 6px}
  label.f{display:block;margin:8px 0;font-size:.9em;color:#54574a}
  label.f input[type=text]{width:100%;margin-top:2px}
  textarea{width:100%;font:inherit;font-size:.95em;padding:8px 10px;border-radius:8px;
    border:1px solid #d8d6c7;min-height:64px}
  .row{border-top:1px dashed #d8d6c7;padding-top:10px;margin-top:10px}
  .rowbtns{float:right}
  button.mini{padding:3px 10px;font-size:.8em;margin-left:6px;background:#3f6b82}
  button.mini.del{background:#a20816}
  select{font:inherit;padding:4px 8px;border-radius:6px;border:1px solid #d8d6c7}
  .savebar{position:sticky;bottom:0;background:#fbfaf5;padding:12px 0;border-top:2px solid #054253}
  small.hint{color:#54574a}
</style></head>
<body>
<h1>Edit — __DATE__</h1>
<p><small class="hint">Text fields may use <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>,
<code>&lt;sup&gt;</code>; anything else is neutralized on save. Prayers keep their
line breaks. Saving re-renders the page immediately.</small></p>
<div id="form"></div>
<div class="savebar">
  <button id="save">Save &amp; re-render</button>
  <a href="/__DATE__/" style="margin-left:14px">View page</a>
  <a href="/admin" style="margin-left:14px">Back to admin</a>
  <span id="msg" style="margin-left:14px"></span>
</div>
<script id="guide-data" type="application/json">__GUIDE__</script>
<script>
const $ = id => document.getElementById(id);
const G = JSON.parse($('guide-data').textContent);

const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'oninput' || k === 'onclick' || k === 'onchange') n[k] = v;
    else if (k === 'checked') n.checked = v;
    else if (k === 'value') n.value = v;
    else n.setAttribute(k, v);
  }
  for (const kid of kids) n.append(kid);
  return n;
};
const txt = (obj, key, label, kind = 'input') => {
  const input = kind === 'input'
    ? el('input', {type: 'text', value: obj[key] || '',
                   oninput: e => obj[key] = e.target.value})
    : el('textarea', {oninput: e => obj[key] = e.target.value}, obj[key] || '');
  return el('label', {class: 'f'}, label, input);
};
const rowBtns = (arr, i, redraw) => el('span', {class: 'rowbtns'},
  el('button', {class: 'mini', onclick: () => { if (i > 0) { [arr[i-1], arr[i]] = [arr[i], arr[i-1]]; redraw(); } }}, '↑'),
  el('button', {class: 'mini', onclick: () => { if (i < arr.length - 1) { [arr[i+1], arr[i]] = [arr[i], arr[i+1]]; redraw(); } }}, '↓'),
  el('button', {class: 'mini del', onclick: () => { arr.splice(i, 1); redraw(); }}, '×'));

function blockRows(item) {
  const wrap = el('div');
  const redraw = () => { wrap.replaceChildren(...build()); };
  const build = () => {
    const rows = (item.body || []).map((b, i) => el('div', {class: 'row'},
      rowBtns(item.body, i, redraw),
      el('select', {onchange: e => b.type = e.target.value},
        ...['para', 'prayer', 'refrain', 'ref', 'verse'].map(t =>
          el('option', {value: t, ...(b.type === t ? {selected: ''} : {})}, t))),
      el('textarea', {oninput: e => b.text = e.target.value}, b.text || '')));
    rows.push(el('button', {class: 'mini', onclick: () => {
      item.body = item.body || []; item.body.push({type: 'para', text: ''}); redraw();
    }}, '+ text block'));
    return rows;
  };
  redraw();
  return wrap;
}

function listSection(title, arr, rowFn, addFn) {
  const fs = el('fieldset', {}, el('legend', {}, title));
  const wrap = el('div');
  const redraw = () => {
    wrap.replaceChildren(
      ...arr.map((entry, i) => {
        const row = el('div', {class: 'row'}, rowBtns(arr, i, redraw));
        rowFn(row, entry, redraw);
        return row;
      }),
      el('button', {class: 'mini', onclick: () => { arr.push(addFn()); redraw(); }}, '+ add'));
  };
  redraw();
  fs.append(wrap);
  return fs;
}

function buildForm() {
  const f = $('form');
  const head = el('fieldset', {}, el('legend', {}, 'Header'));
  head.append(txt(G, 'date', 'Date (as printed)'), txt(G, 'season', 'Season line'));
  G.series = G.series || {title: '', by: ''};
  head.append(txt(G.series, 'title', 'Message series title'),
              txt(G.series, 'by', 'Preacher'),
              txt(G, 'coverAlt', 'Cover image alt text'));
  f.append(head);

  G.welcome = G.welcome || {heading: 'Opening Announcements', who: '', body: []};
  const wfs = el('fieldset', {}, el('legend', {}, 'Welcome'));
  wfs.append(txt(G.welcome, 'heading', 'Section heading'),
             txt(G.welcome, 'who', 'Speaker'), blockRows(G.welcome));
  f.append(wfs);

  G.order = G.order || [];
  const ofs = el('fieldset', {}, el('legend', {}, 'Order of Worship'));
  const owrap = el('div');
  const oredraw = () => {
    owrap.replaceChildren(
      ...G.order.map((o, i) => {
        const row = el('div', {class: 'row'}, rowBtns(G.order, i, oredraw));
        if (o.kind === 'stage') {
          row.append(el('b', {}, 'Stage direction '), txt(o, 'text', ''));
        } else {
          row.append(txt(o, 'label', 'Label'), txt(o, 'title', 'Title'),
            el('label', {class: 'f'},
              el('input', {type: 'checkbox', ...(o.titleQuoted ? {checked: true} : {}),
                           onchange: e => o.titleQuoted = e.target.checked}),
              ' title in quotes'),
            txt(o, 'who', 'Speaker / performer'), txt(o, 'note', 'Italic note'),
            blockRows(o));
        }
        return row;
      }),
      el('button', {class: 'mini', onclick: () => {
        G.order.push({kind: 'item', type: 'plain', label: '', title: null,
                      titleQuoted: false, who: null, note: null, body: []});
        oredraw();
      }}, '+ item'),
      el('button', {class: 'mini', onclick: () => {
        G.order.push({kind: 'stage', text: ''}); oredraw();
      }}, '+ stage direction'));
  };
  oredraw();
  ofs.append(owrap);
  f.append(ofs);

  G.musicTeam = G.musicTeam || [];
  f.append(listSection('Music Team', G.musicTeam,
    (row, m) => row.append(txt(m, 'name', 'Name'), txt(m, 'role', 'Role')),
    () => ({name: '', role: ''})));

  G.prayerRequests = G.prayerRequests || [];
  f.append(listSection('Prayer Requests', G.prayerRequests,
    (row, pr) => row.append(txt(pr, 'name', 'Name (optional)'), txt(pr, 'text', 'Text', 'ta')),
    () => ({name: '', text: ''})));

  G.announcements = G.announcements || [];
  f.append(listSection('Notes & Announcements', G.announcements,
    (row, a) => row.append(txt(a, 'heading', 'Heading'),
      el('label', {class: 'f'}, 'Kind ',
        el('select', {onchange: e => a.kind = e.target.value},
          ...['note', 'attendance'].map(k =>
            el('option', {value: k, ...(a.kind === k ? {selected: ''} : {})}, k)))),
      txt(a, 'text', 'Text', 'ta')),
    () => ({heading: '', text: '', kind: 'note'})));

  G.specialEvents = G.specialEvents || [];
  f.append(listSection('Special Events', G.specialEvents,
    (row, ev) => {
      row.append(txt(ev, 'heading', 'Heading'), txt(ev, 'sectionTitle', 'Section title'));
      const parea = el('textarea', {oninput: e =>
        ev.paragraphs = e.target.value.split(/\n\s*\n/).filter(p => p.trim())},
        (ev.paragraphs || []).join('\n\n'));
      row.append(el('label', {class: 'f'}, 'Paragraphs (blank line between)', parea),
                 txt(ev, 'note', 'Italic footnote'));
    },
    () => ({heading: '', paragraphs: [], note: '', sectionTitle: 'Coming Up'})));

  G.journal = G.journal || {subtitle: '', morning: '', midday: '', evening: ''};
  const jfs = el('fieldset', {}, el('legend', {}, 'Prayer Journal'));
  jfs.append(txt(G.journal, 'subtitle', 'Subtitle'),
             txt(G.journal, 'morning', 'Household Prayer: Morning', 'ta'),
             txt(G.journal, 'midday', 'Midday note', 'ta'),
             txt(G.journal, 'evening', 'Household Prayer: Evening', 'ta'));
  f.append(jfs);
}
buildForm();

$('save').addEventListener('click', async () => {
  $('save').disabled = true;
  $('msg').textContent = 'Saving…';
  try {
    const res = await fetch('/api/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({date: '__DATE__', guide: G}),
    });
    if (res.status === 401) throw new Error('signed out — reload this page to sign in again');
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || res.statusText);
    $('msg').innerHTML = 'Saved &amp; re-rendered — <a href="/__DATE__/">view the page</a>.';
  } catch (e) {
    $('msg').textContent = 'Failed: ' + e.message;
  } finally {
    $('save').disabled = false;
  }
});
</script>
</body></html>
""").replace('__STYLE__', PAGE_STYLE)


def edit_page(d):
    path = os.path.join(PUBLIC, d, 'guide.json')
    with open(path, encoding='utf-8') as fh:
        guide_json = fh.read()
    # </script> inside a JSON string would end the data block early
    guide_json = guide_json.replace('</', '<\\/')
    return EDIT_PAGE.replace('__DATE__', d).replace('__GUIDE__', guide_json)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC, **kwargs)

    # -- helpers ------------------------------------------------------------

    def send_page(self, body, status=200, ctype='text/html; charset=utf-8',
                  cache=None):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self._cache = cache
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_page(json.dumps(obj), status=status, ctype='application/json')

    # -- admin session ------------------------------------------------------

    def cookie_token(self):
        try:
            jar = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
        except http.cookies.CookieError:
            return ''
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else ''

    def session_cookie(self, value, max_age):
        cookie = (f'{COOKIE_NAME}={value}; Path=/; Max-Age={max_age}; '
                  'HttpOnly; SameSite=Lax')
        if self.headers.get('X-Forwarded-Proto') == 'https':
            cookie += '; Secure'
        return cookie

    def redirect_303(self, location, cookie=None):
        self._cache = 'no-store'
        self.send_response(303)
        self.send_header('Location', location)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def require_admin(self):
        """Gate for every admin page: True with a valid session cookie;
        otherwise the sign-in page (or disabled notice) has been sent."""
        expected = ENV.get('UPLOAD_TOKEN', '')
        if not expected:
            self.send_page('admin disabled: set UPLOAD_TOKEN in .env and restart\n',
                           status=503, ctype='text/plain; charset=utf-8',
                           cache='no-store')
            return False
        if hmac.compare_digest(self.cookie_token(), expected):
            return True
        self.send_page(login_page(self.path.split('?', 1)[0]), status=401,
                       cache='no-store')
        return False

    def handle_login(self, body):
        form = urllib.parse.parse_qs(body.decode('utf-8', 'replace'))
        token = (form.get('token') or [''])[0].strip()
        nxt = (form.get('next') or [''])[0]
        if not ADMIN_NEXT_RE.fullmatch(nxt):
            nxt = '/admin'
        expected = ENV.get('UPLOAD_TOKEN', '')
        if not expected:
            self.send_page('admin disabled: set UPLOAD_TOKEN in .env and restart\n',
                           status=503, ctype='text/plain; charset=utf-8',
                           cache='no-store')
            return
        if token and hmac.compare_digest(token, expected):
            audit_log({'action': 'login', 'ok': True})
            self.redirect_303(nxt, cookie=self.session_cookie(token, COOKIE_MAX_AGE))
        else:
            audit_log({'action': 'login', 'ok': False})
            self.send_page(login_page(nxt, error='Wrong token — check '
                                      'UPLOAD_TOKEN in the app&#8217;s .env.'),
                           status=401, cache='no-store')

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
            q = urllib.parse.parse_qs(query).get('q', [''])[0]
            self.send_page(search_page(q))
            return
        if path == '/admin':
            if self.require_admin():
                self.send_page(ADMIN_PAGE
                               .replace('__HISTORY__', recent_uploads_html())
                               .replace('__REVIEW__', manage_html()),
                               cache='no-store')
            return
        if path == '/admin/history':
            if self.require_admin():
                self.send_page(history_page(query), cache='no-store')
            return
        if path == '/admin/logout':
            self.redirect_303('/', cookie=self.session_cookie('', 0))
            return
        m = re.fullmatch(r'/admin/edit/(\d{4}-\d{2}-\d{2})', path)
        if m:
            if not self.require_admin():
                return
            if os.path.exists(os.path.join(PUBLIC, m.group(1), 'guide.json')):
                self.send_page(edit_page(m.group(1)), cache='no-store')
            else:
                self.send_error(404, 'Not Found')
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
        if path == '/admin/login':
            self.handle_login(body)
            return
        if path not in ('/api/upload', '/api/review', '/api/rerender', '/api/unpublish', '/api/save'):
            self.send_json({'ok': False, 'error': 'not found'}, status=404)
            return
        expected = ENV.get('UPLOAD_TOKEN', '')
        if not expected:
            self.send_json({'ok': False, 'error':
                            'uploads disabled: set UPLOAD_TOKEN in .env and restart'},
                           status=503)
            return
        got = self.headers.get('X-Upload-Token') or self.cookie_token()
        if not hmac.compare_digest(got, expected):
            self.send_json({'ok': False, 'error': 'bad upload token'}, status=401)
            return
        if path != '/api/upload':
            self.handle_action(path.rsplit('/', 1)[1], body)
            return
        if not body.startswith(b'%PDF-'):
            self.send_json({'ok': False, 'error': 'not a PDF'}, status=400)
            return
        fname = self.upload_filename()
        tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        try:
            tmp.write(body)
            tmp.close()
            guide, replaced = convert_pdf(tmp.name)
            audit_log({'ok': True, **({'file': fname} if fname else {}),
                       'dateISO': guide['dateISO'],
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
            audit_log({'ok': False, **({'file': fname} if fname else {}),
                       'error': str(e)})
            self.send_json({'ok': False, 'error': str(e)}, status=422)
        finally:
            os.unlink(tmp.name)

    def upload_filename(self):
        """Original filename when the uploader sends X-Filename (the admin
        page does): percent-decoded, basename only, length-capped. None from
        plain curl uploads."""
        raw = (self.headers.get('X-Filename') or '').strip()
        if not raw:
            return None
        name = os.path.basename(urllib.parse.unquote(raw).strip())
        return name[:120] or None

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
            elif action == 'save':
                save_guide(date, data.get('guide'))
            audit_log({'action': action, 'date': date, 'ok': True})
            self.send_json({'ok': True, 'date': date})
        except Exception as e:
            traceback.print_exc()
            audit_log({'action': action, 'date': date, 'ok': False, 'error': str(e)})
            self.send_json({'ok': False, 'error': str(e)}, status=500)

    # -- policy -------------------------------------------------------------

    def end_headers(self):
        # Guides are replaced in place when re-rendered; keep caching short.
        # Admin and sign-in responses override this with no-store.
        self.send_header('Cache-Control',
                         getattr(self, '_cache', None) or 'public, max-age=300')
        self._cache = None
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

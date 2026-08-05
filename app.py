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
    GET  /admin/aiscan/YYYY-MM-DD   AI article scanner: review a Sunday for
                      OCR misclassifications (announcements vs page directions
                      vs content) and apply verified, text-preserving repairs;
                      POST /api/aiscan queues scans (one date or a batch;
                      needs ANTHROPIC_API_KEY in .env) on a durable queue —
                      up to AISCAN_WORKERS run concurrently, markers in
                      queue/aiscan/ survive restarts — GET /api/aiscan-status
                      reports live progress, POST /api/aiscan-apply
                      applies/dismisses selected findings (per Sunday, or a
                      cross-guide batch), and GET /admin/aiscan aggregates
                      matching findings across Sundays for bulk repair
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
import queue
import re
import shutil
import sys
import tempfile
import threading
import traceback
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, 'public')
QUEUE_DIR = os.path.join(ROOT, 'queue')          # spooled uploads awaiting conversion
FAILED_DIR = os.path.join(QUEUE_DIR, 'failed')   # spooled uploads whose conversion failed
DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MAX_UPLOAD = 40 * 1024 * 1024
COOKIE_NAME = 'wg_token'
COOKIE_MAX_AGE = 180 * 24 * 3600
# where /admin/login may redirect after sign-in; anything else falls back
# to /admin so the token can't be used to bounce visitors off-site
ADMIN_NEXT_RE = re.compile(r'/admin(/edit/\d{4}-\d{2}-\d{2})?')

sys.path.insert(0, ROOT)
from wgconvert import aiscan, extract, parse, render  # noqa: E402
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
    if os.path.exists(os.path.join(PUBLIC, d, 'source.pdf')):
        parts.append(f'<a href="/{d}/original">Original PDF</a>')
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


def filename_matches_date(fname, date_iso):
    """True when the upload's filename (or stored source path) independently
    carries the same date — 'WG 010823.pdf', 'WG_2023_01_08.pdf',
    'WG 4.16.23 PDF.pdf', '2023-01-08/source.pdf' all corroborate
    2023-01-08. Used to clear the OCR-date verify warning: two independent
    sources agreeing leaves nothing for a human to check."""
    if not fname or not date_iso:
        return False
    y, mo, d = date_iso.split('-')
    pats = (rf'{y}[ _.-]?{mo}[ _.-]?{d}',       # 20230108 / 2023-01-08 / 2023_01_08
            rf'\b{mo}{d}{y[2:]}\b',             # 010823
            rf'\b{int(mo)}\.{int(d)}\.{y[2:]}\b')   # 4.16.23
    return any(re.search(p, fname) for p in pats)


def convert_pdf(pdf_path, date_override=None, source_name=None):
    """Run the wgconvert pipeline and publish into public/<dateISO>/.
    date_override (YYYY-MM-DD) wins over whatever the parser finds — for
    memorial programs whose printed dates are not the service date.
    source_name (the uploaded filename) corroborates OCR-read dates."""
    church = load_church()
    work_dir = tempfile.mkdtemp(prefix='wg-upload-')
    try:
        extracted = extract(pdf_path, work_dir)
        guide = parse(extracted)
        if date_override:
            guide['dateISO'] = date_override
            if not guide['date']:
                guide['date'] = date_label(date_override)
        elif guide['dateISO'] and filename_matches_date(source_name, guide['dateISO']):
            guide['warnings'] = [w for w in guide['warnings']
                                 if 'service date read from page-image OCR' not in w]
        if not guide['dateISO']:
            raise ValueError('no service date found in the PDF — convert it '
                             'manually with bin/wg-convert and hand-edit guide.json')
        with PUBLISH_LOCK:
            out_dir = os.path.join(PUBLIC, guide['dateISO'])
            replaced = os.path.exists(os.path.join(out_dir, 'index.html'))
            os.makedirs(out_dir, exist_ok=True)
            cover_dest = None
            if extracted.cover_path and not guide.get('suppressCover'):
                cover_dest = os.path.join(out_dir, 'cover' + os.path.splitext(extracted.cover_path)[1])
                shutil.copyfile(extracted.cover_path, cover_dest)
            for fl in guide.get('flyers') or []:
                fl['image'] = f"flyer-{fl['page']}.jpg"
                render_page_image(pdf_path, fl['page'], os.path.join(out_dir, fl['image']))
            # Re-conversions can produce fewer flyers (e.g. a page
            # reclassified as engraved music) — drop images the new guide no
            # longer references, and stale covers when the guide suppresses
            # one (else a later re-render would resurrect it from disk).
            current = {fl['image'] for fl in guide.get('flyers') or []}
            for f in os.listdir(out_dir):
                if re.fullmatch(r'flyer-\d+\.jpg', f) and f not in current:
                    os.unlink(os.path.join(out_dir, f))
                elif guide.get('suppressCover') and re.fullmatch(r'cover\.(jpe?g|png|webp)', f):
                    os.unlink(os.path.join(out_dir, f))
            with open(os.path.join(out_dir, 'guide.json'), 'w', encoding='utf-8') as fh:
                json.dump(guide, fh, indent=2, ensure_ascii=False)
                fh.write('\n')
            # Retain the uploaded PDF so parser upgrades can re-convert
            # server-side (the Re-convert admin action) without a re-upload.
            source_dest = os.path.join(out_dir, 'source.pdf')
            if os.path.abspath(pdf_path) != os.path.abspath(source_dest):
                shutil.copyfile(pdf_path, source_dest)
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


# --- conversion queue -------------------------------------------------------
# Uploads are spooled to QUEUE_DIR and converted one at a time by a worker
# thread, so a batch upload is bounded by bandwidth, not OCR. Job state lives
# in memory for the admin page's polling; the durable record is uploads.log.
# Spool files survive a restart and are re-enqueued (same ids) on startup.

JOBS = {}
JOBS_LOCK = threading.Lock()
CONVERT_Q = queue.Queue()
# Extraction/OCR parallelize across worker threads (the work is all
# subprocesses); publishing into public/<date>/ is serialized so two jobs
# never interleave writes.
PUBLISH_LOCK = threading.Lock()


def convert_workers():
    configured = (ENV.get('CONVERT_WORKERS') or '').strip()
    if configured.isdigit() and int(configured) > 0:
        return int(configured)
    return min(4, max(1, (os.cpu_count() or 1) - 1))


def job_update(jid, **kw):
    with JOBS_LOCK:
        JOBS.setdefault(jid, {}).update(kw)


def spool_upload(body, fname, date_override=None):
    os.makedirs(QUEUE_DIR, exist_ok=True)
    jid = (datetime.datetime.now().strftime('%Y%m%d%H%M%S')
           + '-' + os.urandom(4).hex())
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', fname or 'upload.pdf')[:80]
    path = os.path.join(QUEUE_DIR, f'{jid}__{safe}')
    with open(path, 'wb') as fh:
        fh.write(body)
    if date_override:
        with open(path + '.meta', 'w', encoding='utf-8') as fh:
            json.dump({'date': date_override}, fh)
    job_update(jid, status='queued', file=fname or None, path=path,
               **({'dateOverride': date_override} if date_override else {}))
    CONVERT_Q.put(jid)
    return jid


def convert_worker():
    while True:
        jid = CONVERT_Q.get()
        with JOBS_LOCK:
            job = dict(JOBS.get(jid) or {})
        if not job.get('path') or job.get('status') == 'cancelled':
            continue
        path, fname = job['path'], job.get('file')
        override = job.get('dateOverride')
        keep = job.get('keep')          # re-convert jobs point at a stored
        extra = {**({'file': fname} if fname else {}),  # source.pdf — never
                 **({'dateOverride': override} if override else {}),  # consumed
                 **({'reconvert': True} if keep else {})}
        job_update(jid, status='converting')
        try:
            guide, replaced = convert_pdf(path, override, fname)
            audit_log({'ok': True, **extra, 'dateISO': guide['dateISO'],
                       'replaced': replaced, 'warnings': guide['warnings'],
                       **({'notes': guide['notes']} if guide.get('notes') else {})})
            job_update(jid, status='warned' if guide['warnings'] else 'ok',
                       date=guide['date'], dateISO=guide['dateISO'],
                       url=f"/{guide['dateISO']}/", replaced=replaced,
                       warnings=guide['warnings'], notes=guide.get('notes') or [])
            if not keep:
                os.unlink(path)
                if os.path.exists(path + '.meta'):
                    os.unlink(path + '.meta')
        except Exception as e:
            traceback.print_exc()
            audit_log({'ok': False, **extra, 'error': str(e)})
            job_update(jid, status='failed', error=str(e))
            if not keep:
                try:    # keep the PDF for a retry after the parser learns it
                    os.makedirs(FAILED_DIR, exist_ok=True)
                    shutil.move(path, os.path.join(FAILED_DIR, os.path.basename(path)))
                    if os.path.exists(path + '.meta'):
                        shutil.move(path + '.meta',
                                    os.path.join(FAILED_DIR, os.path.basename(path) + '.meta'))
                except OSError:
                    pass


def queue_snapshot():
    """Live queue state for the admin page and /api/status: how many jobs
    wait, and which files are converting right now (one per worker)."""
    with JOBS_LOCK:
        waiting = sum(1 for j in JOBS.values() if j.get('status') == 'queued')
        conv = [{k: v for k, v in j.items() if k != 'path'}
                for j in JOBS.values() if j.get('status') == 'converting']
    return {'waiting': waiting, 'converting': conv}


def rescan_spool():
    """Re-enqueue spool files found at startup (uploads that a restart
    interrupted). Ids come from the filenames, so a still-open admin page
    keeps polling the same jobs seamlessly."""
    if not os.path.isdir(QUEUE_DIR):
        return
    for name in sorted(os.listdir(QUEUE_DIR)):
        path = os.path.join(QUEUE_DIR, name)
        if not os.path.isfile(path) or name.endswith('.meta'):
            continue
        override = None
        if os.path.exists(path + '.meta'):
            try:
                with open(path + '.meta', encoding='utf-8') as fh:
                    override = (json.load(fh) or {}).get('date')
            except (OSError, ValueError):
                pass
        jid, _, safe = name.partition('__')
        job_update(jid or name, status='queued', file=safe or None, path=path,
                   **({'dateOverride': override} if override else {}))
        CONVERT_Q.put(jid or name)


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
    g['notes'] = existing.get('notes') or []
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


# --- AI article scanner -----------------------------------------------------
# Reviews a published Sunday's guide.json with Claude for text the parser
# filed under the wrong class (announcements vs page/stage directions vs
# worship content) — the classic OCR-backlog failure. Findings live in
# public/<date>/aiscan.json; repairs are verified text-preserving moves.

def aiscan_load(d):
    try:
        with open(os.path.join(PUBLIC, d, 'aiscan.json'), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def aiscan_save(d, scan):
    with open(os.path.join(PUBLIC, d, 'aiscan.json'), 'w', encoding='utf-8') as fh:
        json.dump(scan, fh, indent=2, ensure_ascii=False)
        fh.write('\n')


def aiscan_open_count(scan):
    return sum(1 for f in (scan or {}).get('findings') or []
               if f.get('status') == 'open')


def run_aiscan(d):
    """Scan one Sunday and persist the result. Raises RuntimeError with an
    operator-readable message when the API is unreachable or declines."""
    api_key = ENV.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise RuntimeError('AI scanner disabled: set ANTHROPIC_API_KEY in '
                           '.env and restart')
    with open(os.path.join(PUBLIC, d, 'guide.json'), encoding='utf-8') as fh:
        guide = json.load(fh)
    model = ENV.get('AISCAN_MODEL') or aiscan.DEFAULT_MODEL
    scan = aiscan.scan_guide(guide, api_key, model=model)
    scan['at'] = datetime.datetime.now().isoformat(timespec='seconds')
    # a re-scan rebuilds the working findings but keeps the archive
    resolved = (aiscan_load(d) or {}).get('resolvedFindings')
    if resolved:
        scan['resolvedFindings'] = resolved
    aiscan_save(d, scan)
    return scan


# Scans run through their own durable queue: each request drops a marker in
# queue/aiscan/<date>, a pool of worker threads runs up to AISCAN_WORKERS
# scans concurrently, and markers found at startup are re-enqueued — so an
# `lwcc redeploy` (pm2 restart) pauses scans rather than losing them. Scans
# are idempotent (re-running just rewrites aiscan.json), so the one that was
# mid-flight when the restart hit simply runs again.
AISCAN_QUEUE_DIR = os.path.join(QUEUE_DIR, 'aiscan')
AISCAN_JOBS = {}
AISCAN_LOCK = threading.Lock()
AISCAN_Q = queue.Queue()


def aiscan_workers():
    configured = (ENV.get('AISCAN_WORKERS') or '').strip()
    if configured.isdigit() and 0 < int(configured) <= 10:
        return int(configured)
    return 10


def aiscan_enqueue(d):
    """Queue one Sunday for scanning. A date already waiting or scanning is
    never queued twice; the marker file makes the request survive restarts."""
    with AISCAN_LOCK:
        if (AISCAN_JOBS.get(d) or {}).get('status') in ('queued', 'scanning'):
            return False
        os.makedirs(AISCAN_QUEUE_DIR, exist_ok=True)
        with open(os.path.join(AISCAN_QUEUE_DIR, d), 'w'):
            pass
        AISCAN_JOBS[d] = {'status': 'queued'}
    AISCAN_Q.put(d)
    return True


def aiscan_process_one(d):
    """Run one queued scan: update the in-memory job state for the polling
    UIs, record the outcome in the audit log, clear the durable marker."""
    with AISCAN_LOCK:
        if (AISCAN_JOBS.get(d) or {}).get('status') != 'queued':
            return
        AISCAN_JOBS[d] = {'status': 'scanning'}
    try:
        scan = run_aiscan(d)
        with AISCAN_LOCK:
            AISCAN_JOBS[d] = {'status': 'ok',
                              'findings': len(scan['findings'])}
        audit_log({'action': 'aiscan', 'date': d, 'ok': True,
                   'findings': len(scan['findings'])})
    except Exception as e:
        if not isinstance(e, RuntimeError):
            traceback.print_exc()
        with AISCAN_LOCK:
            AISCAN_JOBS[d] = {'status': 'failed', 'error': str(e)}
        audit_log({'action': 'aiscan', 'date': d, 'ok': False,
                   'error': str(e)})
    finally:
        try:
            os.unlink(os.path.join(AISCAN_QUEUE_DIR, d))
        except OSError:
            pass


def aiscan_worker():
    while True:
        aiscan_process_one(AISCAN_Q.get())


def aiscan_rescan():
    """Re-enqueue scan markers found at startup (requests a restart
    interrupted). Markers for Sundays that are no longer published are
    discarded."""
    if not os.path.isdir(AISCAN_QUEUE_DIR):
        return
    for name in sorted(os.listdir(AISCAN_QUEUE_DIR)):
        if DATE_DIR_RE.match(name) and \
                os.path.exists(os.path.join(PUBLIC, name, 'guide.json')):
            with AISCAN_LOCK:
                AISCAN_JOBS[name] = {'status': 'queued'}
            AISCAN_Q.put(name)
        else:
            try:
                os.unlink(os.path.join(AISCAN_QUEUE_DIR, name))
            except OSError:
                pass


def aiscan_snapshot():
    """Live scan-queue state for the polling UIs: per-date job outcomes plus
    how many wait and which dates are scanning right now."""
    with AISCAN_LOCK:
        jobs = {d: dict(j) for d, j in AISCAN_JOBS.items()}
    return {'jobs': jobs,
            'waiting': sum(1 for j in jobs.values() if j['status'] == 'queued'),
            'scanning': sorted(d for d, j in jobs.items()
                               if j['status'] == 'scanning')}


def aiscan_group_key(f):
    """Findings match across guides when they quote the same text (markup
    and whitespace ignored) and propose the same reclassification — the
    recurring-fixture case: the same masthead, poster blurb, or page
    direction misfiled week after week."""
    quote = re.sub(r'<[^>]+>', '', f.get('quote') or '')
    quote = re.sub(r'\s+', ' ', quote).strip().lower()
    return (quote, f.get('current'), f.get('proposed'))


def aiscan_aggregate():
    """Matching findings across all published Sundays, in two tiers:
    'exact' groups (same quoted text, same reclassification, two or more
    Sundays — the recurring-fixture case) and 'similar' groups (same error
    category — current -> proposed and the same fix op — where the quoted
    text varies week to week, e.g. every announcement misfiled as a special
    event). A finding sits in at most one group; singleton categories stay
    on their own Sunday's scan page. Sorted exact first, then widest."""
    all_items = []
    for d in published_dates():
        for f in (aiscan_load(d) or {}).get('findings') or []:
            all_items.append((d, f))

    def item_view(d, f, full=False):
        v = {'date': d, 'id': f.get('id'), 'status': f.get('status'),
             'confidence': f.get('confidence'), 'fixable': bool(f.get('fix'))}
        if f.get('statusNote'):
            v['note'] = f['statusNote']       # e.g. why a fix was skipped
        if full:
            v['quote'] = f.get('quote')
            v['issue'] = f.get('issue')
        return v

    # actionable first: open findings lead, then skipped (retryable),
    # dismissed, applied — so settled work sinks to the bottom of every list
    status_rank = {'open': 0, 'skipped': 1, 'dismissed': 2, 'applied': 3}

    def item_sort(items):
        items.sort(key=lambda df: df[0], reverse=True)      # newest first…
        items.sort(key=lambda df: status_rank.get(df[1].get('status'), 4))

    exact = {}
    for d, f in all_items:
        key = aiscan_group_key(f)
        if key[0]:
            exact.setdefault(key, []).append((d, f))
    out = []
    grouped = set()
    for items in exact.values():
        if len({d for d, _ in items}) < 2:
            continue
        rep = items[0][1]
        item_sort(items)
        out.append({
            'kind': 'exact',
            'quote': rep.get('quote'),
            'issue': rep.get('issue'),
            'current': rep.get('current'),
            'proposed': rep.get('proposed'),
            'items': [item_view(d, f) for d, f in items],
        })
        grouped.update((d, f.get('id')) for d, f in items)

    cats = {}
    for d, f in all_items:
        if (d, f.get('id')) in grouped:
            continue
        op = (f.get('fix') or {}).get('op') or 'none'
        cats.setdefault((f.get('current'), f.get('proposed'), op), []) \
            .append((d, f))
    for (cur, prop, op), items in sorted(cats.items(),
                                         key=lambda kv: str(kv[0])):
        if len(items) < 2:
            continue
        item_sort(items)
        out.append({
            'kind': 'similar', 'op': op,
            'current': cur, 'proposed': prop,
            'items': [item_view(d, f, full=True) for d, f in items],
        })
    # groups with anything still open lead; fully-settled ones sink
    out.sort(key=lambda g: (0 if g['kind'] == 'exact' else 1,
                            0 if any(i['status'] == 'open'
                                     for i in g['items']) else 1,
                            -len(g['items']), g.get('quote') or ''))
    return out


def apply_aiscan(d, ids, action='apply'):
    """Apply, dismiss, or undismiss selected findings; applied fixes rewrite
    guide.json and re-render the page. Returns per-finding results (None =
    the action landed, a string = why it didn't)."""
    scan = aiscan_load(d)
    if not scan:
        raise RuntimeError('no AI scan stored for this Sunday — run one first')
    findings = scan.get('findings') or []
    ids = [i for i in ids if any(f.get('id') == i for f in findings)]
    if action == 'dismiss':
        for f in findings:
            if f.get('id') in ids and f.get('status') == 'open':
                f['status'] = 'dismissed'
        aiscan_save(d, scan)
        return {i: None for i in ids}
    if action == 'archive':
        # "Clear resolved": settled findings (applied, dismissed) move out of
        # the working list into resolvedFindings — off the pages and out of
        # the aggregate groups, but kept as history. Empty ids = all settled.
        keep, moved = [], []
        for f in findings:
            if f.get('status') in ('applied', 'dismissed') \
                    and (not ids or f.get('id') in ids):
                moved.append(f)
            else:
                keep.append(f)
        scan['findings'] = keep
        if moved:
            scan['resolvedFindings'] = (scan.get('resolvedFindings') or []) + moved
            aiscan_save(d, scan)
        return {f.get('id'): None for f in moved}
    if action == 'retry':
        # One-click recovery for skipped fixes: reopen them and fall through
        # to the apply path, which relocates stale positions by quote. Empty
        # ids = every skipped finding that has a fix.
        retry_ids = []
        for f in findings:
            if f.get('status') == 'skipped' and f.get('fix') \
                    and (not ids or f.get('id') in ids):
                f['status'] = 'open'
                f.pop('statusNote', None)
                retry_ids.append(f['id'])
        if not retry_ids:
            return {}
        ids = retry_ids
        action = 'apply'
    if action == 'undismiss':
        # Reopens dismissed findings and skipped ones alike — a skipped fix
        # (quote mismatch) is retryable once matching improves or the guide
        # is corrected, so it must not be a dead end.
        results = {}
        for f in findings:
            if f.get('id') not in ids:
                continue
            if f.get('status') in ('dismissed', 'skipped'):
                f['status'] = 'open'
                f.pop('statusNote', None)
                results[f['id']] = None
            else:
                results[f['id']] = 'not dismissed or skipped'
        aiscan_save(d, scan)
        return results
    path = os.path.join(PUBLIC, d, 'guide.json')
    with open(path, encoding='utf-8') as fh:
        guide = json.load(fh)
    open_ids = [i for i in ids
                if any(f.get('id') == i and f.get('status') == 'open'
                       for f in findings)]
    new_guide, results = aiscan.apply_findings(guide, findings, open_ids)
    applied = [i for i, reason in results.items() if reason is None]
    if applied:
        notes = new_guide.setdefault('notes', [])
        for f in findings:
            if f.get('id') in applied:
                notes.append(f"AI repair applied: {f.get('issue')} "
                             f"({(f.get('fix') or {}).get('op')})")
        write_guide_json(path, new_guide)
        rerender_date(d)
    for f in findings:
        reason = results.get(f.get('id'), '__untouched__')
        if reason == '__untouched__':
            continue
        if reason is None:
            f['status'] = 'applied'
            f.pop('statusNote', None)
        else:
            f['status'] = 'skipped'
            f['statusNote'] = reason
    aiscan_save(d, scan)
    return results


AISCAN_PAGE = ("""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Scan __DATE__</title><style>__STYLE__
  .finding{border-top:1px dashed #d8d6c7;padding:10px 0;margin-top:10px}
  .finding blockquote{margin:6px 0;padding:6px 10px;background:#f1efe6;
    border-left:3px solid #76a2bf;border-radius:4px;font-size:.92em}
  .tag{font-family:Arial,Helvetica,sans-serif;font-size:.72em;border-radius:6px;
    padding:2px 8px;color:#fff;background:#3f6b82;margin-left:6px}
  .tag.high{background:#a20816}.tag.medium{background:#8a6410}.tag.low{background:#54574a}
  .tag.applied{background:#1f7a44}.tag.dismissed{background:#54574a}
  .tag.skipped{background:#8a6410}
  .meta{color:#54574a;font-size:.88em}
  button.mini{padding:4px 12px;font-size:.82em;margin-left:8px;background:#3f6b82}
</style></head>
<body>
<h1>AI Article Scanner &mdash; __DATE__</h1>
<div class="card">
  <p>Reviews this Sunday&#8217;s parsed guide with Claude for text the OCR
  pipeline filed under the wrong class &mdash; announcements vs page
  directions vs worship content. Repairs move the printed text; nothing is
  rewritten, and every fix is verified against the stored text before it is
  applied.</p>
  __KEYNOTE__
  <p><button id="scan" __SCANDIS__>__SCANLABEL__</button>
     <span id="scanmsg"></span></p>
</div>
<div id="results"></div>
<p><a href="/__DATE__/">View page</a> &middot;
   <a href="/admin/edit/__DATE__">Edit</a> &middot;
   <a href="/admin">Back to admin</a></p>
<script id="scan-data" type="application/json">__SCAN__</script>
<script>
const $ = id => document.getElementById(id);
const SCAN = JSON.parse($('scan-data').textContent);
const esc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function render() {
  const box = $('results');
  if (!SCAN) { box.innerHTML = ''; return; }
  const open = SCAN.findings.filter(f => f.status === 'open');
  let html = '<div class="card"><p><b>Scan from ' + esc(SCAN.at) + '</b>' +
    ' <span class="meta">(' + esc(SCAN.model) + ')</span></p>' +
    '<p>' + esc(SCAN.summary) + '</p>';
  if (!SCAN.findings.length) {
    html += '<p class="ok">No misclassifications found.</p>';
  } else {
    // actionable first: open, then skipped (retryable), dismissed, applied
    const rank = {open: 0, skipped: 1, dismissed: 2, applied: 3};
    const sorted = SCAN.findings.map((f, i) => [f, i]).sort((a, b) =>
      ((rank[a[0].status] ?? 4) - (rank[b[0].status] ?? 4)) || (a[1] - b[1]))
      .map(p => p[0]);
    html += sorted.map(f =>
      '<div class="finding">' +
      (f.status !== 'applied'
        ? '<input type="checkbox" class="pick" value="' + esc(f.id) + '"> '
        : '') +
      '<b>' + esc(f.current) + ' &rarr; ' + esc(f.proposed) + '</b>' +
      '<span class="tag ' + esc(f.confidence) + '">' + esc(f.confidence) + '</span>' +
      (f.status !== 'open'
        ? '<span class="tag ' + esc(f.status) + '">' + esc(f.status) + '</span>' : '') +
      '<div>' + esc(f.issue) + '</div>' +
      '<blockquote>' + esc(f.quote) + '</blockquote>' +
      '<div class="meta">' + esc(f.path) +
      (f.fix ? ' &middot; fix: ' + esc(f.fix.op) : ' &middot; no mechanical fix — edit by hand') +
      (f.statusNote ? ' &middot; ' + esc(f.statusNote) : '') +
      '</div></div>').join('');
    const reopenable = SCAN.findings.filter(f =>
      f.status === 'dismissed' || f.status === 'skipped');
    const settled = SCAN.findings.filter(f =>
      f.status === 'applied' || f.status === 'dismissed').length;
    const skippedFix = SCAN.findings.filter(f =>
      f.status === 'skipped' && f.fix).length;
    const buttons =
      (open.some(f => f.fix) ? '<button id="applysel">Apply selected</button>' : '') +
      (skippedFix ? '<button id="retryskipped" class="mini">Retry skipped fixes (' +
        skippedFix + ')</button>' : '') +
      (open.length ? '<button id="dismisssel" class="mini">Dismiss selected</button>' : '') +
      (reopenable.length ? '<button id="undismisssel" class="mini">Reopen selected</button>' : '') +
      (settled ? '<button id="archiveall" class="mini">Clear resolved (' +
        settled + ')</button>' : '');
    if (buttons) html += '<p>' + buttons + ' <span id="applymsg"></span></p>';
  }
  if (SCAN.findings.some(f => f.status === 'skipped')) {
    html += '<p class="meta"><b>Skipped?</b> Before a fix is applied, its ' +
      'quoted text is re-checked against this Sunday&#8217;s stored guide; ' +
      'a skip means the check failed — usually because the guide changed ' +
      'after the scan (earlier fixes, a hand-edit, or a re-convert). The ' +
      'reason is noted on each skipped finding. <b>Retry skipped fixes</b> ' +
      'relocates the quoted text in the changed guide and applies when it ' +
      'matches exactly one place; text that is gone or ambiguous still ' +
      'refuses, and re-running the scan refreshes everything.</p>';
  }
  const archived = (SCAN.resolvedFindings || []).length;
  if (archived) {
    html += '<p class="meta">' + archived + ' resolved finding' +
      (archived > 1 ? 's' : '') + ' archived (kept in aiscan.json).</p>';
  }
  html += '</div>';
  box.innerHTML = html;
  // mode: 'apply' acts on checked open findings with a fix, 'dismiss' on
  // checked open findings, 'undismiss' reopens checked dismissed/skipped.
  const act = mode => async () => {
    const byId = {};
    for (const f of SCAN.findings) byId[f.id] = f;
    const want = mode === 'undismiss' ? ['dismissed', 'skipped'] : ['open'];
    const ids = [...document.querySelectorAll('.pick:checked')]
      .map(c => c.value)
      .filter(id => byId[id] && want.includes(byId[id].status) &&
                    (mode !== 'apply' || byId[id].fix));
    if (!ids.length) { $('applymsg').textContent = 'Nothing selected for this action.'; return; }
    $('applymsg').textContent = 'Working…';
    const res = await fetch('/api/aiscan-apply', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({date: '__DATE__', ids: ids,
                            dismiss: mode === 'dismiss',
                            undismiss: mode === 'undismiss'}),
    });
    const data = await res.json().catch(() => ({ok: false}));
    if (!data.ok) { $('applymsg').textContent = 'Failed: ' + (data.error || res.status); return; }
    location.reload();
  };
  const ap = $('applysel'), di = $('dismisssel'), un = $('undismisssel');
  if (ap) ap.addEventListener('click', act('apply'));
  if (di) di.addEventListener('click', act('dismiss'));
  if (un) un.addEventListener('click', act('undismiss'));
  const oneShot = (id, body) => {
    const b = $(id);
    if (b) b.addEventListener('click', async () => {
      b.disabled = true;
      const res = await fetch('/api/aiscan-apply', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({ok: false}));
      if (!data.ok) { $('applymsg').textContent = 'Failed: ' + (data.error || res.status); return; }
      location.reload();
    });
  };
  oneShot('archiveall', {date: '__DATE__', archive: true});
  oneShot('retryskipped', {date: '__DATE__', retry: true});
}
render();

// Scans run from the server-side queue (up to 10 concurrently) and survive
// app restarts; this page just watches the queue until this date settles.
let watching = false;
async function pollScan() {
  let job = null, data = null;
  try {
    const res = await fetch('/api/aiscan-status');
    if (res.status === 401) return;          // signed out — stop quietly
    data = await res.json();
    job = (data.jobs || {})['__DATE__'];
  } catch (e) {
    if (watching) setTimeout(pollScan, 2000);
    return;
  }
  if (job && (job.status === 'queued' || job.status === 'scanning')) {
    watching = true;
    $('scan').disabled = true;
    $('scanmsg').textContent = job.status === 'scanning'
      ? 'Scanning — one Claude request, may take a minute…'
      : 'Queued (' + data.scanning.length + ' scanning, ' + data.waiting + ' waiting)…';
    setTimeout(pollScan, 2000);
    return;
  }
  if (!watching) return;
  watching = false;
  if (job && job.status === 'failed') {
    $('scanmsg').textContent = 'Scan failed: ' + (job.error || 'unknown error');
    $('scan').disabled = false;
  } else {
    location.reload();
  }
}
pollScan();      // a scan may already be queued or running for this date

$('scan').addEventListener('click', async () => {
  $('scan').disabled = true;
  $('scanmsg').textContent = 'Queueing…';
  try {
    const res = await fetch('/api/aiscan', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({date: '__DATE__'}),
    });
    const data = await res.json().catch(() => ({ok: false, error: 'HTTP ' + res.status}));
    if (!data.ok) throw new Error(data.error || res.statusText);
    watching = true;
    pollScan();
  } catch (e) {
    $('scanmsg').textContent = 'Failed: ' + e.message;
    $('scan').disabled = false;
  }
});
</script>
</body></html>
""")   # __STYLE__ is filled in aiscan_page (PAGE_STYLE is defined below)


def aiscan_page(d):
    scan = aiscan_load(d)
    scan_json = json.dumps(scan, ensure_ascii=False).replace('</', '<\\/')
    key_set = bool(ENV.get('ANTHROPIC_API_KEY'))
    keynote = ('' if key_set else
               '<p class="warn">Scanning is disabled: set '
               '<code>ANTHROPIC_API_KEY</code> in the app&#8217;s '
               '<code>.env</code> and restart. Stored findings below are '
               'still browsable.</p>')
    return (AISCAN_PAGE
            .replace('__STYLE__', PAGE_STYLE)
            .replace('__SCAN__', scan_json)
            .replace('__KEYNOTE__', keynote)
            .replace('__SCANDIS__', '' if key_set else 'disabled')
            .replace('__SCANLABEL__', 'Re-run AI scan' if scan else 'Run AI scan')
            .replace('__DATE__', d))


AISCAN_AGG_PAGE = ("""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Scan — Matching Findings</title><style>__STYLE__
  .group{border-top:1px dashed #d8d6c7;padding:12px 0;margin-top:12px}
  .group blockquote{margin:6px 0;padding:6px 10px;background:#f1efe6;
    border-left:3px solid #76a2bf;border-radius:4px;font-size:.92em}
  .tag{font-family:Arial,Helvetica,sans-serif;font-size:.72em;border-radius:6px;
    padding:2px 8px;color:#fff;background:#3f6b82;margin-left:6px}
  .tag.high{background:#a20816}.tag.medium{background:#8a6410}.tag.low{background:#54574a}
  .tag.applied{background:#1f7a44}.tag.dismissed{background:#54574a}
  .tag.skipped{background:#8a6410}.tag.open{background:#3f6b82}
  a.datechip{font-family:Arial,Helvetica,sans-serif;font-size:.82em;
    background:#eaf1f5;border:1px solid #76a2bf;border-radius:6px;
    padding:2px 8px;margin:2px 6px 2px 0;display:inline-block;
    text-decoration:none;color:#054253}
  .meta{color:#54574a;font-size:.88em}
  button.mini{padding:4px 12px;font-size:.82em;margin-right:8px;background:#3f6b82}
  h2.sec{font-family:Arial,Helvetica,sans-serif;font-size:.95rem;letter-spacing:2px;
    text-transform:uppercase;color:#054253;border-bottom:2px solid #0a5a6e;
    display:inline-block;padding-bottom:3px;margin:22px 0 2px}
  .itemrow{margin:5px 0}
</style></head>
<body>
<h1>AI Article Scanner &mdash; Matching Findings</h1>
<div class="card">
  <p>Findings that recur across Sundays, in two tiers: <b>identical text</b>
  (the same quoted text flagged the same way on two or more guides — the
  weekly masthead, a repeated page direction) and <b>same error, varying
  text</b> (the same misclassification with different words each week —
  every announcement misfiled as a special event, say). Apply or dismiss a
  whole group at once; each fix is still verified against its own
  Sunday&#8217;s stored text before anything changes, and one-off findings
  stay on their Sunday&#8217;s scan page.</p>
  <div id="groups"></div>
</div>
<p><a href="/admin">Back to admin</a> &middot; <a href="/archive">Archive</a></p>
<script id="agg-data" type="application/json">__GROUPS__</script>
<script>
const $ = id => document.getElementById(id);
const GROUPS = JSON.parse($('agg-data').textContent);
const esc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const cut = (s, n) => {
  s = String(s == null ? '' : s);
  return s.length > n ? s.slice(0, n) + '…' : s;
};

function groupHtml(g, gi) {
  const openFix = g.items.filter(i => i.status === 'open' && i.fixable).length;
  const open = g.items.filter(i => i.status === 'open').length;
  const reopenable = g.items.filter(i =>
    i.status === 'dismissed' || i.status === 'skipped').length;
  const noFixDates = [...new Set(g.items
    .filter(i => i.status === 'open' && !i.fixable).map(i => i.date))];
  const btn = (mode, label) =>
    '<button class="mini" data-gi="' + gi + '" data-mode="' + mode + '">' +
    label + '</button>';
  const skippedFix = g.items.filter(i =>
    i.status === 'skipped' && i.fixable).length;
  const buttons =
    (openFix ? btn('apply', 'Apply all open fixes (' + openFix + ')') : '') +
    (skippedFix ? btn('retry', 'Retry skipped fixes (' + skippedFix + ')') : '') +
    (noFixDates.length ? btn('rescan', 'Re-scan for fixes (' +
      noFixDates.length + ' Sundays)') : '') +
    (open ? btn('dismiss', 'Dismiss all open (' + open + ')') : '') +
    (reopenable ? btn('undismiss', 'Reopen (' + reopenable + ')') : '');
  const dates = new Set(g.items.map(i => i.date)).size;
  const head = '<b>' + esc(g.current) + ' &rarr; ' + esc(g.proposed) + '</b>' +
    (g.kind === 'similar' && g.op !== 'none'
      ? ' <span class="meta">fix: ' + esc(g.op) + '</span>' : '') +
    ' <span class="meta">' + g.items.length + ' finding' +
    (g.items.length > 1 ? 's' : '') + ' on ' + dates + ' Sunday' +
    (dates > 1 ? 's' : '') + '</span>';
  const chip = i =>
    '<a class="datechip" href="/admin/aiscan/' + esc(i.date) + '"' +
    (i.note ? ' title="' + esc(i.note) + '"' : '') + '>' +
    esc(i.date) + '<span class="tag ' + esc(i.status) + '">' +
    esc(i.status) + '</span></a>';
  let body;
  if (g.kind === 'exact') {
    body = '<div>' + esc(g.issue) + '</div>' +
      '<blockquote>' + esc(g.quote) + '</blockquote>' +
      '<div>' + g.items.map(chip).join('') + '</div>';
  } else {
    body = g.items.map(i =>
      '<div class="itemrow" title="' + esc(i.issue) + '">' +
      chip(i) + ' ' +
      '<span class="meta">&#8220;' + esc(cut(i.quote, 100)) + '&#8221;</span>' +
      '<span class="tag ' + esc(i.confidence) + '">' + esc(i.confidence) +
      '</span>' +
      (i.note ? '<div class="meta" style="margin-left:12px">&#8627; ' +
        esc(i.note) + '</div>' : '') +
      '</div>').join('');
  }
  return '<div class="group">' + head + body +
    (buttons ? '<p>' + buttons + '<span id="msg' + gi + '"></span></p>' : '') +
    '</div>';
}

function render() {
  if (!GROUPS.length) {
    $('groups').innerHTML = '<p>No matching findings across Sundays yet — ' +
      'run AI scans from the <a href="/admin">admin panel</a> first.</p>';
    return;
  }
  const sections = [
    ['exact', 'Identical text across Sundays'],
    ['similar', 'Same error, varying text'],
  ];
  let html = sections.map(([kind, title]) => {
    const withIdx = GROUPS.map((g, gi) => [g, gi]).filter(p => p[0].kind === kind);
    if (!withIdx.length) return '';
    return '<h2 class="sec">' + title + '</h2>' +
      withIdx.map(p => groupHtml(p[0], p[1])).join('');
  }).join('');
  if (GROUPS.some(g => g.items.some(i => i.status === 'skipped'))) {
    html += '<p class="meta"><b>Skipped?</b> Before a fix is applied, its ' +
      'quoted text is re-checked against that Sunday&#8217;s stored guide. ' +
      'A skip means the check failed — usually because the guide changed ' +
      'after the scan (earlier fixes applied, a hand-edit, or a re-convert ' +
      'moved the text). Each skipped finding carries its reason. ' +
      '<b>Retry skipped fixes</b> relocates the quoted text in the changed ' +
      'guide and applies when it matches exactly one place; text that is ' +
      'gone or ambiguous still refuses, and a re-scan refreshes everything.</p>';
  }
  $('groups').innerHTML = html;
  document.querySelectorAll('#groups button[data-mode]').forEach(b =>
    b.addEventListener('click', () => b.dataset.mode === 'rescan'
      ? rescan(+b.dataset.gi) : act(+b.dataset.gi, b.dataset.mode)));
}
render();

// A group of flag-only findings can't be applied from the stored scan —
// re-scan its Sundays (through the durable queue) so the model can pick a
// mechanical fix now that the vocabulary covers it, then reload.
async function rescan(gi) {
  const g = GROUPS[gi];
  const dates = [...new Set(g.items
    .filter(i => i.status === 'open' && !i.fixable).map(i => i.date))];
  if (!dates.length) return;
  $('msg' + gi).textContent = ' Queueing re-scans…';
  const res = await fetch('/api/aiscan', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({dates: dates}),
  });
  if (res.status === 401) { location.reload(); return; }
  const data = await res.json().catch(() => ({ok: false}));
  if (!data.ok) { $('msg' + gi).textContent = ' Failed: ' + (data.error || res.status); return; }
  const poll = async () => {
    try {
      const r = await fetch('/api/aiscan-status');
      if (r.status === 401) return;
      const s = await r.json();
      const busy = dates.filter(d => s.jobs[d] &&
        (s.jobs[d].status === 'queued' || s.jobs[d].status === 'scanning'));
      if (!busy.length) { location.reload(); return; }
      $('msg' + gi).textContent = ' Re-scanning: ' + busy.length + ' of ' +
        dates.length + ' Sundays left…';
    } catch (e) { /* transient — keep polling */ }
    setTimeout(poll, 3000);
  };
  poll();
}

// mode: 'apply' (open findings with a fix), 'retry' (skipped findings with
// a fix — reopened, relocated, re-applied), 'dismiss' (open findings),
// 'undismiss' (dismissed or skipped findings back to open)
async function act(gi, mode) {
  const g = GROUPS[gi];
  const want = mode === 'undismiss' ? ['dismissed', 'skipped']
    : mode === 'retry' ? ['skipped'] : ['open'];
  const needFix = mode === 'apply' || mode === 'retry';
  const byDate = {};
  for (const i of g.items) {
    if (!want.includes(i.status) || (needFix && !i.fixable)) continue;
    (byDate[i.date] = byDate[i.date] || []).push(i.id);
  }
  const items = Object.entries(byDate).map(([date, ids]) => ({date: date, ids: ids}));
  if (!items.length) return;
  $('msg' + gi).textContent = ' Working…';
  const res = await fetch('/api/aiscan-apply', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({items: items, dismiss: mode === 'dismiss',
                          undismiss: mode === 'undismiss',
                          retry: mode === 'retry'}),
  });
  if (res.status === 401) { location.reload(); return; }
  const data = await res.json().catch(() => ({ok: false}));
  if (!data.ok) { $('msg' + gi).textContent = ' Failed: ' + (data.error || res.status); return; }
  location.reload();
}
</script>
</body></html>
""")


def aiscan_aggregate_page():
    groups_json = json.dumps(aiscan_aggregate(),
                             ensure_ascii=False).replace('</', '<\\/')
    return (AISCAN_AGG_PAGE
            .replace('__STYLE__', PAGE_STYLE)
            .replace('__GROUPS__', groups_json))


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


def original_page(d):
    """/DATE/original — the scanned/printed PDF exactly as uploaded, with a
    bar back to the converted page (the PDF itself can't carry links)."""
    label = date_label(d)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Original PDF — {label}</title><style>
  html,body{{height:100%;margin:0}}
  .bar{{font-family:Arial,Helvetica,sans-serif;font-size:.9rem;display:flex;
    gap:8px 22px;justify-content:center;flex-wrap:wrap;padding:12px 20px;
    background:#f4f2ea;border-bottom:1px solid #d8d6c7;box-sizing:border-box}}
  .bar a{{color:#a20816}}
  embed{{display:block;width:100%;height:calc(100% - 45px);border:0}}
</style></head>
<body>
<div class="bar">
  <a href="/{d}/">&larr; {label} &mdash; worship guide</a>
  <a href="/{d}/source.pdf" download="WG {d}.pdf">Download the PDF</a>
</div>
<embed src="/{d}/source.pdf" type="application/pdf">
</body></html>
"""


def archive_page():
    dates = published_dates()          # newest first
    years = {}
    for d in dates:
        years.setdefault(d[:4], []).append(d)

    def row(d):
        meta = guide_meta(d)
        line = f'<a href="/{d}/">{date_label(d)}</a>'
        if meta and meta['title']:
            line += f" — <b>{esc(meta['title'])}</b>"
            if meta['by']:
                line += f" <span style=\"color:#54574a\">({esc(meta['by'])})</span>"
        if meta and meta['refs']:
            line += ('<br><span style="color:#54574a;font-size:.9em">'
                     + ' · '.join(esc(r) for r in meta['refs']) + '</span>')
        if meta and meta['warnings']:
            n = len(meta['warnings'])
            line += (f'<br><span class="warn" style="font-size:.9em">⚠ needs review '
                     f'({n} parser warning{"s" if n > 1 else ""})</span>')
        return f'    <li style="margin:8px 0">{line}</li>'

    if years:
        jump = ' · '.join(f'<a href="#y{y}">{y}</a>' for y in sorted(years, reverse=True))
        nav = (f'<p style="font-family:Arial,Helvetica,sans-serif;font-size:.95em">'
               f'Jump to: {jump}</p>')
        cards = []
        for y in sorted(years, reverse=True):
            rows = '\n'.join(row(d) for d in years[y])
            n = len(years[y])
            cards.append(f"""<h2 id="y{y}" style="font-family:Arial,Helvetica,sans-serif;
  font-size:1.05rem;letter-spacing:2px;color:#054253;border-bottom:3px solid #0a5a6e;
  display:inline-block;padding-bottom:4px;margin:26px 0 6px;scroll-margin-top:20px">{y}
  <span style="color:#54574a;font-weight:400;font-size:.85em">({n} Sunday{'s' if n > 1 else ''})</span></h2>
<div class="card"><ul style="list-style:none;padding-left:0">
{rows}
</ul></div>""")
        listing = nav + '\n'.join(cards)
    else:
        listing = '<div class="card"><ul><li>Nothing published yet.</li></ul></div>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Worship Guide Archive</title><style>{PAGE_STYLE}</style></head>
<body>
<h1>Worship Guide Archive</h1>
<form action="/search" style="margin:14px 0"><input type="search" name="q"
  placeholder="Search sermons…" size="28"> <button>Search</button></form>
{listing}
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
                    snippet = esc(snippet)
                    for w in words:
                        if len(w) < 3:      # don't blanket-highlight "a"/"of"/"an"
                            continue
                        snippet = re.sub(f'({re.escape(w)})', r'<mark>\1</mark>',
                                         snippet, flags=re.I)
            head = f'<a href="/{d}/">{date_label(d)}</a>'
            if title:
                head += f' — <b>{esc(title)}</b>'
            if by:
                head += f' <span style="color:#54574a">({esc(by)})</span>'
            if refs:
                head += ('<br><span style="color:#54574a;font-size:.9em">'
                         + ' · '.join(esc(r) for r in refs) + '</span>')
            body = f'<br><span style="font-size:.92em">{snippet}</span>' if snippet else ''
            results.append(f'    <li style="margin:12px 0">{head}{body}</li>')
    if q and not results:
        listing = f'<p>No results for <b>{esc(q)}</b>.</p>'
    elif results:
        joined = '\n'.join(results)
        listing = f'<ul style="list-style:none;padding-left:0">\n{joined}\n</ul>'
    else:
        listing = '<p>Search sermon titles, scripture, speakers, or any text from the guides.</p>'
    q_attr = esc(q)
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
    next_attr = esc(next_path)
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
  body{max-width:960px}
  #drop{border:2px dashed #76a2bf;border-radius:10px;padding:26px;text-align:center;
    color:#54574a;margin:10px 0}
  #drop.over{background:#eaf1f5;border-color:#054253}
  table{width:100%;border-collapse:collapse;font-size:.95em}
  td,th{padding:6px 8px;border-bottom:1px solid #d8d6c7;text-align:left;vertical-align:top}
  .st{white-space:nowrap}
  ul.warns{margin:4px 0 0;padding-left:18px;color:#8a6410}
  ul.notes{margin:4px 0 0;padding-left:18px;color:#54574a;font-size:.92em}
  .detail{max-height:96px;overflow-y:auto;background:#faf9f3;border:1px solid #eeece0;
    border-radius:6px;padding:4px 8px;font-size:.92em}
  .detail ul{margin:0;padding-left:16px}
  .detail li{margin:2px 0}
  .tscroll{overflow-x:auto}
  td.det{min-width:230px}
  #summary{font-weight:700;margin-top:10px}
  button.mini{padding:4px 12px;font-size:.82em;margin-left:8px;background:#3f6b82}
  a.minilink{background:#3f6b82;color:#fff;text-decoration:none;border-radius:8px;
    padding:4px 12px;font-size:.82em;margin-left:8px;font-family:Arial,Helvetica,sans-serif}
  table.pub td{border-bottom:1px solid #eeece0}
  table.pub td.acts{text-align:right;white-space:nowrap}
  table.pub .mini,table.pub .minilink{margin-left:5px;padding:3px 9px;font-size:.78em}
  table.pub tr.yr th{font-family:Arial,Helvetica,sans-serif;font-size:.9em;
    letter-spacing:2px;color:#054253;border-bottom:2px solid #0a5a6e;padding-top:16px}
  table.pub tr.yr th span{color:#54574a;font-weight:400;letter-spacing:0}
</style></head>
<body>
<h1>Publish Worship Guides</h1>
<div class="card">
  <p>Add one PDF or a whole backlog. Files upload first (quick), then convert
  and publish from a server-side queue — once the last upload finishes you can
  close this page and the queue keeps working; results are kept in the upload
  history. The newest Sunday always ends up as the front page, and every
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
const escHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
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
  return {queued: '·', uploading: '⏫ uploading', waiting: '🕓 in queue',
          converting: '⏳ converting', ok: '<span class="ok">✔ published</span>',
          warned: '<span class="warn">⚠ published</span>',
          failed: '<span class="err">✖ failed</span>'}[q.status];
}

function resultCell(q, i) {
  if (q.status === 'queued') {
    return '<input type="date" class="qdate" data-i="' + i + '" value="' + (q.override || '') +
      '" title="Optional: publish under this exact date (memorial programs). Blank = the date printed in the PDF.">';
  }
  if (q.status === 'failed') return '<span class="err">' + escHtml(q.error) + '</span>';
  if (!q.data) return '';
  let html = '<a href="' + q.data.url + '">' + q.data.date + '</a>';
  if (q.data.replaced) html += ' <span class="warn">(replaced existing)</span>';
  let extra = '';
  if (q.data.warnings.length) {
    extra += '<ul class="warns">' +
      q.data.warnings.map(w => '<li>' + escHtml(w) + '</li>').join('') + '</ul>';
  }
  if (q.data.notes && q.data.notes.length) {
    extra += '<ul class="notes">' +
      q.data.notes.map(n => '<li>' + escHtml(n) + '</li>').join('') + '</ul>';
  }
  if (extra) html += '<div class="detail">' + extra + '</div>';
  return html;
}

function renderQueue() {
  const tb = $('queue').querySelector('tbody');
  tb.innerHTML = queue.map((q, i) =>
    '<tr><td>' + escHtml(q.file.name) + '</td><td class="st">' + statusCell(q) +
    '</td><td>' + resultCell(q, i) + '</td></tr>').join('');
  $('queue').hidden = !queue.length;
  $('go').disabled = running || !queue.some(q => q.status === 'queued');
  $('clear').disabled = running || !queue.length;
  const done = queue.filter(q => ['ok', 'warned', 'failed'].includes(q.status));
  const ok = done.filter(q => q.status === 'ok').length;
  const warned = done.filter(q => q.status === 'warned').length;
  const failed = done.filter(q => q.status === 'failed').length;
  const tally = ok + ' clean, ' + warned + ' with warnings, ' + failed + ' failed';
  if (running && queue.length) {
    const up = queue.filter(q => q.status === 'uploading').length;
    const waiting = queue.filter(q => q.status === 'waiting').length;
    const conv = queue.filter(q => q.status === 'converting').length;
    $('summary').textContent = done.length + ' of ' + queue.length + ' done (' + tally + ') — '
      + (up ? 'uploading… ' : '') + waiting + ' in queue, ' + conv + ' converting.';
  } else if (done.length) {
    $('summary').textContent = tally + '.';
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
$('queue').addEventListener('change', e => {
  if (e.target.classList.contains('qdate')) queue[+e.target.dataset.i].override = e.target.value;
});

const _cr = document.getElementById('clearreconverts');
if (_cr) _cr.addEventListener('click', async () => {
  if (!confirm('Cancel all pending re-conversions? Queued uploads and the ' +
               'file converting right now are not affected; stored sources stay put.')) return;
  _cr.disabled = true;
  const res = await fetch('/api/reconvert-clear', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: '{}'});
  const data = await res.json().catch(() => ({ok: false}));
  alert(data.ok ? data.cleared + ' pending re-conversion(s) cancelled.'
                : (data.error || 'failed'));
  location.reload();
});

const _qb = document.getElementById('queuebanner');
async function pollBanner() {
  try {
    const res = await fetch('/api/status?ids=');
    if (res.status === 401) return;        // signed out — stop quietly
    const data = await res.json();
    const qs = data.queue || {};
    const conv = qs.converting || [];
    if (!qs.waiting && !conv.length) {
      if (!running && !queue.length) location.reload();
      return;
    }
    _qb.innerHTML = qs.waiting + ' file' + (qs.waiting === 1 ? '' : 's') + ' waiting' +
      (conv.length ? ', converting ' +
        conv.map(c => '<b>' + escHtml(c.file || '…') + '</b>').join(', ') : '');
  } catch (e) { /* transient — keep polling */ }
  setTimeout(pollBanner, 3000);
}
if (_qb) pollBanner();

const _rv = document.getElementById('reviewall');
if (_rv) _rv.addEventListener('click', async () => {
  const dates = _rv.dataset.dates.split(' ').filter(Boolean);
  if (!confirm('Mark all ' + dates.length + ' listed Sundays reviewed? ' +
               'Their warnings move to reviewedWarnings in each guide.json.')) return;
  _rv.disabled = true;
  let done = 0;
  for (const d of dates) {
    _rv.textContent = 'Reviewing ' + (++done) + '/' + dates.length + ': ' + d + '…';
    await fetch('/api/review', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({date: d}),
    }).catch(() => {});
  }
  location.reload();
});

for (const _id of ['reconvertall', 'reconverteverything']) {
  const _btn = document.getElementById(_id);
  if (!_btn) continue;
  _btn.addEventListener('click', async () => {
    const dates = _btn.dataset.dates.split(' ').filter(Boolean);
    if (!confirm('Re-convert ' + dates.length + ' Sundays from their stored PDFs? ' +
                 'Hand-edits to them will be overwritten.')) return;
    _btn.disabled = true;
    _btn.textContent = 'Queueing ' + dates.length + ' re-conversions…';
    try {
      const res = await fetch('/api/reconvert-batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({dates: dates}),
      });
      const data = await res.json().catch(() => ({ok: false}));
      if (!data.ok) throw new Error(data.error || res.statusText);
      // The server queue takes it from here — the banner shows live progress
      // and this page can be closed.
      location.reload();
    } catch (e) {
      alert('Could not queue re-conversions: ' + e.message);
      _btn.disabled = false;
    }
  });
}

// AI scans run from a server-side queue (up to 10 at a time) that survives
// restarts — queue the batch, then watch until it drains.
const _ab = document.getElementById('aiscanbanner');
const _as = document.getElementById('aiscanall');
async function pollAiscan() {
  try {
    const res = await fetch('/api/aiscan-status');
    if (res.status === 401) return;          // signed out — stop quietly
    const data = await res.json();
    if (!data.waiting && !data.scanning.length) { location.reload(); return; }
    const txt = data.scanning.length + ' scanning, ' + data.waiting + ' waiting…';
    if (_ab) _ab.textContent = 'AI scans in progress: ' + txt;
    if (_as && _as.disabled) _as.textContent = 'AI scans: ' + txt;
  } catch (e) { /* transient — keep polling */ }
  setTimeout(pollAiscan, 3000);
}
if (_ab) pollAiscan();

const _ac = document.getElementById('aiscanclear');
if (_ac) _ac.addEventListener('click', async () => {
  const dates = _ac.dataset.dates.split(' ').filter(Boolean);
  _ac.disabled = true;
  try {
    const res = await fetch('/api/aiscan-apply', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({items: dates.map(d => ({date: d})), archive: true}),
    });
    const data = await res.json().catch(() => ({ok: false}));
    if (!data.ok) throw new Error(data.error || res.statusText);
    location.reload();
  } catch (e) {
    alert('Could not clear resolved findings: ' + e.message);
    _ac.disabled = false;
  }
});

if (_as) _as.addEventListener('click', async () => {
  const dates = _as.dataset.dates.split(' ').filter(Boolean);
  if (!confirm('AI-scan ' + dates.length + ' Sundays? This runs one Claude ' +
               'API request per Sunday, up to 10 at a time.')) return;
  _as.disabled = true;
  _as.textContent = 'Queueing ' + dates.length + ' scans…';
  try {
    const res = await fetch('/api/aiscan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dates: dates}),
    });
    const data = await res.json().catch(() => ({ok: false}));
    if (!data.ok) throw new Error(data.error || res.statusText);
    // The server queue takes it from here — scans survive restarts and this
    // page can be closed; results land in each Sunday's scan page.
    pollAiscan();
  } catch (e) {
    alert('Could not queue AI scans: ' + e.message);
    _as.disabled = false;
  }
});

async function retryFailed(btn) {
  const name = btn.dataset.name;
  const date = btn.parentElement.querySelector('.retrydate').value;
  btn.disabled = true;
  const res = await fetch('/api/retry', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(date ? {name: name, date: date} : {name: name}),
  });
  if (res.status === 401) { location.reload(); return; }
  const data = await res.json().catch(() => ({ok: false, error: res.statusText}));
  if (!data.ok) { alert(data.error || 'failed'); btn.disabled = false; return; }
  location.reload();
}

async function adminAction(action, date) {
  if (action === 'unpublish' && !confirm('Unpublish ' + date + '? The folder is set aside, not deleted.')) return;
  if (action === 'reconvert' && !confirm('Re-convert ' + date + ' from its stored PDF? Hand-edits to this Sunday will be overwritten.')) return;
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
  // Phase 1: ship the bytes — fast, sequential, no conversion yet.
  for (const q of queue) {
    if (q.status !== 'queued') continue;
    q.status = 'uploading';
    renderQueue();
    try {
      const res = await fetch('/api/upload' + (q.override ? '?date=' + q.override : ''), {
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
      q.id = data.id;
      q.status = 'waiting';
    } catch (e) {
      q.status = 'failed';
      q.error = e.message;
    }
    renderQueue();
  }
  // Phase 2: follow the server-side queue until every job settles. Closing
  // the page is safe — conversion continues; results land in the history.
  while (queue.some(q => ['waiting', 'converting'].includes(q.status))) {
    await new Promise(r => setTimeout(r, 2000));
    const ids = queue.filter(q => q.id && ['waiting', 'converting'].includes(q.status))
                     .map(q => q.id);
    if (!ids.length) break;
    try {
      const res = await fetch('/api/status?ids=' + ids.join(','));
      if (res.status === 401) throw new Error('signed out — reload this page to sign in again');
      const data = await res.json();
      if (!data.ok) continue;
      for (const q of queue) {
        const j = q.id && data.jobs[q.id];
        if (!j) continue;
        if (j.status === 'converting') q.status = 'converting';
        else if (j.status === 'ok' || j.status === 'warned') { q.data = j; q.status = j.status; }
        else if (j.status === 'failed') { q.status = 'failed'; q.error = j.error || 'failed'; }
        else if (j.status === 'unknown') {
          q.status = 'failed';
          q.error = 'finished while the app restarted — see the upload history below';
        }
      }
    } catch (e) {
      if (/signed out/.test(e.message)) {
        for (const q of queue) {
          if (['waiting', 'converting'].includes(q.status)) { q.status = 'failed'; q.error = e.message; }
        }
      }
      // other poll errors are transient — keep polling
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
    snap = queue_snapshot()
    busy = (' disabled title="The queue is busy — these Sundays may already '
            'be in it; wait for it to empty."'
            if snap['waiting'] or snap['converting'] else '')
    out = []

    flagged = [(d, m) for d, m in metas.items() if m and m['warnings']]
    if flagged:
        items = []
        for d, m in sorted(flagged, reverse=True):
            warns = ''.join(f'<li class="warn">{esc(w)}</li>' for w in m['warnings'])
            items.append(
                f'<li style="margin:10px 0"><a href="/{d}/">{date_label(d)}</a> '
                f'<button class="mini" onclick="adminAction(\'review\', \'{d}\')">'
                f'Mark reviewed</button>'
                f'<ul style="margin:4px 0 0;padding-left:18px">{warns}</ul></li>')
        bulk = sorted((d for d, _ in flagged
                       if os.path.exists(os.path.join(PUBLIC, d, 'source.pdf'))),
                      reverse=True)
        bulk_html = ''
        if bulk:
            bulk_html = (
                f'<p><button class="mini" id="reconvertall"{busy} '
                f'data-dates="{" ".join(bulk)}">Re-convert all listed '
                f'({len(bulk)})</button> — after a parser upgrade, re-runs the '
                f'converter on each flagged Sunday&#8217;s stored PDF.</p>')
        all_dates = ' '.join(d for d, _ in sorted(flagged, reverse=True))
        bulk_html += (
            f'<p><button class="mini" id="reviewall" data-dates="{all_dates}">'
            f'Mark all reviewed ({len(flagged)})</button> — accept every '
            f'listed Sunday as-is (warnings move to reviewedWarnings).</p>')
        out.append('<div class="card"><p><b>Needs review</b> — published with '
                   'parser warnings. Check the page; if it reads right, mark it '
                   'reviewed (warnings are kept in guide.json under '
                   'reviewedWarnings). Or fix and re-upload the PDF.</p>'
                   + bulk_html +
                   '<ul style="list-style:none;padding-left:0">'
                   + ''.join(items) + '</ul></div>')

    scans = {d: aiscan_load(d) for d in dates}
    flagged_ai = [(d, aiscan_open_count(scans[d])) for d in dates
                  if aiscan_open_count(scans[d])]
    key_set = bool(ENV.get('ANTHROPIC_API_KEY'))
    ai_items = ''.join(
        f'<li style="margin:6px 0"><a href="/admin/aiscan/{d}">{date_label(d)}</a> '
        f'<span class="warn">— {n} open finding{"s" if n > 1 else ""}</span></li>'
        for d, n in flagged_ai)
    if ai_items:
        ai_items = ('<ul style="list-style:none;padding-left:0">'
                    + ai_items + '</ul>')
    unscanned = ' '.join(d for d in dates if scans[d] is None)
    scan_all = ''
    if key_set and unscanned:
        n = len(unscanned.split())
        scan_all = (f'<p><button class="mini" id="aiscanall" '
                    f'data-dates="{unscanned}">Scan all unscanned ({n})'
                    f'</button> — runs one Claude request per Sunday.</p>')
    key_note = ('' if key_set else
                '<p class="warn">Scanning is disabled: set '
                '<code>ANTHROPIC_API_KEY</code> in <code>.env</code> and '
                'restart.</p>')
    settled_dates = [d for d in dates
                     if any(f.get('status') in ('applied', 'dismissed')
                            for f in (scans[d] or {}).get('findings') or [])]
    settled_total = sum(1 for d in settled_dates
                        for f in scans[d].get('findings') or []
                        if f.get('status') in ('applied', 'dismissed'))
    clear_all = ''
    if settled_total:
        clear_all = (f'<p><button class="mini" id="aiscanclear" '
                     f'data-dates="{" ".join(settled_dates)}">Clear resolved '
                     f'findings ({settled_total})</button> — archives applied '
                     f'and dismissed findings into each Sunday&#8217;s scan '
                     f'history (kept in aiscan.json).</p>')
    snap = aiscan_snapshot()
    active = ''
    if snap['waiting'] or snap['scanning']:
        active = (f'<p class="warn"><span id="aiscanbanner">AI scans in '
                  f'progress: {len(snap["scanning"])} scanning, '
                  f'{snap["waiting"]} waiting…</span> — the queue survives '
                  f'restarts and this page can be closed; it reloads when '
                  f'the scans finish.</p>')
    out.append('<div class="card"><p><b>AI article scanner</b> — reviews each '
               'Sunday&#8217;s parsed guide for text the OCR pipeline filed '
               'under the wrong class (announcements vs page directions vs '
               'worship content). Open each Sunday&#8217;s scan page from the '
               'list below to review and apply repairs.</p>'
               '<p><a href="/admin/aiscan">Matching findings across '
               'Sundays</a> — recurring misclassifications grouped, with '
               'their fixes applied in bulk.</p>'
               + key_note + active + scan_all + clear_all + ai_items + '</div>')

    rows = []
    year = None
    for d in dates:
        if d[:4] != year:
            year = d[:4]
            n = sum(1 for x in dates if x[:4] == year)
            rows.append(f'<tr class="yr"><th colspan="3">{year} '
                        f'<span>({n} Sunday{"s" if n > 1 else ""})</span></th></tr>')
        m = metas.get(d)
        title = esc(m['title']) if m and m['title'] else ''
        reconvert = ''
        if os.path.exists(os.path.join(PUBLIC, d, 'source.pdf')):
            reconvert = (f'<button class="mini" onclick="adminAction(\'reconvert\', '
                         f'\'{d}\')">Re-convert</button>')
        n_open = aiscan_open_count(scans[d])
        ai_badge = (f' <span class="warn" style="font-size:.85em">🔎 {n_open}'
                    f'</span>' if n_open else '')
        rows.append(
            f'<tr><td class="st"><a href="/{d}/">{d}</a></td>'
            f'<td>{title}</td>'
            f'<td class="acts">'
            f'<a class="minilink" href="/admin/edit/{d}">Edit</a>'
            f'<a class="minilink" href="/admin/aiscan/{d}">AI scan</a>{ai_badge}'
            f'<button class="mini" onclick="adminAction(\'rerender\', \'{d}\')">'
            f'Re-render</button>'
            f'{reconvert}'
            f'<button class="mini" onclick="adminAction(\'unpublish\', \'{d}\')">'
            f'Unpublish</button></td></tr>')
    src_dates = [d for d in dates
                 if os.path.exists(os.path.join(PUBLIC, d, 'source.pdf'))]
    sweep = ''
    if src_dates:
        sweep = (f'<p><button class="mini" id="reconverteverything"{busy} '
                 f'data-dates="{" ".join(src_dates)}">Re-convert every Sunday '
                 f'({len(src_dates)})</button> — full sweep through the server '
                 f'queue after a converter fix, flagged or not.</p>')
    out.append('<div class="card"><p><b>Published Sundays</b> — re-render '
               'rebuilds the page from its guide.json (after hand-edits); '
               're-convert re-runs the converter on the stored source PDF '
               '(picks up parser upgrades, discards hand-edits); '
               'unpublish sets the folder aside without deleting it.</p>'
               + sweep +
               '<div style="overflow-x:auto"><table class="pub"><tbody>'
               + ''.join(rows) + '</tbody></table></div></div>')
    return '\n'.join(out)


def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


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
        when = esc(e.get('at') or '').replace('T', '<br>')
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
        if e.get('dateOverride'):
            status_html += ' <span class="warn">(date set manually)</span>'
        if e.get('notes'):
            detail += ('<ul class="notes">'
                       + ''.join(f'<li>{esc(n)}</li>' for n in e['notes'])
                       + '</ul>')
        if detail:
            detail = f'<div class="detail">{detail}</div>'
        rows.append(f'<tr><td class="st">{when}</td><td>{esc(e.get("file") or "—")}</td>'
                    f'<td class="st">{sunday}</td><td>{status_html}</td>'
                    f'<td class="det">{detail}</td></tr>')
    return '\n'.join(rows)


HISTORY_TABLE_HEAD = ('<div class="tscroll"><table><thead><tr><th>When</th>'
                      '<th>File</th><th>Sunday</th><th>Status</th>'
                      '<th>Details</th></tr></thead><tbody>')
HISTORY_TABLE_FOOT = '</tbody></table></div>'


def failed_uploads_html():
    """Failed conversions whose PDFs were kept in queue/failed/ — offer a
    retry (optionally pinned to a date) instead of a re-upload."""
    try:
        names = sorted(f for f in os.listdir(FAILED_DIR) if not f.endswith('.meta'))
    except OSError:
        return ''
    if not names:
        return ''
    items = []
    for n in names:
        disp = n.partition('__')[2] or n
        items.append(
            f'<li style="margin:8px 0"><code>{esc(disp)}</code> '
            f'<input type="date" class="retrydate"> '
            f'<button class="mini" data-name="{esc(n)}" '
            f'onclick="retryFailed(this)">Retry</button></li>')
    return ('<div class="card"><p><b>Failed conversions</b> — these PDFs are '
            'kept on the server, so after a parser fix just retry (no '
            're-upload). Set the date to force publishing under a specific '
            'Sunday — for memorial programs whose printed dates are not the '
            'service date — or leave it blank to let the parser decide.</p>'
            '<ul style="list-style:none;padding-left:0">'
            + ''.join(items) + '</ul></div>')


def recent_uploads_html():
    """Compact last-few-uploads card for /admin — the batch results table
    above is per-visit, this one survives leaving the page. When the server
    queue is still working, say so up top (this page reloads fresh)."""
    entries = upload_history(limit=8)
    snap = queue_snapshot()
    active = ''
    if snap['waiting'] or snap['converting']:
        now = ''
        if snap['converting']:
            names = ', '.join(f"<b>{esc(c.get('file') or '…')}</b>"
                              for c in snap['converting'])
            now = f', converting {names}'
        active = (f'<p class="warn"><b>Server queue active:</b> '
                  f'<span id="queuebanner">{snap["waiting"]} '
                  f'file{"s" if snap["waiting"] != 1 else ""} '
                  f'waiting{now}</span> — updates live; finished results appear '
                  f'below and in the history, and this page reloads when the '
                  f'queue empties. '
                  f'<button class="mini" id="clearreconverts">Cancel pending '
                  f're-conversions</button></p>')
    if not entries and not active:
        return ''
    table = (HISTORY_TABLE_HEAD + history_rows(entries) + HISTORY_TABLE_FOOT) if entries else ''
    return ('<div class="card">' + active +
            '<p><b>Recent uploads</b> — results are kept, '
            'so closing this page loses nothing. '
            '<a href="/admin/history">Browse the full upload history</a>.</p>'
            + table + '</div>')


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
  body{{max-width:960px}}
  table{{width:100%;border-collapse:collapse;font-size:.95em}}
  td,th{{padding:6px 8px;border-bottom:1px solid #d8d6c7;text-align:left;vertical-align:top}}
  .st{{white-space:nowrap}}
  ul.warns{{margin:4px 0 0;padding-left:18px;color:#8a6410}}
  ul.notes{{margin:4px 0 0;padding-left:18px;color:#54574a;font-size:.92em}}
  .detail{{max-height:96px;overflow-y:auto;background:#faf9f3;border:1px solid #eeece0;
    border-radius:6px;padding:4px 8px;font-size:.92em}}
  .detail ul{{margin:0;padding-left:16px}}
  .detail li{{margin:2px 0}}
  .tscroll{{overflow-x:auto}}
  td.det{{min-width:230px}}
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
{HISTORY_TABLE_FOOT}
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
        self.send_page(json.dumps(obj), status=status, ctype='application/json',
                       cache='no-store')

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
        if path == '/api/status':
            expected = ENV.get('UPLOAD_TOKEN', '')
            got = self.headers.get('X-Upload-Token') or self.cookie_token()
            if not expected or not hmac.compare_digest(got, expected):
                self.send_json({'ok': False, 'error': 'bad upload token'}, status=401)
                return
            ids = [i for i in urllib.parse.parse_qs(query).get('ids', [''])[0].split(',') if i]
            with JOBS_LOCK:
                jobs = {i: {k: v for k, v in JOBS.get(i, {'status': 'unknown'}).items()
                            if k != 'path'} for i in ids}
            self.send_json({'ok': True, 'jobs': jobs, 'queue': queue_snapshot()})
            return
        if path == '/api/aiscan-status':
            expected = ENV.get('UPLOAD_TOKEN', '')
            got = self.headers.get('X-Upload-Token') or self.cookie_token()
            if not expected or not hmac.compare_digest(got, expected):
                self.send_json({'ok': False, 'error': 'bad upload token'}, status=401)
                return
            self.send_json({'ok': True, **aiscan_snapshot()})
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
                               .replace('__HISTORY__',
                                        failed_uploads_html() + recent_uploads_html())
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
        if path == '/admin/aiscan':
            if self.require_admin():
                self.send_page(aiscan_aggregate_page(), cache='no-store')
            return
        m = re.fullmatch(r'/admin/aiscan/(\d{4}-\d{2}-\d{2})', path)
        if m:
            if not self.require_admin():
                return
            if os.path.exists(os.path.join(PUBLIC, m.group(1), 'guide.json')):
                self.send_page(aiscan_page(m.group(1)), cache='no-store')
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
        m = re.fullmatch(r'/(\d{4}-\d{2}-\d{2})/original/?', path)
        if m:
            if os.path.exists(os.path.join(PUBLIC, m.group(1), 'source.pdf')):
                self.send_page(original_page(m.group(1)))
            else:
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
        path, _, query = self.path.partition('?')
        if path == '/admin/login':
            self.handle_login(body)
            return
        if path not in ('/api/upload', '/api/retry', '/api/review', '/api/rerender',
                        '/api/reconvert', '/api/reconvert-batch',
                        '/api/reconvert-clear', '/api/unpublish', '/api/save',
                        '/api/aiscan', '/api/aiscan-apply'):
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
        if path == '/api/retry':
            self.handle_retry(body)
            return
        if path == '/api/reconvert-batch':
            self.handle_reconvert_batch(body)
            return
        if path == '/api/reconvert-clear':
            # Cancel pending re-conversions (sources stay put; the job
            # currently converting finishes). Uploads are untouched.
            cleared = 0
            with JOBS_LOCK:
                for j in JOBS.values():
                    if j.get('keep') and j.get('status') == 'queued':
                        j['status'] = 'cancelled'
                        cleared += 1
            audit_log({'action': 'reconvert-clear', 'ok': True, 'cleared': cleared})
            self.send_json({'ok': True, 'cleared': cleared})
            return
        if path in ('/api/aiscan', '/api/aiscan-apply'):
            self.handle_aiscan(path, body)
            return
        if path != '/api/upload':
            self.handle_action(path.rsplit('/', 1)[1], body)
            return
        if not body.startswith(b'%PDF-'):
            self.send_json({'ok': False, 'error': 'not a PDF'}, status=400)
            return
        fname = self.upload_filename()
        qs = urllib.parse.parse_qs(query)
        override = (qs.get('date') or [''])[0]
        if override and not DATE_DIR_RE.match(override):
            self.send_json({'ok': False, 'error': 'date must be YYYY-MM-DD'}, status=400)
            return
        override = override or None
        if (qs.get('sync') or ['0'])[0] not in ('1', 'true'):
            # Default: accept the bytes, convert from the queue. ?sync=1 keeps
            # the old convert-before-replying behavior for scripts that want
            # the result inline.
            jid = spool_upload(body, fname, override)
            self.send_json({'ok': True, 'queued': True, 'id': jid,
                            **({'file': fname} if fname else {})})
            return
        tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        try:
            tmp.write(body)
            tmp.close()
            guide, replaced = convert_pdf(tmp.name, override, fname)
            audit_log({'ok': True, **({'file': fname} if fname else {}),
                       **({'dateOverride': override} if override else {}),
                       'dateISO': guide['dateISO'],
                       'replaced': replaced, 'warnings': guide['warnings'],
                       **({'notes': guide['notes']} if guide.get('notes') else {})})
            self.send_json({
                'ok': True,
                'date': guide['date'],
                'dateISO': guide['dateISO'],
                'url': f"/{guide['dateISO']}/",
                'replaced': replaced,
                'warnings': guide['warnings'],
                'notes': guide.get('notes') or [],
            })
        except Exception as e:                        # surface, don't 500-blank
            traceback.print_exc()
            audit_log({'ok': False, **({'file': fname} if fname else {}),
                       'error': str(e)})
            self.send_json({'ok': False, 'error': str(e)}, status=422)
        finally:
            os.unlink(tmp.name)

    def handle_reconvert_batch(self, body):
        """Queue re-conversions of published Sundays from their stored source
        PDFs. Runs through the same worker as uploads, so the queue banner
        shows live progress, results land in the history, and the browser
        need not stay open."""
        try:
            data = json.loads(body or b'{}')
            dates = data.get('dates') or []
        except ValueError:
            self.send_json({'ok': False, 'error': 'invalid JSON body'}, status=400)
            return
        queued, skipped, already = [], [], []
        with JOBS_LOCK:
            pending = {j.get('file') for j in JOBS.values()
                       if j.get('keep') and j.get('status') in ('queued', 'converting')}
        for date in list(dates)[:500]:
            date = str(date)
            src = os.path.join(PUBLIC, date, 'source.pdf')
            if not DATE_DIR_RE.match(date) or not os.path.exists(src):
                skipped.append(date)
                continue
            fname = f'{date}/source.pdf'
            if fname in pending:
                already.append(date)     # a re-convert of this Sunday is
                continue                 # queued or running — don't stack
            pending.add(fname)
            jid = (datetime.datetime.now().strftime('%Y%m%d%H%M%S')
                   + '-' + os.urandom(4).hex())
            job_update(jid, status='queued', file=fname, path=src, keep=True)
            CONVERT_Q.put(jid)
            queued.append(date)
        audit_log({'action': 'reconvert-batch', 'ok': True,
                   'queued': len(queued),
                   **({'skipped': skipped} if skipped else {}),
                   **({'alreadyQueued': len(already)} if already else {})})
        self.send_json({'ok': True, 'queued': len(queued), 'skipped': skipped,
                        'alreadyQueued': already})

    def handle_aiscan(self, path, body):
        """Queue AI misclassification scans (one date or a batch — they run
        from the durable scan queue, up to AISCAN_WORKERS at a time), or
        apply/dismiss selected findings from a stored scan."""
        try:
            data = json.loads(body or b'{}')
        except ValueError:
            self.send_json({'ok': False, 'error': 'invalid JSON body'}, status=400)
            return
        if path == '/api/aiscan':
            if not ENV.get('ANTHROPIC_API_KEY'):
                self.send_json({'ok': False, 'error':
                                'AI scanner disabled: set ANTHROPIC_API_KEY in '
                                '.env and restart'}, status=503)
                return
            dates = [str(d) for d in (data.get('dates') or [])]
            if data.get('date'):
                dates.insert(0, str(data.get('date')))
            dates = list(dict.fromkeys(dates))[:500]
            if not dates:
                self.send_json({'ok': False, 'error': 'no dates given'}, status=400)
                return
            queued, skipped = [], {}
            for d in dates:
                if not DATE_DIR_RE.match(d) or \
                        not os.path.exists(os.path.join(PUBLIC, d, 'guide.json')):
                    skipped[d] = 'no published guide'
                elif aiscan_enqueue(d):
                    queued.append(d)
                else:
                    skipped[d] = 'already queued or scanning'
            if not queued and any(v == 'no published guide' for v in skipped.values()):
                self.send_json({'ok': False, 'error':
                                'no published guide for the requested date(s)',
                                'skipped': skipped}, status=404)
                return
            audit_log({'action': 'aiscan-queue', 'ok': True,
                       'queued': len(queued),
                       **({'skipped': skipped} if skipped else {})})
            self.send_json({'ok': True, 'queued': queued, 'skipped': skipped})
            return
        action = ('retry' if data.get('retry')
                  else 'archive' if data.get('archive')
                  else 'undismiss' if data.get('undismiss')
                  else 'dismiss' if data.get('dismiss') else 'apply')
        if isinstance(data.get('items'), list):
            # Cross-guide batch (the aggregate view): apply/dismiss/undismiss
            # matching findings on each of their own Sundays independently.
            results = {}
            for entry in data['items'][:200]:
                if not isinstance(entry, dict):
                    continue
                d = str(entry.get('date') or '')
                ids = [str(i) for i in (entry.get('ids') or [])][:200]
                if not ids and action not in ('archive', 'retry'):
                    continue      # archive/retry with no ids = all eligible
                if not DATE_DIR_RE.match(d) or \
                        not os.path.exists(os.path.join(PUBLIC, d, 'guide.json')):
                    results[d or '?'] = {'error': 'no published guide'}
                    continue
                try:
                    r = apply_aiscan(d, ids, action=action)
                    results[d] = {
                        'applied': sorted(i for i, v in r.items() if v is None),
                        'skipped': {i: v for i, v in r.items() if v is not None}}
                except Exception as e:
                    traceback.print_exc()
                    results[d] = {'error': str(e)}
            audit_log({'action': f'aiscan-{action}', 'ok': True,
                       'dates': sorted(results)})
            self.send_json({'ok': True, 'results': results})
            return
        date = str(data.get('date') or '')
        if not DATE_DIR_RE.match(date) or \
                not os.path.exists(os.path.join(PUBLIC, date, 'guide.json')):
            self.send_json({'ok': False, 'error': f'no published guide for {date!r}'},
                           status=404)
            return
        try:
            ids = [str(i) for i in (data.get('ids') or [])][:200]
            results = apply_aiscan(date, ids, action=action)
            applied = sorted(i for i, r in results.items() if r is None)
            skipped = {i: r for i, r in results.items() if r is not None}
            audit_log({'action': f'aiscan-{action}',
                       'date': date, 'ok': True, 'ids': ids,
                       **({'skipped': skipped} if skipped else {})})
            self.send_json({'ok': True, 'date': date,
                            'applied': applied, 'skipped': skipped})
        except RuntimeError as e:
            audit_log({'action': 'aiscan-apply', 'date': date, 'ok': False,
                       'error': str(e)})
            self.send_json({'ok': False, 'error': str(e)}, status=502)
        except Exception as e:
            traceback.print_exc()
            audit_log({'action': 'aiscan-apply', 'date': date, 'ok': False,
                       'error': str(e)})
            self.send_json({'ok': False, 'error': str(e)}, status=500)

    def handle_retry(self, body):
        """Re-enqueue a failed conversion from queue/failed/ — the PDF was
        kept, so no re-upload is needed. An optional date pins the publish
        date (memorials); otherwise any date stored with the original
        upload is carried over."""
        try:
            data = json.loads(body or b'{}')
        except ValueError:
            self.send_json({'ok': False, 'error': 'invalid JSON body'}, status=400)
            return
        name = os.path.basename(str(data.get('name') or ''))
        src = os.path.join(FAILED_DIR, name)
        if not name or name.endswith('.meta') or not os.path.isfile(src):
            self.send_json({'ok': False, 'error': f'no failed upload named {name!r}'},
                           status=404)
            return
        date = str(data.get('date') or '') or None
        if date and not DATE_DIR_RE.match(date):
            self.send_json({'ok': False, 'error': 'date must be YYYY-MM-DD'}, status=400)
            return
        if not date and os.path.exists(src + '.meta'):
            try:
                with open(src + '.meta', encoding='utf-8') as fh:
                    date = (json.load(fh) or {}).get('date')
            except (OSError, ValueError):
                pass
        with open(src, 'rb') as fh:
            pdf = fh.read()
        fname = name.partition('__')[2] or name
        jid = spool_upload(pdf, fname, date)
        for stale in (src, src + '.meta'):
            if os.path.exists(stale):
                os.unlink(stale)
        audit_log({'action': 'retry', 'file': fname, 'ok': True,
                   **({'dateOverride': date} if date else {})})
        self.send_json({'ok': True, 'id': jid})

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
            if action == 'reconvert':
                src = os.path.join(PUBLIC, date, 'source.pdf')
                if not os.path.exists(src):
                    self.send_json({'ok': False, 'error':
                                    'no stored source PDF for this Sunday — it was '
                                    'uploaded before retention; re-upload it once'},
                                   status=404)
                    return
                guide, replaced = convert_pdf(src, None, f'{date}/source.pdf')
                # No 'action' key: reconversions are conversions, so they
                # belong in the /admin/history record.
                audit_log({'ok': True, 'file': f'{date}/source.pdf',
                           'reconvert': True, 'dateISO': guide['dateISO'],
                           'replaced': replaced, 'warnings': guide['warnings'],
                           **({'notes': guide['notes']} if guide.get('notes') else {})})
                self.send_json({'ok': True, 'date': guide['dateISO'],
                                'warnings': guide['warnings']})
                return
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
    rescan_spool()
    workers = convert_workers()
    for i in range(workers):
        threading.Thread(target=convert_worker, daemon=True,
                         name=f'convert-worker-{i + 1}').start()
    print(f'conversion workers: {workers} '
          f'(set CONVERT_WORKERS in .env to override)', flush=True)
    aiscan_rescan()
    scan_workers = aiscan_workers()
    for i in range(scan_workers):
        threading.Thread(target=aiscan_worker, daemon=True,
                         name=f'aiscan-worker-{i + 1}').start()
    print(f'AI scan workers: {scan_workers} '
          f'(set AISCAN_WORKERS in .env to override, max 10)', flush=True)
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

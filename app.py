#!/usr/bin/env python3
"""lwcc.lab980.com — worship guide site + converter app.

Serves the published worship guides from public/ (one directory per Sunday,
newest at /) and converts newly uploaded worship-guide PDFs in place:

    GET  /            current (newest) Sunday's guide
    GET  /YYYY-MM-DD/ any published Sunday
    GET  /archive     list of every published Sunday
    GET  /admin       admin area (upload, review, edit) — every page is gated
                      by a sign-in cookie carrying a server-side session id;
                      POST /admin/login sets it after checking an email and
                      password
    GET  /invite/<token>            redeem a one-time invite or password-reset
                      link: the person sets their own password and is signed
                      in; POST redeems it. Accounts exist only this way
    GET  /admin/users admins invite, reset, remove and sign out accounts
                      (POST /api/users); staff never see it
    POST /api/upload  raw PDF body -> convert -> publish; needs a signed-in
                      session (there is no API token)
    GET  /healthz     liveness for the platform health-check sweep

Accounts, invite links and sessions live in users.json next to this file
(see lwccauth.py). Two roles: staff upload, review and edit; admins also get
the maintenance tools (re-render, re-convert, the sweeps, unpublish) and the
user list — hidden from staff in the HTML *and* refused server-side.

Stdlib only, runs under pm2 behind the site's nginx vhost per lab980
conventions. Uploads run the wgconvert pipeline synchronously (a few seconds);
warnings from the parser are returned to the uploader so odd content is seen,
not silently dropped.
"""
import argparse
import datetime
import http.cookies
import http.server
import json
import os
import posixpath
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, 'public')
QUEUE_DIR = os.path.join(ROOT, 'queue')          # spooled uploads awaiting conversion
FAILED_DIR = os.path.join(QUEUE_DIR, 'failed')   # spooled uploads whose conversion failed
RECONVERT_QUEUE_DIR = os.path.join(QUEUE_DIR, 'reconvert')  # one marker per
                                                 # queued batch re-conversion,
                                                 # so a restart resumes them
DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MAX_UPLOAD = 40 * 1024 * 1024
# Signing in and redeeming an invite are the only POSTs served before anyone
# is authenticated, and an email, a password and a next path come to a few
# hundred bytes. They are capped far below MAX_UPLOAD because the body is read
# and form-parsed before the password reaches the hash queue: a thread parked
# on a hash slot holding 40 MB of "password" (and the copies parse_qs makes of
# it) is the memory exhaustion that bounding the hashes was meant to close.
MAX_AUTH_BODY = 8 * 1024
AUTH_POST_RE = re.compile(r'/admin/login|/invite/[A-Za-z0-9_-]{8,128}')
COOKIE_NAME = 'wg_session'
COOKIE_MAX_AGE = 180 * 24 * 3600
# where /admin/login may redirect after sign-in; anything else falls back
# to /admin so the form can't be used to bounce visitors off-site
ADMIN_NEXT_RE = re.compile(
    r'/admin(/history|/users|/edit/\d{4}-\d{2}-\d{2})?')
# The maintenance tools: everything that rewrites or withdraws a published
# Sunday. Staff never see the buttons (manage_html / recent_uploads_html
# don't render them) and the API refuses them here too — the panel is a
# convenience, the server is the rule.
ADMIN_ONLY_ACTIONS = frozenset((
    '/api/rerender', '/api/rerender-all', '/api/reconvert',
    '/api/reconvert-merge', '/api/reconvert-batch', '/api/reconvert-clear',
    '/api/unpublish', '/api/users'))

sys.path.insert(0, ROOT)
import lwccauth  # noqa: E402
from wgconvert import aiscan, extract, parse, render  # noqa: E402
from wgconvert.extract import render_page_image, render_page_region  # noqa: E402
from wgconvert.merge import merge_guides  # noqa: E402

# Every timestamp the app stamps or shows is Pacific wall-clock — the
# church's time — no matter what timezone the server itself runs in. New
# stamps carry the UTC offset so they stay unambiguous in the logs.
try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo('America/Los_Angeles')
except Exception:                       # no tzdata on this host
    PACIFIC = None


def now_pacific():
    return (datetime.datetime.now(PACIFIC) if PACIFIC
            else datetime.datetime.now())


def fmt_at(s):
    """A stored timestamp, for display: always Pacific wall-clock with a PT
    label. New stamps carry their offset; legacy stamps have none recorded —
    they came from the droplet's clock, which runs UTC, so naive parses as
    UTC and converts too."""
    s = str(s or '')
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    if PACIFIC:
        dt = dt.astimezone(PACIFIC)
    return dt.strftime('%Y-%m-%dT%H:%M:%S') + ' PT'


def audit_log(entry):
    """Append one JSON line per upload to uploads.log — the durable record of
    every conversion, including failures that publish nothing."""
    entry = {'at': now_pacific().isoformat(timespec='seconds'), **entry}
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


def is_admin(user):
    """The maintenance tools and the people list are the admin role's. Admin
    is a role, not an address: whoever was invited with --admin has it, so
    an install with one such invite has exactly one admin. Pinning it to a
    specific address instead would be a change here and nowhere else."""
    return bool(user) and user.get('role') == 'admin'


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


def convert_pdf(pdf_path, date_override=None, source_name=None, keep_edits=False):
    """Run the wgconvert pipeline and publish into public/<dateISO>/.
    date_override (YYYY-MM-DD) wins over whatever the parser finds — for
    memorial programs whose printed dates are not the service date.
    source_name (the uploaded filename) corroborates OCR-read dates.
    keep_edits: merge into the published guide.json instead of replacing it
    — hand edits win, the fresh conversion contributes markup/accents and
    the page-image inventory (see wgconvert.merge)."""
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
            guide_path = os.path.join(out_dir, 'guide.json')
            if keep_edits and os.path.exists(guide_path):
                with open(guide_path, encoding='utf-8') as fh:
                    guide, _ = merge_guides(json.load(fh), guide)
            os.makedirs(out_dir, exist_ok=True)
            cover_dest = None
            if extracted.cover_path and not guide.get('suppressCover'):
                cover_dest = os.path.join(out_dir, 'cover' + os.path.splitext(extracted.cover_path)[1])
                shutil.copyfile(extracted.cover_path, cover_dest)
            for fl in guide.get('flyers') or []:
                fl['image'] = f"flyer-{fl['page']}.jpg"
                render_page_image(pdf_path, fl['page'], os.path.join(out_dir, fl['image']))
            for n, im in enumerate(guide.get('images') or [], 1):
                im['image'] = f"photo-{im['page']}-{n}.jpg"
                render_page_region(pdf_path, im['page'], im,
                                   os.path.join(out_dir, im['image']))
            # Re-conversions can produce fewer flyers or photos (e.g. a page
            # reclassified as engraved music) — drop images the new guide no
            # longer references, and stale covers when the guide suppresses
            # one (else a later re-render would resurrect it from disk).
            current = {fl['image'] for fl in guide.get('flyers') or []} \
                | {im['image'] for im in guide.get('images') or []}
            for f in os.listdir(out_dir):
                if (re.fullmatch(r'flyer-\d+\.jpg', f)
                        or re.fullmatch(r'photo-\d+-\d+\.jpg', f)) and f not in current:
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
    # The heavy lifting is subprocess work (pdftoppm, tesseract), so use
    # every core by default — on the 2-core droplet that means bulk sweeps
    # run two conversions at once instead of one.
    return min(4, max(1, os.cpu_count() or 1))


def job_update(jid, **kw):
    with JOBS_LOCK:
        JOBS.setdefault(jid, {}).update(kw)


# Re-convert batches get a meter: each batch records its size when queued and
# counts jobs as they settle (converted, failed, or cancelled), so the admin
# banner can show "37/120, 3 failed" instead of only what's left in the
# queue. In-memory like the job table; queued re-converts leave durable
# markers (queue/reconvert/) and a restart re-registers the survivors as a
# fresh batch, so the meter picks up where the batch left off.
BATCHES = {}


def batch_update(bid, **deltas):
    with JOBS_LOCK:
        b = BATCHES.get(bid) if bid else None
        if b:
            for k, v in deltas.items():
                b[k] = b.get(k, 0) + v


def spool_upload(body, fname, date_override=None):
    os.makedirs(QUEUE_DIR, exist_ok=True)
    jid = (now_pacific().strftime('%Y%m%d%H%M%S')
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
        merge = job.get('merge')        # source.pdf — never consumed
        extra = {**({'file': fname} if fname else {}),
                 **({'dateOverride': override} if override else {}),
                 **({'reconvert': True} if keep else {}),
                 **({'merge': True} if merge else {})}
        job_update(jid, status='converting')
        try:
            guide, replaced = convert_pdf(path, override, fname, keep_edits=merge)
            audit_log({'ok': True, **extra, 'dateISO': guide['dateISO'],
                       'replaced': replaced, 'warnings': guide['warnings'],
                       **({'notes': guide['notes']} if guide.get('notes') else {})})
            job_update(jid, status='warned' if guide['warnings'] else 'ok',
                       date=guide['date'], dateISO=guide['dateISO'],
                       url=f"/{guide['dateISO']}/", replaced=replaced,
                       warnings=guide['warnings'], notes=guide.get('notes') or [])
            batch_update(job.get('batch'), done=1)
            if not keep:
                os.unlink(path)
                if os.path.exists(path + '.meta'):
                    os.unlink(path + '.meta')
        except Exception as e:
            traceback.print_exc()
            audit_log({'ok': False, **extra, 'error': str(e)})
            job_update(jid, status='failed', error=str(e))
            batch_update(job.get('batch'), done=1, failed=1)
            if not keep:
                try:    # keep the PDF for a retry after the parser learns it
                    os.makedirs(FAILED_DIR, exist_ok=True)
                    shutil.move(path, os.path.join(FAILED_DIR, os.path.basename(path)))
                    if os.path.exists(path + '.meta'):
                        shutil.move(path + '.meta',
                                    os.path.join(FAILED_DIR, os.path.basename(path) + '.meta'))
                except OSError:
                    pass
        if job.get('marker'):            # settled either way: durable marker done
            try:
                os.unlink(job['marker'])
            except OSError:
                pass


def rescan_reconverts():
    """Re-enqueue batch re-conversions that a restart interrupted. Their
    jobs point at stored source PDFs rather than spool files, so each
    queued Sunday leaves a marker in queue/reconvert/ until its job
    settles; markers found at startup resume the batch — including the job
    a restart may have killed mid-write, which simply re-runs. Survivors
    register as a fresh batch so the admin meter keeps a denominator."""
    if not os.path.isdir(RECONVERT_QUEUE_DIR):
        return
    resumed = []
    for name in sorted(os.listdir(RECONVERT_QUEUE_DIR)):
        marker = os.path.join(RECONVERT_QUEUE_DIR, name)
        if not DATE_DIR_RE.match(name) or not os.path.isfile(marker):
            continue
        src = os.path.join(PUBLIC, name, 'source.pdf')
        if not os.path.exists(src):
            os.unlink(marker)            # unpublished since — nothing to redo
            continue
        merge = False
        try:
            with open(marker, encoding='utf-8') as fh:
                merge = bool((json.load(fh) or {}).get('merge'))
        except (OSError, ValueError):
            pass
        resumed.append((name, marker, merge))
    if not resumed:
        return
    bid = (now_pacific().strftime('%Y%m%d%H%M%S') + '-' + os.urandom(4).hex())
    with JOBS_LOCK:
        BATCHES[bid] = {'total': len(resumed), 'done': 0, 'failed': 0,
                        'cancelled': 0,
                        **({'merge': True} if all(m for _, _, m in resumed) else {})}
    for name, marker, merge in resumed:
        jid = (now_pacific().strftime('%Y%m%d%H%M%S') + '-' + os.urandom(4).hex())
        job_update(jid, status='queued', file=f'{name}/source.pdf',
                   path=os.path.join(PUBLIC, name, 'source.pdf'),
                   keep=True, batch=bid, marker=marker,
                   **({'merge': True, 'dateOverride': name} if merge else {}))
        CONVERT_Q.put(jid)
    print(f'resumed {len(resumed)} interrupted re-conversion(s)', flush=True)


def queue_snapshot():
    """Live queue state for the admin page and /api/status: how many jobs
    wait, which files are converting right now (one per worker), and the
    meter of every re-convert batch still in flight (finished batches are
    pruned here — the banner they fed reloads the page when the queue
    empties)."""
    with JOBS_LOCK:
        waiting = sum(1 for j in JOBS.values() if j.get('status') == 'queued')
        conv = [{k: v for k, v in j.items() if k not in ('path', 'marker')}
                for j in JOBS.values() if j.get('status') == 'converting']
        for bid in [bid for bid, b in BATCHES.items() if b['done'] >= b['total']]:
            del BATCHES[bid]
        batches = [dict(b) for b in BATCHES.values()]
    return {'waiting': waiting, 'converting': conv, 'batches': batches}


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


def rerender_all():
    """Rebuild every published Sunday's page from its stored guide.json with
    the current template — a template/CSS or renderer upgrade reaches the
    whole backlog without re-converting a single PDF or touching hand-edits.
    (Changes that need re-extraction from the PDF — like detecting printed
    accent colors — are the re-convert sweeps' job, not this one's.)
    Returns (rendered_dates, {date: error})."""
    rendered, failed = [], {}
    for d in published_dates():
        try:
            rerender_date(d)
            rendered.append(d)
        except Exception as e:
            failed[d] = str(e)
    return rendered, failed


def unpublish_date(d):
    """Take a Sunday off the site without destroying it: the folder is renamed
    aside (restore by renaming it back and re-uploading is never needed)."""
    ts = now_pacific().strftime('%Y%m%d%H%M%S')
    os.rename(os.path.join(PUBLIC, d),
              os.path.join(PUBLIC, f'.unpublished-{d}-{ts}'))


ACCENT_NAMES = ('maroon', 'gold', 'green', 'blue', 'purple')
ALLOWED_TAG_RE = re.compile(
    r'</?(b|i)>|<sup>|</sup>'
    r'|<span class="fc-(?:' + '|'.join(ACCENT_NAMES) + r'|[0-9a-f]{6})">|</span>',
    re.I)
BLOCK_TYPES = ('para', 'prayer', 'refrain', 'ref', 'verse')
ITEM_TYPES = ('music', 'prayer', 'litany', 'scripture', 'message', 'plain')


def clean_accent(v):
    """Accent field: an exact ink '#rrggbb' or a legacy palette name."""
    v = str(v or '').strip().lower()
    if v in ACCENT_NAMES or re.fullmatch(r'#[0-9a-f]{6}', v):
        return v
    return None


def clean_plain(s):
    """Plain-text field: strip all markup; the renderer escapes it."""
    return re.sub(r'<[^>]*>', '', str(s or '')).strip()


def clean_rich(s):
    """Rich field: keep only the trusted <b>/<i>/<sup>/accent-<span>
    vocabulary, escape everything else (existing entities pass through
    untouched)."""
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
                                        'text': clean_rich(pr.get('text')),
                                        'nameColor': clean_accent(pr.get('nameColor'))})
    g['announcements'] = []
    for a in sub.get('announcements') or []:
        if isinstance(a, dict) and clean_rich(a.get('text')):
            heading = clean_plain(a.get('heading')) or None
            # A rainbow heading's rich form survives only while it still
            # spells the (possibly edited) plain heading — else it is stale
            # and the plain heading renders.
            hh = clean_rich(a.get('headingHtml')) or None
            if hh and (re.sub(r'<[^>]+>', '', hh).replace('&amp;', '&')
                       != (heading or '')):
                hh = None
            g['announcements'].append({
                'heading': heading,
                'headingHtml': hh,
                'text': clean_rich(a.get('text')),
                'kind': 'attendance' if a.get('kind') == 'attendance' else 'note',
                'color': clean_accent(a.get('color'))})
    g['specialEvents'] = []
    for ev in sub.get('specialEvents') or []:
        if not isinstance(ev, dict) or not clean_plain(ev.get('heading')):
            continue
        g['specialEvents'].append({
            'heading': clean_plain(ev.get('heading')),
            'paragraphs': [clean_rich(p) for p in ev.get('paragraphs') or [] if clean_rich(p)],
            'note': clean_plain(ev.get('note')) or None,
            'sectionTitle': clean_plain(ev.get('sectionTitle')) or 'Coming Up',
            'color': clean_accent(ev.get('color'))})

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

    # The photo inventory mirrors the crops on disk, so it always comes from
    # the existing file — but captions are operator text: a submission that
    # carries images (positionally) may edit them.
    g['images'] = existing.get('images') or []
    sub_images = sub.get('images') if isinstance(sub.get('images'), list) else None
    if sub_images is not None:
        for i, im in enumerate(g['images']):
            if i < len(sub_images) and isinstance(sub_images[i], dict):
                im['caption'] = clean_plain(sub_images[i].get('caption')) or None
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


# --- AI article scanner (dormant) -------------------------------------------
# NOT REACHABLE OVER HTTP. The scanner's routes, admin card and worker
# threads were removed; the machinery below is kept intact and still tested
# in-process, so re-enabling it is a matter of restoring the routes and the
# worker startup in main() rather than rewriting any of this.
#
# Reviews a published Sunday's guide.json with Claude for text the parser
# filed under the wrong class (announcements vs page/stage directions vs
# worship content) — the classic OCR-backlog failure. A second agent pass
# (aiscan.scan_verses) reads the scripture section and checks every verse
# against its passage reference for its <sup> superscript verse number. A
# third (aiscan.scan_photos) looks at the published photo crops themselves
# and flags any that are really sheet music or unrelated printed text.
# Findings live in public/<date>/aiscan.json; repairs are verified
# text-preserving moves, markup-only <sup> adjustments, or photo drops.

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


# The scanner's three agents, in canonical run/display order, with the id
# prefix each one's findings carry. Any subset can run à la carte; the
# prefix is what lets a partial run replace only its own findings.
AISCAN_AGENTS = ('article', 'verses', 'photos')
AISCAN_PREFIX = {'article': 'f', 'verses': 'v', 'photos': 'p'}
AISCAN_LABEL = {'article': None, 'verses': 'Scripture verses',
                'photos': 'Photos'}


def aiscan_agent_list(agents):
    """Canonicalize an agent selection: the known names in canonical order,
    or None for the full bundle (which empty and all-inclusive selections
    both mean)."""
    if not agents:
        return None
    chosen = [a for a in AISCAN_AGENTS if a in agents]
    return None if not chosen or len(chosen) == len(AISCAN_AGENTS) else chosen


def aiscan_summary(runs):
    """One display line from the stored per-agent summaries, in canonical
    order: the article reviewer's leads bare, the others carry their
    label."""
    parts = []
    for name in AISCAN_AGENTS:
        s = ((runs.get(name) or {}).get('summary') or '').strip()
        if s:
            label = AISCAN_LABEL[name]
            parts.append(label + ': ' + s if label else s)
    return ' — '.join(parts)


def aiscan_usage(runs):
    """Token usage summed across the stored per-agent runs."""
    usage = {}
    for run in runs.values():
        u = run.get('usage') or {}
        for k in ('input', 'output'):
            if u.get(k) is not None:
                usage[k] = usage.get(k, 0) + u[k]
    return usage


def run_aiscan(d, agents=None):
    """Scan one Sunday and persist the result. The three agents — the
    article classifier ('article', ids f…), the scripture verse-number
    checker ('verses', ids v…), and the photo verifier ('photos', ids p…) —
    run in series; agents picks a subset for an à la carte run (None = all
    three). A partial run replaces only the selected agents' findings and
    keeps the others' findings, statuses included, untouched. Raises
    RuntimeError with an operator-readable message when the API is
    unreachable or declines."""
    agents = aiscan_agent_list(agents) or list(AISCAN_AGENTS)
    api_key = ENV.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise RuntimeError('AI scanner disabled: set ANTHROPIC_API_KEY in '
                           '.env and restart')
    with open(os.path.join(PUBLIC, d, 'guide.json'), encoding='utf-8') as fh:
        guide = json.load(fh)
    model = ENV.get('AISCAN_MODEL') or aiscan.DEFAULT_MODEL
    prev = aiscan_load(d) or {}
    runs = dict(prev.get('agents') or {})
    at = now_pacific().isoformat(timespec='seconds')
    fresh = []
    for name in agents:
        if name == 'article':
            res = aiscan.scan_guide(guide, api_key, model=model)
        elif name == 'verses':
            res = aiscan.scan_verses(guide, api_key, model=model)
        else:
            res = aiscan.scan_photos(guide, os.path.join(PUBLIC, d),
                                     api_key, model=model)
        # None = the agent had nothing to read (no scripture, no photos);
        # the run is still recorded so the page shows the ground is covered
        runs[name] = {'at': at, 'model': (res or {}).get('model') or model,
                      'summary': (res or {}).get('summary') or '',
                      'usage': (res or {}).get('usage')}
        if res:
            fresh += res['findings']
    # findings from agents not in this run survive with their statuses; the
    # re-run agents' old findings are replaced wholesale (ids restart)
    rerun = {AISCAN_PREFIX[a] for a in agents}
    kept = [f for f in prev.get('findings') or []
            if str(f.get('id') or '')[:1] not in rerun]
    scan = {'model': model, 'summary': aiscan_summary(runs),
            'usage': aiscan_usage(runs), 'findings': kept + fresh,
            'agents': runs, 'at': at}
    # a re-scan rebuilds the working findings but keeps the archive
    if prev.get('resolvedFindings'):
        scan['resolvedFindings'] = prev['resolvedFindings']
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


def aiscan_enqueue(d, agents=None):
    """Queue one Sunday for scanning, optionally for a subset of the agents
    (an à la carte run). A date already waiting or scanning is never queued
    twice — a request for a still-waiting date widens its pending run to
    cover both agent selections instead. The marker file (its content is
    the agent list; empty = all) makes the request survive restarts."""
    agents = aiscan_agent_list(agents)
    with AISCAN_LOCK:
        job = AISCAN_JOBS.get(d) or {}
        if job.get('status') == 'scanning':
            return False
        os.makedirs(AISCAN_QUEUE_DIR, exist_ok=True)
        if job.get('status') == 'queued':
            if agents is None or job.get('agents') is None:
                agents = None
            else:
                agents = aiscan_agent_list(list(job['agents']) + agents)
            AISCAN_JOBS[d]['agents'] = agents
            with open(os.path.join(AISCAN_QUEUE_DIR, d), 'w') as fh:
                fh.write(','.join(agents or ()))
            return False
        with open(os.path.join(AISCAN_QUEUE_DIR, d), 'w') as fh:
            fh.write(','.join(agents or ()))
        AISCAN_JOBS[d] = {'status': 'queued', 'agents': agents}
    AISCAN_Q.put(d)
    return True


def aiscan_process_one(d):
    """Run one queued scan: update the in-memory job state for the polling
    UIs, record the outcome in the audit log, clear the durable marker."""
    with AISCAN_LOCK:
        job = AISCAN_JOBS.get(d) or {}
        if job.get('status') != 'queued':
            return
        agents = job.get('agents')
        AISCAN_JOBS[d] = {'status': 'scanning', 'agents': agents}
    try:
        scan = run_aiscan(d, agents)
        with AISCAN_LOCK:
            AISCAN_JOBS[d] = {'status': 'ok',
                              'findings': len(scan['findings'])}
        audit_log({'action': 'aiscan', 'date': d, 'ok': True,
                   'findings': len(scan['findings']),
                   **({'agents': agents} if agents else {})})
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
            try:
                with open(os.path.join(AISCAN_QUEUE_DIR, name)) as fh:
                    marker = fh.read().strip()
            except OSError:
                marker = ''
            with AISCAN_LOCK:
                AISCAN_JOBS[name] = {
                    'status': 'queued',
                    'agents': aiscan_agent_list(marker.split(','))
                    if marker else None}
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
    on their own Sunday's scan page. Applied findings are settled — the fix
    is in the guide itself — so they are left out entirely: chips never show
    them, and a group whose every finding was applied disappears (each
    Sunday's own scan page keeps the history). Sorted exact first, then
    widest."""
    all_items = []
    for d in published_dates():
        for f in (aiscan_load(d) or {}).get('findings') or []:
            if f.get('status') == 'applied':
                continue
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

    # actionable first: open findings lead, then skipped (retryable), then
    # dismissed (reopenable) — applied never reaches this view
    status_rank = {'open': 0, 'skipped': 1, 'dismissed': 2}

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
        if any((f.get('fix') or {}).get('op') == 'drop_photo'
               for f in findings if f.get('id') in applied):
            # the dropped entry's crop leaves the disk too, so the photo
            # inventory keeps mirroring the files (as convert-time pruning does)
            keep = {im.get('image') for im in new_guide.get('images') or []}
            for fn in os.listdir(os.path.join(PUBLIC, d)):
                if re.fullmatch(r'photo-\d+-\d+\.jpg', fn) and fn not in keep:
                    try:
                        os.unlink(os.path.join(PUBLIC, d, fn))
                    except OSError:
                        pass
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
  button.busy{opacity:.85}
  button.busy::after{content:'';display:inline-block;width:.75em;height:.75em;
    margin-left:7px;vertical-align:-.08em;border:2px solid #fff;
    border-top-color:transparent;border-radius:50%;
    animation:busyspin .8s linear infinite}
  @keyframes busyspin{to{transform:rotate(360deg)}}
  .warn{color:#8a6410}.err{color:#a20816}.ok{color:#1f7a44}
  #queuebanner progress,#sweepstatus progress{width:180px;height:12px;
    vertical-align:-.1em;margin:0 6px;accent-color:#054253}
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


def login_page(next_path, error=None, email=''):
    """The sign-in gate every admin page falls back to. Fails closed before
    the first invite is redeemed: there is no bootstrap password, only the
    CLI."""
    err = f'<p class="err">{esc(error)}</p>\n' if error else ''
    next_attr = esc(next_path)
    if lwccauth.any_users():
        note = ('<p><small style="color:#54574a">One sign-in lasts about six '
                'months on this browser. No account? Ask an administrator for '
                'an invite link — nobody can create one for you from this '
                'page.</small></p>')
    else:
        note = ('<p class="warn">There are no accounts yet. The first one is '
                'minted on the server:<br>'
                '<code>lwcc invite --admin --for you@example.com</code></p>')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign in</title><style>{PAGE_STYLE}
  label.f{{display:block;margin:10px 0;font-size:.9em;color:#54574a}}
  label.f input{{width:100%;max-width:320px;margin-top:3px}}
</style></head>
<body>
<h1>Sign in</h1>
<div class="card">
{err}<form method="POST" action="/admin/login">
  <input type="hidden" name="next" value="{next_attr}">
  <label class="f">Email
    <input type="email" name="email" autofocus autocomplete="username"
           value="{esc(email)}"></label>
  <label class="f">Password
    <input type="password" name="password" autocomplete="current-password"></label>
  <p><button>Sign in</button></p>
{note}
</form>
</div>
<p><a href="/">Current guide</a></p>
</body></html>
"""


def gone_page():
    """A spent or expired invite link. 410, not 404: the link was real."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Link no longer works</title><style>{PAGE_STYLE}</style></head>
<body>
<h1>Link no longer works</h1>
<div class="card">
  <p>Invite and password-reset links work once and expire seven days after
  they are issued. This one has been used already, was revoked, or is past
  its week.</p>
  <p>Ask an administrator for a fresh link. If you already have an account,
  you can <a href="/admin">sign in</a>.</p>
</div>
<p><a href="/">Current guide</a></p>
</body></html>
"""


def store_error_page(detail):
    """users.json is present and unreadable. Nobody can sign in, nothing has
    been written, and the page says which file to go and look at — an
    operator staring at a blank sign-in form would reasonably conclude the
    accounts were gone, and mint a new admin over the wreckage."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Account store unreadable</title><style>{PAGE_STYLE}</style></head>
<body>
<h1>Account store unreadable</h1>
<div class="card">
  <p><code>users.json</code> is there but is not valid JSON, so nobody can be
  signed in and nothing can be written to it.
  <b>Nothing has been written over it</b> — every account is still in that
  file, and repairing the JSON brings them all back.</p>
  <p class="warn">{esc(detail)}</p>
  <p>On the server: look at <code>users.json</code> in the app directory
  (usually a hand edit that lost a brace or a comma). <code>lwcc logs</code>
  carries the same message.</p>
</div>
<p><a href="/">Current guide</a></p>
</body></html>
"""


def forbidden_page():
    """Signed in, but not an administrator."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Administrators only</title><style>{PAGE_STYLE}</style></head>
<body>
<h1>Administrators only</h1>
<div class="card">
  <p>Your account can upload, review and edit worship guides. This page is
  part of the maintenance side, which is kept to administrators.</p>
</div>
<p><a href="/admin">Back to admin</a> · <a href="/">Current guide</a></p>
</body></html>
"""


def invite_page(token, invite, error=None):
    """Where an account is actually created: the person opening the link
    chooses their own password, so it never passes through whoever invited
    them. The same page, with a reset invite, re-keys an existing account."""
    err = f'<p class="err">{esc(error)}</p>\n' if error else ''
    reset = bool(invite.get('reset'))
    email = invite.get('email') or ''
    heading = 'Choose a new password' if reset else 'Set up your account'
    if email:
        who = (f'<p>For <b>{esc(email)}</b>'
               + ('' if reset else f' — {esc(invite.get("role") or "staff")} access')
               + '.</p>')
        email_field = f'<input type="hidden" name="email" value="{esc(email)}">'
    else:
        who = '<p>This link is not pinned to an address — choose the one you will sign in with.</p>'
        email_field = ('<label class="f">Email\n'
                       '    <input type="email" name="email" required '
                       'autocomplete="username"></label>')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{heading}</title><style>{PAGE_STYLE}
  label.f{{display:block;margin:10px 0;font-size:.9em;color:#54574a}}
  label.f input{{width:100%;max-width:320px;margin-top:3px}}
</style></head>
<body>
<h1>{heading}</h1>
<div class="card">
{who}
{err}<form method="POST" action="/invite/{esc(token)}" id="setpw">
  {email_field}
  <label class="f">Password (at least {lwccauth.MIN_PASSWORD} characters)
    <input type="password" name="password" id="pw" autofocus required
           minlength="{lwccauth.MIN_PASSWORD}" autocomplete="new-password"></label>
  <label class="f">Repeat it
    <input type="password" name="confirm" id="pw2" required
           autocomplete="new-password"></label>
  <p><button>{'Change password' if reset else 'Create account'} and sign in</button></p>
  <p><small style="color:#54574a">This link works once and expires seven days
  after it was issued.</small></p>
</form>
</div>
<script>
const _f = document.getElementById('setpw');
_f.addEventListener('submit', e => {{
  if (document.getElementById('pw').value !== document.getElementById('pw2').value) {{
    e.preventDefault();
    alert('The two passwords do not match.');
  }}
}});
</script>
</body></html>
"""


def users_page(user):
    """/admin/users — admins only. Invites, resets, removals and the pending
    links, all minted server-side so the issuer never handles a password."""
    rows = []
    for u in lwccauth.list_users():
        me = u['email'] == user['email']
        actions = (
            f'<button class="mini" data-op="reset" data-email="{esc(u["email"])}">'
            f'Send reset link</button>')
        if not me:
            actions += (f'<button class="mini del" data-op="remove" '
                        f'data-email="{esc(u["email"])}">Remove</button>')
        rows.append(
            f'<tr><td class="st">{esc(u["email"])}'
            + (' <small>(you)</small>' if me else '') +
            f'</td><td>{esc(u.get("role") or "staff")}</td>'
            f'<td class="st">{esc(fmt_at(u.get("created")))}</td>'
            f'<td class="st">{u.get("sessions", 0)}</td>'
            f'<td class="acts">{actions}</td></tr>')
    pending = []
    for i in lwccauth.list_invites():
        kind = 'password reset' if i.get('reset') else f'invite ({esc(i.get("role") or "staff")})'
        pending.append(
            f'<li style="margin:8px 0"><b>{esc(i.get("email") or "(open link)")}</b> '
            f'— {kind}, issued {esc(fmt_at(i.get("created")))}<br>'
            f'<code class="link">{esc(lwccauth.invite_url(i["token"]))}</code> '
            f'<button class="mini" data-op="revoke" data-token="{esc(i["token"])}">'
            f'Revoke</button></li>')
    pending_html = ('<ul style="list-style:none;padding-left:0">'
                    + ''.join(pending) + '</ul>') if pending else         '<p><small style="color:#54574a">No links waiting to be used.</small></p>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>People</title><style>{PAGE_STYLE}
  body{{max-width:820px}}
  table{{width:100%;border-collapse:collapse;font-size:.95em}}
  td,th{{padding:6px 8px;border-bottom:1px solid #d8d6c7;text-align:left;vertical-align:top}}
  .st{{white-space:nowrap}}
  td.acts{{text-align:right;white-space:nowrap}}
  button.mini{{padding:3px 10px;font-size:.8em;margin-left:6px;background:#3f6b82}}
  button.mini.del{{background:#a20816}}
  code.link{{word-break:break-all;font-size:.8em}}
  label.f{{display:inline-block;margin:6px 12px 6px 0;font-size:.9em;color:#54574a}}
  select{{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid #d8d6c7}}
  #newlink{{margin-top:10px}}
</style></head>
<body>
<h1>People</h1>
<div class="card">
  <p><b>Invite someone</b> — they get a one-time link, choose their own
  password, and the account exists from that moment. Nobody but them ever
  types it.</p>
  <p><label class="f">Email <input type="email" id="email"
       placeholder="office@example.com"></label>
     <label class="f">Access <select id="role">
       <option value="staff">Staff — upload, review, edit</option>
       <option value="admin">Admin — also maintenance and this page</option>
     </select></label>
     <button id="invite">Create invite link</button></p>
  <div id="newlink"></div>
</div>
<div class="card">
  <p><b>Accounts</b> — removing one signs it out of every browser
  immediately. A reset link does the same to that account's other sessions
  once it is used.</p>
  <div style="overflow-x:auto"><table><thead><tr><th>Email</th><th>Access</th>
    <th>Created</th><th>Sessions</th><th></th></tr></thead>
  <tbody>{''.join(rows)}</tbody></table></div>
</div>
<div class="card">
  <p><b>Links waiting to be used</b> — each works once, for seven days.</p>
  {pending_html}
</div>
<p><a href="/admin">Back to admin</a> · <a href="/admin/logout">Sign out</a></p>
<script>
const $ = id => document.getElementById(id);
const escHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

async function users(op, extra) {{
  const res = await fetch('/api/users', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(Object.assign({{op: op}}, extra || {{}})),
  }});
  if (res.status === 401) {{ location.reload(); return null; }}
  const data = await res.json().catch(() => ({{ok: false, error: res.statusText}}));
  if (!data.ok) throw new Error(data.error || 'failed');
  return data;
}}

$('invite').addEventListener('click', async () => {{
  const email = $('email').value.trim();
  if (!email) {{ alert('An email address, please.'); return; }}
  $('invite').disabled = true;
  try {{
    const data = await users('invite', {{email: email, role: $('role').value}});
    if (!data) return;
    $('newlink').innerHTML =
      '<p class="ok">Link for ' + escHtml(email) + ' — copy it to them; it ' +
      'works once.</p><p><code class="link">' + escHtml(data.url) + '</code></p>';
    $('email').value = '';
  }} catch (e) {{
    alert('Could not create the invite: ' + e.message);
  }} finally {{
    $('invite').disabled = false;
  }}
}});

for (const btn of document.querySelectorAll('button[data-op]')) {{
  btn.addEventListener('click', async () => {{
    const op = btn.dataset.op;
    if (op === 'remove' && !confirm('Remove ' + btn.dataset.email +
        '? They are signed out everywhere straight away.')) return;
    if (op === 'revoke' && !confirm('Revoke this link? It stops working.')) return;
    btn.disabled = true;
    try {{
      const data = await users(op, op === 'revoke'
        ? {{token: btn.dataset.token}} : {{email: btn.dataset.email}});
      if (!data) return;
      if (op === 'reset') {{
        $('newlink').innerHTML =
          '<p class="ok">Password-reset link for ' + escHtml(btn.dataset.email) +
          ' — it works once, and signs their other browsers out.</p>' +
          '<p><code class="link">' + escHtml(data.url) + '</code></p>';
        window.scrollTo(0, 0);
        btn.disabled = false;
        return;
      }}
      location.reload();
    }} catch (e) {{
      alert('Failed: ' + e.message);
      btn.disabled = false;
    }}
  }});
}}
</script>
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
__FAILED__
__REVIEW__
__HISTORY__
<p><a href="/">Current guide</a> · <a href="/archive">Archive</a> ·
   <a href="/admin/history">Upload history</a>__NAV__ ·
   <a href="/admin/logout">Sign out (__WHO__)</a></p>
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
const _sw = document.getElementById('sweepstatus');
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
    // Each re-convert batch carries its own server-side meter (total, done,
    // failed, cancelled), so every browser sees the same fraction — no
    // client-side bookkeeping, and plain uploads never skew it.
    const meters = (qs.batches || []).map(b =>
      (b.merge ? 'Refreshing (keep edits)' : 'Re-converting') + ': ' +
      b.done + ' of ' + b.total + ' done' +
      (b.failed ? ', ' + b.failed + ' failed' : '') +
      (b.cancelled ? ', ' + b.cancelled + ' cancelled' : '') +
      ' <progress max="' + b.total + '" value="' + b.done + '"></progress> '
    ).join('');
    const detail = qs.waiting + ' file' + (qs.waiting === 1 ? '' : 's') + ' waiting' +
      (conv.length ? ', converting ' +
        conv.map(c => '<b>' + escHtml(c.file || '…') + '</b>').join(', ') : '');
    _qb.innerHTML = meters + detail;
    if (_sw) {
      _sw.hidden = false;
      _sw.innerHTML = 'Server queue: ' + meters + detail +
        ' — this page reloads when the queue empties.';
    }
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

for (const _id of ['reconvertall', 'reconverteverything', 'refresheverything']) {
  const _btn = document.getElementById(_id);
  if (!_btn) continue;
  const _merge = _id === 'refresheverything';
  _btn.addEventListener('click', async () => {
    const dates = _btn.dataset.dates.split(' ').filter(Boolean);
    const ask = _merge
      ? 'Re-convert ' + dates.length + ' Sundays from their stored PDFs, merging? ' +
        'Hand-edits are kept; unedited text gains the latest markup and accent colors.'
      : 'Re-convert ' + dates.length + ' Sundays from their stored PDFs? ' +
        'Hand-edits to them will be overwritten.';
    if (!confirm(ask)) return;
    const undo = setBusy(_btn, 'Queueing ' + dates.length + ' re-conversions…');
    try {
      const res = await fetch('/api/reconvert-batch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(_merge ? {dates: dates, merge: true} : {dates: dates}),
      });
      const data = await res.json().catch(() => ({ok: false}));
      if (!data.ok) throw new Error(data.error || res.statusText);
      // The server queue takes it from here — it registered the batch's
      // meter, so the banner and the sweep-card status show live progress
      // in any browser, and this page can be closed.
      location.reload();
    } catch (e) {
      alert('Could not queue re-conversions: ' + e.message);
      undo();
    }
  });
}

const _ra = document.getElementById('rerenderall');
if (_ra) _ra.addEventListener('click', async () => {
  const undo = setBusy(_ra, 'Re-rendering every Sunday…');
  try {
    const res = await fetch('/api/rerender-all', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: '{}'});
    const data = await res.json().catch(() => ({ok: false}));
    if (!data.ok) throw new Error(data.error || res.statusText);
    const bad = data.failed ? Object.keys(data.failed) : [];
    alert(data.rendered + ' Sunday(s) re-rendered with the current template.' +
          (bad.length ? ' Failed: ' + bad.join(', ') : ''));
    location.reload();
  } catch (e) {
    alert('Re-render failed: ' + e.message);
    undo();
  }
});

async function retryFailed(btn) {
  const name = btn.dataset.name;
  const date = btn.parentElement.querySelector('.retrydate').value;
  const undo = setBusy(btn, 'Retrying…');
  let res;
  try {
    res = await fetch('/api/retry', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(date ? {name: name, date: date} : {name: name}),
    });
  } catch (e) { undo(); alert('Request failed: ' + e.message); return; }
  if (res.status === 401) { location.reload(); return; }
  const data = await res.json().catch(() => ({ok: false, error: res.statusText}));
  if (!data.ok) { alert(data.error || 'failed'); undo(); return; }
  location.reload();
}

// The clicked button carries the live state: it shows a running label and a
// spinner until the action settles (the page reloads on success, the label
// comes back on failure), so a table full of identical buttons never leaves
// you guessing which one is working.
const RUNNING = {rerender: 'Rendering…', reconvert: 'Re-converting…',
                 'reconvert-merge': 'Merging…', unpublish: 'Unpublishing…',
                 review: 'Saving…'};

function setBusy(btn, label) {
  const was = btn.textContent;
  btn.disabled = true;
  btn.classList.add('busy');
  if (label) btn.textContent = label;
  return () => {                      // undo — call when the action fails
    btn.disabled = false;
    btn.classList.remove('busy');
    btn.textContent = was;
  };
}

async function adminAction(btn, action, date) {
  if (action === 'unpublish' && !confirm('Unpublish ' + date + '? The folder is set aside, not deleted.')) return;
  if (action === 'reconvert' && !confirm('Re-convert ' + date + ' from its stored PDF? Hand-edits to this Sunday will be overwritten.')) return;
  if (action === 'reconvert-merge' && !confirm('Re-convert ' + date + ' from its stored PDF and merge? Hand-edits are kept; unedited text gains the latest markup and accent colors.')) return;
  const undo = setBusy(btn, RUNNING[action] || 'Working…');
  let res;
  try {
    res = await fetch('/api/' + action, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({date}),
    });
  } catch (e) { undo(); alert('Request failed: ' + e.message); return; }
  if (res.status === 401) { location.reload(); return; }  // cookie expired -> sign-in page
  const data = await res.json().catch(() => ({ok: false, error: res.statusText}));
  if (!data.ok) { undo(); alert(data.error || 'failed'); return; }
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


def manage_html(user):
    """Server-rendered management panel for /admin: Sundays needing review
    (with their warnings and a Mark-reviewed action) plus a compact list of
    everything published with re-render/unpublish actions.

    The maintenance actions are admin-only, so staff are never handed a button
    the API would refuse: the buttons and their bulk sweeps simply are not in
    the HTML. ADMIN_ONLY_ACTIONS is the enforcement; this is the courtesy."""
    admin = is_admin(user)
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
                f'<button class="mini" onclick="adminAction(this, \'review\', \'{d}\')">'
                f'Mark reviewed</button>'
                f'<ul style="margin:4px 0 0;padding-left:18px">{warns}</ul></li>')
        bulk = sorted((d for d, _ in flagged
                       if os.path.exists(os.path.join(PUBLIC, d, 'source.pdf'))),
                      reverse=True)
        bulk_html = ''
        if bulk and admin:
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
        acts = f'<a class="minilink" href="/admin/edit/{d}">Edit</a>'
        if admin:
            acts += (f'<button class="mini" onclick="adminAction(this, \'rerender\', '
                     f'\'{d}\')">Re-render</button>')
            if os.path.exists(os.path.join(PUBLIC, d, 'source.pdf')):
                acts += (f'<button class="mini" onclick="adminAction(this, '
                         f'\'reconvert-merge\', \'{d}\')">Re-convert, keep '
                         f'edits</button>'
                         f'<button class="mini" onclick="adminAction(this, '
                         f'\'reconvert\', \'{d}\')">Re-convert</button>')
            acts += (f'<button class="mini" onclick="adminAction(this, \'unpublish\', '
                     f'\'{d}\')">Unpublish</button>')
        rows.append(
            f'<tr><td class="st"><a href="/{d}/">{d}</a></td>'
            f'<td>{title}</td>'
            f'<td class="acts">{acts}</td></tr>')
    src_dates = [d for d in dates
                 if os.path.exists(os.path.join(PUBLIC, d, 'source.pdf'))]
    sweep = ''
    if dates and admin:
        sweep += (f'<p><button class="mini" id="rerenderall">Re-render every '
                  f'Sunday ({len(dates)})</button> — rebuilds each page from '
                  f'its stored guide.json with the current template, so a '
                  f'template or styling upgrade reaches the whole backlog '
                  f'without re-converting; hand-edits are kept.</p>')
    if src_dates and admin:
        sweep += (f'<p><button class="mini" id="refresheverything"{busy} '
                  f'data-dates="{" ".join(src_dates)}">Refresh every Sunday, '
                  f'keep edits ({len(src_dates)})</button> — merge re-convert '
                  f'through the server queue: hand-edits kept, unedited text '
                  f'gains the converter&#8217;s latest markup and accent '
                  f'colors.</p>'
                  f'<p><button class="mini" id="reconverteverything"{busy} '
                  f'data-dates="{" ".join(src_dates)}">Re-convert every Sunday '
                  f'({len(src_dates)})</button> — full sweep through the server '
                  f'queue after a converter fix, flagged or not '
                  f'(discards hand-edits).</p>'
                  f'<p class="warn" id="sweepstatus" hidden></p>')
    blurb = ('<p><b>Published Sundays</b> — re-render rebuilds the page from '
             'its guide.json (after hand-edits); re-convert re-runs the '
             'converter on the stored source PDF (picks up parser upgrades, '
             'discards hand-edits); re-convert-keep-edits does the same but '
             'merges: hand-edits win, everything else gains the latest markup '
             'and colors; unpublish sets the folder aside without deleting '
             'it.</p>') if admin else \
        ('<p><b>Published Sundays</b> — open one to edit its text. Re-render, '
         're-convert and unpublish are administrator tools; ask an '
         'administrator if a Sunday needs one.</p>')
    out.append('<div class="card">' + blurb
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
        when = esc(fmt_at(e.get('at'))).replace('T', '<br>', 1)
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
        if e.get('merge'):
            status_html += ' <span class="ok">(merged — hand-edits kept)</span>'
        if e.get('dateOverride') and not e.get('merge'):
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


def batch_meter_html(b):
    """One re-convert batch's meter for the queue banner (the JS poll renders
    the same shape client-side — keep them in step)."""
    label = 'Refreshing (keep edits)' if b.get('merge') else 'Re-converting'
    extra = ((f', {b["failed"]} failed' if b.get('failed') else '')
             + (f', {b["cancelled"]} cancelled' if b.get('cancelled') else ''))
    return (f'{label}: {b["done"]} of {b["total"]} done{extra} '
            f'<progress max="{b["total"]}" value="{b["done"]}"></progress> ')


def recent_uploads_html(user):
    """Compact last-few-uploads card for the bottom of /admin — the batch
    results table in the upload card is per-visit, this one survives leaving
    the page. When the server queue is still working, say so (this page
    reloads fresh when it empties)."""
    entries = upload_history(limit=8)
    snap = queue_snapshot()
    active = ''
    if snap['waiting'] or snap['converting']:
        now = ''
        if snap['converting']:
            names = ', '.join(f"<b>{esc(c.get('file') or '…')}</b>"
                              for c in snap['converting'])
            now = f', converting {names}'
        meters = ''.join(batch_meter_html(b) for b in snap['batches'])
        cancel = ('<button class="mini" id="clearreconverts">Cancel pending '
                  're-conversions</button>' if is_admin(user) else '')
        active = (f'<p class="warn"><b>Server queue active:</b> '
                  f'<span id="queuebanner">{meters}{snap["waiting"]} '
                  f'file{"s" if snap["waiting"] != 1 else ""} '
                  f'waiting{now}</span> — updates live; finished results appear '
                  f'below and in the history, and this page reloads when the '
                  f'queue empties. {cancel}</p>')
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
  .found{outline:3px solid #8a6410;background:#fdf6df}
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
// Heading accent (detected from the printed ink; '' = site default)
// Accent input: an exact ink like #e36c0a (as printed), a legacy palette
// name (maroon/gold/green/blue/purple), or blank for the site default.
const accentSel = (obj, key = 'color', label = 'Heading accent (#rrggbb or blank)') =>
  el('label', {class: 'f'}, label,
    el('input', {type: 'text', value: obj[key] || '', placeholder: '#e36c0a',
                 oninput: e => obj[key] = e.target.value.trim() || null}));
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
    (row, pr) => row.append(txt(pr, 'name', 'Name (optional)'),
      accentSel(pr, 'nameColor', 'Name accent (#rrggbb or blank)'),
      txt(pr, 'text', 'Text', 'ta')),
    () => ({name: '', text: '', nameColor: null})));

  G.announcements = G.announcements || [];
  f.append(listSection('Notes & Announcements', G.announcements,
    (row, a) => row.append(txt(a, 'heading', 'Heading'),
      el('label', {class: 'f'}, 'Kind ',
        el('select', {onchange: e => a.kind = e.target.value},
          ...['note', 'attendance'].map(k =>
            el('option', {value: k, ...(a.kind === k ? {selected: ''} : {})}, k)))),
      accentSel(a),
      txt(a, 'text', 'Text', 'ta')),
    () => ({heading: '', text: '', kind: 'note', color: null})));

  G.specialEvents = G.specialEvents || [];
  f.append(listSection('Special Events', G.specialEvents,
    (row, ev) => {
      row.append(txt(ev, 'heading', 'Heading'), txt(ev, 'sectionTitle', 'Section title'),
                 accentSel(ev));
      const parea = el('textarea', {oninput: e =>
        ev.paragraphs = e.target.value.split(/\n\s*\n/).filter(p => p.trim())},
        (ev.paragraphs || []).join('\n\n'));
      row.append(el('label', {class: 'f'}, 'Paragraphs (blank line between)', parea),
                 txt(ev, 'note', 'Italic footnote'));
    },
    () => ({heading: '', paragraphs: [], note: '', sectionTitle: 'Coming Up',
            color: null})));

  G.journal = G.journal || {subtitle: '', morning: '', midday: '', evening: ''};
  const jfs = el('fieldset', {}, el('legend', {}, 'Prayer Journal'));
  jfs.append(txt(G.journal, 'subtitle', 'Subtitle'),
             txt(G.journal, 'morning', 'Household Prayer: Morning', 'ta'),
             txt(G.journal, 'midday', 'Midday note', 'ta'),
             txt(G.journal, 'evening', 'Household Prayer: Evening', 'ta'));
  f.append(jfs);
}
buildForm();

// #find=<quoted text> (from the AI scan pages' edit-by-hand links): locate
// the field holding that text, scroll to it, and highlight it. Matching
// mirrors the scanner's canonical form — tags stripped, entities and
// typographic quotes/dashes normalized, whitespace collapsed, case ignored —
// with shorter prefixes as fallback for quotes that span two blocks.
const canon = s => String(s == null ? '' : s)
  .replace(/<[^>]+>/g, '')
  .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/[‘’]/g, "'").replace(/[“”]/g, '"')
  .replace(/[–—−]/g, '-').replace(/ /g, ' ')
  .replace(/…/g, '...')
  .replace(/\s+/g, ' ').trim().toLowerCase();

(function jumpToFind() {
  const m = location.hash.match(/^#find=(.*)$/);
  if (!m) return;
  let target = '';
  try { target = canon(decodeURIComponent(m[1])); } catch (e) { return; }
  if (!target) return;
  const fields = [...document.querySelectorAll(
    '#form input[type=text], #form textarea')];
  const words = target.split(' ');
  const probes = [target,
                  words.slice(0, 8).join(' '),
                  words.slice(0, 4).join(' ')];
  let hit = null;
  for (const p of probes) {
    hit = fields.find(f => canon(f.value).includes(p));
    if (hit) break;
  }
  if (!hit) {
    $('msg').textContent =
      'Quoted text not found in any field — it may have been edited since the scan.';
    return;
  }
  hit.classList.add('found');
  hit.scrollIntoView({block: 'center'});
  hit.focus({preventScroll: true});
})();

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

    # -- accounts and sessions ----------------------------------------------

    def session_id(self):
        """The cookie's session id — a random string that means nothing on its
        own; the account it belongs to is looked up server-side, so signing
        out or removing a user takes effect at once."""
        try:
            jar = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
        except http.cookies.CookieError:
            return ''
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else ''

    def current_user(self):
        return lwccauth.session_user(self.session_id())

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

    def require_user(self):
        """Gate for every admin page: the signed-in account, or None once the
        sign-in page has been sent."""
        user = self.current_user()
        if user:
            return user
        self.send_page(login_page(self.path.split('?', 1)[0]), status=401,
                       cache='no-store')
        return None

    def require_admin(self):
        """As require_user, plus the admin role — the maintenance tools and
        the people list. Pinning admin to a single address would be a change
        to is_admin and nothing else."""
        user = self.require_user()
        if user is None or is_admin(user):
            return user
        self.send_page(forbidden_page(), status=403, cache='no-store')
        return None

    def api_user(self):
        """The signed-in account for a JSON endpoint, or None once the 401
        has been sent. There is no API token: a session or nothing."""
        user = self.current_user()
        if not user:
            self.send_json({'ok': False, 'error': 'not signed in'}, status=401)
        return user

    def handle_login(self, body):
        form = urllib.parse.parse_qs(body.decode('utf-8', 'replace'))
        email = lwccauth.norm_email((form.get('email') or [''])[0])
        password = (form.get('password') or [''])[0]
        nxt = (form.get('next') or [''])[0]
        if not ADMIN_NEXT_RE.fullmatch(nxt):
            nxt = '/admin'
        sid, user = (lwccauth.login(email, password) if email and password
                     else (None, None))
        if not user:
            # The whole of the rate limiting: enough to make a password
            # guessing run through this form pointless, cheap enough that a
            # mistyped password is barely noticeable. The attempted address
            # goes to uploads.log so a run is visible afterwards.
            time.sleep(0.5)
            # The attempted address is recorded so a guessing run is visible
            # afterwards. It is bounded first: this is attacker-supplied text
            # going into a log nothing rotates, and a password is not even
            # needed to reach this line, so an unbounded address here is a
            # few kilobytes of disk per unauthenticated request.
            audit_log({'action': 'login', 'ok': False,
                       **({'email': email[:lwccauth.MAX_EMAIL]} if email else {})})
            self.send_page(login_page(nxt, error='Wrong email or password.',
                                      email=email), status=401, cache='no-store')
            return
        audit_log({'action': 'login', 'ok': True, 'email': user['email']})
        self.redirect_303(nxt, cookie=self.session_cookie(sid, COOKIE_MAX_AGE))

    def handle_invite(self, token, body):
        """Redeem a one-time link: the password is chosen here and nowhere
        else, and redeeming signs that browser in."""
        invite = lwccauth.get_invite(token)
        if not invite:
            self.send_page(gone_page(), status=410, cache='no-store')
            return
        form = urllib.parse.parse_qs(body.decode('utf-8', 'replace'))
        try:
            sid, user = lwccauth.redeem_invite(
                token, (form.get('password') or [''])[0],
                (form.get('confirm') or [''])[0],
                (form.get('email') or [''])[0])
        except lwccauth.AuthError as e:
            if e.status == 410:
                self.send_page(gone_page(), status=410, cache='no-store')
                return
            self.send_page(invite_page(token, invite, error=str(e)),
                           status=e.status, cache='no-store')
            return
        audit_log({'action': 'invite-redeem', 'ok': True, 'email': user['email'],
                   **({'reset': True} if invite.get('reset') else
                      {'role': user.get('role')})})
        self.redirect_303('/admin', cookie=self.session_cookie(sid, COOKIE_MAX_AGE))

    def handle_users(self, user, body):
        """/api/users — invite, reset, remove, revoke. Admin-only (the gate is
        ADMIN_ONLY_ACTIONS); every reply that carries a link carries no
        password, because there is never one to carry."""
        try:
            data = json.loads(body or b'{}')
        except ValueError:
            self.send_json({'ok': False, 'error': 'invalid JSON body'}, status=400)
            return
        op = str(data.get('op') or '')
        email = lwccauth.norm_email(data.get('email'))
        try:
            if op in ('invite', 'reset'):
                role = str(data.get('role') or 'staff')
                inv = lwccauth.create_invite(email, role=role, reset=op == 'reset')
                audit_log({'action': f'user-{op}', 'ok': True, 'email': email,
                           'by': user['email'],
                           **({'role': inv['role']} if op == 'invite' else {})})
                self.send_json({'ok': True, 'email': email, 'url': inv['url'],
                                'token': inv['token']})
                return
            if op == 'remove':
                lwccauth.remove_user(email, by=user['email'])
                audit_log({'action': 'user-remove', 'ok': True, 'email': email,
                           'by': user['email']})
                self.send_json({'ok': True, 'email': email})
                return
            if op == 'revoke':
                lwccauth.revoke_invite(str(data.get('token') or ''))
                audit_log({'action': 'invite-revoke', 'ok': True,
                           'by': user['email']})
                self.send_json({'ok': True})
                return
        except lwccauth.AuthError as e:
            self.send_json({'ok': False, 'error': str(e)}, status=e.status)
            return
        self.send_json({'ok': False, 'error':
                        'op must be invite, reset, remove or revoke'}, status=400)

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        try:
            self.route_get()
        except lwccauth.StoreError as e:
            self.store_error(e)

    def do_POST(self):
        try:
            self.route_post()
        except lwccauth.StoreError as e:
            self.store_error(e)

    def store_error(self, e):
        """A damaged account store, answered in the shape the caller expects.
        Raised before any route has replied — every path that touches the
        store reads it before it writes anything back."""
        sys.stderr.write(f'ACCOUNT STORE UNREADABLE: {e.detail} — '
                         f'users.json left untouched; repair it by hand\n')
        if self.path.startswith('/api/'):
            self.send_json({'ok': False, 'error': str(e)}, status=503)
        else:
            self.send_page(store_error_page(e.detail), status=503,
                           cache='no-store')

    def route_get(self):
        path, _, query = self.path.partition('?')
        if path == '/healthz':
            missing = missing_deps()
            body = 'ok\n' if not missing else \
                'degraded: missing ' + ', '.join(missing) + \
                ' — apt-get install -y poppler-utils tesseract-ocr\n'
            self.send_page(body, ctype='text/plain')
            return
        if path == '/api/status':
            if not self.api_user():
                return
            ids = [i for i in urllib.parse.parse_qs(query).get('ids', [''])[0].split(',') if i]
            with JOBS_LOCK:
                jobs = {i: {k: v for k, v in JOBS.get(i, {'status': 'unknown'}).items()
                            if k not in ('path', 'marker')} for i in ids}
            self.send_json({'ok': True, 'jobs': jobs, 'queue': queue_snapshot()})
            return
        if path == '/archive':
            self.send_page(archive_page())
            return
        if path == '/search':
            q = urllib.parse.parse_qs(query).get('q', [''])[0]
            self.send_page(search_page(q))
            return
        if path == '/admin':
            user = self.require_user()
            if user:
                nav = (' · <a href="/admin/users">People</a>'
                       if is_admin(user) else '')
                self.send_page(ADMIN_PAGE
                               .replace('__FAILED__', failed_uploads_html())
                               .replace('__REVIEW__', manage_html(user))
                               .replace('__HISTORY__', recent_uploads_html(user))
                               .replace('__NAV__', nav)
                               .replace('__WHO__', esc(user['email'])),
                               cache='no-store')
            return
        if path == '/admin/history':
            if self.require_user():
                self.send_page(history_page(query), cache='no-store')
            return
        if path == '/admin/users':
            user = self.require_admin()
            if user:
                self.send_page(users_page(user), cache='no-store')
            return
        if path == '/admin/logout':
            # Server-side: the session is gone for every browser holding it,
            # not merely forgotten by this one.
            lwccauth.destroy_session(self.session_id())
            self.redirect_303('/', cookie=self.session_cookie('', 0))
            return
        m = re.fullmatch(r'/admin/edit/(\d{4}-\d{2}-\d{2})', path)
        if m:
            if not self.require_user():
                return
            if os.path.exists(os.path.join(PUBLIC, m.group(1), 'guide.json')):
                self.send_page(edit_page(m.group(1)), cache='no-store')
            else:
                self.send_error(404, 'Not Found')
            return
        m = re.fullmatch(r'/invite/([A-Za-z0-9_-]{8,128})', path)
        if m:
            invite = lwccauth.get_invite(m.group(1))
            if invite:
                self.send_page(invite_page(m.group(1), invite), cache='no-store')
            else:
                self.send_page(gone_page(), status=410, cache='no-store')
            return
        if path == '/':
            dates = published_dates()
            if dates:
                self.send_page(guide_with_nav(dates[0]))
                return
        # Deny by the name the static handler will actually resolve, not by
        # the one in the request line. SimpleHTTPRequestHandler percent-
        # decodes and normalizes before it opens a file, so a rule matched
        # against the raw target is one that %69 steps around.
        try:
            served = urllib.parse.unquote(path, errors='surrogatepass')
        except UnicodeDecodeError:
            served = urllib.parse.unquote(path)
        served = posixpath.normpath(served)
        # A published Sunday's directory IS the static root, so a scanner
        # artifact left in one from before the scanner was stood down is
        # served to anyone who asks for it by name — the routes being gone
        # is not the same as the scanner being unreachable.
        if '/.' in path or '/.' in served or served.endswith('/aiscan.json'):
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

    def route_post(self):
        # One-shot handling: read the (bounded) body before any error reply,
        # otherwise the client hits a broken pipe mid-upload and never sees it.
        # The bound is per-route and is applied before the read, so an
        # oversized body is refused rather than held.
        self.close_connection = True
        path, _, query = self.path.partition('?')
        limit = MAX_AUTH_BODY if AUTH_POST_RE.fullmatch(path) else MAX_UPLOAD
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if length < 0 or length > limit:
            self.send_json({'ok': False, 'error': f'body must be 1..{limit} bytes'},
                           status=413)
            return
        body = self.rfile.read(length)
        if path == '/admin/login':
            self.handle_login(body)
            return
        m = re.fullmatch(r'/invite/([A-Za-z0-9_-]{8,128})', path)
        if m:
            self.handle_invite(m.group(1), body)
            return
        if path not in ('/api/upload', '/api/retry', '/api/review', '/api/rerender',
                        '/api/rerender-all',
                        '/api/reconvert', '/api/reconvert-merge',
                        '/api/reconvert-batch',
                        '/api/reconvert-clear', '/api/unpublish', '/api/save',
                        '/api/users'):
            self.send_json({'ok': False, 'error': 'not found'}, status=404)
            return
        user = self.api_user()
        if not user:
            return
        if path in ADMIN_ONLY_ACTIONS and not is_admin(user):
            self.send_json({'ok': False, 'error': 'admin role required'}, status=403)
            return
        if path == '/api/users':
            self.handle_users(user, body)
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
                        if j.get('marker'):
                            try:         # cancelled: durable marker done too
                                os.unlink(j['marker'])
                            except OSError:
                                pass
                        b = BATCHES.get(j.get('batch'))
                        if b:            # cancelled jobs settle their meter
                            b['done'] += 1
                            b['cancelled'] += 1
            audit_log({'action': 'reconvert-clear', 'ok': True, 'cleared': cleared})
            self.send_json({'ok': True, 'cleared': cleared})
            return
        if path == '/api/rerender-all':
            # Rebuild every published page from its guide.json with the
            # current template — no PDFs touched, hand-edits kept.
            rendered, failed = rerender_all()
            audit_log({'action': 'rerender-all', 'ok': not failed,
                       'rendered': len(rendered),
                       **({'failed': failed} if failed else {})})
            self.send_json({'ok': True, 'rendered': len(rendered),
                            **({'failed': failed} if failed else {})})
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
            merge = bool(data.get('merge'))
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
            queued.append(date)
        # The batch meter must exist before its first job can finish, so the
        # batch is registered up front and the jobs enqueued after.
        bid = None
        if queued:
            bid = (now_pacific().strftime('%Y%m%d%H%M%S')
                   + '-' + os.urandom(4).hex())
            with JOBS_LOCK:
                BATCHES[bid] = {'total': len(queued), 'done': 0, 'failed': 0,
                                'cancelled': 0,
                                **({'merge': True} if merge else {})}
        os.makedirs(RECONVERT_QUEUE_DIR, exist_ok=True)
        for date in queued:
            jid = (now_pacific().strftime('%Y%m%d%H%M%S')
                   + '-' + os.urandom(4).hex())
            # A durable marker per queued Sunday: deleted when its job
            # settles, rescanned on startup so a restart resumes the batch
            # instead of losing it.
            marker = os.path.join(RECONVERT_QUEUE_DIR, date)
            with open(marker, 'w', encoding='utf-8') as fh:
                json.dump({'merge': merge}, fh)
            # Merge jobs pin the date so the refresh lands on this Sunday's
            # guide even if the parser would read the date differently.
            job_update(jid, status='queued', file=f'{date}/source.pdf',
                       path=os.path.join(PUBLIC, date, 'source.pdf'),
                       keep=True, batch=bid, marker=marker,
                       **({'merge': True, 'dateOverride': date} if merge else {}))
            CONVERT_Q.put(jid)
        audit_log({'action': 'reconvert-batch', 'ok': True,
                   **({'merge': True} if merge else {}),
                   'queued': len(queued),
                   **({'skipped': skipped} if skipped else {}),
                   **({'alreadyQueued': len(already)} if already else {})})
        self.send_json({'ok': True, 'queued': len(queued), 'skipped': skipped,
                        'alreadyQueued': already})

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
            if action in ('reconvert', 'reconvert-merge'):
                merge = action == 'reconvert-merge'
                src = os.path.join(PUBLIC, date, 'source.pdf')
                if not os.path.exists(src):
                    self.send_json({'ok': False, 'error':
                                    'no stored source PDF for this Sunday — it was '
                                    'uploaded before retention; re-upload it once'},
                                   status=404)
                    return
                # Merges pin the date so the refresh lands on this Sunday's
                # guide even if the parser would read the date differently.
                guide, replaced = convert_pdf(src, date if merge else None,
                                              f'{date}/source.pdf', keep_edits=merge)
                # No 'action' key: reconversions are conversions, so they
                # belong in the /admin/history record.
                audit_log({'ok': True, 'file': f'{date}/source.pdf',
                           'reconvert': True,
                           **({'merge': True} if merge else {}),
                           'dateISO': guide['dateISO'],
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
    rescan_reconverts()
    workers = convert_workers()
    for i in range(workers):
        threading.Thread(target=convert_worker, daemon=True,
                         name=f'convert-worker-{i + 1}').start()
    print(f'conversion workers: {workers} '
          f'(set CONVERT_WORKERS in .env to override)', flush=True)
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    accounts, broken = None, None
    try:
        accounts = len(lwccauth.list_users())
    except lwccauth.StoreError as exc:
        broken = exc.detail
    who = 'ACCOUNT STORE UNREADABLE' if broken else f'{accounts} account(s)'
    print(f'lwcc serving {PUBLIC} on http://{args.host}:{args.port} — {who}',
          flush=True)
    if broken:
        # Serve anyway: the admin area answers 503 with the reason, which is
        # how the operator finds out. Refusing to boot would take the site's
        # public guides down over a file only /admin needs.
        print(f'users.json is present and not valid JSON ({broken}) — nobody '
              f'can sign in. Nothing has been written to it; repair the file '
              f'by hand and every account comes back.', flush=True)
    if accounts == 0:
        # Fail closed, deliberately: there is no bootstrap password, so an
        # app with no accounts admits nobody until an invite is minted here.
        print('NO ACCOUNTS YET — run `lwcc invite --admin --for you@example.com` '
              'and open the link it prints', flush=True)
    missing = missing_deps()
    if missing:
        print(f"WARNING: missing converter deps: {', '.join(missing)} — "
              'uploads will fail until: apt-get install -y poppler-utils tesseract-ocr',
              flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()

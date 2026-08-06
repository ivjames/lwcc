"""End-to-end test of the site app: start it on a scratch public/ dir, upload
the sample PDF through /api/upload, and check routing + publishing.

Run:  python3 test/test_app.py
"""
import json
import os
import re
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

# --- AI article scanner: digest, response parsing, and repair application run
# without a key or network (the HTTP transport is injected).
from wgconvert import aiscan  # noqa: E402

_g = {
    'dateISO': '2026-08-02', 'season': None, 'welcome': None,
    'order': [
        {'kind': 'stage', 'text': 'please stand as you are able'},
        {'kind': 'item', 'type': 'plain', 'label': None, 'title': None,
         'titleQuoted': False, 'who': None, 'note': None,
         'body': [{'type': 'para',
                   'text': 'Please remain seated during the postlude.'}]},
        {'kind': 'item', 'type': 'prayer', 'label': 'Blessing', 'title': None,
         'titleQuoted': False, 'who': None, 'note': None,
         'body': [{'type': 'prayer', 'text': 'Go in peace. Amen.'},
                  {'type': 'para',
                   'text': 'The <b>potluck</b> is next Sunday at noon.'}]},
    ],
    'announcements': [
        {'heading': None, 'kind': 'note', 'text': 'LEISURE WORLD COMMUNITY CHURCH'},
        {'heading': 'Concert', 'kind': 'note', 'text': 'Duo concert Sunday at 3:00 PM.'},
    ],
    'specialEvents': [], 'notes': [], 'warnings': [],
}
_digest = aiscan.guide_digest(_g)
assert _digest['order'][1]['i'] == 1 and _digest['announcements'][1]['i'] == 1
assert _digest['order'][2]['body'][1]['j'] == 1


def _canned(findings):
    return {'model': 'claude-opus-5', 'stop_reason': 'end_turn',
            'usage': {'input_tokens': 10, 'output_tokens': 5},
            'content': [{'type': 'text', 'text': json.dumps(
                {'summary': 'reviewed', 'findings': findings})}]}


def _fix(op, **kw):
    return {'op': op, 'orderIndex': None, 'blockIndex': None,
            'annIndex': None, 'eventIndex': None, 'heading': None, **kw}


def _fake_transport(payload, api_key):
    assert api_key == 'test-key'
    assert payload['model'] == 'claude-opus-5'
    assert payload['fallbacks'] == 'default', 'refusal fallback opted in'
    assert payload['output_config']['format']['type'] == 'json_schema'
    assert 'please stand' in payload['messages'][0]['content']
    return _canned([
        {'path': 'order[1]', 'quote': 'Please remain seated',
         'issue': 'untitled item is a page direction', 'current': 'content',
         'proposed': 'stage', 'confidence': 'high',
         'fix': _fix('item_to_stage', orderIndex=1)},
        {'path': 'order[2].body[1]', 'quote': 'The <b>potluck</b> is next Sunday',
         'issue': 'announcement inside the blessing', 'current': 'content',
         'proposed': 'announcement', 'confidence': 'high',
         'fix': _fix('para_to_announcement', orderIndex=2, blockIndex=1,
                     heading='Potluck')},
        {'path': 'announcements[0]', 'quote': 'LEISURE WORLD COMMUNITY CHURCH',
         'issue': 'masthead page furniture', 'current': 'announcement',
         'proposed': 'discard', 'confidence': 'high',
         'fix': _fix('discard_announcement', annIndex=0)},
        {'path': 'announcements[1]', 'quote': 'NOT ACTUALLY IN THE GUIDE',
         'issue': 'stale finding must be skipped', 'current': 'announcement',
         'proposed': 'discard', 'confidence': 'medium',
         'fix': _fix('discard_announcement', annIndex=1)},
        {'path': 'welcome', 'quote': 'something', 'issue': 'flag only',
         'current': 'content', 'proposed': 'announcement', 'confidence': 'low',
         'fix': _fix('none')},
    ])


_scan = aiscan.scan_guide(_g, 'test-key', transport=_fake_transport)
assert [f['id'] for f in _scan['findings']] == ['f1', 'f2', 'f3', 'f4', 'f5']
assert all(f['status'] == 'open' for f in _scan['findings'])
assert _scan['findings'][4]['fix'] is None, 'op "none" becomes flag-only'
_g2, _res = aiscan.apply_findings(_g, _scan['findings'],
                                  ['f1', 'f2', 'f3', 'f4', 'f5'])
assert _res['f1'] is None and _res['f2'] is None and _res['f3'] is None
assert 'no longer exists' in _res['f4'], 'vanished quote refused, not applied'
assert 'no mechanical fix' in _res['f5']
assert _g2['order'][1] == {'kind': 'stage',
                           'text': 'Please remain seated during the postlude.'}
assert [b['text'] for b in _g2['order'][2]['body']] == ['Go in peace. Amen.']
assert [a['heading'] for a in _g2['announcements']] == ['Concert', 'Potluck'], \
    'furniture discarded, potluck moved — text preserved verbatim'
assert _g2['announcements'][1]['text'] == 'The <b>potluck</b> is next Sunday at noon.'
assert _g['order'][1]['kind'] == 'item', 'apply works on a copy'

# verification is typography-tolerant: a quote in plain ASCII matches the
# printed curly quotes/dashes — while the moved text keeps its typography.
_g3 = {'order': [], 'welcome': {'heading': 'Welcome', 'who': None, 'body': [
           {'type': 'para', 'text': 'Coffee with Pastor is Tuesday at 10 AM.'}]},
       'announcements': [{'heading': None, 'kind': 'note',
                          'text': 'Please stand — it’s “time”.'}],
       'specialEvents': []}
_g4, _res = aiscan.apply_findings(_g3, [
    {'id': 'a1', 'quote': 'please stand - it\'s "time"', 'issue': 'x',
     'current': 'announcement', 'proposed': 'stage', 'confidence': 'high',
     'status': 'open', 'fix': _fix('announcement_to_stage', annIndex=0,
                                   orderIndex=0)},
    {'id': 'a2', 'quote': 'Coffee with Pastor', 'issue': 'x',
     'current': 'content', 'proposed': 'announcement', 'confidence': 'high',
     'status': 'open', 'fix': _fix('welcome_to_announcement', blockIndex=0,
                                   heading='Coffee with Pastor')},
], ['a1', 'a2'])
assert _res == {'a1': None, 'a2': None}, _res
assert _g4['order'] == [{'kind': 'stage', 'text': 'Please stand — it’s “time”.'}]
assert _g4['welcome']['body'] == []
assert _g4['announcements'] == [{'heading': 'Coffee with Pastor', 'kind': 'note',
                                 'text': 'Coffee with Pastor is Tuesday at 10 AM.'}]

_g5 = {'order': [], 'announcements': [], 'welcome': None,
       'specialEvents': [{'heading': 'Duo Concert', 'paragraphs': ['At 3 PM.'],
                          'note': None, 'sectionTitle': 'Coming Up'}]}
_g6, _res = aiscan.apply_findings(_g5, [
    {'id': 'e1', 'quote': 'Duo Concert', 'issue': 'x',
     'current': 'announcement', 'proposed': 'announcement',
     'confidence': 'high', 'status': 'open',
     'fix': _fix('event_to_announcement', eventIndex=0)}], ['e1'])
assert _res == {'e1': None} and _g6['specialEvents'] == []
assert _g6['announcements'] == [{'heading': 'Duo Concert', 'kind': 'note',
                                 'text': 'At 3 PM.'}]

# discard_para drops page-furniture body blocks (masthead/letterhead
# residue); an untitled item emptied by the discards is pruned, not left as
# a blank entry.
_g7 = {'order': [
        {'kind': 'item', 'type': 'plain', 'label': None, 'title': None,
         'titleQuoted': False, 'who': None, 'note': None,
         'body': [{'type': 'para',
                   'text': 'Seal Beach, CA 90740-5307 (562) 431-2503'},
                  {'type': 'para', 'text': '« COMMUNITY he CHURCH Word'}]},
        {'kind': 'item', 'type': 'plain', 'label': 'Welcome', 'title': None,
         'titleQuoted': False, 'who': None, 'note': None,
         'body': [{'type': 'para', 'text': 'Good morning and welcome.'}]},
    ], 'announcements': [], 'specialEvents': [], 'welcome': None}
_g8, _res = aiscan.apply_findings(_g7, [
    {'id': 'p1', 'quote': 'Seal Beach, CA 90740-5307', 'issue': 'letterhead',
     'current': 'content', 'proposed': 'discard', 'confidence': 'high',
     'status': 'open',
     'fix': _fix('discard_para', orderIndex=0, blockIndex=0)},
    {'id': 'p2', 'quote': '« COMMUNITY he CHURCH Word',
     'issue': 'banner residue', 'current': 'content', 'proposed': 'discard',
     'confidence': 'high', 'status': 'open',
     'fix': _fix('discard_para', orderIndex=0, blockIndex=1)},
], ['p1', 'p2'])
assert _res == {'p1': None, 'p2': None}, _res
assert len(_g8['order']) == 1 and _g8['order'][0]['label'] == 'Welcome', \
    'furniture dropped and the emptied untitled item pruned'
assert _g8['order'][0]['body'][0]['text'] == 'Good morning and welcome.'

# stale indexes are recoverable: the applier relocates the quoted text and
# applies only when it matches exactly one plausible target — equal matches
# refuse rather than guess.
_g9 = {'order': [], 'welcome': None, 'specialEvents': [],
       'announcements': [
           {'heading': 'New', 'kind': 'note', 'text': 'Inserted since the scan.'},
           {'heading': None, 'kind': 'note',
            'text': 'LEISURE WORLD COMMUNITY CHURCH'}]}
_g10, _res = aiscan.apply_findings(_g9, [
    {'id': 'r1', 'quote': 'LEISURE WORLD COMMUNITY CHURCH', 'issue': 'x',
     'current': 'announcement', 'proposed': 'discard', 'confidence': 'high',
     'status': 'open', 'fix': _fix('discard_announcement', annIndex=0)}],
    ['r1'])
assert _res['r1'] is None, _res
assert [a['heading'] for a in _g10['announcements']] == ['New'], \
    'stale index relocated by quote — the right entry removed'

_g11 = {'order': [], 'welcome': None, 'specialEvents': [],
        'announcements': [{'heading': None, 'kind': 'note', 'text': 'dup'},
                          {'heading': None, 'kind': 'note', 'text': 'dup'},
                          {'heading': None, 'kind': 'note', 'text': 'other'}]}
_g12, _res = aiscan.apply_findings(_g11, [
    {'id': 'r2', 'quote': 'dup', 'issue': 'x', 'current': 'announcement',
     'proposed': 'discard', 'confidence': 'high', 'status': 'open',
     'fix': _fix('discard_announcement', annIndex=2)}], ['r2'])
assert 'ambiguous' in _res['r2'], 'never guesses between equal matches'
assert len(_g12['announcements']) == 3, 'nothing removed on ambiguity'

# a refusal from the safety classifiers is an error, not an empty scan
try:
    aiscan.scan_guide(_g, 'test-key', transport=lambda p, k: {
        'stop_reason': 'refusal', 'content': []})
    raise AssertionError('refusal must raise')
except RuntimeError as e:
    assert 'declined' in str(e)

# --- scripture verse-number agent: a dedicated pass reads the scripture
# section against each passage reference and fixes labeling with markup-only
# moves — wrap a bare verse number in <sup>, unwrap a wrong one. Same
# injected transport, no key or network.
_vg = {
    'dateISO': '2026-08-02', 'season': None, 'welcome': None,
    'order': [
        {'kind': 'stage', 'text': 'please stand as you are able'},
        {'kind': 'item', 'type': 'scripture', 'label': 'Scripture',
         'title': None, 'titleQuoted': False, 'who': None, 'note': None,
         'body': [
            {'type': 'ref', 'text': 'Matthew 14:13-15'},
            {'type': 'verse', 'text':
             '<sup>13</sup> Now when Jesus heard this, he withdrew. '
             '14 When he went ashore, he saw a crowd of 14 people. '
             '<sup>15</sup> When it was evening, at <sup>3</sup>:00 '
             'the disciples came to him.'},
         ]},
    ],
    'announcements': [], 'specialEvents': [], 'notes': [], 'warnings': [],
}
_vd = aiscan.verse_digest(_vg)
assert [it['i'] for it in _vd['scripture']] == [1], 'scripture items only'
assert _vd['scripture'][0]['body'][0]['text'] == 'Matthew 14:13-15'
assert '<sup>13</sup>' in _vd['scripture'][0]['body'][1]['text'], \
    'raw markup reaches the agent'

# a guide with no scripture is skipped without an API call
assert aiscan.scan_verses(_g, 'test-key', transport=lambda p, k: (
    (_ for _ in ()).throw(AssertionError('must not call the API')))) is None


def _vfix(op, **kw):
    return {'op': op, 'orderIndex': None, 'blockIndex': None,
            'number': None, **kw}


def _verse_transport(payload, api_key):
    assert api_key == 'test-key'
    assert payload['model'] == 'claude-opus-5'
    assert payload['fallbacks'] == 'default'
    assert 'verse-number checker' in payload['system']
    assert payload['output_config']['format']['type'] == 'json_schema'
    assert 'Now when Jesus heard' in payload['messages'][0]['content']
    assert 'please stand' not in payload['messages'][0]['content'], \
        'the verse agent reads the scripture section only'
    return _canned([
        {'path': 'order[1].body[1]', 'quote': '14 When he went ashore',
         'issue': 'verse 14 is bare, not superscripted', 'current': 'bare',
         'proposed': 'superscript', 'confidence': 'high',
         'fix': _vfix('sup_verse', orderIndex=1, blockIndex=1, number=14)},
        {'path': 'order[1].body[1]', 'quote': 'at <sup>3</sup>:00',
         'issue': 'a clock time superscripted as if a verse',
         'current': 'superscripted', 'proposed': 'plain',
         'confidence': 'high',
         'fix': _vfix('unsup_verse', orderIndex=1, blockIndex=1, number=3)},
        {'path': 'order[1].body[1]', 'quote': '<sup>15</sup> When it was evening',
         'issue': 'verse 16 dropped by OCR — no markup-only fix',
         'current': 'missing', 'proposed': 'flag', 'confidence': 'low',
         'fix': _vfix('none')},
    ])


_vscan = aiscan.scan_verses(_vg, 'test-key', transport=_verse_transport)
assert [f['id'] for f in _vscan['findings']] == ['v1', 'v2', 'v3'], \
    'verse ids never collide with the classifier pass'
assert _vscan['findings'][2]['fix'] is None, 'missing number is flag-only'
_vg2, _res = aiscan.apply_findings(_vg, _vscan['findings'], ['v1', 'v2'])
assert _res == {'v1': None, 'v2': None}, _res
_vt = _vg2['order'][1]['body'][1]['text']
assert '<sup>14</sup> When he went ashore' in _vt, _vt
assert 'crowd of 14 people' in _vt, \
    'the prose 14 untouched — the quote context picks the verse boundary'
assert 'at 3:00 the disciples' in _vt, _vt
assert '<sup>13</sup>' in _vt and '<sup>15</sup>' in _vt, \
    'correct labels untouched'
assert _vg['order'][1]['body'][1]['text'].count('<sup>') == 3, \
    'apply works on a copy'

# a quote without disambiguating context refuses between equal matches
_vg3, _res = aiscan.apply_findings(_vg, [
    {'id': 'x1', 'quote': '14', 'issue': 'x', 'current': 'bare',
     'proposed': 'superscript', 'confidence': 'high', 'status': 'open',
     'fix': _vfix('sup_verse', orderIndex=1, blockIndex=1, number=14)}],
    ['x1'])
assert 'ambiguous' in _res['x1'], _res
assert _vg3['order'][1]['body'][1]['text'] == \
    _vg['order'][1]['body'][1]['text'], 'nothing changed on ambiguity'

# a stale block index relocates by quote, like every other fix
_vg4, _res = aiscan.apply_findings(_vg, [
    {'id': 'x2', 'quote': '14 When he went ashore', 'issue': 'x',
     'current': 'bare', 'proposed': 'superscript', 'confidence': 'high',
     'status': 'open',
     'fix': _vfix('sup_verse', orderIndex=0, blockIndex=5, number=14)}],
    ['x2'])
assert _res == {'x2': None}, _res
assert '<sup>14</sup> When he went ashore' in _vg4['order'][1]['body'][1]['text']

# --- merge re-convert: the published (hand-edited) guide is the skeleton;
# a fresh conversion contributes markup/accents for canonically identical
# text, heading accents, and the page-image inventory. Edited text has no
# canonical match and passes through untouched; ambiguous matches are
# never guessed at.
from wgconvert.merge import merge_guides  # noqa: E402

_cur = {'welcome': None, 'order': [
            {'kind': 'item', 'type': 'prayer', 'label': 'Blessing', 'title': None,
             'titleQuoted': False, 'who': None, 'note': None,
             'body': [{'type': 'prayer', 'text': 'Go in peace. Amen.'}]}],
        'announcements': [
            {'heading': 'Coffee', 'kind': 'note', 'text': 'Same words here.', 'color': None},
            {'heading': 'Picnic', 'kind': 'note', 'text': 'dup', 'color': None},
            {'heading': 'Edited', 'kind': 'note', 'text': 'Rewritten by hand.',
             'color': 'blue'}],
        'specialEvents': [], 'prayerRequests': [],
        'flyers': [{'page': 9, 'image': 'flyer-9.jpg'}], 'notes': [], 'dateISO': '2026-01-04'}
_frs = {'welcome': None, 'order': [
            {'kind': 'item', 'type': 'prayer', 'label': 'Blessing', 'title': None,
             'titleQuoted': False, 'who': None, 'note': None,
             'body': [{'type': 'prayer', 'text': 'Go in peace.  Amen.'}]}],
        'announcements': [
            {'heading': 'Coffee', 'kind': 'note',
             'text': '<span class="fc-maroon">Same words</span> here.', 'color': 'maroon'},
            {'heading': 'Picnic', 'kind': 'note', 'text': '<b>dup</b>', 'color': 'green'},
            {'heading': 'Picnic', 'kind': 'note', 'text': '<i>dup</i>', 'color': 'gold'},
            {'heading': 'Edited', 'kind': 'note', 'text': 'The original printed text.',
             'color': 'purple'}],
        'specialEvents': [], 'prayerRequests': [],
        'flyers': [], 'notes': ['fresh-parse note (not adopted)'], 'dateISO': '2026-01-04'}
_m, _st = merge_guides(_cur, _frs)
assert _m['announcements'][0]['text'] == '<span class="fc-maroon">Same words</span> here.', \
    'canonically identical text adopts the fresh markup'
assert _m['announcements'][0]['color'] == 'maroon', 'heading accent adopted'
assert _m['announcements'][1]['text'] == 'dup', 'ambiguous canonical match left alone'
assert _m['announcements'][1]['color'] is None, 'ambiguous accent left alone'
assert _m['announcements'][2]['text'] == 'Rewritten by hand.', 'hand edit kept'
assert _m['announcements'][2]['color'] == 'blue', 'hand-set accent kept'
assert _m['order'][0]['body'][0]['text'] == 'Go in peace.  Amen.', \
    'whitespace-only drift still counts as canonically identical'
assert _m['flyers'] == [], 'page-image inventory follows the fresh conversion'
assert _st == {'texts': 2, 'accents': 1}, _st
assert any('refreshed from the printed PDF (hand edits kept)' in n for n in _m['notes'])
assert 'fresh-parse note (not adopted)' not in _m['notes']
_m2, _st2 = merge_guides(_cur, {'welcome': None, 'order': [], 'announcements': [],
                                'specialEvents': [], 'prayerRequests': [],
                                'flyers': [], 'dateISO': '2026-01-04'})
assert _st2 == {'texts': 0, 'accents': 0} and _m2['notes'] == [], \
    'a no-change merge adds no note'
assert _m2['announcements'] == _cur['announcements'], 'nothing to adopt → untouched'

# --- AI scan queue: durable markers, dedupe, worker outcomes, and restart
# rescan — exercised in-process against scratch dirs with run_aiscan stubbed
# (no key or network), so a redeploy provably pauses scans instead of losing
# them.
_qscratch = tempfile.mkdtemp(prefix='lwcc-aiscanq-')
_saved = {k: getattr(app_mod, k) for k in
          ('PUBLIC', 'QUEUE_DIR', 'AISCAN_QUEUE_DIR', 'run_aiscan', 'audit_log')}
try:
    app_mod.PUBLIC = os.path.join(_qscratch, 'public')
    app_mod.QUEUE_DIR = os.path.join(_qscratch, 'queue')
    app_mod.AISCAN_QUEUE_DIR = os.path.join(app_mod.QUEUE_DIR, 'aiscan')
    app_mod.audit_log = lambda entry: None
    for d in ('2026-01-04', '2026-01-11'):
        os.makedirs(os.path.join(app_mod.PUBLIC, d))
        json.dump({}, open(os.path.join(app_mod.PUBLIC, d, 'guide.json'), 'w'))

    assert app_mod.aiscan_workers() == 10, 'default concurrency is 10'
    app_mod.ENV['AISCAN_WORKERS'] = '3'
    assert app_mod.aiscan_workers() == 3
    app_mod.ENV['AISCAN_WORKERS'] = '99'
    assert app_mod.aiscan_workers() == 10, 'cap holds'
    del app_mod.ENV['AISCAN_WORKERS']

    assert app_mod.aiscan_enqueue('2026-01-04') is True
    assert app_mod.aiscan_enqueue('2026-01-04') is False, 'queued date deduped'
    assert os.path.exists(os.path.join(app_mod.AISCAN_QUEUE_DIR, '2026-01-04')), \
        'durable marker written — the request survives a restart'
    snap = app_mod.aiscan_snapshot()
    assert snap['waiting'] == 1 and snap['scanning'] == [], snap

    app_mod.run_aiscan = lambda d: {'findings': [1, 2]}
    app_mod.aiscan_process_one(app_mod.AISCAN_Q.get())
    snap = app_mod.aiscan_snapshot()
    assert snap['jobs']['2026-01-04'] == {'status': 'ok', 'findings': 2}, snap
    assert not os.path.exists(os.path.join(app_mod.AISCAN_QUEUE_DIR, '2026-01-04')), \
        'marker cleared once the scan settles'
    assert app_mod.aiscan_enqueue('2026-01-04') is True, 're-scan allowed after done'
    app_mod.aiscan_process_one(app_mod.AISCAN_Q.get())   # settle it again

    def _boom(d):
        raise RuntimeError('rate limited')
    app_mod.run_aiscan = _boom
    app_mod.aiscan_enqueue('2026-01-11')
    app_mod.aiscan_process_one(app_mod.AISCAN_Q.get())
    snap = app_mod.aiscan_snapshot()
    assert snap['jobs']['2026-01-11'] == {'status': 'failed', 'error': 'rate limited'}

    # restart simulation: markers on disk re-enqueue; stale markers (Sunday
    # unpublished meanwhile, junk names) are discarded.
    app_mod.AISCAN_JOBS.clear()
    while not app_mod.AISCAN_Q.empty():
        app_mod.AISCAN_Q.get()
    open(os.path.join(app_mod.AISCAN_QUEUE_DIR, '2026-01-11'), 'w').close()
    open(os.path.join(app_mod.AISCAN_QUEUE_DIR, '1999-01-01'), 'w').close()
    open(os.path.join(app_mod.AISCAN_QUEUE_DIR, 'junk'), 'w').close()
    app_mod.aiscan_rescan()
    snap = app_mod.aiscan_snapshot()
    assert list(snap['jobs']) == ['2026-01-11'] and snap['waiting'] == 1, snap
    assert app_mod.AISCAN_Q.get() == '2026-01-11'
    assert sorted(os.listdir(app_mod.AISCAN_QUEUE_DIR)) == ['2026-01-11'], \
        'stale markers discarded, live one kept for the worker'

    # Aggregation: findings match across guides on (quote, current, proposed)
    # with markup/whitespace/case ignored; singletons don't form groups.
    def _f(fid, quote, status='open', fix=True):
        return {'id': fid, 'quote': quote, 'issue': 'recurring masthead',
                'current': 'announcement', 'proposed': 'discard',
                'confidence': 'high', 'status': status,
                'fix': ({'op': 'discard_announcement', 'orderIndex': None,
                         'blockIndex': None, 'annIndex': 0, 'heading': None}
                        if fix else None)}

    def _ev(fid, quote, status='open', note=None):
        return {'id': fid, 'quote': quote, 'issue': 'event misfiled',
                'current': 'announcement', 'proposed': 'event',
                'confidence': 'medium', 'status': status,
                **({'statusNote': note} if note else {}),
                'fix': {'op': 'announcement_to_event', 'orderIndex': None,
                        'blockIndex': None, 'annIndex': 1, 'eventIndex': None,
                        'heading': None}}
    for d, fs in (('2026-01-04', [_f('f1', 'LEISURE  WORLD community church'),
                                  _f('f2', 'one-off block', fix=False),
                                  _ev('f3', 'Concert this Sunday at 3 PM')]),
                  ('2026-01-11', [_f('f1', '<b>Leisure World</b> Community Church',
                                     status='dismissed'),
                                  _ev('f3', 'Church picnic next Saturday',
                                      status='skipped',
                                      note='quoted text not found')]),
                  ('2026-01-18', [_f('f1', 'Leisure World Community Church',
                                     status='applied')])):
        os.makedirs(os.path.join(app_mod.PUBLIC, d), exist_ok=True)
        open(os.path.join(app_mod.PUBLIC, d, 'index.html'), 'w').write('x')
        json.dump({'findings': fs},
                  open(os.path.join(app_mod.PUBLIC, d, 'aiscan.json'), 'w'))
    agg = app_mod.aiscan_aggregate()
    assert [g['kind'] for g in agg] == ['exact', 'similar'], agg
    assert [i['status'] for i in agg[0]['items']] == ['open', 'dismissed'], \
        'open findings lead, settled sink; singleton category excluded'
    assert [i['date'] for i in agg[0]['items']] == ['2026-01-04', '2026-01-11']
    assert not any(i['status'] == 'applied' for g in agg for i in g['items']), \
        'applied findings never reach the aggregate view'
    assert agg[0]['current'] == 'announcement' and agg[0]['proposed'] == 'discard'
    sim = agg[1]
    assert sim['op'] == 'announcement_to_event' and len(sim['items']) == 2, sim
    assert [i['status'] for i in sim['items']] == ['open', 'skipped'], \
        'skipped rows sort below open ones'
    assert sim['items'][1]['note'] == 'quoted text not found', \
        'skip reason carried to the aggregate view'
    assert {i['quote'] for i in sim['items']} == \
        {'Concert this Sunday at 3 PM', 'Church picnic next Saturday'}, \
        'varying-text findings grouped by error category, quotes carried'
    assert all(i['fixable'] for i in sim['items'])

    # run_aiscan runs both agents — the classifier and the scripture
    # verse-number checker — and merges their findings (distinct id
    # prefixes) with summed usage; a guide with no scripture merges nothing.
    _scanstubs = (aiscan.scan_guide, aiscan.scan_verses)
    _had_key = 'ANTHROPIC_API_KEY' in app_mod.ENV
    try:
        app_mod.ENV['ANTHROPIC_API_KEY'] = 'test-key'
        aiscan.scan_guide = lambda g, k, model=None: {
            'model': 'm', 'summary': 'classes look right',
            'usage': {'input': 10, 'output': 4},
            'findings': [{'id': 'f1', 'status': 'open'}]}
        aiscan.scan_verses = lambda g, k, model=None: {
            'model': 'm', 'summary': 'one bare verse number',
            'usage': {'input': 7, 'output': 3},
            'findings': [{'id': 'v1', 'status': 'open'}]}
        merged = _saved['run_aiscan']('2026-01-04')
        assert [f['id'] for f in merged['findings']] == ['f1', 'v1'], merged
        assert merged['usage'] == {'input': 17, 'output': 7}, merged
        assert merged['summary'] == \
            'classes look right — Scripture verses: one bare verse number'
        aiscan.scan_verses = lambda g, k, model=None: None
        merged = _saved['run_aiscan']('2026-01-04')
        assert [f['id'] for f in merged['findings']] == ['f1'] \
            and merged['usage'] == {'input': 10, 'output': 4}, merged
    finally:
        aiscan.scan_guide, aiscan.scan_verses = _scanstubs
        if not _had_key:
            app_mod.ENV.pop('ANTHROPIC_API_KEY', None)
finally:
    app_mod.AISCAN_JOBS.clear()
    for k, v in _saved.items():
        setattr(app_mod, k, v)
    shutil.rmtree(_qscratch, ignore_errors=True)

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


import re as re_mod  # noqa: E402
import subprocess as sp_mod  # noqa: E402
NODE = shutil.which('node')
SCRIPT_RE = re_mod.compile(
    rb'<script(?![^>]*type="application/json")[^>]*>(.*?)</script>', re_mod.S)


def check_page_js(body, label):
    """Syntax-check every inline <script> with node — Python string escaping
    has silently broken page JS before (a \\' in a template collapsed to a
    bare quote), and plain HTTP assertions can't see that."""
    if not NODE:
        print(f'  (node not installed — JS syntax check skipped for {label})',
              file=sys.stderr)
        return
    for m in SCRIPT_RE.finditer(body):
        src = m.group(1)
        if not src.strip():
            continue
        with tempfile.NamedTemporaryFile(suffix='.js', delete=False) as fh:
            fh.write(src)
        try:
            r = sp_mod.run([NODE, '--check', fh.name],
                           capture_output=True, text=True)
            assert r.returncode == 0, \
                f'{label}: inline JS fails to parse:\n{r.stderr}'
        finally:
            os.unlink(fh.name)


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
    assert snap['waiting'] == 0 and snap['converting'] == [], \
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

    # AI article scanner: page and endpoints are gated; scanning is refused
    # without ANTHROPIC_API_KEY; applying repairs from a stored scan works
    # end-to-end (moves are verified against the guide text, guide.json is
    # rewritten, the page re-renders, statuses persist).
    status, body = req('/admin/aiscan/2026-08-02')
    assert status == 401 and b'Admin Sign-in' in body, 'aiscan page gated'
    status, body = req('/admin/aiscan/2026-08-02', headers=COOKIE)
    assert status == 200 and b'AI Article Scanner' in body
    assert b'ANTHROPIC_API_KEY' in body, 'page says how to enable scanning'
    status, body = req('/admin/aiscan/1999-01-01', headers=COOKIE)
    assert status == 404
    status, body = req('/api/aiscan', data=json.dumps({'date': '2026-08-02'}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 503 and b'ANTHROPIC_API_KEY' in body, 'scan fails closed without a key'
    status, body = req('/api/aiscan', data=json.dumps({'date': '2026-08-02'}).encode(),
                       headers={'Content-Type': 'application/json'})
    assert status == 401, 'aiscan API needs the admin token'
    status, body = req('/api/aiscan-status')
    assert status == 401, 'scan-queue status gated like /api/status'
    status, body = req('/api/aiscan-status', headers=COOKIE)
    assert status == 200, (status, body)
    ss = json.loads(body)
    assert ss['ok'] and ss['jobs'] == {} and ss['waiting'] == 0 \
        and ss['scanning'] == [], ss
    status, body = req('/admin', headers=COOKIE)
    assert b'AI article scanner' in body and b'/admin/aiscan/2026-08-02' in body, \
        'admin page carries the scanner card and per-Sunday links'

    # Stored-scan repair flow on the 2020-01-05 copy: move one real order
    # item into announcements, dismiss a second flag-only finding.
    ai_dir = os.path.join(scratch, 'public', '2020-01-05')
    ai_guide = json.load(open(os.path.join(ai_dir, 'guide.json')))
    idx, item = next((i, o) for i, o in enumerate(ai_guide['order'])
                     if o['kind'] == 'item' and o.get('label') and o.get('body')
                     and '<' not in o['body'][0]['text'])
    scan_fixture = {
        'at': '2026-08-05T10:00:00', 'model': 'claude-opus-5',
        'summary': 'test fixture', 'usage': {'input': 1, 'output': 1},
        'findings': [
            {'id': 'f1', 'path': f'order[{idx}]',
             'quote': item['body'][0]['text'][:80],
             'issue': 'misfiled as order-of-worship content',
             'current': 'content', 'proposed': 'announcement',
             'confidence': 'high', 'status': 'open',
             'fix': {'op': 'item_to_announcement', 'orderIndex': idx,
                     'blockIndex': None, 'annIndex': None,
                     'heading': 'Moved by scanner'}},
            {'id': 'f2', 'path': 'welcome', 'quote': 'x', 'issue': 'flag only',
             'current': 'content', 'proposed': 'announcement',
             'confidence': 'low', 'status': 'open', 'fix': None},
            {'id': 'f3', 'path': 'order[0]', 'quote': 'TEXT THAT IS NOT THERE',
             'issue': 'stale quote must be skipped, then reopenable',
             'current': 'content', 'proposed': 'stage',
             'confidence': 'high', 'status': 'open',
             'fix': {'op': 'item_to_stage', 'orderIndex': 0,
                     'blockIndex': None, 'annIndex': None, 'eventIndex': None,
                     'heading': None}},
        ],
    }
    json.dump(scan_fixture, open(os.path.join(ai_dir, 'aiscan.json'), 'w'))
    status, body = req('/admin', headers=COOKIE)
    assert '🔎 3'.encode() in body, 'open-findings badge on the Sundays list'
    assert b'3 open findings' in body, 'scanner card lists the flagged Sunday'
    status, body = req('/api/aiscan-apply',
                       data=json.dumps({'date': '2020-01-05',
                                        'ids': ['f1', 'f3']}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200, (status, body)
    ap = json.loads(body)
    assert ap['ok'] and ap['applied'] == ['f1'] and list(ap['skipped']) == ['f3'], ap
    g_after = json.load(open(os.path.join(ai_dir, 'guide.json')))
    assert len(g_after['order']) == len(ai_guide['order']) - 1, 'item moved out'
    moved = g_after['announcements'][-1]
    assert moved['heading'] == 'Moved by scanner'
    _norm = lambda s: ' '.join(app_mod.clean_plain(s).split())  # noqa: E731
    assert _norm(item['body'][0]['text'])[:40] in _norm(moved['text']), \
        'printed text preserved, not rewritten'
    assert any('AI repair applied' in n for n in g_after['notes'])
    scan_after = json.load(open(os.path.join(ai_dir, 'aiscan.json')))
    assert scan_after['findings'][0]['status'] == 'applied'
    assert 'Moved by scanner' in open(os.path.join(ai_dir, 'index.html')).read(), \
        'apply re-rendered the page'

    # Retry skipped fixes: one call reopens them and re-applies through the
    # relocation path; a quote gone from the guide re-skips with the clearer
    # reason rather than being guessed at.
    status, body = req('/api/aiscan-apply',
                       data=json.dumps({'date': '2020-01-05',
                                        'retry': True}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200, (status, body)
    rt = json.loads(body)
    assert rt['ok'] and rt['applied'] == [] \
        and 'no longer exists' in rt['skipped']['f3'], rt
    scan_after = json.load(open(os.path.join(ai_dir, 'aiscan.json')))
    assert scan_after['findings'][2]['status'] == 'skipped', \
        'unrecoverable fix re-skips instead of applying blind'
    status, body = req('/api/aiscan-apply',
                       data=json.dumps({'date': '2020-01-05', 'ids': ['f2'],
                                        'dismiss': True}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200 and json.loads(body)['ok'], body
    scan_after = json.load(open(os.path.join(ai_dir, 'aiscan.json')))
    assert scan_after['findings'][1]['status'] == 'dismissed'
    status, body = req('/admin/aiscan/2020-01-05', headers=COOKIE)
    assert status == 200 and b'misfiled as order-of-worship content' in body, \
        'scan page embeds the stored findings'
    assert b'"applied"' in body and b'"dismissed"' in body, 'statuses persisted'

    # Dismissed and skipped findings are both reopenable; an applied finding
    # is refused, not silently touched.
    status, body = req('/api/aiscan-apply',
                       data=json.dumps({'date': '2020-01-05',
                                        'ids': ['f2', 'f3'],
                                        'undismiss': True}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200 and json.loads(body)['applied'] == ['f2', 'f3'], body
    scan_after = json.load(open(os.path.join(ai_dir, 'aiscan.json')))
    assert scan_after['findings'][1]['status'] == 'open', 'dismissal reversed'
    assert scan_after['findings'][2]['status'] == 'open', 'skip reversed'
    assert 'statusNote' not in scan_after['findings'][2], 'skip note cleared'
    status, body = req('/api/aiscan-apply',
                       data=json.dumps({'date': '2020-01-05', 'ids': ['f1'],
                                        'undismiss': True}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert json.loads(body)['skipped'] == {'f1': 'not dismissed or skipped'}, body

    # "Clear resolved" archives settled findings (no re-scan needed): out of
    # the working list, kept as history, open findings untouched.
    status, body = req('/api/aiscan-apply',
                       data=json.dumps({'date': '2020-01-05',
                                        'archive': True}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200 and json.loads(body)['applied'] == ['f1'], body
    scan_after = json.load(open(os.path.join(ai_dir, 'aiscan.json')))
    assert [f['id'] for f in scan_after['findings']] == ['f2', 'f3'], \
        'settled finding out of the way, open ones untouched'
    assert [f['id'] for f in scan_after['resolvedFindings']] == ['f1'], \
        'archived into history, not deleted'
    status, body = req('/admin/aiscan/2020-01-05', headers=COOKIE)
    assert status == 200 and b'"resolvedFindings"' in body, \
        'scan page carries the archive'

    # Matching findings across guides: the same quoted text flagged the same
    # way on several Sundays aggregates at /admin/aiscan, and one action
    # applies each Sunday's fix on its own guide.
    import re as _re

    def _mkquote(t):
        return ' '.join(_re.sub(r'<[^>]+>', '', t).split()[:8])

    agg_dates = ['2026-08-02', '2020-01-05']
    for d in agg_dates:
        gg = json.load(open(os.path.join(scratch, 'public', d, 'guide.json')))
        json.dump(
            {'at': '2026-08-05T10:05:00', 'model': 'claude-opus-5',
             'summary': 'fixture', 'usage': {},
             'findings': [{'id': 'g1', 'path': 'announcements[0]',
                           'quote': _mkquote(gg['announcements'][0]['text']),
                           'issue': 'recurring flowers blurb',
                           'current': 'announcement', 'proposed': 'discard',
                           'confidence': 'high', 'status': 'open',
                           'fix': {'op': 'discard_announcement',
                                   'orderIndex': None, 'blockIndex': None,
                                   'annIndex': 0, 'heading': None}}]},
            open(os.path.join(scratch, 'public', d, 'aiscan.json'), 'w'))
    status, body = req('/admin/aiscan')
    assert status == 401 and b'Admin Sign-in' in body, 'aggregate page gated'
    status, body = req('/admin/aiscan', headers=COOKIE)
    assert status == 200 and b'Matching Findings' in body
    assert b'recurring flowers blurb' in body and b'2026-08-02' in body \
        and b'2020-01-05' in body, 'both Sundays in the group'
    before_counts = {
        d: len(json.load(open(os.path.join(scratch, 'public', d,
                                           'guide.json')))['announcements'])
        for d in agg_dates}
    status, body = req('/api/aiscan-apply',
                       data=json.dumps({'items': [{'date': d, 'ids': ['g1']}
                                                  for d in agg_dates]}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200, (status, body)
    br = json.loads(body)
    assert br['ok'] and all(br['results'][d]['applied'] == ['g1']
                            for d in agg_dates), br
    for d in agg_dates:
        gg = json.load(open(os.path.join(scratch, 'public', d, 'guide.json')))
        assert len(gg['announcements']) == before_counts[d] - 1, \
            'recurring block discarded on each Sunday'
        sc = json.load(open(os.path.join(scratch, 'public', d, 'aiscan.json')))
        assert sc['findings'][0]['status'] == 'applied'
    status, body = req('/admin/aiscan', headers=COOKIE)
    assert status == 200 and b'recurring flowers blurb' not in body \
        and b'"applied"' not in body, \
        'a fully-applied group drops off the aggregate view'
    status, body = req('/admin', headers=COOKIE)
    assert b'Matching findings across' in body, \
        'admin card links the aggregate view'

    # Group-level dismiss/undismiss round-trip across guides.
    for d in agg_dates:
        sp = os.path.join(scratch, 'public', d, 'aiscan.json')
        sc = json.load(open(sp))
        sc['findings'].append({'id': 'g2', 'path': 'order[0]',
                               'quote': 'RECURRING POSTER FRAGMENT',
                               'issue': 'poster residue kept as content',
                               'current': 'content', 'proposed': 'discard',
                               'confidence': 'low', 'status': 'open',
                               'fix': None})
        json.dump(sc, open(sp, 'w'))
    items = [{'date': d, 'ids': ['g2']} for d in agg_dates]
    status, body = req('/api/aiscan-apply',
                       data=json.dumps({'items': items, 'dismiss': True}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200 and json.loads(body)['ok'], body
    for d in agg_dates:
        sc = json.load(open(os.path.join(scratch, 'public', d, 'aiscan.json')))
        assert sc['findings'][-1]['status'] == 'dismissed', (d, sc['findings'][-1])
    status, body = req('/admin/aiscan', headers=COOKIE)
    assert status == 200 and b'RECURRING POSTER FRAGMENT' in body \
        and b'Reopen' in body, 'dismissed group offers reopen'
    status, body = req('/api/aiscan-apply',
                       data=json.dumps({'items': items, 'undismiss': True}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200, (status, body)
    br = json.loads(body)
    assert br['ok'] and all(br['results'][d]['applied'] == ['g2']
                            for d in agg_dates), br
    for d in agg_dates:
        sc = json.load(open(os.path.join(scratch, 'public', d, 'aiscan.json')))
        assert sc['findings'][-1]['status'] == 'open', 'group dismissal reversed'

    # The admin card offers clear-resolved across Sundays; the batch archive
    # sweeps each Sunday's settled findings without needing ids.
    status, body = req('/admin', headers=COOKIE)
    assert b'id="aiscanclear"' in body \
        and b'Clear resolved findings (2)' in body, 'clear-all offered'
    status, body = req('/api/aiscan-apply',
                       data=json.dumps({'items': [{'date': d} for d in agg_dates],
                                        'archive': True}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200 and json.loads(body)['ok'], (status, body)
    for d in agg_dates:
        sc = json.load(open(os.path.join(scratch, 'public', d, 'aiscan.json')))
        assert [f['id'] for f in sc['findings']] == ['g2'], (d, sc['findings'])
        assert [f['id'] for f in sc['resolvedFindings']] == ['g1']
    status, body = req('/admin', headers=COOKIE)
    assert b'id="aiscanclear"' not in body, 'nothing settled left to clear'

    # Varying-text findings with the same error category group in the
    # "similar" tier of the aggregate view.
    for d, q in zip(agg_dates, ('Concert this Sunday at 3 PM',
                                'Church picnic next Saturday')):
        sp = os.path.join(scratch, 'public', d, 'aiscan.json')
        sc = json.load(open(sp))
        sc['findings'].append({'id': 'h1', 'quote': q,
                               'issue': 'event misfiled as announcement',
                               'current': 'announcement', 'proposed': 'event',
                               'confidence': 'medium', 'status': 'open',
                               'fix': {'op': 'announcement_to_event',
                                       'orderIndex': None, 'blockIndex': None,
                                       'annIndex': 0, 'eventIndex': None,
                                       'heading': None}})
        json.dump(sc, open(sp, 'w'))
    status, body = req('/admin/aiscan', headers=COOKIE)
    assert status == 200 and b'"kind": "similar"' in body \
        and b'Church picnic next Saturday' in body, \
        'varying-text category appears in the similar tier'

    # Every admin page's inline JS must actually parse (all scanner states
    # are populated at this point: open, applied, dismissed, groups).
    for js_path in ('/admin', '/admin/history', '/admin/aiscan',
                    '/admin/aiscan/2020-01-05', '/admin/edit/2026-08-02'):
        status, body = req(js_path, headers=COOKIE)
        assert status == 200, (js_path, status)
        check_page_js(body, js_path)

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

    # Bulk re-convert runs through the server queue: jobs from stored
    # sources (unknown dates skipped), progress in the live snapshot, and
    # the stored source is never consumed.
    gj02 = os.path.join(scratch, 'public', '2026-08-02', 'guide.json')
    g02 = json.load(open(gj02))
    g02['warnings'] = ['synthetic warning for the batch test']
    json.dump(g02, open(gj02, 'w'))
    status, body = req('/api/reconvert-batch',
                       data=json.dumps({'dates': ['2026-08-02', '2026-08-02',
                                                  '1999-01-01']}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200, (status, body)
    rb = json.loads(body)
    assert rb['ok'] and rb['queued'] == 1 and rb['skipped'] == ['1999-01-01'], rb
    assert rb['alreadyQueued'] == ['2026-08-02'], 'duplicate dates never stack'
    qs = None
    for _ in range(240):
        status, body = req('/api/status?ids=', headers=COOKIE)
        qs = json.loads(body)['queue']
        if not qs['waiting'] and not qs['converting']:
            break
        time.sleep(0.5)
    assert qs and not qs['waiting'] and not qs['converting'], qs
    g02 = json.load(open(gj02))
    assert g02['warnings'] == [], 'batch re-convert regenerated the guide'
    assert os.path.exists(os.path.join(scratch, 'public', '2026-08-02', 'source.pdf')), \
        'stored source survives re-conversion'
    entries = [json.loads(l) for l in open(log_path)]
    assert any(e.get('action') == 'reconvert-batch' and e.get('queued') == 1
               for e in entries), 'batch queueing audited'
    status, body = req('/api/reconvert-clear', data=b'{}',
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200 and json.loads(body) == {'ok': True, 'cleared': 0}, \
        'clearing an idle queue is a no-op'

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
    assert b'id="reconverteverything"' in body, 'full-sweep re-convert offered'
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

    # Merge re-convert: hand edits kept, everything unedited gains the
    # latest markup and accent colors from a fresh conversion of the PDF.
    status, body = req('/admin', headers=COOKIE)
    assert b'reconvert-merge' in body and b'id="refresheverything"' in body, \
        'admin offers the keep-edits re-convert, per Sunday and as a sweep'
    mgj = os.path.join(out_dir, 'guide.json')
    g = json.load(open(mgj))
    # Simulate a pre-color, hand-edited Sunday: strip every accent, rewrite
    # one announcement by hand, and add one that never came from the PDF.
    for a in g['announcements']:
        a['color'] = None
        a['text'] = re.sub(r'</?span[^>]*>', '', a['text'])
    for ev in g['specialEvents']:
        ev['color'] = None
        ev['paragraphs'] = [re.sub(r'</?span[^>]*>', '', p) for p in ev['paragraphs']]
    g['announcements'][0]['text'] = 'Rewritten by hand — no longer the printed text.'
    g['announcements'].append({'heading': 'Hand Added', 'kind': 'note',
                               'text': 'Stays after the merge.', 'color': None})
    json.dump(g, open(mgj, 'w'))
    status, body = action('reconvert-merge', '2026-08-02')
    assert status == 200 and json.loads(body)['ok'], body
    merged = json.load(open(mgj))
    assert merged['announcements'][0]['text'] == \
        'Rewritten by hand — no longer the printed text.', 'hand edit kept'
    assert any(a.get('heading') == 'Hand Added' for a in merged['announcements']), \
        'operator-added announcement kept'
    by_head = {a.get('heading'): a for a in merged['announcements']}
    assert by_head['Coffee with Pastor']['color'] == 'maroon', 'heading accent restored'
    assert '<span class="fc-' in by_head['August Communion Sunday']['text'] \
        or by_head['August Communion Sunday']['color'] == 'gold', 'accents restored'
    assert merged['specialEvents'][0]['color'] == 'purple'
    assert '<span class="fc-purple">' in merged['specialEvents'][0]['paragraphs'][0], \
        'markup restored where the text was not edited'
    assert any('refreshed from the printed PDF (hand edits kept)' in n
               for n in merged['notes'])
    page = open(os.path.join(out_dir, 'index.html'), 'rb').read()
    assert b'Rewritten by hand' in page and b'fc-purple' in page, \
        'merge re-rendered with edits and colors together'
    entries = [json.loads(l) for l in open(log_path)]
    assert any(e.get('merge') and e.get('reconvert') and e.get('ok')
               for e in entries), 'merge audited as a merged re-conversion'

    # The batch endpoint carries the merge flag through the worker queue.
    g = json.load(open(mgj))
    g['specialEvents'][0]['color'] = None
    json.dump(g, open(mgj, 'w'))
    status, body = req('/api/reconvert-batch',
                       data=json.dumps({'dates': ['2026-08-02'], 'merge': True}).encode(),
                       headers={**COOKIE, 'Content-Type': 'application/json'})
    assert status == 200 and json.loads(body)['queued'] == 1, body
    for _ in range(240):
        status, body = req('/api/status?ids=', headers=COOKIE)
        qs = json.loads(body)['queue']
        if not qs['waiting'] and not qs['converting']:
            break
        time.sleep(0.5)
    merged = json.load(open(mgj))
    assert merged['specialEvents'][0]['color'] == 'purple', \
        'batch merge refreshed the accent'
    assert merged['announcements'][0]['text'] == \
        'Rewritten by hand — no longer the printed text.', \
        'batch merge kept the hand edit'

    # Guide <-> original PDF navigation: the week-nav links to /DATE/original
    # when a source is stored; the viewer page links back and embeds the PDF.
    status, body = req('/2026-08-02/')
    assert status == 200 and b'/2026-08-02/original' in body, 'guide links its PDF'
    status, body = req('/2026-08-02/original')
    assert status == 200 and b'source.pdf' in body \
        and 'worship guide'.encode() in body, 'viewer embeds PDF and links back'
    status, body = req('/2026-08-09/')
    assert status == 200 and b'/2026-08-09/original' not in body, \
        'no stored source -> no link'
    status, body = req('/2026-08-09/original')
    assert status == 404, 'viewer 404s without a stored source'

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
    g['announcements'][1]['text'] = (
        'Accent <span class="fc-purple">kept</span> '
        '<span class="fc-hack">rogue class</span> '
        '<span onclick="x()" class="fc-gold">attributes</span>')
    g['announcements'][1]['color'] = 'purple'              # accent: whitelisted
    g['announcements'][2]['color'] = 'hotpink'             # off-palette: dropped
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
    assert '<span class="fc-purple">kept</span>' in saved['announcements'][1]['text'], \
        'palette accent span survives the sanitizer'
    assert '<span class="fc-hack">' not in saved['announcements'][1]['text'] \
        and '<span onclick' not in saved['announcements'][1]['text'], \
        'off-vocabulary spans neutralized'
    assert saved['announcements'][1]['color'] == 'purple'
    assert saved['announcements'][2]['color'] is None, 'off-palette accent dropped'
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

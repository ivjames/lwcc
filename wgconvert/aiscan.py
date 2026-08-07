"""AI article scanner: review a published guide.json for OCR/parse
misclassifications — worship content vs page/stage directions vs
announcements — and repair them with text-preserving moves.

A second, dedicated agent (scan_verses) reads the scripture section: it
works out from each passage reference which verse numbers the text must
carry and checks every one is labeled with its superscript <sup>N</sup>
marker. Its fixes are markup-only — wrap a bare verse number, unwrap a
wrongly superscripted one — never text edits.

A third agent (scan_photos) looks at the published photo crops themselves:
each interstitial photo a Sunday publishes is sent to Claude as an image,
and crops that are really sheet music or a block of unrelated printed text
— the pixel heuristics' misses — are flagged. Its only mechanical fix
drops the crop from the page's Photos section; the printed page itself is
untouched.

The printed PDF is the source of truth, so the scanner never rewrites text.
Claude reports findings that each quote the misfiled text verbatim and name
one mechanical fix (reclassify an order item as a stage direction, move a
stray paragraph into announcements, drop page furniture that landed in
announcements, …). apply_findings() verifies every quote against the guide
before touching it, so a stale or hallucinated finding is skipped, never
silently applied.

Stdlib only (urllib), matching the rest of the app; the Claude call is a
single Messages API request with a structured-output schema. The HTTP
transport is injectable so tests run without a key or network.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import time
import urllib.error
import urllib.request

API_URL = 'https://api.anthropic.com/v1/messages'
DEFAULT_MODEL = 'claude-opus-5'

OPS = ('item_to_stage', 'stage_to_item', 'item_to_announcement',
       'para_to_announcement', 'para_to_stage', 'discard_para',
       'discard_announcement', 'announcement_to_event',
       'announcement_to_stage', 'event_to_announcement',
       'welcome_to_announcement')

# The scripture verse-number agent's fixes. sup_verse/unsup_verse are
# markup-only; fix_verse/insert_verse restore a printed verse number that
# OCR garbled into other glyphs or dropped — the replacement is always just
# <sup>N</sup>, never other words.
VERSE_OPS = ('sup_verse', 'unsup_verse', 'fix_verse', 'insert_verse')

# The photo agent's one fix: remove a crop that is not a photograph from the
# published photo inventory. Recrop advice stays flag-only — a human trims.
PHOTO_OPS = ('drop_photo',)

SYSTEM_PROMPT = """\
You are the quality reviewer for a church worship-guide conversion pipeline.
A printed Sunday worship guide PDF was OCR'd and parsed into structured JSON.
The parser sometimes files text under the wrong one of three categories:

- content: worship material that belongs inside order-of-worship items —
  prayers, scripture, litanies with congregation refrains, hymn/anthem titles,
  sermon (message) info, questions for reflection, the pastor's welcome.
- stage: page/stage directions — short instructions about posture, movement,
  or how to take part ("please stand as you are able", "the congregation
  reads the bold text", "ushers, please come forward", "please be seated").
  These live in the order array as {"kind": "stage"} entries.
- announcement: community news and notices — events, schedules, cancellations,
  thanks, flower dedications, attendance counts, sign-ups. These live in the
  announcements array (some also belong in specialEvents).

Review the guide below and report ONLY misclassifications between those
categories (the parser's own "notes" and "warnings" often point at the
suspicious spots). Typical failures: a stage direction absorbed into an
item's body or kept as an untitled item; an announcement paragraph appended
to a prayer or filed as order-of-worship content; recurring page furniture or
poster fragments filed as an announcement; real content parked in
announcements by the fallback classifier.

Hard rules:
- The printed page is the source of truth. Never rewrite, correct, or
  paraphrase text. Every fix is a move/reclassification of existing text.
- "quote" must be a verbatim excerpt (at most 120 characters) copied exactly
  from the misclassified text, including any <b>/<i>/<sup>/<span> markup, so the fix
  can be verified mechanically before it is applied.
- Address text by the indices given in the JSON: "i" for order,
  announcements, and specialEvents entries, "j" for body blocks inside an
  order item and paragraphs of the welcome section.
- Only these mechanical fixes exist:
  - item_to_stage(orderIndex): an order item that is really a page direction.
  - stage_to_item(orderIndex): a stage entry that is really worship content.
  - item_to_announcement(orderIndex, heading): an order item that is really
    an announcement.
  - para_to_announcement(orderIndex, blockIndex, heading): one body block of
    an item that is really an announcement.
  - para_to_stage(orderIndex, blockIndex): one body block that is really a
    page direction.
  - discard_para(orderIndex, blockIndex): one body block that is page
    furniture — masthead/letterhead residue (church name and address lines,
    phone numbers, fragmented banner words), poster fragments, or OCR junk
    that is not text a reader should see. NEVER use it for worship text,
    prayers, or anything a person wrote to be read; when unsure, prefer op
    "none". File one finding per block.
  - discard_announcement(annIndex): an announcements entry that is page
    furniture / poster residue / a duplicated fragment, not news.
  - announcement_to_event(annIndex, heading): an announcements entry that is
    really an upcoming special event.
  - announcement_to_stage(annIndex, orderIndex): an announcements entry that
    is really a page direction; it becomes a stage entry inserted at
    orderIndex (null appends at the end of the order).
  - event_to_announcement(eventIndex): a specialEvents entry that is really
    an ordinary announcement.
  - welcome_to_announcement(blockIndex, heading): one paragraph of the
    welcome section that is really a standalone announcement.
  Prefer a mechanical fix whenever one of these expresses the move — reach
  for op "none" only when genuinely nothing fits (a human then edits by
  hand). Unused fix fields are null.
- confidence: "high" = clearly misclassified, safe to fix mechanically;
  "medium" = probably; "low" = worth a human look, do not auto-fix.
- If everything is classified correctly, return an empty findings list.
  Do not invent problems, and do not flag correct classifications.
"""

FINDINGS_SCHEMA = {
    'type': 'object',
    'properties': {
        'findings': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string'},
                    'quote': {'type': 'string'},
                    'issue': {'type': 'string'},
                    'current': {'type': 'string',
                                'enum': ['content', 'stage', 'announcement']},
                    'proposed': {'type': 'string',
                                 'enum': ['content', 'stage', 'announcement',
                                          'event', 'discard']},
                    'confidence': {'type': 'string',
                                   'enum': ['high', 'medium', 'low']},
                    'fix': {
                        'type': 'object',
                        'properties': {
                            'op': {'type': 'string', 'enum': list(OPS) + ['none']},
                            'orderIndex': {'type': ['integer', 'null']},
                            'blockIndex': {'type': ['integer', 'null']},
                            'annIndex': {'type': ['integer', 'null']},
                            'eventIndex': {'type': ['integer', 'null']},
                            'heading': {'type': ['string', 'null']},
                        },
                        'required': ['op', 'orderIndex', 'blockIndex',
                                     'annIndex', 'eventIndex', 'heading'],
                        'additionalProperties': False,
                    },
                },
                'required': ['path', 'quote', 'issue', 'current', 'proposed',
                             'confidence', 'fix'],
                'additionalProperties': False,
            },
        },
        'summary': {'type': 'string'},
    },
    'required': ['findings', 'summary'],
    'additionalProperties': False,
}


VERSE_SYSTEM_PROMPT = """\
You are the scripture verse-number checker for a church worship-guide
conversion pipeline. A printed Sunday worship guide PDF was OCR'd and parsed
into structured JSON. Scripture passages print each verse number as a small
superscript, which the JSON marks up as <sup>N</sup>. The PDF's superscript
detection sometimes misses: a verse number is left as a bare digit in the
running text ("13 Now when Jesus heard…"), was dropped by OCR entirely, or a
number that is not a verse number got wrapped in <sup> by mistake.

OCR also often misreads the tiny superscript digits as other glyphs
entirely: apostrophes and quotes (' ‘ ’ "), degree signs (7° for 20, ?°),
asterisks, percent and ampersand signs, &gt;, or stray letters — sometimes
fused onto the next word ("Sand they were baptized" is the printed
"6 and they were baptized"; "'8As he walked" is "18 As he walked"). The
passage reference plus the numbers already present around it tell you
exactly which number belongs at each spot.

Read every scripture item below. Each "ref" block names a passage
(book chapter:verse-range); the "verse" blocks after it carry that passage's
text. From the reference, work out exactly which verse numbers the text must
carry (Romans 9:1-5 → 1 2 3 4 5; a ref with no verse range, like Psalm 121,
covers the whole chapter starting at verse 1), then check each expected
number off in order against the <sup> markers in the text.

Report ONLY verse-number labeling problems:
- bare: the verse number's digits are present at the verse boundary but not
  wrapped in <sup></sup> → fix sup_verse.
- superscripted (wrongly): a <sup>-wrapped number that is not one of this
  passage's verse numbers — a time, a chapter number stuck mid-text, page
  furniture → fix unsup_verse.
- garbled: the glyphs standing where the printed verse number was are not
  its digits (OCR misread them) → fix fix_verse: "garbled" is the exact
  misread run copied verbatim (including any <sup> tags around or inside
  it), "number" is the true printed number; the run is replaced by
  <sup>number</sup> and nothing else. A wrong number inside <sup> ("1!"
  for 11) is also fix_verse, with the tags in "garbled".
- missing: the number appears nowhere — not even as garbled glyphs — but
  you know exactly which verse starts where → fix insert_verse; "quote"
  must begin exactly at the verse's first words so the number can be
  inserted in front of them. If you cannot anchor the spot, op "none".

Hard rules:
- The printed page is the source of truth: these fixes RESTORE what the
  printer set — a superscript verse number — and may touch nothing else.
  Never rewrite, reorder, correct, or paraphrase words, even obvious OCR
  misspellings next to the number ("lmmediately"): fix the number, leave
  the word.
- "garbled" must contain ONLY the glyphs standing where the number was.
  Include a letter only when it is the misread number fused onto an intact
  word ("Sand" → garbled "S", leaving the real word "and"). Never take
  letters that belong to the word itself — if removing them would leave a
  mangled fragment, the fix is not mechanical: use op "none".
- Do NOT flag numbers that belong to the scripture prose itself (counts,
  measures, years, times) — they are only a problem when wrongly inside
  <sup>.
- Translation section headings printed inside the text ("The Cost of
  Discipleship", "A Song of Ascents.") are normal and never a finding.
- "quote" must be a verbatim excerpt (at most 120 characters) copied exactly
  from the verse text — the number (or garbled run) plus the first words of
  its verse — so the fix can be located and verified mechanically even when
  the same glyphs occur elsewhere in the passage.
- Address text by the indices given in the JSON: "i" for the order item,
  "j" for the body block; "number" is the verse number itself.
- confidence: "high" = certain, safe to fix mechanically; "medium" =
  probably; "low" = worth a human look, do not auto-fix.
- If every passage is fully and correctly labeled, return an empty findings
  list. Do not invent problems.
"""

VERSE_FINDINGS_SCHEMA = {
    'type': 'object',
    'properties': {
        'findings': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string'},
                    'quote': {'type': 'string'},
                    'issue': {'type': 'string'},
                    'current': {'type': 'string',
                                'enum': ['bare', 'superscripted', 'garbled',
                                         'missing']},
                    'proposed': {'type': 'string',
                                 'enum': ['superscript', 'plain', 'flag']},
                    'confidence': {'type': 'string',
                                   'enum': ['high', 'medium', 'low']},
                    'fix': {
                        'type': 'object',
                        'properties': {
                            'op': {'type': 'string',
                                   'enum': list(VERSE_OPS) + ['none']},
                            'orderIndex': {'type': ['integer', 'null']},
                            'blockIndex': {'type': ['integer', 'null']},
                            'number': {'type': ['integer', 'null']},
                            'garbled': {'type': ['string', 'null']},
                        },
                        'required': ['op', 'orderIndex', 'blockIndex',
                                     'number', 'garbled'],
                        'additionalProperties': False,
                    },
                },
                'required': ['path', 'quote', 'issue', 'current', 'proposed',
                             'confidence', 'fix'],
                'additionalProperties': False,
            },
        },
        'summary': {'type': 'string'},
    },
    'required': ['findings', 'summary'],
    'additionalProperties': False,
}


PHOTO_SYSTEM_PROMPT = """\
You are the photo reviewer for a church worship-guide conversion pipeline.
A printed Sunday worship guide PDF was converted to a web page. Photographs
set between the text on its pages were cropped out and published in the
page's Photos section. The crop chooser is a pixel heuristic and sometimes
publishes the wrong thing: a scanned piece of sheet music pasted into the
bulletin, a block of printed text, or a crop that drags neighboring page
content in along a photograph's edge.

You are shown each published crop as an image, preceded by a JSON label with
its index "i", filename, page number, and any caption. Judge only what the
pixels show:

- A photograph (people, places, objects, artwork, scenery) is publishable —
  never a finding, even when it is low quality, black-and-white, or has a
  little incidental text inside the scene (a banner, a sign, a name tag).
- sheet_music: engraved or handwritten musical notation — staff lines,
  notes, hymn scores — fills the crop or a substantial part of it.
- text: the crop is mostly printed text from the page — announcements,
  liturgy, poster lettering — rather than a photograph.
- mixed: a real photograph plus a substantial slice of sheet music or
  unrelated printed text that the crop dragged in with it.

Hard rules:
- Report ONLY crops that should not publish as photos (sheet_music, text,
  mixed). A genuine photograph is never a finding.
- "quote" must be the crop's exact filename as labeled (e.g.
  "photo-10-1.jpg") so the finding can be verified mechanically before it
  is applied.
- The only mechanical fix is drop_photo(imageIndex, image): the crop is
  removed from the published Photos section (the printed page is
  unaffected). Use it for sheet_music and text. For mixed — a real
  photograph that needs a tighter crop — use op "none" so a person recrops
  by hand.
- confidence: "high" = clearly not a publishable photograph, safe to drop
  mechanically; "medium" = probably; "low" = worth a human look, do not
  auto-fix.
- If every crop is a genuine photograph, return an empty findings list.
  Do not invent problems.
"""

PHOTO_FINDINGS_SCHEMA = {
    'type': 'object',
    'properties': {
        'findings': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string'},
                    'quote': {'type': 'string'},
                    'issue': {'type': 'string'},
                    'current': {'type': 'string',
                                'enum': ['sheet_music', 'text', 'mixed']},
                    'proposed': {'type': 'string',
                                 'enum': ['drop', 'recrop']},
                    'confidence': {'type': 'string',
                                   'enum': ['high', 'medium', 'low']},
                    'fix': {
                        'type': 'object',
                        'properties': {
                            'op': {'type': 'string',
                                   'enum': list(PHOTO_OPS) + ['none']},
                            'imageIndex': {'type': ['integer', 'null']},
                            'image': {'type': ['string', 'null']},
                        },
                        'required': ['op', 'imageIndex', 'image'],
                        'additionalProperties': False,
                    },
                },
                'required': ['path', 'quote', 'issue', 'current', 'proposed',
                             'confidence', 'fix'],
                'additionalProperties': False,
            },
        },
        'summary': {'type': 'string'},
    },
    'required': ['findings', 'summary'],
    'additionalProperties': False,
}


def guide_digest(guide):
    """The slice of guide.json the reviewer needs, with explicit indices so
    fixes can address entries unambiguously."""
    order = []
    for i, o in enumerate(guide.get('order') or []):
        if o.get('kind') == 'stage':
            order.append({'i': i, 'kind': 'stage', 'text': o.get('text')})
        else:
            order.append({
                'i': i, 'kind': 'item', 'type': o.get('type'),
                'label': o.get('label'), 'title': o.get('title'),
                'who': o.get('who'), 'note': o.get('note'),
                'body': [{'j': j, 'type': b.get('type'), 'text': b.get('text')}
                         for j, b in enumerate(o.get('body') or [])],
            })
    welcome = guide.get('welcome')
    return {
        'dateISO': guide.get('dateISO'),
        'season': guide.get('season'),
        'welcome': ({'heading': welcome.get('heading'),
                     'body': [{'j': j, 'text': b.get('text')}
                              for j, b in enumerate(welcome.get('body') or [])]}
                    if welcome else None),
        'order': order,
        'announcements': [
            {'i': i, 'heading': a.get('heading'), 'kind': a.get('kind'),
             'text': a.get('text')}
            for i, a in enumerate(guide.get('announcements') or [])],
        'specialEvents': [
            {'i': i, 'heading': ev.get('heading'),
             'paragraphs': ev.get('paragraphs')}
            for i, ev in enumerate(guide.get('specialEvents') or [])],
        'parserNotes': (guide.get('notes') or []) + (guide.get('warnings') or [])
                       + (guide.get('reviewedWarnings') or []),
    }


def verse_digest(guide):
    """The scripture items only — refs and verse text with their raw
    <sup>/<b>/… markup intact, indexed so fixes can address blocks."""
    items = []
    for i, o in enumerate(guide.get('order') or []):
        if o.get('kind') != 'item':
            continue
        body = o.get('body') or []
        if o.get('type') != 'scripture' and \
                not any(b.get('type') in ('ref', 'verse') for b in body):
            continue
        items.append({
            'i': i, 'type': o.get('type'), 'label': o.get('label'),
            'title': o.get('title'),
            'body': [{'j': j, 'type': b.get('type'), 'text': b.get('text')}
                     for j, b in enumerate(body)],
        })
    return {'dateISO': guide.get('dateISO'), 'scripture': items}


def photo_files(guide, photo_dir):
    """The guide's published photo crops that exist on disk, as
    [(imageIndex, filename, jpeg_bytes)]. Entries never materialized or
    whose crop is unreadable are skipped — there is nothing to look at."""
    out = []
    for i, im in enumerate(guide.get('images') or []):
        fn = im.get('image')
        if not fn or not re.fullmatch(r'photo-\d+-\d+\.jpg', fn):
            continue
        try:
            with open(os.path.join(photo_dir, fn), 'rb') as fh:
                data = fh.read()
        except OSError:
            continue
        if data:
            out.append((i, fn, data))
    return out


def _request(content, system, schema, model):
    if not isinstance(content, list):
        content = json.dumps(content, ensure_ascii=False, indent=1)
    return {
        'model': model,
        'max_tokens': 8192,
        'system': system,
        'messages': [{'role': 'user', 'content': content}],
        'output_config': {'format': {'type': 'json_schema',
                                     'schema': schema}},
        # Opus 5's safety classifiers can decline a request outright; the
        # server-side fallback re-runs it on Anthropic's recommended model
        # instead of failing the scan.
        'fallbacks': 'default',
    }


def build_request(guide, model=DEFAULT_MODEL):
    return _request(guide_digest(guide), SYSTEM_PROMPT, FINDINGS_SCHEMA, model)


def build_verse_request(guide, model=DEFAULT_MODEL):
    return _request(verse_digest(guide), VERSE_SYSTEM_PROMPT,
                    VERSE_FINDINGS_SCHEMA, model)


def build_photo_request(guide, photos, model=DEFAULT_MODEL):
    """photos: [(imageIndex, filename, jpeg_bytes)] from photo_files().
    Each crop goes to the model as an image block preceded by a JSON label
    carrying its index, filename, page, and caption."""
    inventory = guide.get('images') or []
    content = []
    for i, fn, data in photos:
        im = inventory[i] if 0 <= i < len(inventory) else {}
        content.append({'type': 'text', 'text': json.dumps(
            {'i': i, 'image': fn, 'page': im.get('page'),
             'caption': im.get('caption')}, ensure_ascii=False)})
        content.append({'type': 'image', 'source': {
            'type': 'base64', 'media_type': 'image/jpeg',
            'data': base64.b64encode(data).decode('ascii')}})
    return _request(content, PHOTO_SYSTEM_PROMPT, PHOTO_FINDINGS_SCHEMA,
                    model)


def http_transport(payload, api_key, timeout=600):
    """POST the Messages API request; retry rate limits and server errors."""
    body = json.dumps(payload).encode('utf-8')
    headers = {
        'x-api-key': api_key,
        'anthropic-version': '2023-06-01',
        'anthropic-beta': 'server-side-fallback-2026-07-01',
        'content-type': 'application/json',
    }
    for attempt in range(3):
        req = urllib.request.Request(API_URL, data=body, headers=headers,
                                     method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:400]
            if e.code in (429, 529) or e.code >= 500:
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
            raise RuntimeError(f'Claude API error {e.code}: {detail}') from None
        except OSError as e:
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(f'Claude API unreachable: {e}') from None


def scan_guide(guide, api_key, model=DEFAULT_MODEL, transport=None):
    """Run one classification review. Returns {'model', 'summary', 'usage',
    'findings'}; findings carry ids (f1, f2, …) and status 'open'."""
    transport = transport or http_transport
    response = transport(build_request(guide, model), api_key)
    return _parse_scan(response, model, prefix='f', ops=OPS)


def scan_verses(guide, api_key, model=DEFAULT_MODEL, transport=None):
    """Run the scripture verse-number agent over the guide's scripture
    items. Returns the same shape as scan_guide with ids v1, v2, … — or
    None (no API call) when the guide has no scripture to read."""
    if not verse_digest(guide)['scripture']:
        return None
    transport = transport or http_transport
    response = transport(build_verse_request(guide, model), api_key)
    return _parse_scan(response, model, prefix='v', ops=VERSE_OPS)


def scan_photos(guide, photo_dir, api_key, model=DEFAULT_MODEL,
                transport=None):
    """Run the photo reviewer over the guide's published photo crops in
    photo_dir. Returns the same shape as scan_guide with ids p1, p2, … — or
    None (no API call) when no crops are on disk to look at."""
    photos = photo_files(guide, photo_dir)
    if not photos:
        return None
    transport = transport or http_transport
    response = transport(build_photo_request(guide, photos, model), api_key)
    return _parse_scan(response, model, prefix='p', ops=PHOTO_OPS)


def _parse_scan(response, model, prefix, ops):
    stop = response.get('stop_reason')
    if stop == 'refusal':
        raise RuntimeError('the model declined to review this guide '
                           '(safety classifiers) — no findings produced')
    text = next((b.get('text') for b in response.get('content') or []
                 if b.get('type') == 'text'), None)
    if not text:
        raise RuntimeError(f'no text in Claude response (stop_reason={stop})')
    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError('Claude response was not valid JSON'
                           + (' (output truncated — max_tokens hit)'
                              if stop == 'max_tokens' else '')) from None
    findings = []
    for n, f in enumerate(data.get('findings') or [], 1):
        if not isinstance(f, dict) or not str(f.get('quote') or '').strip():
            continue
        fix = f.get('fix') if isinstance(f.get('fix'), dict) else None
        if fix and fix.get('op') not in ops:
            fix = None                      # "none" or unknown: flag-only
        findings.append({
            'id': f'{prefix}{n}',
            'path': str(f.get('path') or ''),
            'quote': str(f.get('quote'))[:200],
            'issue': str(f.get('issue') or ''),
            'current': f.get('current'),
            'proposed': f.get('proposed'),
            'confidence': f.get('confidence')
            if f.get('confidence') in ('high', 'medium', 'low') else 'low',
            'fix': fix,
            'status': 'open',
        })
    usage = response.get('usage') or {}
    return {
        'model': response.get('model') or model,
        'summary': str(data.get('summary') or ''),
        'usage': {'input': usage.get('input_tokens'),
                  'output': usage.get('output_tokens')},
        'findings': findings,
    }


# --- applying fixes ---------------------------------------------------------

def _plain(s):
    s = re.sub(r'<[^>]+>', '', str(s or ''))
    s = s.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    return re.sub(r'\s+', ' ', s).strip()


# The guides print typographic quotes, dashes, and spaces; the model often
# quotes them back as plain ASCII. Verification compares canonical forms so
# a straight apostrophe still matches the printed ’ — the guide text itself
# is never touched.
_CANON_MAP = str.maketrans({
    '‘': "'", '’': "'", '“': '"', '”': '"',
    '–': '-', '—': '-', '−': '-',
    ' ': ' ', '…': '...',
})


def _canon(s):
    s = _plain(s).translate(_CANON_MAP)
    return re.sub(r'\s+', ' ', s).strip().casefold()


def _item_text(item):
    parts = [item.get('label'), item.get('title'), item.get('who'),
             item.get('note')] + [b.get('text') for b in item.get('body') or []]
    return ' '.join(p for p in parts if p)


def _body_text(item):
    return ' '.join(b.get('text') or '' for b in item.get('body') or [])


def _plain_item(text):
    return {'kind': 'item', 'type': 'plain', 'label': None, 'title': None,
            'titleQuoted': False, 'who': None, 'note': None,
            'body': [{'type': 'para', 'text': text}]}


# A verse number can also occur in the passage prose ("crowd of 14 people"),
# so a fix is located by the finding's quote: the plain text around the
# number in the quote must match the text around the candidate occurrence.
# Zero or several surviving candidates refuse, never guess.

def _pick_span(text, spans, n, quote):
    if len(spans) > 1 and quote:
        q = _canon(quote)
        m = re.search(r'(?<!\d)' + re.escape(n) + r'(?!\d)', q)
        if m:
            before = q[:m.start()].strip()[-24:]
            after = q[m.end():].strip()[:24]
            if after:
                spans = [sp for sp in spans
                         if _canon(text[sp[1]:sp[1] + 200]).startswith(after)] \
                        or spans
            if len(spans) > 1 and before:
                spans = [sp for sp in spans
                         if _canon(text[max(0, sp[0] - 200):sp[0]])
                         .endswith(before)] or spans
    if not spans:
        return None, f'verse number {n} not found where the fix points — skipped'
    if len(spans) > 1:
        return None, (f'verse number {n} matches {len(spans)} places — '
                      'ambiguous, fix by hand')
    return spans[0], None


def _sup_verse_text(text, number, quote):
    """Wrap a bare verse number in <sup></sup>; returns (new_text, None) or
    (None, reason)."""
    if not isinstance(number, int) or number < 1:
        return None, f'bad verse number {number!r}'
    n = str(number)
    spans = []
    for m in re.finditer(r'(?<!\d)' + re.escape(n) + r'(?!\d)', text):
        s = m.start()
        if text.rfind('<', 0, s) > text.rfind('>', 0, s):
            continue                        # inside a tag
        if text.rfind('<sup>', 0, s) > text.rfind('</sup>', 0, s):
            continue                        # already superscripted
        spans.append((s, m.end()))
    span, reason = _pick_span(text, spans, n, quote)
    if reason:
        return None, reason
    s, e = span
    return text[:s] + '<sup>' + n + '</sup>' + text[e:], None


def _unsup_verse_text(text, number, quote):
    """Unwrap a wrongly superscripted number; returns (new_text, None) or
    (None, reason)."""
    if not isinstance(number, int) or number < 0:
        return None, f'bad number {number!r}'
    n = str(number)
    spans = [(m.start(), m.end())
             for m in re.finditer(r'<sup>\s*' + re.escape(n) + r'\s*</sup>',
                                  text)]
    span, reason = _pick_span(text, spans, n, quote)
    if reason:
        return None, reason
    s, e = span
    return text[:s] + n + text[e:], None


def _put_sup(text, s, e, n):
    """Replace text[s:e] with <sup>n</sup>, restoring the printed space
    when the number was fused onto the following word."""
    rep = '<sup>' + n + '</sup>'
    if e < len(text) and (text[e].isalnum() or text[e] == '<'):
        rep += ' '
    return text[:s] + rep + text[e:]


def _fix_verse_text(text, number, garbled, quote):
    """Replace an OCR-garbled verse-number run with the printed
    <sup>number</sup>; returns (new_text, None) or (None, reason)."""
    if not isinstance(number, int) or number < 1:
        return None, f'bad verse number {number!r}'
    g = str(garbled or '')
    plain_g = re.sub(r'<[^>]+>', '', g)
    if not g or not plain_g or len(plain_g) > 8:
        return None, 'garbled run missing or too long to be a verse number'
    if re.search(r'[A-Za-z]{3,}', plain_g):
        return None, 'garbled run looks like a word — refused'
    spans = []
    at = text.find(g)
    while at != -1:
        ok = True
        if '<' not in g:
            if text.rfind('<', 0, at) > text.rfind('>', 0, at):
                ok = False                  # inside a tag
        if ok:
            spans.append((at, at + len(g)))
        at = text.find(g, at + 1)
    # disambiguate by the quote: canonical context around the garbled run
    if len(spans) > 1 and quote and g in quote:
        qb, _, qa = quote.partition(g)
        after = _canon(qa)[:24]
        before = _canon(qb)[-24:]
        if after:
            spans = [sp for sp in spans
                     if _canon(text[sp[1]:sp[1] + 200]).startswith(after)] \
                    or spans
        if len(spans) > 1 and before:
            spans = [sp for sp in spans
                     if _canon(text[max(0, sp[0] - 200):sp[0]])
                     .endswith(before)] or spans
    if not spans:
        return None, 'garbled text not found where the fix points — skipped'
    if len(spans) > 1:
        return None, (f'garbled run appears in {len(spans)} places — '
                      'ambiguous, fix by hand')
    s, e = spans[0]
    return _put_sup(text, s, e, str(number)), None


def _insert_verse_text(text, number, quote):
    """Insert a verse number OCR dropped entirely, in front of the verse's
    first words (the quote must start exactly at them); returns
    (new_text, None) or (None, reason)."""
    if not isinstance(number, int) or number < 1:
        return None, f'bad verse number {number!r}'
    q = str(quote or '').strip()
    if len(re.sub(r'<[^>]+>', '', q)) < 10:
        return None, 'quote too short to anchor the insertion'
    spans = [m.start() for m in re.finditer(re.escape(q), text)]
    if not spans:
        return None, ('quoted verse text not found verbatim — cannot anchor '
                      'the insertion; fix by hand')
    if len(spans) > 1:
        return None, (f'quoted verse text appears in {len(spans)} places — '
                      'ambiguous, fix by hand')
    s = spans[0]
    return text[:s] + '<sup>' + str(number) + '</sup> ' + text[s:], None


def _apply_one(g, fix, quote):
    """Apply one verified fix in place; return None on success or a reason
    string on refusal (bad index, quote mismatch)."""
    op = fix.get('op')
    order = g.setdefault('order', [])
    anns = g.setdefault('announcements', [])

    def check(target_text):
        q = _canon(quote)
        if not q or q not in _canon(target_text):
            return 'quoted text not found where the fix points — skipped'
        return None

    if op in ('item_to_stage', 'stage_to_item', 'item_to_announcement',
              'para_to_announcement', 'para_to_stage', 'discard_para'):
        i = fix.get('orderIndex')
        if not isinstance(i, int) or not 0 <= i < len(order):
            return f'order index {i!r} out of range'
        entry = order[i]
        if op == 'item_to_stage':
            if entry.get('kind') != 'item':
                return 'target is not an item'
            err = check(_item_text(entry))
            if err:
                return err
            order[i] = {'kind': 'stage',
                        'text': _plain(_body_text(entry)) or _plain(entry.get('label'))}
        elif op == 'stage_to_item':
            if entry.get('kind') != 'stage':
                return 'target is not a stage direction'
            err = check(entry.get('text'))
            if err:
                return err
            order[i] = _plain_item(entry.get('text') or '')
        elif op == 'item_to_announcement':
            if entry.get('kind') != 'item':
                return 'target is not an item'
            err = check(_item_text(entry))
            if err:
                return err
            text = _body_text(entry).strip() or _plain(entry.get('title'))
            anns.append({'heading': fix.get('heading') or entry.get('label'),
                         'kind': 'note', 'text': re.sub(r'\s+', ' ', text)})
            del order[i]
        else:            # para_to_announcement / para_to_stage / discard_para
            if entry.get('kind') != 'item':
                return 'target is not an item'
            j = fix.get('blockIndex')
            body = entry.get('body') or []
            if not isinstance(j, int) or not 0 <= j < len(body):
                return f'body block {j!r} out of range'
            err = check(body[j].get('text'))
            if err:
                return err
            block = body.pop(j)
            if op == 'para_to_announcement':
                anns.append({'heading': fix.get('heading') or None,
                             'kind': 'note', 'text': block.get('text') or ''})
            elif op == 'para_to_stage':
                # a direction at the head of a body reads before its item
                at = i if j == 0 else i + 1
                order.insert(at, {'kind': 'stage',
                                  'text': _plain(block.get('text'))})
            # discard_para: page furniture — the block is simply dropped
        return None

    if op in ('discard_announcement', 'announcement_to_event',
              'announcement_to_stage'):
        i = fix.get('annIndex')
        if not isinstance(i, int) or not 0 <= i < len(anns):
            return f'announcement index {i!r} out of range'
        a = anns[i]
        err = check((a.get('heading') or '') + ' ' + (a.get('text') or ''))
        if err:
            return err
        del anns[i]
        if op == 'announcement_to_event':
            g.setdefault('specialEvents', []).append({
                'heading': fix.get('heading') or a.get('heading') or 'Coming Up',
                'paragraphs': [a.get('text') or ''],
                'note': None, 'sectionTitle': 'Coming Up'})
        elif op == 'announcement_to_stage':
            at = fix.get('orderIndex')
            if not isinstance(at, int) or not 0 <= at <= len(order):
                at = len(order)
            order.insert(at, {'kind': 'stage', 'text': _plain(a.get('text'))})
        return None

    if op == 'event_to_announcement':
        events = g.setdefault('specialEvents', [])
        i = fix.get('eventIndex')
        if not isinstance(i, int) or not 0 <= i < len(events):
            return f'event index {i!r} out of range'
        ev = events[i]
        err = check((ev.get('heading') or '') + ' '
                    + ' '.join(ev.get('paragraphs') or []))
        if err:
            return err
        del events[i]
        anns.append({'heading': ev.get('heading') or None, 'kind': 'note',
                     'text': re.sub(r'\s+', ' ', ' '.join(
                         ev.get('paragraphs') or [])).strip()})
        return None

    if op in VERSE_OPS:
        i = fix.get('orderIndex')
        if not isinstance(i, int) or not 0 <= i < len(order):
            return f'order index {i!r} out of range'
        entry = order[i]
        if entry.get('kind') != 'item':
            return 'target is not an item'
        j = fix.get('blockIndex')
        body = entry.get('body') or []
        if not isinstance(j, int) or not 0 <= j < len(body):
            return f'body block {j!r} out of range'
        text = body[j].get('text') or ''
        # sup/unsup verify the quote canonically (tags stripped); the OCR
        # repairs verify by their own raw anchors instead — a garbled quote
        # canonicalizes unpredictably.
        if op in ('sup_verse', 'unsup_verse'):
            err = check(text)
            if err:
                return err
            wrap = _sup_verse_text if op == 'sup_verse' else _unsup_verse_text
            new_text, reason = wrap(text, fix.get('number'), quote)
        elif op == 'fix_verse':
            new_text, reason = _fix_verse_text(text, fix.get('number'),
                                               fix.get('garbled'), quote)
        else:                               # insert_verse
            new_text, reason = _insert_verse_text(text, fix.get('number'),
                                                  quote)
        if reason:
            return reason
        body[j]['text'] = new_text
        return None

    if op == 'drop_photo':
        # verified by filename, not quote-in-text: the crop the model looked
        # at must be the crop the index points at.
        images = g.setdefault('images', [])
        i = fix.get('imageIndex')
        if not isinstance(i, int) or not 0 <= i < len(images):
            return f'image index {i!r} out of range'
        name = str(quote or fix.get('image') or '').strip()
        if not name or name != (images[i].get('image') or ''):
            return 'photo filename does not match where the fix points — skipped'
        del images[i]
        return None

    if op == 'welcome_to_announcement':
        body = (g.get('welcome') or {}).get('body') or []
        j = fix.get('blockIndex')
        if not isinstance(j, int) or not 0 <= j < len(body):
            return f'welcome block {j!r} out of range'
        err = check(body[j].get('text'))
        if err:
            return err
        block = body.pop(j)
        anns.append({'heading': fix.get('heading') or None, 'kind': 'note',
                     'text': block.get('text') or ''})
        return None

    return f'unknown fix op {op!r}'


def _relocate(g, op, quote):
    """A fix whose stored index went stale (earlier fixes, hand edits, or a
    re-convert shifted the guide) can often be recovered: search the whole
    guide for the quoted text among targets the op could act on. Returns
    (index_updates, reason) — updates only when exactly one target matches;
    zero or several matches refuse with a reason, never a guess."""
    q = _canon(quote)
    if not q:
        return None, None
    order = g.get('order') or []
    anns = g.get('announcements') or []
    if op in ('item_to_stage', 'item_to_announcement'):
        cands = [{'orderIndex': i} for i, o in enumerate(order)
                 if o.get('kind') == 'item' and q in _canon(_item_text(o))]
    elif op == 'stage_to_item':
        cands = [{'orderIndex': i} for i, o in enumerate(order)
                 if o.get('kind') == 'stage' and q in _canon(o.get('text'))]
    elif op in ('para_to_announcement', 'para_to_stage', 'discard_para',
                'sup_verse', 'unsup_verse', 'fix_verse', 'insert_verse'):
        cands = [{'orderIndex': i, 'blockIndex': j}
                 for i, o in enumerate(order) if o.get('kind') == 'item'
                 for j, b in enumerate(o.get('body') or [])
                 if q in _canon(b.get('text'))]
    elif op in ('discard_announcement', 'announcement_to_event',
                'announcement_to_stage'):
        cands = [{'annIndex': i} for i, a in enumerate(anns)
                 if q in _canon((a.get('heading') or '') + ' '
                                + (a.get('text') or ''))]
    elif op == 'event_to_announcement':
        cands = [{'eventIndex': i}
                 for i, ev in enumerate(g.get('specialEvents') or [])
                 if q in _canon((ev.get('heading') or '') + ' '
                                + ' '.join(ev.get('paragraphs') or []))]
    elif op == 'drop_photo':
        name = str(quote or '').strip()
        cands = [{'imageIndex': i}
                 for i, im in enumerate(g.get('images') or [])
                 if im.get('image') == name]
    elif op == 'welcome_to_announcement':
        cands = [{'blockIndex': j}
                 for j, b in enumerate((g.get('welcome') or {}).get('body') or [])
                 if q in _canon(b.get('text'))]
    else:
        return None, None
    if len(cands) == 1:
        return cands[0], None
    if not cands:
        return None, ('quoted text no longer exists in the guide — likely '
                      'already fixed or edited away; dismiss this finding')
    return None, (f'quoted text appears in {len(cands)} places — '
                  'ambiguous, fix by hand')


def _apply_relocated(g, fix, quote):
    """Apply at the stored index; if that fails, relocate the quote and
    retry once at the recovered position."""
    err = _apply_one(g, fix, quote)
    if err is None:
        return None
    updates, reason = _relocate(g, fix.get('op'), quote)
    if updates is not None:
        return _apply_one(g, {**fix, **updates}, quote)
    return reason or err


def apply_findings(guide, findings, ids):
    """Apply the selected findings to a copy of the guide. Returns
    (new_guide, results) where results maps finding id -> None (applied) or a
    reason string (skipped). Findings are applied highest-index first so
    earlier removals don't shift later targets."""
    g = copy.deepcopy(guide)
    chosen = [f for f in findings if f.get('id') in set(ids) and f.get('fix')]

    def sort_key(f):
        # Order-array ops run first (their appends to announcements don't
        # shift existing indices), then announcements ops (which may insert
        # into the already-settled order), then welcome/event ops (append
        # only). Within a group, highest index first so removals don't shift
        # later targets.
        op = f['fix'].get('op')
        if op in ('discard_announcement', 'announcement_to_event',
                  'announcement_to_stage'):
            rank, idx = 1, f['fix'].get('annIndex')
        elif op == 'welcome_to_announcement':
            rank, idx = 2, f['fix'].get('blockIndex')
        elif op == 'event_to_announcement':
            rank, idx = 3, f['fix'].get('eventIndex')
        elif op == 'drop_photo':
            rank, idx = 4, f['fix'].get('imageIndex')
        else:
            rank, idx = 0, f['fix'].get('orderIndex')
        blk = f['fix'].get('blockIndex')
        return (rank, -(idx if isinstance(idx, int) else 0),
                -(blk if isinstance(blk, int) else 0))

    results = {}
    for f in sorted(chosen, key=sort_key):
        results[f['id']] = _apply_relocated(g, f['fix'], f.get('quote') or '')
    if any(r is None for r in results.values()):
        # an untitled item whose body was entirely moved/discarded is an
        # empty husk — drop it rather than render a blank entry
        g['order'] = [o for o in g.get('order') or []
                      if not (o.get('kind') == 'item' and not o.get('label')
                              and not o.get('title') and not o.get('body'))]
    for f in findings:
        if f.get('id') in set(ids) and not f.get('fix'):
            results[f['id']] = 'no mechanical fix for this finding'
    return g, results

"""AI article scanner: review a published guide.json for OCR/parse
misclassifications — worship content vs page/stage directions vs
announcements — and repair them with text-preserving moves.

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

import copy
import json
import re
import time
import urllib.error
import urllib.request

API_URL = 'https://api.anthropic.com/v1/messages'
DEFAULT_MODEL = 'claude-opus-5'

OPS = ('item_to_stage', 'stage_to_item', 'item_to_announcement',
       'para_to_announcement', 'para_to_stage', 'discard_announcement',
       'announcement_to_event')

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
  from the misclassified text, including any <b>/<i>/<sup> markup, so the fix
  can be verified mechanically before it is applied.
- Address text by the indices given in the JSON: "i" for order and
  announcements entries, "j" for body blocks inside an order item.
- Only these mechanical fixes exist:
  - item_to_stage(orderIndex): an order item that is really a page direction.
  - stage_to_item(orderIndex): a stage entry that is really worship content.
  - item_to_announcement(orderIndex, heading): an order item that is really
    an announcement.
  - para_to_announcement(orderIndex, blockIndex, heading): one body block of
    an item that is really an announcement.
  - para_to_stage(orderIndex, blockIndex): one body block that is really a
    page direction.
  - discard_announcement(annIndex): an announcements entry that is page
    furniture / poster residue / a page direction fragment, not news.
  - announcement_to_event(annIndex, heading): an announcements entry that is
    really an upcoming special event.
  If a problem fits none of these, still report it with op "none" so a human
  can look. Unused fix fields are null.
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
                            'heading': {'type': ['string', 'null']},
                        },
                        'required': ['op', 'orderIndex', 'blockIndex',
                                     'annIndex', 'heading'],
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
                     'body': [b.get('text') for b in welcome.get('body') or []]}
                    if welcome else None),
        'order': order,
        'announcements': [
            {'i': i, 'heading': a.get('heading'), 'kind': a.get('kind'),
             'text': a.get('text')}
            for i, a in enumerate(guide.get('announcements') or [])],
        'specialEvents': [
            {'heading': ev.get('heading'), 'paragraphs': ev.get('paragraphs')}
            for ev in guide.get('specialEvents') or []],
        'parserNotes': (guide.get('notes') or []) + (guide.get('warnings') or [])
                       + (guide.get('reviewedWarnings') or []),
    }


def build_request(guide, model=DEFAULT_MODEL):
    return {
        'model': model,
        'max_tokens': 8192,
        'system': SYSTEM_PROMPT,
        'messages': [{
            'role': 'user',
            'content': json.dumps(guide_digest(guide), ensure_ascii=False, indent=1),
        }],
        'output_config': {'format': {'type': 'json_schema',
                                     'schema': FINDINGS_SCHEMA}},
        # Opus 5's safety classifiers can decline a request outright; the
        # server-side fallback re-runs it on Anthropic's recommended model
        # instead of failing the scan.
        'fallbacks': 'default',
    }


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
    """Run one review. Returns {'model', 'summary', 'usage', 'findings'};
    findings carry ids (f1, f2, …) and status 'open'."""
    transport = transport or http_transport
    response = transport(build_request(guide, model), api_key)
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
        if fix and fix.get('op') not in OPS:
            fix = None                      # "none" or unknown: flag-only
        findings.append({
            'id': f'f{n}',
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


def _apply_one(g, fix, quote):
    """Apply one verified fix in place; return None on success or a reason
    string on refusal (bad index, quote mismatch)."""
    op = fix.get('op')
    order = g.setdefault('order', [])
    anns = g.setdefault('announcements', [])

    def check(target_text):
        q = _plain(quote)
        if not q or q not in _plain(target_text):
            return 'quoted text not found where the fix points — skipped'
        return None

    if op in ('item_to_stage', 'stage_to_item', 'item_to_announcement',
              'para_to_announcement', 'para_to_stage'):
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
        else:                               # para_to_announcement / para_to_stage
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
            else:
                # a direction at the head of a body reads before its item
                at = i if j == 0 else i + 1
                order.insert(at, {'kind': 'stage',
                                  'text': _plain(block.get('text'))})
        return None

    if op in ('discard_announcement', 'announcement_to_event'):
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
        return None

    return f'unknown fix op {op!r}'


def apply_findings(guide, findings, ids):
    """Apply the selected findings to a copy of the guide. Returns
    (new_guide, results) where results maps finding id -> None (applied) or a
    reason string (skipped). Findings are applied highest-index first so
    earlier removals don't shift later targets."""
    g = copy.deepcopy(guide)
    chosen = [f for f in findings if f.get('id') in set(ids) and f.get('fix')]

    def sort_key(f):
        fix = f['fix']
        in_order = fix.get('op') not in ('discard_announcement',
                                         'announcement_to_event')
        idx = fix.get('orderIndex') if in_order else fix.get('annIndex')
        blk = fix.get('blockIndex')
        return (1 if in_order else 0,               # announcements ops first
                -(idx if isinstance(idx, int) else 0),
                -(blk if isinstance(blk, int) else 0))

    results = {}
    for f in sorted(chosen, key=sort_key):
        results[f['id']] = _apply_one(g, f['fix'], f.get('quote') or '')
    for f in findings:
        if f.get('id') in set(ids) and not f.get('fix'):
            results[f['id']] = 'no mechanical fix for this finding'
    return g, results

"""Merge a fresh conversion into a published, possibly hand-edited guide.

Re-converting a Sunday from its stored PDF picks up converter upgrades
(accent colors, markup fixes) but historically discarded hand edits. This
merge keeps the published guide as the skeleton — the operator's text,
structure, and classifications win — and enriches it from the fresh
conversion wherever that cannot conflict with an edit:

- a rich-text field whose canonical text (tags stripped, typography
  normalized — the aiscan matching rules) is identical to a freshly
  converted field adopts the fresh markup, gaining accent-color spans the
  old converter never produced;
- announcement/special-event headings with no accent set adopt the accent
  the fresh conversion detected for the same (canonical) heading;
- the page-image inventory (flyers, cover suppression) follows the fresh
  conversion, because that is what the re-conversion just wrote to disk.

Text the operator changed has no canonical match and passes through
untouched. Ambiguous matches (the same canonical text appearing with
different markup) are left alone rather than guessed at.
"""
from __future__ import annotations

import copy

from .aiscan import _canon


def _rich_slots(g):
    """Every rich-text slot in a guide as (container, key) pairs, so the
    same walk can read a fresh guide and write a merged one."""
    for b in (g.get('welcome') or {}).get('body') or []:
        yield b, 'text'
    for o in g.get('order') or []:
        if o.get('kind') == 'item':
            for b in o.get('body') or []:
                yield b, 'text'
    for a in g.get('announcements') or []:
        yield a, 'text'
    for ev in g.get('specialEvents') or []:
        paragraphs = ev.get('paragraphs') or []
        for i in range(len(paragraphs)):
            yield paragraphs, i
    for pr in g.get('prayerRequests') or []:
        yield pr, 'text'


def merge_guides(current, fresh):
    """(merged, stats) — `current` (the published, hand-edited guide)
    enriched from `fresh` (a new conversion of the same PDF)."""
    merged = copy.deepcopy(current)

    index = {}
    for container, key in _rich_slots(fresh):
        text = container[key]
        if not text or not str(text).strip():
            continue
        canon = _canon(text)
        if canon:
            index.setdefault(canon, set()).add(text)

    texts = 0
    for container, key in _rich_slots(merged):
        text = container[key]
        if not text:
            continue
        candidates = index.get(_canon(text))
        if candidates and len(candidates) == 1:
            new = next(iter(candidates))
            if new != text:
                container[key] = new
                texts += 1

    accents = 0
    for field in ('announcements', 'specialEvents'):
        by_heading = {}
        for entry in fresh.get(field) or []:
            if entry.get('heading') and entry.get('color'):
                by_heading.setdefault(_canon(entry['heading']), set()).add(entry['color'])
        for entry in merged.get(field) or []:
            if entry.get('color') or not entry.get('heading'):
                continue                    # hand-set accents are edits: kept
            colors = by_heading.get(_canon(entry['heading']))
            if colors and len(colors) == 1:
                entry['color'] = next(iter(colors))
                accents += 1

    # The re-conversion just (re)wrote the page images — the inventory must
    # match the disk, not the old guide.
    merged['flyers'] = fresh.get('flyers') or []
    if fresh.get('suppressCover'):
        merged['suppressCover'] = True
    else:
        merged.pop('suppressCover', None)
    merged['dateISO'] = fresh.get('dateISO') or merged.get('dateISO')
    if not merged.get('date'):
        merged['date'] = fresh.get('date')

    stats = {'texts': texts, 'accents': accents}
    if texts or accents:
        merged.setdefault('notes', []).append(
            f'refreshed from the printed PDF (hand edits kept): '
            f'{texts} text field(s) updated, {accents} heading accent(s) set')
    return merged, stats

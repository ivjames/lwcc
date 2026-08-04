"""Parse stage: extracted lines -> structured guide dict (guide.json).

The weekly guides follow a stable shape: masthead + date/season + cover on
page 1, the order of worship (bold ALL-CAPS labels, bracketed stage
directions, sheet music we skip), a community page (music team, prayer
requests, notes & announcements, special events), a GPS notes card (skipped)
and a Prayer Journal page (both flattened images, read via OCR).

Unrecognized content is never silently dropped: it is parsed generically
where possible and reported in ``warnings`` otherwise, so drift in older
backlog guides surfaces immediately.
"""
from __future__ import annotations

import re

from .extract import Line, Run

from .known_texts import KNOWN_TEXTS

MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
          'August', 'September', 'October', 'November', 'December']
# Accepts the plain form ("August 2, 2026") plus festival variants seen in the
# backlog: "May the 4th, 2025" and "April 20, 2025 – EASTER SUNDAY" (season on
# the same line after a dash).
_WEEKDAY = r'(?:(?:Sun|Mon|Tues|Wednes|Thurs|Fri|Satur)day,?\s+)?'
DATE_RE = re.compile(
    r'^' + _WEEKDAY + r'(' + '|'.join(MONTHS) + r')\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?,\s+(\d{4})'
    r'(?:\s*[–—-]\s*(.+))?$')
# The 2022-23 backlog flips the header: season first, date after the dash
# ("Second Sunday of Easter — April 16, 2023").
SEASON_DATE_RE = re.compile(
    r'^(?P<season>[^.!?:]{3,60}?)\s*[–—-]\s*' + _WEEKDAY
    + r'(?P<month>' + '|'.join(MONTHS) + r')'
    r'\s+(?:the\s+)?(?P<day>\d{1,2})(?:st|nd|rd|th)?,\s+(?P<year>\d{4})$')
# A date appearing anywhere in a run of OCR text (comma optional — OCR drops
# it sometimes), and a lifespan pair ("July 21, 1944 – November 29, 2025")
# as printed on Celebration of Life covers.
_DATE_INLINE = (r'(?:' + '|'.join(MONTHS) + r')\s+(?:the\s+)?\d{1,2}'
                r'(?:st|nd|rd|th)?,?\s+\d{4}')
DATE_INLINE_RE = re.compile(
    r'\b(' + '|'.join(MONTHS) + r')\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b')
LIFESPAN_RE = re.compile(_DATE_INLINE + r'\s*[–—-]\s*' + _DATE_INLINE)


def plausible_service_year(year):
    """This church's guides exist from the 2020s on; memorial covers carry
    lifespan dates ("July 21, 1944 – …") that must never become the service
    date."""
    return 2015 <= year <= 2100
SMALL_WORDS = {'of', 'the', 'to', 'for', 'and', 'a', 'an', 'in', 'on', 'with', 'at', 'by', 'or', 'nor', 'but'}

# Order-of-worship label kinds. `music` items carry a title whose engraved
# score we drop; `prayer` bodies render as spoken-prayer blocks; `litany`
# alternates paragraphs with bold congregation refrains.
LABEL_KINDS = [
    (re.compile(r'^CALL TO WORSHIP'), 'music'),
    (re.compile(r'^HYMN\b'), 'music'),
    (re.compile(r'^CLOSING HYMN'), 'music'),
    (re.compile(r'^OFFERTORY'), 'music'),
    (re.compile(r'^SENDING FORTH'), 'music'),
    (re.compile(r'^REFLECTION'), 'music'),
    (re.compile(r'^SPECIAL MUSIC'), 'music'),
    (re.compile(r'PROCESSION'), 'music'),
    (re.compile(r'^OPENING PRAYER'), 'prayer'),
    (re.compile(r'^OPENING WORDS'), 'plain'),
    (re.compile(r'CONFESSION'), 'prayer'),
    (re.compile(r'FORGIVENESS'), 'plain'),
    (re.compile(r'^INTRODUCTION'), 'plain'),
    (re.compile(r'BAPTISM'), 'plain'),
    (re.compile(r'^ANTHEM'), 'music'),
    (re.compile(r'PRAYER OF THE DAY'), 'prayer'),
    (re.compile(r'PRAYER FOR ILLUMINATION'), 'prayer'),
    (re.compile(r'PRAYER OF THANKSGIVING'), 'prayer'),
    (re.compile(r'^BLESSING OF'), 'plain'),
    (re.compile(r'^(THE )?SHARING OF'), 'plain'),
    (re.compile(r'^UNISON PRAYER'), 'prayer'),
    (re.compile(r'GREAT THANKSGIVING'), 'litany'),
    (re.compile(r'COMMUNION|EUCHARIST'), 'litany'),
    (re.compile(r'^BLESSING'), 'prayer'),
    (re.compile(r'^BENEDICTION'), 'prayer'),
    (re.compile(r'INTERCESSION'), 'litany'),
    (re.compile(r'^SCRIPTURE'), 'scripture'),
    (re.compile(r'^MESSAGE'), 'message'),
    (re.compile(r'^PRELUDE'), 'music'),
    (re.compile(r'^HOMILY'), 'message'),
    (re.compile(r'CANDLE LIGHTING|PASCHAL CANDLE'), 'plain'),
    # PASTOR/LITURGIST/CONGREGATION/LEADER/PEOPLE are dialog turns, not
    # sections — deliberately absent so title-case turns stay inside the
    # liturgy body they belong to.
    (re.compile(r'^PRESENTATION OF'), 'plain'),
    (re.compile(r'^STEWARDSHIP'), 'plain'),
    (re.compile(r'^FLOWERING'), 'plain'),
    (re.compile(r'^EASTER PROCLAMATION'), 'plain'),
    (re.compile(r'THANKSGIVING FOR WATER'), 'litany'),
]

SEASON_RE = re.compile(
    r'Sunday|Pentecost|Advent|Lent|Easter|Christmas|Epiphany|Ascension|Trinity|Creation',
    re.I)

SCRIPTURE_REF_RE = re.compile(
    r'^(?:[1-3]\s)?[A-Z][a-z]+(?:\s[A-Za-z]+)?\s\d+(?::\d+(?:[-–]\d+)?)?$')


def title_case(s):
    if '|' in s:
        return ' | '.join(title_case(part) for part in s.split('|'))
    words = s.strip().split()
    out = []
    for idx, w in enumerate(words):
        # Consonant-only all-caps words are acronyms (LWCC, GPS) — keep them.
        if re.fullmatch(r'[BCDFGHJKLMNPQRSTVWXZ]{2,}', w):
            out.append(w)
            continue
        lower = w.lower()
        if 0 < idx < len(words) - 1 and lower in SMALL_WORDS:
            out.append(lower)
        else:
            out.append(lower[:1].upper() + lower[1:])
    return ' '.join(out)


def straight_quotes(s):
    return re.sub(r'[‘’]', "'", re.sub(r'[“”]', '"', s))


def tidy_prose(s):
    s = re.sub(r'\s+', ' ', s)
    s = s.replace(' – ', ' — ')   # en → em dash between words, per house style
    return s.strip()


def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def runs_to_markup(runs, bold=True, italic=True, sup=True):
    """Serialize a line's runs to text with minimal inline markup (<b>, <i>,
    <sup>) that guide.json stores and the renderer trusts."""
    out = ''
    for r in runs:
        t = esc(r.text)
        if not t:
            continue
        if r.sup and sup:
            # Chapter-prefixed first verse ("9:1") renders as just the verse no.
            v = re.sub(r'^(\d+):(\d+)$', r'\2', t.strip())
            t = t.replace(t.strip(), f'<sup>{v}</sup>', 1)
        if not r.sup and ((r.b and bold) or (r.i and italic)):
            # keep leading/trailing whitespace outside the tags
            m = re.fullmatch(r'(\s*)([\s\S]*?)(\s*)', t)
            lead, core, tail = m.group(1), m.group(2), m.group(3)
            wrapped = core
            if core:
                if r.b and bold:
                    wrapped = f'<b>{wrapped}</b>'
                if r.i and italic:
                    wrapped = f'<i>{wrapped}</i>'
            t = lead + wrapped + tail
        out += t
    # merge adjacent identical tags split by the extractor
    out = out.replace('</b><b>', '').replace('</i><i>', '').replace('</sup><sup>', '')
    return re.sub(r'\s+', ' ', out).strip()


def line_is_stage(l):
    return bool(re.fullmatch(r'\[.*\]', l.text)) and all(r.i or not r.text.strip() for r in l.runs)


def match_label(l):
    """A label line: a bold ALL-CAPS run at (or, for liturgy sub-labels like
    the Communion parts, near) the left margin. The 2023-24 backlog also
    prints labels as plain single-run text — caps with the title/attribution
    in the same run ("PRAYER OF CONFESSION – Taylor White"), or title-case
    music labels ("Hymn:", "Offertory:") — accepted when the vocabulary
    knows them."""
    if l.left > 320 or not l.runs:
        return None
    first = l.runs[0]
    label = title = None
    rest = ''
    m = re.fullmatch(r"\s*([A-Z][A-Z'’&|\s]{2,}?):?\s*(?:[“\"](.+?)[”\"])?\s*",
                     first.text) if (first.b and l.left <= 160) else None
    if m:
        label = re.sub(r'\s+', ' ', m.group(1)).strip()
        if all(w.lower() in SMALL_WORDS for w in label.split(' ')):
            return None
        if l.left > 105:
            # Indented: only explicit sub-labels, not bold body text — need a
            # colon, a "— Speaker" attribution, or a known liturgical label.
            rest_text = ''.join(r.text for r in l.runs[1:])
            if not (first.text.rstrip().endswith(':')
                    or re.search(r'[—–]', rest_text)
                    or kind_for(label) != 'plain'):
                return None
        title = m.group(2)
        # Rest of the line: optional title (if not inside the bold run) and
        # "— Speaker[, role]" attribution.
        rest = ''.join(r.text for r in l.runs[1:])
    else:
        # Centered labels sit well right of the margin; junk prefixes
        # (backticks, emoji-placeholder backslashes) precede some. The
        # vocabulary constraint is what keeps this fallback honest.
        text = l.text.strip().lstrip('`\\').strip()
        lm = re.match(r"([A-Z][A-Z'’&|\s-]{2,}?)\s*(?=:|[–—]|[“\"]|$)", text)
        cand = re.sub(r'\s+', ' ', lm.group(1)).strip().rstrip('- ') if lm else ''
        if cand and known_label(cand) \
                and not all(w.lower() in SMALL_WORDS for w in cand.split(' ')):
            label = cand
            rest = text[lm.end():].lstrip(': ').strip()
        else:
            # The early-2023 backlog prints labels in Title Case ("Opening
            # Prayer – Kelly…", "The Sharing of the Cup"): accept short
            # candidates the vocabulary knows once uppercased. Sentences
            # protect themselves — trailing periods break the lookahead
            # and long candidates are rejected.
            tc = re.match(r"([A-Z][A-Za-z'’&\s]{2,38}?)\s*(?=:|[–—]|[“\"]|$)", text)
            cand2 = re.sub(r'\s+', ' ', tc.group(1)).strip() if tc else ''
            if cand2 and len(cand2.split()) <= 5 and known_label(cand2.upper()) \
                    and not all(w.lower() in SMALL_WORDS for w in cand2.split(' ')):
                label = cand2.upper()
                rest = text[tc.end():].lstrip(': ').strip()
            else:
                tm = re.match(r"(Hymn|Closing Hymn|Offertory|Anthem|Special Music)"
                              r"\s*:\s*(.*)$", text)
                if not tm:
                    return None
                label = tm.group(1).upper()
                rest = tm.group(2)
    if not title:
        tm = re.search(r'[“"](.+?)[”"]', rest)
        if tm:
            title = tm.group(1)
            rest = rest.replace(tm.group(0), '', 1)
    who = None
    quoted = title is not None
    wm = re.search(r'[—–]\s*(.+?)\s*$', rest)
    if wm:
        who = tidy_prose(wm.group(1))
        rest = rest[:wm.start()]
    if who is not None and not re.search(r'\w', who):
        who = None                       # dash residue, not an attribution
    # Unquoted title after the colon, e.g. "CALL TO WORSHIP: Doxology"
    leftover = rest.strip().lstrip(':').strip()
    if not title and leftover:
        title = leftover
        quoted = False
    elif title and who is None and re.search(r'\w', leftover):
        # Performer follows the quoted title without a dash, 2023 style:
        # 'Offertory: "Alleluia! Give the Glory" David Albulario'
        who = tidy_prose(leftover)
    return {
        'label': label,
        'title': straight_quotes(title) if title else None,
        'titleQuoted': quoted,
        'who': who,
    }


def kind_for(label):
    for regex, kind in LABEL_KINDS:
        if regex.search(label):
            return kind
    return 'plain'


def known_label(label):
    """True when the vocabulary recognizes the label (kind_for falls back to
    'plain' either way, but unknown labels warn)."""
    return any(regex.search(label) for regex, _ in LABEL_KINDS)


# --- body assembly ---------------------------------------------------------

def group_paragraphs(lines, extra_boundary=None):
    """Group consecutive lines into paragraphs using vertical rhythm: a gap
    noticeably larger than the running line leading starts a new paragraph.
    Page breaks join silently (paragraphs flow across pages)."""
    paras = []
    cur = None
    prev = None
    for l in lines:
        new_page = prev is not None and l.page != prev.page
        gap = l.top - prev.top if prev is not None and not new_page else 0
        boundary = prev is not None and (
            (not new_page and gap > prev.height * 1.5)
            or (extra_boundary is not None and extra_boundary(prev, l)))
        if cur is None or boundary:
            cur = []
            paras.append(cur)
        cur.append(l)
        prev = l
    return paras


def all_bold_line(l):
    return all(r.b or not r.text.strip() for r in l.runs)


def prayer_text(lines):
    """Prayers set as verse (hanging indents) keep their line structure;
    prayers set as plain wrapped text flow into one paragraph."""
    lefts = sorted({l.left for l in lines})
    base = lefts[0]
    has_indents = any(left > base + 20 for left in lefts)
    if not has_indents:
        return tidy_prose(' '.join(runs_to_markup(l.runs, bold=False) for l in lines))
    out = ''
    for l in lines:
        t = runs_to_markup(l.runs, bold=False)
        out += ('' if out == '' else '\n' if l.left <= base + 20 else ' ') + t
    return out.strip()


def scripture_body(lines):
    blocks = []
    verse = None
    for l in lines:
        if all(r.b or not r.text.strip() for r in l.runs) and SCRIPTURE_REF_RE.match(l.text):
            blocks.append({'type': 'ref', 'text': l.text})
            verse = None
        else:
            if verse is None:
                verse = {'type': 'verse', 'text': ''}
                blocks.append(verse)
            markup = runs_to_markup(l.runs, bold=False, italic=False)
            verse['text'] += (' ' if verse['text'] else '') + markup
    for b in blocks:
        b['text'] = re.sub(r'\s+', ' ', b['text']).strip()
    return blocks


def litany_body(lines):
    blocks = []
    # Congregation refrains are fully bold lines; split on the style change
    # even when the vertical gap is tight.
    for para in group_paragraphs(lines, lambda a, b: all_bold_line(a) != all_bold_line(b)):
        if all(all_bold_line(l) for l in para):
            blocks.append({'type': 'refrain', 'text': tidy_prose(
                ' '.join(runs_to_markup(l.runs, bold=False) for l in para))})
        else:
            blocks.append({'type': 'para', 'text': tidy_prose(
                ' '.join(runs_to_markup(l.runs) for l in para))})
    return blocks


def plain_body(lines):
    return [{'type': 'para', 'text': tidy_prose(' '.join(runs_to_markup(l.runs) for l in para))}
            for para in group_paragraphs(lines)]


def build_item_body(kind, lines, item):
    if lines:
        # Hymn credit blobs sometimes trail the last music items inside the
        # order-of-worship pages (not just on the community page) — drop any
        # paragraph carrying attribution markers, same policy as elsewhere:
        # the site reproduces no music or lyrics, so no credits apply.
        kept = []
        for para in group_paragraphs(lines):
            if any(BOX_CREDITS_RE.search(l.text) for l in para):
                continue
            kept.extend(para)
        lines = kept
    if not lines:
        if kind == 'music' and item['title'] and item['title'] in KNOWN_TEXTS:
            return [{'type': 'prayer', 'text': KNOWN_TEXTS[item['title']]}]
        return []
    # An italic parenthetical right after the label is a performance/translation note.
    if lines and re.fullmatch(r'\(.*\)', lines[0].text) and \
            all(r.i or not r.text.strip() for r in lines[0].runs):
        item['note'] = re.sub(r'^\((.*)\)$', r'\1', lines[0].text)
        lines = lines[1:]
    if kind == 'prayer':
        return [{'type': 'prayer', 'text': prayer_text(lines)}] if lines else []
    if kind == 'scripture':
        return scripture_body(lines)
    if kind == 'litany':
        return litany_body(lines)
    return plain_body(lines)


# --- community page (music team / prayer requests / announcements) ---------

CALENDAR_BLOCK_RE = re.compile(r'^(' + '|'.join(MONTHS) + r')\s+\d{1,2}:\s')
# Giant display words from poster art bleeding onto content pages
# ("PEACE LOVE", "JOY KINDNESS") — no lowercase, no digits, no colon.
DISPLAY_CAPS_RE = re.compile(r"^[A-Z][A-Z\s&'!,-]{2,40}$")


# Recurring page furniture on community/back pages — hymn-quote art, the
# masthead vision/mission block, the squished church-name graphic, and
# stray display fragments. Matched against a box's first line; discarded
# silently (it recurs weekly and is design, not news).
FIXTURE_BLOCK_RE = re.compile(
    r'^(Let us build a house where hands will reach beyond\b.*|'
    r'Our (Vision|Mission):.*|'
    r'LEISURE\s?WORLD\s?COMMUNITY\s?CHURCH$|'
    r'continues!$)')


def poster_residue(line_texts):
    """Small display fragments interleaved with poster art on a mixed page —
    the connective and date/time lines an all-caps rule can't catch
    ("presents", "SUNDAY / APRIL 27 AT 3:00 PM"): a handful of short lines
    with no sentence prose. Only consulted after the page has already shed an
    all-caps poster block, so ordinary announcements never reach it."""
    texts = [t.strip() for t in line_texts if t.strip()]
    if not texts or len(texts) > 3:
        return False
    for t in texts:
        if len(t) > 40 or len(t.split()) > 6:
            return False
        if re.search(r'[.!?;]|:(?!\d)', t):
            return False          # sentence punctuation — real prose
    return True

COMMUNITY_HEAD_RE = re.compile(
    r'^\*?\s*(PRAYER REQUESTS|NOTES AND ANNOUNCEMENTS|Notes and Announcements|Music Team\b)')


def split_boxes(lines):
    boxes = []
    cur = None
    prev = None
    for l in lines:
        new_page = prev is not None and l.page != prev.page
        gap = l.top - prev.top if prev is not None and not new_page else float('inf')
        if cur is None or gap > 60 or new_page or COMMUNITY_HEAD_RE.match(l.text):
            cur = []
            boxes.append(cur)
        cur.append(l)
        prev = l
    return boxes


CREDITS_LINE_RE = re.compile(r'©|\bMusic by\b|\bWords by\b|\barr\.|“')
BOX_CREDITS_RE = re.compile(r'©|\bMusic by\b|\bWords by\b|\barr\.')


def parse_music_team(lines):
    """Names/roles. The hymn attribution/copyright block that often sits
    directly under them is detected and discarded — the site reproduces
    neither the engraved music nor the lyrics, so their licensing credits
    don't apply (and the multi-column block extracts as scrambled text)."""
    team_lines = []
    for l in lines:
        if CREDITS_LINE_RE.search(l.text):
            break
        team_lines.append(l)
    lines = team_lines
    team = []
    name = None
    for l in lines:
        for r in l.runs:
            t = re.sub(r'^Music Team:?', '', r.text).strip()
            if not t:
                continue
            if r.i:
                if name:
                    role = re.sub(r'\s*/\s*', ' / ', t)
                    role = re.sub(r'[,\s]+$', '', role)
                    team.append({'name': name, 'role': role})
                name = None
            elif not r.b or not re.match(r'^MUSIC', t, re.I):
                name = re.sub(r'[,\s]+$', '', t)
    return team


def parse_prayer_requests(lines):
    """Heading line, then entries: bold name + " – " + text; paragraph gap
    separates entries."""
    entries = []
    for para in group_paragraphs(lines[1:]):
        runs = [r for l in para for r in l.runs]
        name_run = next((r for r in runs if r.b), None)
        name = tidy_prose(name_run.text) if name_run else None
        if name and len(name) <= 40:
            text = tidy_prose(' '.join(
                ''.join(r.text for r in l.runs if not r.b) for l in para))
            text = re.sub(r'^[—–-]\s*', '', text)
        else:
            # no short bold name — keep the whole paragraph, markup intact
            name = None
            text = tidy_prose(' '.join(runs_to_markup(l.runs) for l in para))
        entries.append({'name': name, 'text': text})
    return entries


def parse_announcements(lines):
    items = []
    for para in group_paragraphs(lines[1:]):
        text = tidy_prose(' '.join(runs_to_markup(l.runs) for l in para))
        # Sub-item heading: an ALL-CAPS bold prefix ending at a colon, e.g.
        # "<b>FLOWERS</b> &amp; <b>FELLOWSHIP:</b> Today's flowers…" (the
        # rainbow-colored letters split into several bold runs).
        m = re.match(r'^(<b>[^:]{2,110}?):(</b>)?\s*([\s\S]*)$', text)
        plain_head = None
        mostly_caps = False
        if m:
            plain_head = re.sub(r'<[^>]+>', '', m.group(1)).replace('&amp;', '&').strip()
            alpha = [c for c in plain_head if c.isalpha()]
            mostly_caps = bool(alpha) and sum(c.isupper() for c in alpha) / len(alpha) >= 0.6
        if m and mostly_caps and re.fullmatch(r"[A-Za-z0-9\s&'’…!?.,-]+", plain_head):
            heading = title_case(plain_head)
            kind = 'attendance' if re.search(r'attendance', heading, re.I) else 'note'
            body = tidy_prose(re.sub(r'^</b>\s*', '', m.group(3)))
            if kind == 'attendance':
                body = re.sub(r'</?[bi]>', '', body)  # keep only <sup>
            items.append({'heading': heading, 'text': body, 'kind': kind})
        elif any(BOX_CREDITS_RE.search(l.text) for l in para):
            continue          # stray hymn-credits lines — not announcements
        elif all(DISPLAY_CAPS_RE.fullmatch(l.text) for l in para):
            continue          # poster display-text fragments
        elif items:
            items[-1]['text'] += ' ' + text
        else:
            items.append({'heading': None, 'text': text, 'kind': 'note'})
    return items


def parse_special_event(lines):
    """First line starts with a bold-italic display heading; an italic closing
    line is a footnote (e.g. suggested donation)."""
    head_text = lines[0].runs[0].text.strip()
    heading = head_text
    today_match = re.fullmatch(r'(.*?)\s*[—–-]?\s*(TODAY!?)', head_text)
    if today_match:
        heading = f"{title_case(re.sub(r'!$', '', today_match.group(1)))} — {today_match.group(2)}"
    elif head_text == head_text.upper():
        heading = title_case(re.sub(r'[:!]$', '', head_text))

    # drop the heading run, then peel an italic footnote line off the end
    first = lines[0]
    stripped_first = type(first)(
        page=first.page, top=first.top, bottom=first.bottom, left=first.left,
        height=first.height, runs=first.runs[1:], text=first.text)
    body = [stripped_first] + list(lines[1:])
    note = None
    last = body[-1]
    if len(body) > 1 and all(r.i or not r.text.strip() for r in last.runs):
        note = tidy_prose(last.text)
        body = body[:-1]
    if body and not body[0].runs and len(body) == 1:
        paras = []
    else:
        paras = [p for p in (
            tidy_prose(' '.join(runs_to_markup(l.runs, italic=False) for l in para))
            for para in group_paragraphs(body)) if p]
    return {
        'heading': heading,
        'paragraphs': paras,
        'note': note,
        'sectionTitle': 'This Afternoon' if re.search(r'TODAY', head_text, re.I) else 'Coming Up',
    }


# --- journal (OCR) ---------------------------------------------------------

def journal_from_lines(lines):
    """Prayer Journal page that has a real text layer (newer guides are
    flattened images handled by OCR; older ones are plain text). Knows the
    Household Morning/Evening shape and generic titled prayers such as
    "A Prayer Before Reading the News:" with a "— written by …" attribution."""
    journal = {'subtitle': None, 'morning': None, 'midday': None, 'evening': None,
               'sections': []}
    section = None            # 'morning' | 'evening' | dict in sections
    for l in lines:
        t = l.text
        if re.fullmatch(r'Prayer Journal', t, re.I):
            continue
        if re.match(r'^Use this prayer', t, re.I):
            journal['subtitle'] = t
            continue
        hp = re.match(r'^Household Prayer:\s*(Morning|Evening)\s*(.*)$', t, re.I)
        if hp:
            section = hp.group(1).lower()
            if hp.group(2):
                journal[section] = hp.group(2).strip()
            continue
        if re.search(r'Upper Room|Midday Prayer', t, re.I):
            journal['midday'] = (journal['midday'] + ' ' + t) if journal['midday'] else t
            section = None
            continue
        if len(t) < 70 and t.endswith(':'):
            section = {'heading': t.rstrip(':').strip(), 'text': None, 'attribution': None}
            journal['sections'].append(section)
            continue
        if isinstance(section, dict) and re.match(r'^[—–-]\s*\S', t) \
                and all(r.i or not r.text.strip() for r in l.runs):
            section['attribution'] = re.sub(r'^[—–-]\s*', '', t)
            continue
        if isinstance(section, dict):
            section['text'] = (section['text'] + ' ' + t) if section['text'] else t
        elif section:
            journal[section] = (journal[section] + ' ' + t) if journal[section] else t
    journal['sections'] = [s for s in journal['sections'] if s['text']]
    if journal['morning'] or journal['evening'] or journal['sections']:
        return journal
    return None


GPS_PAGE_RE = re.compile(r'Notes from the Service|My Prayers this week', re.I)

# An engraved hymn scanned/photocopied into the bulletin: its lyrics OCR as
# hyphenated syllables ("chan - nel of your peace"), which prose never
# produces. Measured on real scans: score pages hit 12-25 breaks, every
# other page type hits 0.
SYLLABLE_BREAK_RE = re.compile(r'[a-z] ?- [a-z]')
SCORE_CREDITS_RE = re.compile(r'©|Music by|CCLI|OCP|Transcribed|Adapted')


def score_page(ocr_text):
    breaks = len(SYLLABLE_BREAK_RE.findall(ocr_text))
    return breaks >= 4 or (breaks >= 2 and bool(SCORE_CREDITS_RE.search(ocr_text)))


def ocr_lines(p):
    """Pseudo-Lines from a typed scan's OCR text: page number, vertical
    order, paragraph gaps (blank lines widen the gap so split_boxes sees
    paragraphs), and bracket-only lines marked italic so stage directions
    survive. Position and boldness are unknowable — label matching rides
    the vocabulary fallback, which these bulletins' 2023-style labels use
    anyway."""
    lines = []
    top = 0
    for raw in (p.ocr_text or '').split('\n'):
        t = ' '.join(raw.split())
        # OCR renders the printed en dash as an ASCII hyphen; the label
        # machinery splits attribution on [–—].
        t = re.sub(r'(?<=\S) - (?=\S)', ' – ', t)
        if not t:
            top += 50
            continue
        italic = bool(re.fullmatch(r'\[.*\]', t))
        lines.append(Line(page=p.number, top=top, bottom=top + 12, left=50,
                          height=12, runs=[Run(text=t, i=italic)], text=t))
        top += 20
    return lines


def text_journal_blob(page_lines):
    """A text-layer Prayer Journal page, reshaped into the paragraph blob
    parse_journal expects: paragraph breaks forced at the known section
    markers (visual box gaps are unreliable — the title, subtitle, and
    morning prayer often share one box)."""
    blob = ' '.join(l.text for l in page_lines).replace('\\', ' ').strip()
    blob = re.sub(r'\s*(Household Prayer:\s*(?:Morning|Evening)'
                  r'|Consider The Upper Room|Use this prayer)',
                  r'\n\n\1', blob)
    return re.sub(r'^\s*(Prayer Journal)\s*', r'\1\n\n', blob)


def parse_journal(ocr_text):
    paras = [re.sub(r'\s+', ' ', p).strip()
             for p in re.split(r'\n\s*\n', ocr_text)]
    paras = [p for p in paras if p]
    journal = {'subtitle': None, 'morning': None, 'midday': None, 'evening': None,
               'sections': []}
    section = None
    for p in paras:
        if re.fullmatch(r'Prayer Journal', p, re.I):
            continue
        if re.match(r'^Use this prayer', p, re.I):
            journal['subtitle'] = p
            continue
        hp = re.match(r'^Household Prayer:\s*(Morning|Evening)\s*(.*)$', p, re.I | re.S)
        if hp:
            section = hp.group(1).lower()
            if hp.group(2):
                journal[section] = re.sub(r'\s+', ' ', hp.group(2)).strip()
            continue
        if re.search(r'Midday Prayer|^Consider', p, re.I):
            journal['midday'] = p
            section = None
            continue
        gm = re.match(r'^([^:]{2,60}):\s*([\s\S]*)$', p) if p.rstrip().endswith(':') or ':' in p[:60] else None
        if gm and not section and gm.group(1).strip() and len(gm.group(1)) < 60:
            journal.setdefault('sections', []).append(
                {'heading': gm.group(1).strip(), 'text': gm.group(2).strip() or None,
                 'attribution': None})
            section = 'generic'
            continue
        if section == 'generic' and journal.get('sections'):
            cur = journal['sections'][-1]
            if re.match(r'^[—–-]\s*\S', p):
                cur['attribution'] = re.sub(r'^[—–-]\s*', '', p)
            else:
                cur['text'] = (cur['text'] + ' ' + p) if cur['text'] else p
            continue
        if section and section != 'generic' and not journal[section]:
            journal[section] = p
            continue
        if section and section != 'generic':
            journal[section] += ' ' + p
    journal['sections'] = [s for s in journal.get('sections') or [] if s['text']]
    if journal['morning'] or journal['evening'] or journal['sections']:
        return journal
    return None


# --- main ------------------------------------------------------------------

def parse(extracted, opts=None):
    warnings = list(extracted.warnings or [])
    # Two tiers: warnings mean content may be lost or wrong (review-worthy);
    # notes mean content was kept but classified loosely (recorded in the
    # history, no review needed).
    notes = []
    guide = {
        'date': None, 'dateISO': None, 'season': None,
        'series': None, 'coverAlt': None,
        'welcome': None, 'order': [],
        'musicTeam': [], 'prayerRequests': [], 'announcements': [],
        'specialEvents': [], 'flyers': [], 'journal': None,
        'warnings': warnings,
        'notes': notes,
    }

    text_pages = [p for p in extracted.pages if p.lines]
    if not text_pages:
        # A typed bulletin scanned without a text layer: the categories are
        # all there — read them. Structure the pages whose OCR reads as
        # prose; leave art/poster, score, journal, and GPS pages to the
        # image pipeline; adopt the result only when enough known labels
        # emerge (pure-art scans stay facsimiles).
        candidates = []
        for p in extracted.pages:
            if not p.ocr_text or score_page(p.ocr_text) \
                    or GPS_PAGE_RE.search(p.ocr_text) \
                    or re.search(r'\bGROW\b[\s\S]*\bPRAY\b[\s\S]*\bSTUDY\b', p.ocr_text) \
                    or re.search(r'Prayer Journal', p.ocr_text, re.I):
                continue
            if len(p.ocr_text.split()) < 60:
                continue          # short display text — an art page
            candidates.append((p, ocr_lines(p)))
        hits = sum(1 for _, ls in candidates for l in ls if match_label(l))
        if hits >= 4:
            for p, ls in candidates:
                p.lines = ls
            text_pages = [p for p in extracted.pages if p.lines]
            guide['suppressCover'] = True   # page 1 is content, not cover art
            notes.append('structured from OCR of a scanned typed bulletin — '
                         'text may carry OCR misreads; the Original PDF link '
                         'shows the printed page')
    all_lines = [l for p in text_pages for l in p.lines]

    # -- page 1 header: date + season --------------------------------------
    i = 0
    while i < len(all_lines):
        l = all_lines[i]
        dm = DATE_RE.match(l.text)
        if dm and (not plausible_service_year(int(dm.group(3)))
                   or (dm.group(4) and DATE_INLINE_RE.match(dm.group(4)))):
            dm = None    # lifespan line on a memorial cover — not a service date
        sm = None if dm else SEASON_DATE_RE.match(l.text)
        if sm and not plausible_service_year(int(sm.group('year'))):
            sm = None
        if sm:
            # season leads the date line in the 2022-23 backlog format
            season = sm.group('season')
            guide['season'] = title_case(season) if season == season.upper() else season
            guide['date'] = l.text[sm.start('month'):]
            month = str(MONTHS.index(sm.group('month')) + 1).zfill(2)
            guide['dateISO'] = f"{sm.group('year')}-{month}-{sm.group('day').zfill(2)}"
            i += 1
            break
        if dm:
            tail = dm.group(4)
            if tail:
                # "April 20, 2025 – EASTER SUNDAY": season rides the date line
                guide['date'] = l.text[:dm.start(4)].rstrip(' –—-')
                guide['season'] = title_case(tail) if tail == tail.upper() else tail
            else:
                guide['date'] = l.text
            month = str(MONTHS.index(dm.group(1)) + 1).zfill(2)
            guide['dateISO'] = f"{dm.group(3)}-{month}-{dm.group(2).zfill(2)}"
            nxt = all_lines[i + 1] if i + 1 < len(all_lines) else None
            if guide['season'] is None and nxt is not None and not match_label(nxt) \
                    and len(nxt.text) < 45 \
                    and (any(r.i for r in nxt.runs) or SEASON_RE.search(nxt.text)):
                guide['season'] = nxt.text
                i += 1
            i += 1
            break
        if match_label(l):
            break  # no date found before content
        i += 1
    if not guide['date']:
        # Scanned/flattened guides have no text layer at all — the date only
        # exists inside the page images. Fall back to OCR, skipping lifespan
        # pairs and implausible years (memorial covers).
        for p in extracted.pages:
            if not p.ocr_text:
                continue
            lifespans = [m.span() for m in LIFESPAN_RE.finditer(p.ocr_text)]
            for m in DATE_INLINE_RE.finditer(p.ocr_text):
                if any(s <= m.start() < e for s, e in lifespans):
                    continue
                year = int(m.group(3))
                if not plausible_service_year(year):
                    continue
                month = str(MONTHS.index(m.group(1)) + 1).zfill(2)
                day = str(int(m.group(2))).zfill(2)
                guide['date'] = f'{m.group(1)} {int(m.group(2))}, {year}'
                guide['dateISO'] = f'{year}-{month}-{day}'
                warnings.append(f'page {p.number}: service date read from '
                                f'page-image OCR: {guide["date"]} — verify')
                break
            if guide['date']:
                break
    if not guide['date']:
        warnings.append('no service date found on page 1')

    # -- order of worship ---------------------------------------------------
    current = None   # {'item':…, 'kind':…, 'lines':[…]}

    def flush():
        nonlocal current
        if current is None:
            return
        current['item']['body'] = build_item_body(
            current['kind'], current['lines'], current['item'])
        current = None

    community_start = -1
    while i < len(all_lines):
        l = all_lines[i]
        if re.match(r'^\*?\s*Music Team\b', l.text) or re.match(r'^PRAYER REQUESTS', l.text) \
                or re.match(r'^(NOTES AND ANNOUNCEMENTS|Notes and Announcements)', l.text):
            community_start = i
            break
        if line_is_stage(l):
            flush()
            guide['order'].append({'kind': 'stage', 'text': re.sub(r'^\[(.*)\]$', r'\1', l.text)})
            i += 1
            continue
        label = match_label(l)
        if label:
            flush()
            if re.match(r'^OPENING ANNOUNCEMENTS|^WELCOME|^GREETING', label['label']):
                guide['welcome'] = {'heading': title_case(label['label']),
                                    'who': label['who'], 'body': []}
                current = {'item': guide['welcome'], 'kind': 'plain', 'lines': []}
                i += 1
                continue
            kind = kind_for(label['label'])
            item = {
                'kind': 'item', 'type': kind,
                'label': title_case(label['label']),
                'title': label['title'], 'titleQuoted': label['titleQuoted'],
                'who': label['who'], 'note': None, 'body': [],
            }
            if kind == 'message':
                guide['series'] = {'title': label['title'], 'by': label['who']}
            if kind == 'plain' and not any(rx.search(label['label']) for rx, _ in LABEL_KINDS):
                # generic fallback — fine, but let the operator know
                if not re.search(r'QUESTIONS? FOR REFLECTION|INVITATION TO THE OFFERING',
                                 label['label']):
                    notes.append(
                        f'unrecognized order-of-worship label "{label["label"]}" (parsed generically)')
            guide['order'].append(item)
            current = {'item': item, 'kind': kind, 'lines': []}
            i += 1
            continue
        if current is None:
            if not any(o['kind'] == 'item' for o in guide['order']) \
                    and all(r.i or not r.text.strip() for r in l.runs):
                # Italic instructions before any item (e.g. Easter's "Please
                # gather in the narthex") read as stage directions.
                guide['order'].append({'kind': 'stage', 'text': l.text})
                i += 1
                continue
            # Body text after a stage direction with no new label. The 2025
            # format puts a stage direction between a label (typically
            # PRAYERS OF INTERCESSION) and its text: if the last labeled item
            # is still bodyless and isn't a music item, this is its body —
            # reattach silently. Otherwise (e.g. the Great Thanksgiving
            # flowing on after Communion versicles) keep an untitled item so
            # nothing is lost, and flag it for review.
            prev = next((o for o in reversed(guide['order']) if o['kind'] == 'item'), None)
            if prev is not None and not prev['body'] and prev['type'] != 'music':
                current = {'item': prev, 'kind': prev['type'], 'lines': []}
            elif (prev is not None and re.search(r'COMMUNION|EUCHARIST', prev['label'] or '', re.I)) \
                    or re.match(r'^It is right, and a good and joyful thing', l.text):
                # The eucharistic prayer continuing after the Communion
                # versicles — a known fixture, labeled rather than flagged.
                item = {'kind': 'item', 'type': 'litany', 'label': 'The Great Thanksgiving',
                        'title': None, 'titleQuoted': False, 'who': None, 'note': None,
                        'body': []}
                guide['order'].append(item)
                current = {'item': item, 'kind': 'litany', 'lines': []}
            else:
                item = {'kind': 'item', 'type': 'plain', 'label': None, 'title': None,
                        'titleQuoted': False, 'who': None, 'note': None, 'body': []}
                guide['order'].append(item)
                current = {'item': item, 'kind': 'plain', 'lines': []}
                notes.append(
                    f'page {l.page}: content without a label kept as an untitled '
                    f'item (starts: "{l.text[:50]}")')
        current['lines'].append(l)
        i += 1
    flush()

    # -- community boxes ----------------------------------------------------
    if community_start >= 0:
        rest = all_lines[community_start:]
        pages = {}
        for l in rest:
            pages.setdefault(l.page, []).append(l)
        boxable = []
        for page_num, page_lines in sorted(pages.items()):
            texts = [l.text for l in page_lines]
            if any(GPS_PAGE_RE.search(t) for t in texts) or \
                    ('GROW' in ' '.join(texts) and 'STUDY' in ' '.join(texts)):
                continue                     # print-only GPS notes card
            if any(re.fullmatch(r'Prayer Journal', t, re.I) for t in texts):
                guide['journal'] = journal_from_lines(page_lines)
                if not guide['journal']:
                    warnings.append(
                        f'page {page_num}: Prayer Journal page found but not parsed')
                continue
            boxable.append((page_num, page_lines))
        for page_num, page_lines in boxable:
            # 2026 winter/spring editions print the Prayer Journal as real
            # text, not the usual flattened screenshot — rebuild the OCR-style
            # paragraph blob from the line boxes and reuse the same parser.
            if guide['journal'] is None and any(
                    re.fullmatch(r'\\?\s*Prayer Journal', l.text.strip())
                    for l in page_lines):
                j = parse_journal(text_journal_blob(page_lines))
                if j:
                    guide['journal'] = j
                    continue
            classified = False
            poster_seen = False
            pending = []
            for box in split_boxes(page_lines):
                first = box[0]
                if re.match(r'^\*?\s*Music Team\b', first.text):
                    guide['musicTeam'] = parse_music_team(box)
                    classified = True
                elif re.match(r'^PRAYER REQUESTS', first.text):
                    guide['prayerRequests'] = parse_prayer_requests(box)
                    classified = True
                elif re.match(r'^(NOTES AND ANNOUNCEMENTS|Notes and Announcements)', first.text):
                    guide['announcements'] = parse_announcements(box)
                    classified = True
                elif any(BOX_CREDITS_RE.search(l.text) for l in box):
                    pass      # hymn copyright block — discarded (see above)
                elif first.runs and first.runs[0].b and first.runs[0].i \
                        and len(first.runs[0].text.strip()) >= 8:
                    guide['specialEvents'].append(parse_special_event(box))
                    classified = True
                elif all(DISPLAY_CAPS_RE.fullmatch(l.text) for l in box):
                    poster_seen = True   # poster display text — not content
                elif CALENDAR_BLOCK_RE.match(first.text):
                    guide['announcements'].append({
                        'heading': 'This Week', 'kind': 'note',
                        'text': tidy_prose(' '.join(runs_to_markup(l.runs) for l in box))})
                    classified = True
                else:
                    pending.append(box)
            if pending and not classified:
                # A page of nothing but display blocks is a designed insert
                # (concert flyer, seasonal page): publish it as an image.
                guide['flyers'].append({'page': page_num, 'image': None})
            else:
                for box in pending:
                    if not re.search(r'\w', ' '.join(l.text for l in box)):
                        continue    # stray punctuation fragments ("”")
                    if FIXTURE_BLOCK_RE.match(box[0].text.strip()):
                        continue    # recurring page furniture
                    if poster_seen and poster_residue(l.text for l in box):
                        continue    # residue between poster display blocks
                    first = box[0]
                    text = tidy_prose(' '.join(runs_to_markup(l.runs) for l in box))
                    # A shouted heading with a colon (or exclamation) leading
                    # body text is an announcement in its own box — file it
                    # under its heading, no review needed.
                    hm = re.match(r'^(?:<b>)?([^:<!]{2,80}?)[:!](?:</b>)?\s*([\s\S]+)$', text)
                    head = (re.sub(r'<[^>]+>', '', hm.group(1))
                            .replace('&amp;', '&').strip()) if hm else ''
                    alpha = [c for c in head if c.isalpha()]
                    if head and alpha \
                            and sum(c.isupper() for c in alpha) / len(alpha) >= 0.6 \
                            and hm.group(2).strip():
                        guide['announcements'].append({
                            'heading': title_case(head), 'kind': 'note',
                            'text': hm.group(2).strip()})
                        continue
                    notes.append(
                        f'page {first.page}: unclassified block starting '
                        f'"{first.text[:50]}" (added as announcement)')
                    guide['announcements'].append({
                        'heading': None, 'kind': 'note', 'text': text})
    elif text_pages:
        # Only meaningful for text guides — a scan's community content is in
        # its page images.
        warnings.append('no community section (Music Team / Prayer Requests / Announcements) found')

    # -- OCR pages: journal; GPS notes card is intentionally skipped --------
    for p in extracted.pages:
        if not p.ocr_text or p.lines:   # p.lines: structured from OCR above
            continue
        if re.search(r'Prayer Journal', p.ocr_text, re.I):
            guide['journal'] = parse_journal(p.ocr_text)
            if guide['journal']:
                guide['journal']['fromOcr'] = True
            else:
                warnings.append(
                    f'page {p.number}: Prayer Journal page found but could not be parsed from OCR')
        elif score_page(p.ocr_text):
            # The music-not-reproduced rule, extended to scans.
            notes.append(f'page {p.number}: engraved score page '
                         f'(music is not reproduced)')
        elif not (re.search(r'\bGROW\b[\s\S]*\bPRAY\b[\s\S]*\bSTUDY\b', p.ocr_text)
                  or GPS_PAGE_RE.search(p.ocr_text)):
            # GPS notes card: the three caps headings (case-sensitive — any
            # worship page mentions "pray") or its printed phrases.
            # Page 1 of an all-image guide is already shown as the cover.
            if p.number != 1 or not extracted.cover_path:
                guide['flyers'].append({'page': p.number, 'image': None})

    if guide['series']:
        by = guide['series']['by'] or ''
        guide['coverAlt'] = f"Message series artwork: {guide['series']['title']}. {by}".strip()

    # sanity checks the operator will want to hear about. Scans publish as
    # page images (no items is their nature), and plenty of editions simply
    # have no Prayer Journal — a journal page that FAILED to parse warns
    # specifically above.
    if not guide['order'] and text_pages:
        warnings.append('no order-of-worship items found')
    guide['flyers'].sort(key=lambda f: f['page'])
    return guide

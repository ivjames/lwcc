"""Render stage: guide dict + church config + images -> self-contained
index.html in the approved lab-tested design (single file, images inlined as
data URIs, no external requests, large-type/high-contrast friendly)."""
from __future__ import annotations

import base64
import os
import re

TEMPLATE_CSS = os.path.join(os.path.dirname(__file__), '..', 'template', 'guide.css')

AMEN_RE = re.compile(r'(Amen[.!]?|And So It Is!|Alleluia[.!]?)\s*$')


def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


# Body strings in guide.json are pre-escaped with a small trusted markup
# vocabulary (<b>, <i>, <sup>) produced by the parser — insert verbatim.
def rich(s):
    return str(s)


def sup_ordinals(s):
    """'10th Sunday' -> '10<sup>th</sup> Sunday'"""
    return re.sub(r'\b(\d+)(st|nd|rd|th)\b', r'\1<sup>\2</sup>', esc(s))


def prayer_html(text):
    t = rich(text)
    m = AMEN_RE.search(t)
    if m:
        t = t[:m.start()] + f'<span class="amen">{m.group(1)}</span>'
    return t


def data_uri(file):
    ext = os.path.splitext(file)[1].lower()
    mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.webp': 'image/webp'}.get(ext)
    if not mime:
        raise ValueError(f'unsupported image type: {file}')
    with open(file, 'rb') as fh:
        b64 = base64.b64encode(fh.read()).decode('ascii')
    return f'data:{mime};base64,{b64}'


def announcement_emoji(heading):
    if not heading:
        return ''
    if re.search(r'flower', heading, re.I):
        return '🌷 '
    if re.search(r'coffee', heading, re.I):
        return '☕ '
    if re.search(r'communion', heading, re.I):
        return '✝ '
    if re.search(r'attendance', heading, re.I):
        return '📊 '
    if re.search(r'bible|study', heading, re.I):
        return '📖 '
    if re.search(r'birthday', heading, re.I):
        return '🎂 '
    if re.search(r'animal|\bpet\b', heading, re.I):
        return '🐾 '
    return ''


def event_emoji(heading):
    if re.search(r'concert|recital|violin|piano|music', heading, re.I):
        return '🎻 '
    if re.search(r'lunch|dinner|breakfast|potluck|picnic', heading, re.I):
        return '🍽 '
    return '✨ '


def item_header(item):
    h = esc(item['label'])
    if item.get('title'):
        t = f'"{esc(item["title"])}"' if item.get('titleQuoted') else esc(item['title'])
        h += f': <span class="ttl">{t}</span>'
    if item.get('who'):
        h += f' <span class="who">— {esc(item["who"])}</span>'
    return h


def body_html(item):
    parts = []
    if item.get('note'):
        parts.append(f'<p class="who">({esc(item["note"])})</p>')
    for b in item.get('body') or []:
        t = b['type']
        if t == 'prayer':
            parts.append(f'<p class="prayer">{prayer_html(b["text"])}</p>')
        elif t == 'refrain':
            parts.append(f'<p class="refrain">{rich(b["text"])}</p>')
        elif t == 'ref':
            parts.append(f'<div class="ref">{esc(b["text"])}</div>')
        elif t == 'verse':
            parts.append(f'<p class="verse">{rich(b["text"])}</p>')
        else:
            parts.append(f'<p>{prayer_html(b["text"])}</p>')
    return '\n      '.join(parts)


def render(guide, church, banner_path=None, cover_path=None, css_path=None):
    with open(css_path or TEMPLATE_CSS, encoding='utf-8') as fh:
        css = fh.read()
    banner = data_uri(banner_path) if banner_path and os.path.exists(banner_path) else None
    cover = data_uri(cover_path) if cover_path and os.path.exists(cover_path) else None

    toc = []
    sections = []

    # -- welcome ------------------------------------------------------------
    if guide.get('welcome'):
        w = guide['welcome']
        toc.append(('#welcome', 'Welcome'))
        who = f' <span class="who">— {esc(w["who"])}</span>' if w.get('who') else ''
        sections.append(f'''
  <section id="welcome">
    <h2 class="sec">{esc(w.get('heading') or 'Welcome')}</h2>
    <div class="item">
      <div class="h">Welcome to Worship{who}</div>
      {body_html({'body': w['body']})}
    </div>
  </section>''')

    # -- order of worship ---------------------------------------------------
    if guide['order']:
        toc.append(('#worship', 'Order of Worship'))
        if any(o.get('type') == 'scripture' for o in guide['order']):
            toc.append(('#scripture', 'Scripture'))
        rows = []
        for o in guide['order']:
            if o['kind'] == 'stage':
                rows.append(f'    <span class="stage">[{esc(o["text"])}]</span>')
                continue
            sid = ' id="scripture"' if o.get('type') == 'scripture' else ''
            hdr = f'<div class="h">{item_header(o)}</div>\n      ' if o.get('label') else ''
            rows.append(f'''    <div{sid} class="item">
      {hdr}{body_html(o)}
    </div>''')
        joined = '\n\n'.join(rows)
        sections.append(f'''
  <section id="worship">
    <h2 class="sec">Order of Worship</h2>

{joined}
  </section>''')

    # -- music team ---------------------------------------------------------
    if guide['musicTeam']:
        toc.append(('#music', 'Music Team'))
        cells = '\n'.join(
            f'        <div>{esc(m["name"])}'
            + (f' <span class="role">— {esc(m["role"])}</span>' if m.get('role') else '')
            + '</div>'
            for m in guide['musicTeam'])
        credits = ''
        if guide.get('musicCredits'):
            joined = '<br>'.join(esc(t) for t in guide['musicCredits'])
            credits = ('\n      <div style="font-family:Arial,Helvetica,sans-serif;'
                       'font-size:.72rem;color:#54574a;margin-top:14px;line-height:1.7">'
                       + joined + '</div>')
        sections.append(f'''
  <section id="music">
    <h2 class="sec">Music Team</h2>
    <div class="item">
      <div class="grid music">
{cells}
      </div>{credits}
    </div>
  </section>''')

    # -- prayer requests ----------------------------------------------------
    if guide['prayerRequests']:
        toc.append(('#prayers', 'Prayer Requests'))
        cards = '\n'.join(f'''    <div class="callout co-prayer">
      {f'<h3>{esc(pr["name"])}</h3>' if pr.get('name') else ''}
      <p>{rich(pr['text'])}</p>
    </div>''' for pr in guide['prayerRequests'])
        sections.append(f'''
  <section id="prayers">
    <h2 class="sec">Prayer Requests</h2>
{cards}
  </section>''')

    # -- announcements ------------------------------------------------------
    if guide['announcements']:
        toc.append(('#news', 'Announcements'))
        blks = []
        for a in guide['announcements']:
            head = f'<b>{announcement_emoji(a["heading"])}{esc(a["heading"])}</b>' if a.get('heading') else ''
            if a.get('kind') == 'attendance':
                att = re.sub(r'\s*\|\s*', ' &nbsp;|&nbsp; ', rich(a['text']))
                blks.append(f'''      <div class="blk">{head}
        <div class="att">{att}</div></div>''')
            else:
                br = '<br>' if head else ''
                blks.append(f'''      <div class="blk">{head}{br}
        {rich(a['text'])}</div>''')
        joined = '\n'.join(blks)
        sections.append(f'''
  <section id="news">
    <h2 class="sec">Notes &amp; Announcements</h2>
    <div class="callout co-note">
{joined}
    </div>
  </section>''')

    # -- special events -----------------------------------------------------
    for idx, ev in enumerate(guide['specialEvents']):
        eid = 'event' if idx == 0 else f'event-{idx + 1}'
        toc_label = re.sub(r'[!]+$', '', re.sub(r'\s*—\s*TODAY!?$', '', ev['heading'], flags=re.I))
        toc.append((f'#{eid}', toc_label))
        paras = '\n'.join(f'      <p>{rich(p)}</p>' for p in ev['paragraphs'])
        note = f'<p class="sug">{esc(ev["note"])}</p>' if ev.get('note') else ''
        sections.append(f'''
  <section id="{eid}">
    <h2 class="sec">{esc(ev.get('sectionTitle') or 'Coming Up')}</h2>
    <div class="callout co-concert">
      <h3>{event_emoji(ev['heading'])}{esc(ev['heading'])}</h3>
{paras}
      {note}
    </div>
  </section>''')

    # -- prayer journal -----------------------------------------------------
    if guide.get('journal'):
        toc.append(('#journal', 'Prayer Journal'))
        j = guide['journal']
        blks = []
        if j.get('morning'):
            blks.append(f'''      <div class="blk"><b>Household Prayer: Morning</b>
        <p class="prayer">{prayer_html(esc(j['morning']))}</p></div>''')
        if j.get('midday'):
            blks.append(f'''      <div class="blk" style="text-align:center;color:var(--gold);font-style:italic;font-weight:700">
        {esc(j['midday'])}</div>''')
        if j.get('evening'):
            blks.append(f'''      <div class="blk"><b>Household Prayer: Evening</b>
        <p class="prayer">{prayer_html(esc(j['evening']))}</p></div>''')
        subtitle = (f'<p style="margin-top:-6px;color:var(--muted);font-style:italic">'
                    f'{esc(j["subtitle"])}</p>') if j.get('subtitle') else ''
        joined = '\n'.join(blks)
        sections.append(f'''
  <section id="journal">
    <h2 class="sec">Prayer Journal</h2>
    {subtitle}
    <div class="callout co-note">
{joined}
    </div>
  </section>''')

    toc_html = '\n'.join(f'    <a href="{href}">{esc(label)}</a>' for href, label in toc)

    banner_html = (
        f'<img class="banner" src="{banner}" alt="{esc(church.get("bannerAlt") or church["name"])}">'
        if banner else f'<div class="mark">{esc(church["name"])}</div>')
    season_html = f'<div class="season">{sup_ordinals(guide["season"])}</div>' if guide.get('season') else ''
    cover_html = (f'<img class="cover" src="{cover}" '
                  f'alt="{esc(guide.get("coverAlt") or "Message series artwork")}">') if cover else ''
    series_html = ''
    if guide.get('series'):
        s = guide['series']
        by = f'<div class="by">{esc(s["by"])}</div>' if s.get('by') else ''
        series_html = f'''<div class="series">
      <div class="kicker">MESSAGE SERIES</div>
      <h1>{esc(s['title'])}</h1>
      {by}
    </div>'''

    vision = f' · <em>{esc(church["vision"])}</em>' if church.get('vision') else ''
    sections_html = '\n'.join(sections)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(church['name'])} — Worship for {esc(guide.get('date') or '')}</title>
<style>
{css}</style>
</head>
<body>

<a class="skip" href="#worship">Skip to the order of worship</a>

<header class="hero">
  <div class="wrap">
    {banner_html}
    <div class="contact">
      {esc(church['address'])} &nbsp;·&nbsp;
      {esc(church['serviceTime'])} &nbsp;·&nbsp; <a href="tel:{esc(church['phone'])}">{esc(church['phoneDisplay'])}</a>
    </div>
  </div>
</header>

<div class="titlecard">
  <div class="wrap">
    <div class="date">{esc(guide.get('date') or '')}</div>
    {season_html}
    {cover_html}
    {series_html}
  </div>
</div>

<nav class="toc" aria-label="Sections of the worship guide">
  <div class="wrap">
{toc_html}
  </div>
</nav>

<main class="wrap" id="content">
{sections_html}

</main>

<footer>
  {esc(church['name'])}<br>
  {esc(church['address'])} · <a href="tel:{esc(church['phone'])}">{esc(church['phoneDisplay'])}</a><br>
  {esc(church.get('footerServiceTime') or church['serviceTime'])}{vision}
</footer>

</body>
</html>
'''

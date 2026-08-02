'use strict';
// Render stage: guide.json + church config + images -> self-contained
// index.html in the approved lab-tested design (single file, images inlined
// as data URIs, no external requests, large-type/high-contrast friendly).

const fs = require('fs');
const path = require('path');

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Body strings in guide.json are pre-escaped with a small trusted markup
// vocabulary (<b>, <i>, <sup>) produced by the parser — insert verbatim.
const rich = s => String(s);

// "10th Sunday" -> "10<sup>th</sup> Sunday"
const supOrdinals = s => esc(s).replace(/\b(\d+)(st|nd|rd|th)\b/g, '$1<sup>$2</sup>');

const AMEN_RE = /(Amen[.!]?|And So It Is!|Alleluia[.!]?)\s*$/;

function prayerHtml(text) {
  let t = rich(text);
  const m = t.match(AMEN_RE);
  if (m) t = t.slice(0, m.index) + `<span class="amen">${m[1]}</span>`;
  return t;
}

function dataUri(file) {
  const ext = path.extname(file).toLowerCase();
  const mime = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp' }[ext];
  if (!mime) throw new Error(`unsupported image type: ${file}`);
  return `data:${mime};base64,${fs.readFileSync(file).toString('base64')}`;
}

function announcementEmoji(heading) {
  if (!heading) return '';
  if (/flower/i.test(heading)) return '🌷 ';
  if (/coffee/i.test(heading)) return '☕ ';
  if (/communion/i.test(heading)) return '✝ ';
  if (/attendance/i.test(heading)) return '📊 ';
  if (/bible|study/i.test(heading)) return '📖 ';
  if (/birthday/i.test(heading)) return '🎂 ';
  return '';
}

function eventEmoji(heading) {
  if (/concert|recital|violin|piano|music/i.test(heading)) return '🎻 ';
  if (/lunch|dinner|breakfast|potluck|picnic/i.test(heading)) return '🍽 ';
  return '✨ ';
}

function itemHeader(item) {
  let h = esc(item.label);
  if (item.title) {
    const t = item.titleQuoted ? `"${esc(item.title)}"` : esc(item.title);
    h += `: <span class="ttl">${t}</span>`;
  }
  if (item.who) h += ` <span class="who">— ${esc(item.who)}</span>`;
  return h;
}

function bodyHtml(item) {
  const parts = [];
  if (item.note) parts.push(`<p class="who">(${esc(item.note)})</p>`);
  for (const b of item.body || []) {
    switch (b.type) {
      case 'prayer': parts.push(`<p class="prayer">${prayerHtml(b.text)}</p>`); break;
      case 'refrain': parts.push(`<p class="refrain">${rich(b.text)}</p>`); break;
      case 'ref': parts.push(`<div class="ref">${esc(b.text)}</div>`); break;
      case 'verse': parts.push(`<p class="verse">${rich(b.text)}</p>`); break;
      default: parts.push(`<p>${prayerHtml(b.text)}</p>`);
    }
  }
  return parts.join('\n      ');
}

function render(guide, church, { bannerPath, coverPath, cssPath } = {}) {
  const css = fs.readFileSync(cssPath || path.join(__dirname, '..', 'template', 'guide.css'), 'utf8');
  const banner = bannerPath && fs.existsSync(bannerPath) ? dataUri(bannerPath) : null;
  const cover = coverPath && fs.existsSync(coverPath) ? dataUri(coverPath) : null;

  const toc = [];
  const sections = [];

  // -- welcome ------------------------------------------------------------
  if (guide.welcome) {
    toc.push(['#welcome', 'Welcome']);
    sections.push(`
  <section id="welcome">
    <h2 class="sec">${esc(guide.welcome.heading || 'Welcome')}</h2>
    <div class="item">
      <div class="h">Welcome to Worship${guide.welcome.who ? ` <span class="who">— ${esc(guide.welcome.who)}</span>` : ''}</div>
      ${bodyHtml({ body: guide.welcome.body })}
    </div>
  </section>`);
  }

  // -- order of worship ---------------------------------------------------
  if (guide.order.length) {
    toc.push(['#worship', 'Order of Worship']);
    const hasScripture = guide.order.some(o => o.type === 'scripture');
    if (hasScripture) toc.push(['#scripture', 'Scripture']);
    const rows = guide.order.map(o => {
      if (o.kind === 'stage') return `    <span class="stage">[${esc(o.text)}]</span>`;
      const id = o.type === 'scripture' ? ' id="scripture"' : '';
      return `    <div${id} class="item">
      <div class="h">${itemHeader(o)}</div>
      ${bodyHtml(o)}
    </div>`;
    });
    sections.push(`
  <section id="worship">
    <h2 class="sec">Order of Worship</h2>

${rows.join('\n\n')}
  </section>`);
  }

  // -- music team ---------------------------------------------------------
  if (guide.musicTeam.length) {
    toc.push(['#music', 'Music Team']);
    const cells = guide.musicTeam.map(m =>
      `        <div>${esc(m.name)}${m.role ? ` <span class="role">— ${esc(m.role)}</span>` : ''}</div>`).join('\n');
    sections.push(`
  <section id="music">
    <h2 class="sec">Music Team</h2>
    <div class="item">
      <div class="grid music">
${cells}
      </div>
    </div>
  </section>`);
  }

  // -- prayer requests ----------------------------------------------------
  if (guide.prayerRequests.length) {
    toc.push(['#prayers', 'Prayer Requests']);
    const cards = guide.prayerRequests.map(pr => `    <div class="callout co-prayer">
      ${pr.name ? `<h3>${esc(pr.name)}</h3>` : ''}
      <p>${rich(pr.text)}</p>
    </div>`).join('\n');
    sections.push(`
  <section id="prayers">
    <h2 class="sec">Prayer Requests</h2>
${cards}
  </section>`);
  }

  // -- announcements ------------------------------------------------------
  if (guide.announcements.length) {
    toc.push(['#news', 'Announcements']);
    const blks = guide.announcements.map(a => {
      const head = a.heading ? `<b>${announcementEmoji(a.heading)}${esc(a.heading)}</b>` : '';
      if (a.kind === 'attendance') {
        const att = rich(a.text).replace(/\s*\|\s*/g, ' &nbsp;|&nbsp; ');
        return `      <div class="blk">${head}
        <div class="att">${att}</div></div>`;
      }
      return `      <div class="blk">${head}${head ? '<br>' : ''}
        ${rich(a.text)}</div>`;
    }).join('\n');
    sections.push(`
  <section id="news">
    <h2 class="sec">Notes &amp; Announcements</h2>
    <div class="callout co-note">
${blks}
    </div>
  </section>`);
  }

  // -- special events -----------------------------------------------------
  guide.specialEvents.forEach((ev, idx) => {
    const id = idx === 0 ? 'event' : `event-${idx + 1}`;
    const tocLabel = ev.heading.replace(/\s*—\s*TODAY!?$/i, '').replace(/[!]+$/, '');
    toc.push([`#${id}`, tocLabel]);
    sections.push(`
  <section id="${id}">
    <h2 class="sec">${esc(ev.sectionTitle || 'Coming Up')}</h2>
    <div class="callout co-concert">
      <h3>${eventEmoji(ev.heading)}${esc(ev.heading)}</h3>
${ev.paragraphs.map(p => `      <p>${rich(p)}</p>`).join('\n')}
      ${ev.note ? `<p class="sug">${esc(ev.note)}</p>` : ''}
    </div>
  </section>`);
  });

  // -- prayer journal -----------------------------------------------------
  if (guide.journal) {
    toc.push(['#journal', 'Prayer Journal']);
    const j = guide.journal;
    const blks = [];
    if (j.morning) blks.push(`      <div class="blk"><b>Household Prayer: Morning</b>
        <p class="prayer">${prayerHtml(esc(j.morning))}</p></div>`);
    if (j.midday) blks.push(`      <div class="blk" style="text-align:center;color:var(--gold);font-style:italic;font-weight:700">
        ${esc(j.midday)}</div>`);
    if (j.evening) blks.push(`      <div class="blk"><b>Household Prayer: Evening</b>
        <p class="prayer">${prayerHtml(esc(j.evening))}</p></div>`);
    sections.push(`
  <section id="journal">
    <h2 class="sec">Prayer Journal</h2>
    ${j.subtitle ? `<p style="margin-top:-6px;color:var(--muted);font-style:italic">${esc(j.subtitle)}</p>` : ''}
    <div class="callout co-note">
${blks.join('\n')}
    </div>
  </section>`);
  }

  const tocHtml = toc.map(([href, label]) => `    <a href="${href}">${esc(label)}</a>`).join('\n');

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${esc(church.name)} — Worship for ${esc(guide.date || '')}</title>
<style>
${css}</style>
</head>
<body>

<a class="skip" href="#worship">Skip to the order of worship</a>

<header class="hero">
  <div class="wrap">
    ${banner
      ? `<img class="banner" src="${banner}" alt="${esc(church.bannerAlt || church.name)}">`
      : `<div class="mark">${esc(church.name)}</div>`}
    <div class="contact">
      ${esc(church.address)} &nbsp;·&nbsp;
      ${esc(church.serviceTime)} &nbsp;·&nbsp; <a href="tel:${esc(church.phone)}">${esc(church.phoneDisplay)}</a>
    </div>
  </div>
</header>

<div class="titlecard">
  <div class="wrap">
    <div class="date">${esc(guide.date || '')}</div>
    ${guide.season ? `<div class="season">${supOrdinals(guide.season)}</div>` : ''}
    ${cover ? `<img class="cover" src="${cover}" alt="${esc(guide.coverAlt || 'Message series artwork')}">` : ''}
    ${guide.series ? `<div class="series">
      <div class="kicker">MESSAGE SERIES</div>
      <h1>${esc(guide.series.title)}</h1>
      ${guide.series.by ? `<div class="by">${esc(guide.series.by)}</div>` : ''}
    </div>` : ''}
  </div>
</div>

<nav class="toc" aria-label="Sections of the worship guide">
  <div class="wrap">
${tocHtml}
  </div>
</nav>

<main class="wrap" id="content">
${sections.join('\n')}

</main>

<footer>
  ${esc(church.name)}<br>
  ${esc(church.address)} · <a href="tel:${esc(church.phone)}">${esc(church.phoneDisplay)}</a><br>
  ${esc(church.footerServiceTime || church.serviceTime)}${church.vision ? ` · <em>${esc(church.vision)}</em>` : ''}
</footer>

</body>
</html>
`;
}

module.exports = { render };

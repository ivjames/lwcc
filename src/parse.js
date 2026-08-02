'use strict';
// Parse stage: extracted lines -> structured guide object (guide.json).
//
// The weekly guides follow a stable shape: masthead + date/season + cover on
// page 1, the order of worship (bold ALL-CAPS labels, bracketed stage
// directions, sheet music we skip), a community page (music team, prayer
// requests, notes & announcements, special events), a GPS notes card
// (skipped) and a Prayer Journal page (both flattened images, read via OCR).
//
// Unrecognized content is never silently dropped: it is parsed generically
// where possible and reported in `warnings` otherwise, so drift in older
// backlog guides surfaces immediately.

const knownTexts = require('./known-texts');

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];
const DATE_RE = new RegExp(`^(${MONTHS.join('|')})\\s+(\\d{1,2}),\\s+(\\d{4})$`);
const SMALL_WORDS = new Set(['of', 'the', 'to', 'for', 'and', 'a', 'an', 'in', 'on', 'with', 'at', 'by']);

// Order-of-worship label kinds. `music` items carry a title whose engraved
// score we drop; `prayer` bodies render as spoken-prayer blocks; `litany`
// alternates paragraphs with bold congregation refrains.
const LABEL_KINDS = [
  [/^CALL TO WORSHIP/, 'music'],
  [/^HYMN\b/, 'music'],
  [/^OFFERTORY/, 'music'],
  [/^SENDING FORTH/, 'music'],
  [/^REFLECTION/, 'music'],
  [/^SPECIAL MUSIC/, 'music'],
  [/^ANTHEM/, 'music'],
  [/PRAYER OF THE DAY/, 'prayer'],
  [/PRAYER FOR ILLUMINATION/, 'prayer'],
  [/PRAYER OF THANKSGIVING/, 'prayer'],
  [/^BLESSING/, 'prayer'],
  [/^BENEDICTION/, 'prayer'],
  [/INTERCESSION/, 'litany'],
  [/^SCRIPTURE/, 'scripture'],
  [/^MESSAGE/, 'message'],
];

function titleCase(s) {
  const words = s.trim().split(/\s+/);
  return words.map((w, idx) => {
    // Consonant-only all-caps words are acronyms (LWCC, GPS) — keep them.
    if (/^[BCDFGHJKLMNPQRSTVWXZ]{2,}$/.test(w)) return w;
    const lower = w.toLowerCase();
    return idx > 0 && idx < words.length - 1 && SMALL_WORDS.has(lower)
      ? lower
      : lower.replace(/^./, c => c.toUpperCase());
  }).join(' ');
}

const straightQuotes = s => s.replace(/[“”]/g, '"').replace(/[‘’]/g, "'");
const tidyProse = s => s
  .replace(/\s+/g, ' ')
  .replace(/ – /g, ' — ')   // en → em dash between words, per house style
  .trim();

function esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Serialize a line's runs to text with minimal inline markup (<b>, <i>,
// <sup>) that guide.json stores and the renderer trusts.
function runsToMarkup(runs, { bold = true, italic = true, sup = true } = {}) {
  let out = '';
  for (const r of runs) {
    let t = esc(r.text);
    if (!t) continue;
    if (r.sup && sup) {
      // Chapter-prefixed first verse ("9:1") renders as just the verse no.
      const v = t.trim().replace(/^\d+:(\d+)$/, '$1');
      t = t.replace(t.trim(), `<sup>${v}</sup>`);
    }
    if (!r.sup && ((r.b && bold) || (r.i && italic))) {
      // keep leading/trailing whitespace outside the tags
      const [, lead, core, tail] = t.match(/^(\s*)([\s\S]*?)(\s*)$/);
      let wrapped = core;
      if (core) {
        if (r.b && bold) wrapped = `<b>${wrapped}</b>`;
        if (r.i && italic) wrapped = `<i>${wrapped}</i>`;
      }
      t = lead + wrapped + tail;
    }
    out += t;
  }
  // merge adjacent identical tags split by the extractor
  return out.replace(/<\/b><b>/g, '').replace(/<\/i><i>/g, '')
    .replace(/<\/sup><sup>/g, '').replace(/\s+/g, ' ').trim();
}

const lineIsStage = l => /^\[.*\]$/.test(l.text) && l.runs.every(r => r.i || !r.text.trim());

// A label line: starts at the left margin with a bold ALL-CAPS run.
function matchLabel(l) {
  if (l.left > 105 || !l.runs.length) return null;
  const first = l.runs[0];
  if (!first.b) return null;
  const m = first.text.match(/^\s*([A-Z][A-Z'’&\s]{2,}?):?\s*(?:[“"](.+?)[”"])?\s*$/);
  if (!m) return null;
  const label = m[1].replace(/\s+/g, ' ').trim();
  if (label.split(' ').every(w => SMALL_WORDS.has(w.toLowerCase()))) return null;
  let title = m[2] || null;
  // Rest of the line: optional title (if not inside the bold run) and
  // "— Speaker[, role]" attribution.
  let rest = l.runs.slice(1).map(r => r.text).join('');
  if (!title) {
    const tm = rest.match(/[“"](.+?)[”"]/);
    if (tm) { title = tm[1]; rest = rest.replace(tm[0], ''); }
  }
  let who = null;
  let quoted = title !== null;
  const wm = rest.match(/[—–]\s*(.+?)\s*$/);
  if (wm) { who = tidyProse(wm[1]); rest = rest.slice(0, wm.index); }
  // Unquoted title after the colon, e.g. "CALL TO WORSHIP: Doxology"
  if (!title && rest.trim()) { title = rest.trim(); quoted = false; }
  return { label, title: title ? straightQuotes(title) : null, titleQuoted: quoted, who };
}

function kindFor(label) {
  for (const [re, kind] of LABEL_KINDS) if (re.test(label)) return kind;
  return 'plain';
}

// --- body assembly ------------------------------------------------------

// Group consecutive lines into paragraphs using vertical rhythm: a gap
// noticeably larger than the running line leading starts a new paragraph.
// Page breaks join silently (paragraphs flow across pages).
function groupParagraphs(lines, extraBoundary = null) {
  const paras = [];
  let cur = null, prev = null;
  for (const l of lines) {
    const newPage = prev && l.page !== prev.page;
    const gap = prev && !newPage ? l.top - prev.top : 0;
    const boundary = prev && ((!newPage && gap > prev.height * 1.5) ||
      (extraBoundary && extraBoundary(prev, l)));
    if (!cur || boundary) {
      cur = [];
      paras.push(cur);
    }
    cur.push(l);
    prev = l;
  }
  return paras;
}

const allBoldLine = l => l.runs.every(r => r.b || !r.text.trim());

// Prayers set as verse (hanging indents) keep their line structure; prayers
// set as plain wrapped text flow into one paragraph.
function prayerText(lines) {
  const lefts = [...new Set(lines.map(l => l.left))].sort((a, b) => a - b);
  const base = lefts[0];
  const hasIndents = lefts.some(left => left > base + 20);
  if (!hasIndents) return tidyProse(lines.map(l => runsToMarkup(l.runs, { bold: false })).join(' '));
  let out = '';
  for (const l of lines) {
    const t = runsToMarkup(l.runs, { bold: false });
    out += (out === '' ? '' : l.left <= base + 20 ? '\n' : ' ') + t;
  }
  return out.trim();
}

const SCRIPTURE_REF_RE = /^(?:[1-3]\s)?[A-Z][a-z]+(?:\s[A-Za-z]+)?\s\d+(?::\d+(?:[-–]\d+)?)?$/;

function scriptureBody(lines) {
  const blocks = [];
  let verse = null;
  for (const l of lines) {
    if (l.runs.every(r => r.b || !r.text.trim()) && SCRIPTURE_REF_RE.test(l.text)) {
      blocks.push({ type: 'ref', text: l.text });
      verse = null;
    } else {
      if (!verse) { verse = { type: 'verse', text: '' }; blocks.push(verse); }
      verse.text += (verse.text ? ' ' : '') + runsToMarkup(l.runs, { bold: false, italic: false });
    }
  }
  for (const b of blocks) b.text = b.text.replace(/\s+/g, ' ').trim();
  return blocks;
}

function litanyBody(lines) {
  const blocks = [];
  // Congregation refrains are fully bold lines; split on the style change
  // even when the vertical gap is tight.
  for (const para of groupParagraphs(lines, (a, b) => allBoldLine(a) !== allBoldLine(b))) {
    const allBold = para.every(allBoldLine);
    if (allBold) {
      blocks.push({ type: 'refrain', text: tidyProse(para.map(l => runsToMarkup(l.runs, { bold: false })).join(' ')) });
    } else {
      blocks.push({ type: 'para', text: tidyProse(para.map(l => runsToMarkup(l.runs)).join(' ')) });
    }
  }
  return blocks;
}

function plainBody(lines) {
  return groupParagraphs(lines).map(para => ({
    type: 'para',
    text: tidyProse(para.map(l => runsToMarkup(l.runs)).join(' ')),
  }));
}

function buildItemBody(kind, lines, item) {
  if (!lines.length) {
    if (kind === 'music' && item.title && knownTexts[item.title]) {
      return [{ type: 'prayer', text: knownTexts[item.title] }];
    }
    return [];
  }
  // An italic parenthetical right after the label is a performance/translation note.
  if (lines.length && /^\(.*\)$/.test(lines[0].text) && lines[0].runs.every(r => r.i || !r.text.trim())) {
    item.note = lines[0].text.replace(/^\((.*)\)$/, '$1');
    lines = lines.slice(1);
  }
  switch (kind) {
    case 'prayer': return lines.length ? [{ type: 'prayer', text: prayerText(lines) }] : [];
    case 'scripture': return scriptureBody(lines);
    case 'litany': return litanyBody(lines);
    default: return plainBody(lines);
  }
}

// --- community page (music team / prayer requests / announcements) -------

function splitBoxes(lines) {
  const boxes = [];
  let cur = null, prev = null;
  for (const l of lines) {
    const newPage = prev && l.page !== prev.page;
    const gap = prev && !newPage ? l.top - prev.top : Infinity;
    if (!cur || gap > 60 || newPage) {
      cur = [];
      boxes.push(cur);
    }
    cur.push(l);
    prev = l;
  }
  return boxes;
}

function parseMusicTeam(lines) {
  const team = [];
  let name = null;
  for (const l of lines) {
    for (const r of l.runs) {
      const t = r.text.replace(/^Music Team:?/, '').trim();
      if (!t) continue;
      if (r.i) {
        if (name) team.push({ name, role: t.replace(/\s*\/\s*/g, ' / ').replace(/[,\s]+$/, '') });
        name = null;
      } else if (!r.b || !/^MUSIC/i.test(t)) {
        name = t.replace(/[,\s]+$/, '');
      }
    }
  }
  return team;
}

function parsePrayerRequests(lines) {
  // heading line, then entries: bold name + " – " + text; paragraph gap
  // separates entries.
  const entries = [];
  for (const para of groupParagraphs(lines.slice(1))) {
    const runs = para.flatMap(l => l.runs);
    const nameRun = runs.find(r => r.b);
    const name = nameRun ? tidyProse(nameRun.text) : null;
    const text = tidyProse(
      para.map(l => l.runs.filter(r => !r.b).map(r => r.text).join('')).join(' ')
    ).replace(/^[—–-]\s*/, '');
    entries.push({ name, text });
  }
  return entries;
}

function parseAnnouncements(lines) {
  const items = [];
  for (const para of groupParagraphs(lines.slice(1))) {
    const text = tidyProse(para.map(l => runsToMarkup(l.runs)).join(' '));
    // Sub-item heading: an ALL-CAPS bold prefix ending at a colon, e.g.
    // "<b>FLOWERS</b> &amp; <b>FELLOWSHIP:</b> Today's flowers…" (the
    // rainbow-colored letters split into several bold runs).
    const m = text.match(/^(<b>[^:]{2,60}?):(<\/b>)?\s*([\s\S]*)$/);
    const plainHead = m && m[1].replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').trim();
    if (m && /^[A-Z0-9\s&'’…]+$/.test(plainHead)) {
      const heading = titleCase(plainHead);
      const kind = /attendance/i.test(heading) ? 'attendance' : 'note';
      let body = tidyProse(m[3].replace(/^<\/b>\s*/, ''));
      if (kind === 'attendance') body = body.replace(/<\/?[bi]>/g, ''); // keep only <sup>
      items.push({ heading, text: body, kind });
    } else if (items.length) {
      items[items.length - 1].text += ' ' + text;
    } else {
      items.push({ heading: null, text, kind: 'note' });
    }
  }
  return items;
}

function parseSpecialEvent(lines) {
  // First line starts with a bold-italic display heading; an italic closing
  // line is a footnote (e.g. suggested donation).
  const headText = lines[0].runs[0].text.trim();
  let heading = headText;
  const todayMatch = headText.match(/^(.*?)\s*[—–-]?\s*(TODAY!?)$/);
  if (todayMatch) heading = `${titleCase(todayMatch[1].replace(/!$/, ''))} — ${todayMatch[2]}`;
  else if (headText === headText.toUpperCase()) heading = titleCase(headText.replace(/[:!]$/, ''));

  // drop the heading run, then peel an italic footnote line off the end
  let body = [{ ...lines[0], runs: lines[0].runs.slice(1) }, ...lines.slice(1)];
  let note = null;
  const last = body[body.length - 1];
  if (body.length > 1 && last.runs.every(r => r.i || !r.text.trim())) {
    note = tidyProse(last.text);
    body = body.slice(0, -1);
  }
  const paras = body.length && body[0].runs.length === 0 && body.length === 1 ? [] :
    groupParagraphs(body).map(para =>
      tidyProse(para.map(l => runsToMarkup(l.runs, { italic: false })).join(' '))
    ).filter(Boolean);
  return { heading, paragraphs: paras, note, sectionTitle: /TODAY/i.test(headText) ? 'This Afternoon' : 'Coming Up' };
}

// --- journal (OCR) --------------------------------------------------------

function parseJournal(ocrText) {
  const paras = ocrText.split(/\n\s*\n/).map(p => p.replace(/\s+/g, ' ').trim()).filter(Boolean);
  const journal = { subtitle: null, morning: null, midday: null, evening: null };
  let section = null;
  for (const p of paras) {
    if (/^Prayer Journal$/i.test(p)) continue;
    if (/^Use this prayer/i.test(p)) { journal.subtitle = p; continue; }
    const hp = p.match(/^Household Prayer:\s*(Morning|Evening)\s*(.*)$/is);
    if (hp) {
      section = hp[1].toLowerCase();
      if (hp[2]) journal[section] = hp[2].replace(/\s+/g, ' ').trim();
      continue;
    }
    if (/Midday Prayer|^Consider/i.test(p)) { journal.midday = p; section = null; continue; }
    if (section && !journal[section]) { journal[section] = p; continue; }
    if (section) journal[section] += ' ' + p;
  }
  return (journal.morning || journal.evening) ? journal : null;
}

// --- main -----------------------------------------------------------------

function parse(extracted, opts = {}) {
  const warnings = [...(extracted.warnings || [])];
  const guide = {
    date: null, dateISO: null, season: null,
    series: null, coverAlt: null,
    welcome: null, order: [],
    musicTeam: [], prayerRequests: [], announcements: [],
    specialEvents: [], journal: null,
    warnings,
  };

  const textPages = extracted.pages.filter(p => p.lines.length);
  const allLines = textPages.flatMap(p => p.lines);

  // -- page 1 header: date + season --------------------------------------
  let i = 0;
  for (; i < allLines.length; i++) {
    const l = allLines[i];
    const dm = l.text.match(DATE_RE);
    if (dm) {
      guide.date = l.text;
      const month = String(MONTHS.indexOf(dm[1]) + 1).padStart(2, '0');
      guide.dateISO = `${dm[3]}-${month}-${String(dm[2]).padStart(2, '0')}`;
      const next = allLines[i + 1];
      if (next && next.runs.some(r => r.i) && !matchLabel(next)) {
        guide.season = next.text;
        i++;
      }
      i++;
      break;
    }
    if (matchLabel(l)) break; // no date found before content
  }
  if (!guide.date) warnings.push('no service date found on page 1');

  // -- order of worship ---------------------------------------------------
  let current = null;   // { item, kind, lines }
  const flush = () => {
    if (!current) return;
    current.item.body = buildItemBody(current.kind, current.lines, current.item);
    current = null;
  };

  let communityStart = -1;
  for (; i < allLines.length; i++) {
    const l = allLines[i];
    if (/^Music Team\b/.test(l.text) || /^PRAYER REQUESTS/.test(l.text) || /^NOTES AND ANNOUNCEMENTS/.test(l.text)) {
      communityStart = i;
      break;
    }
    if (lineIsStage(l)) {
      flush();
      guide.order.push({ kind: 'stage', text: l.text.replace(/^\[(.*)\]$/, '$1') });
      continue;
    }
    const label = matchLabel(l);
    if (label) {
      flush();
      if (/^OPENING ANNOUNCEMENTS|^WELCOME/.test(label.label)) {
        guide.welcome = { heading: titleCase(label.label), who: label.who, body: [] };
        current = { item: guide.welcome, kind: 'plain', lines: [] };
        continue;
      }
      const kind = kindFor(label.label);
      const item = {
        kind: 'item', type: kind,
        label: titleCase(label.label),
        title: label.title, titleQuoted: label.titleQuoted, who: label.who, note: null, body: [],
      };
      if (kind === 'message') {
        guide.series = { title: label.title, by: label.who };
      }
      if (kind === 'plain' && !LABEL_KINDS.some(([re]) => re.test(label.label))) {
        // generic fallback — fine, but let the operator know
        if (!/QUESTIONS FOR REFLECTION|INVITATION TO THE OFFERING/.test(label.label)) {
          warnings.push(`unrecognized order-of-worship label "${label.label}" (parsed generically)`);
        }
      }
      guide.order.push(item);
      current = { item, kind, lines: [] };
      continue;
    }
    if (current) {
      current.lines.push(l);
    } else {
      warnings.push(`page ${l.page}: unattached line ignored: "${l.text.slice(0, 60)}"`);
    }
  }
  flush();

  // -- community boxes ----------------------------------------------------
  if (communityStart >= 0) {
    const rest = allLines.slice(communityStart);
    for (const box of splitBoxes(rest)) {
      const first = box[0];
      if (/^Music Team\b/.test(first.text)) {
        guide.musicTeam = parseMusicTeam(box);
      } else if (/^PRAYER REQUESTS/.test(first.text)) {
        guide.prayerRequests = parsePrayerRequests(box);
      } else if (/^NOTES AND ANNOUNCEMENTS/.test(first.text)) {
        guide.announcements = parseAnnouncements(box);
      } else if (first.runs[0] && first.runs[0].b && first.runs[0].i) {
        guide.specialEvents.push(parseSpecialEvent(box));
      } else {
        warnings.push(`page ${first.page}: unclassified block starting "${first.text.slice(0, 50)}" (added as announcement)`);
        guide.announcements.push({ heading: null, kind: 'note', text: tidyProse(box.map(l => runsToMarkup(l.runs)).join(' ')) });
      }
    }
  } else {
    warnings.push('no community section (Music Team / Prayer Requests / Announcements) found');
  }

  // -- OCR pages: journal; GPS notes card is intentionally skipped --------
  for (const p of extracted.pages) {
    if (!p.ocrText) continue;
    if (/Prayer Journal/i.test(p.ocrText)) {
      guide.journal = parseJournal(p.ocrText);
      if (guide.journal) guide.journal.fromOcr = true;
      else warnings.push(`page ${p.number}: Prayer Journal page found but could not be parsed from OCR`);
    } else if (!/GROW|PRAY|STUDY|Notes from the Service/i.test(p.ocrText)) {
      warnings.push(`page ${p.number}: image-only page with unrecognized content (skipped)`);
    }
  }

  if (guide.series) {
    guide.coverAlt = `Message series artwork: ${guide.series.title}. ${guide.series.by || ''}`.trim();
  }

  // sanity checks the operator will want to hear about
  if (!guide.order.length) warnings.push('no order-of-worship items found');
  if (!guide.journal) warnings.push('no Prayer Journal parsed');
  return guide;
}

module.exports = { parse };

'use strict';
// Extract stage: PDF -> { pages: [{ number, width, height, lines: [...] }], images }
//
// Uses poppler CLI tools (pdftohtml -xml for styled text, pdfimages for the
// cover art). Sheet-music engraving noise (Finale exports: note glyphs, lyric
// syllables, composer credits) is dropped by fontspec: engraving text uses the
// Maestro music fonts and prints in near-black #231f20, while real document
// text is pure #000000 or a vivid accent color.

const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ENGRAVING_COLOR = '#231f20';
const MUSIC_FONT_RE = /maestro|opus|bravura|sonata/i;
// Emoji are embedded as invisible placeholder glyphs (opacity 0) that extract
// as junk ASCII; the visible emoji is a separate image we ignore.
const EMOJI_FONT_RE = /emoji/i;

function run(cmd, args) {
  return execFileSync(cmd, args, { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
}

function decodeEntities(s) {
  return s
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&');
}

// Parse the body of a <text> element into runs of {text, b, i}.
function parseRuns(inner) {
  const runs = [];
  const re = /<(\/?)(b|i)>|([^<]+)/gi;
  let b = false, i = false, m;
  while ((m = re.exec(inner)) !== null) {
    if (m[3] !== undefined) {
      runs.push({ text: decodeEntities(m[3]), b, i });
    } else if (m[2].toLowerCase() === 'b') {
      b = m[1] !== '/';
    } else {
      i = m[1] !== '/';
    }
  }
  return runs;
}

function parseXml(xml) {
  const fonts = new Map();
  for (const m of xml.matchAll(/<fontspec id="(\d+)" size="(-?\d+)" family="([^"]*)" color="([^"]*)"( opacity="([\d.]+)")?/g)) {
    fonts.set(m[1], {
      size: Number(m[2]), family: m[3], color: m[4].toLowerCase(),
      opacity: m[6] === undefined ? 1 : Number(m[6]),
    });
  }
  const pages = [];
  const pageRe = /<page ([^>]*)>([\s\S]*?)<\/page>/g;
  const attr = (s, name) => {
    const m = s.match(new RegExp(`${name}="(-?\\d+)"`));
    return m ? Number(m[1]) : 0;
  };
  let pm;
  while ((pm = pageRe.exec(xml)) !== null) {
    const page = { number: attr(pm[1], 'number'), width: attr(pm[1], 'width'), height: attr(pm[1], 'height'), items: [] };
    const textRe = /<text top="(-?\d+)" left="(-?\d+)" width="(-?\d+)" height="(-?\d+)" font="(\d+)">([\s\S]*?)<\/text>/g;
    let tm;
    while ((tm = textRe.exec(pm[2])) !== null) {
      const font = fonts.get(tm[5]) || { size: 0, family: '', color: '#000000' };
      const text = tm[6];
      page.items.push({
        top: Number(tm[1]), left: Number(tm[2]),
        width: Number(tm[3]), height: Number(tm[4]),
        font, runs: parseRuns(text),
      });
    }
    pages.push(page);
  }
  return pages;
}

function isNoise(item, page) {
  const { font } = item;
  if (font.color === ENGRAVING_COLOR) return true;         // engraving text
  if (MUSIC_FONT_RE.test(font.family)) return true;        // music glyph fonts
  if (EMOJI_FONT_RE.test(font.family) || font.opacity === 0) return true;
  const text = item.runs.map(r => r.text).join('');
  if (!text.trim()) return true;                           // whitespace filler
  // Page number: a lone integer in the bottom margin, horizontally centered
  // (verse superscripts can also be bare numbers low on the page — keep those).
  if (/^\s*\d+\s*$/.test(text) && item.top > page.height * 0.92 &&
      Math.abs(item.left + item.width / 2 - page.width / 2) < 30) return true;
  return false;
}

// Cluster items into visual lines by vertical position, then order runs
// left-to-right. Superscripts (verse numbers, ordinal "th") sit on the same
// visual line but with a smaller font: mark them sup so the parser/renderer
// can re-create them.
function buildLines(page) {
  const items = page.items.filter(it => !isNoise(it, page)).sort((a, b) => a.top - b.top || a.left - b.left);
  const lines = [];
  let cur = null;
  for (const it of items) {
    // An item joins the current line if its vertical center falls within it.
    const center = it.top + it.height / 2;
    if (cur && center >= cur.top - 2 && center <= cur.bottom + 2) {
      cur.items.push(it);
      cur.top = Math.min(cur.top, it.top);
      cur.bottom = Math.max(cur.bottom, it.top + it.height);
    } else {
      cur = { top: it.top, bottom: it.top + it.height, items: [it] };
      lines.push(cur);
    }
  }
  return lines.map(l => finishLine(l, page));
}

function finishLine(l, page) {
  const items = l.items.slice().sort((a, b) => a.left - b.left);
  const maxH = Math.max(...items.map(i => i.height));
  const runs = [];
  let prevRight = null;
  for (const it of items) {
    const sup = it.height <= maxH * 0.78;
    for (let r of it.runs) {
      let text = r.text;
      if (prevRight !== null && it.left - prevRight > 4 &&
          runs.length && !/\s$/.test(runs[runs.length - 1].text) && !/^\s/.test(text)) {
        text = ' ' + text;
      }
      const run = { text, b: r.b, i: r.i, sup };
      const last = runs[runs.length - 1];
      if (last && last.b === run.b && last.i === run.i && last.sup === run.sup) {
        last.text += run.text;
      } else {
        runs.push(run);
      }
      prevRight = it.left + it.width;
    }
  }
  return {
    page: page.number,
    top: l.top,
    bottom: l.bottom,
    left: items[0].left,
    height: maxH,
    runs,
    text: runs.map(r => r.text).join('').replace(/\s+/g, ' ').trim(),
  };
}

// Pick the weekly cover art: the largest embedded image on page 1 that is not
// masthead-shaped (the banner is a fixed ~4:1 strip; covers are photo-shaped).
function findCoverImage(pdfPath, outDir) {
  const list = run('pdfimages', ['-list', pdfPath]);
  const rows = [];
  for (const line of list.split('\n').slice(2)) {
    const c = line.trim().split(/\s+/);
    if (c.length < 5 || c[2] === 'smask') continue;
    rows.push({ page: Number(c[0]), num: Number(c[1]), width: Number(c[3]), height: Number(c[4]) });
  }
  const candidates = rows
    .filter(r => r.page === 1 && r.width / r.height < 3)
    .sort((a, b) => b.width * b.height - a.width * a.height);
  if (!candidates.length) return null;
  const target = candidates[0];

  const prefix = path.join(outDir, 'img');
  run('pdfimages', ['-all', '-f', '1', '-l', '1', pdfPath, prefix]);
  const suffix = String(target.num).padStart(3, '0');
  const file = fs.readdirSync(outDir).find(f => f.startsWith(`img-${suffix}.`));
  if (!file) return null;
  const src = path.join(outDir, file);
  const dest = path.join(outDir, 'cover' + path.extname(file));
  fs.renameSync(src, dest);
  // Clean up the other page-1 extractions (banner + mask copies).
  for (const f of fs.readdirSync(outDir)) {
    if (f.startsWith('img-')) fs.unlinkSync(path.join(outDir, f));
  }
  return dest;
}

function hasTesseract() {
  try { execFileSync('tesseract', ['--version'], { stdio: 'ignore' }); return true; }
  catch { return false; }
}

// Some pages (GPS notes card, Prayer Journal) are flattened screenshots with
// no text layer. OCR them so the parser can still read the journal prayers.
function ocrPage(pdfPath, pageNum, workDir) {
  const prefix = path.join(workDir, `ocr-${pageNum}`);
  run('pdftoppm', ['-f', String(pageNum), '-l', String(pageNum), '-r', '300', '-png', pdfPath, prefix]);
  const png = fs.readdirSync(workDir).find(f => f.startsWith(`ocr-${pageNum}-`) && f.endsWith('.png'));
  if (!png) return null;
  run('tesseract', [path.join(workDir, png), prefix]);
  const text = fs.readFileSync(prefix + '.txt', 'utf8')
    // Common OCR slip on this material: standalone "I" read as a pipe.
    .replace(/(^|\s)\|(?=\s)/g, '$1I');
  return text;
}

function extract(pdfPath, workDir, { ocr = true } = {}) {
  fs.mkdirSync(workDir, { recursive: true });
  const xmlPath = path.join(workDir, 'wg.xml');
  const warnings = [];
  // -i: ignore images; -q: quiet
  run('pdftohtml', ['-xml', '-i', '-q', pdfPath, xmlPath]);
  const xml = fs.readFileSync(xmlPath, 'utf8');
  const pages = parseXml(xml).map(p => ({
    number: p.number, width: p.width, height: p.height, lines: buildLines(p), ocrText: null,
  }));
  const imagePages = pages.filter(p => p.lines.length === 0);
  if (imagePages.length) {
    if (ocr && hasTesseract()) {
      for (const p of imagePages) p.ocrText = ocrPage(pdfPath, p.number, workDir);
    } else if (ocr) {
      warnings.push(`pages ${imagePages.map(p => p.number).join(', ')} have no text layer and tesseract is not installed — content on them (e.g. the Prayer Journal) will be missing. Install tesseract-ocr.`);
    }
  }
  const coverPath = findCoverImage(pdfPath, workDir);
  return { pages, coverPath, warnings };
}

module.exports = { extract, parseXml, buildLines };

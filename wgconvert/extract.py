"""Extract stage: PDF -> pages of styled text lines + cover image.

Uses poppler CLI tools (pdftohtml -xml for styled text, pdfimages for the
cover art). Sheet-music engraving noise (Finale exports: note glyphs, lyric
syllables, composer credits) is dropped by fontspec: engraving text uses the
Maestro music fonts and prints in near-black #231f20, while real document
text is pure #000000 or a vivid accent color.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

ENGRAVING_COLOR = '#231f20'
MUSIC_FONT_RE = re.compile(r'maestro|opus|bravura|sonata', re.I)


def accent_for(color):
    """Hue family of a printed font color (maroon/gold/green/blue/purple),
    or None for ordinary ink — black, the near-black engraving tone, greys,
    and white. The family is used for structure decisions (is this line
    emphasized? do these words share one ink? is this lettering deliberately
    rainbow?); presentation carries the exact ink via accent_hex."""
    m = re.fullmatch(r'#([0-9a-f]{6})', (color or '').lower())
    if not m:
        return None
    r, g, b = (int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hi, lo = max(r, g, b), min(r, g, b)
    if hi < 0.25 or hi - lo < 0.12:
        return None                       # too dark or too grey to be an accent
    d = hi - lo
    if hi == r:
        h = (60 * ((g - b) / d)) % 360
    elif hi == g:
        h = 60 * ((b - r) / d) + 120
    else:
        h = 60 * ((r - g) / d) + 240
    if h < 20 or h >= 320:
        return 'maroon'
    if h < 70:
        return 'gold'
    if h < 170:
        return 'green'
    if h < 255:
        return 'blue'
    return 'purple'


def accent_hex(color):
    """The exact printed ink for presentation, lowercased '#rrggbb' — or None
    when the color is ordinary ink (same gates as accent_for)."""
    return (color or '').lower() if accent_for(color) else None
# Emoji are embedded as invisible placeholder glyphs (opacity 0) that extract
# as junk ASCII; the visible emoji is a separate image we ignore.
EMOJI_FONT_RE = re.compile(r'emoji', re.I)


@dataclass
class Font:
    size: int = 0
    family: str = ''
    color: str = '#000000'
    opacity: float = 1.0


@dataclass
class Run:
    text: str
    b: bool = False
    i: bool = False
    sup: bool = False
    color: str = '#000000'   # printed ink color from the fontspec


@dataclass
class Line:
    page: int
    top: int
    bottom: int
    left: int
    height: int
    runs: list
    text: str


@dataclass
class Page:
    number: int
    width: int
    height: int
    lines: list = field(default_factory=list)
    images: list = field(default_factory=list)  # placed-image boxes from the
                                # XML: {'top','left','width','height'}
    ocr_text: str | None = None
    ocr_rich: list | None = None  # per ocr_text line: [(word, ink hex|None)]
    engraved: bool = False      # a sheet-music score page — deliberately
                                # not reproduced: no OCR, no flyer image


# Real-text residue on an engraved score page: hymn credits and the title
# over the staves. Absorbed with the engraving.
CREDITS_LINE_RE = re.compile(
    r'©|℗|CCLI|Public Domain|Used by [Pp]ermission|\b[Ww]ords\b|\b[Mm]usic\b'
    r'|\b[Tt]ext:|\b[Tt]une:|arr\.')


def page_is_engraved(raw_items, lines):
    """A page whose extracted text is (almost) all engraving: music fonts or
    the engraving ink color, with at most a few surviving real-text lines
    that are credits or short display text (the hymn title)."""
    if not any(it['font'].color == ENGRAVING_COLOR
               or MUSIC_FONT_RE.search(it['font'].family)
               for it in raw_items):
        return False
    if len(lines) > 4:
        return False
    return all(CREDITS_LINE_RE.search(l.text) or len(l.text.split()) <= 6
               for l in lines)


def _run(cmd):
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    except FileNotFoundError:
        pkg = 'tesseract-ocr' if cmd[0] == 'tesseract' else 'poppler-utils'
        raise RuntimeError(
            f"'{cmd[0]}' is not installed on this server — "
            f"run: apt-get install -y {pkg}") from None
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or '').strip().splitlines()[-1:] or ['(no stderr)']
        raise RuntimeError(f"{cmd[0]} failed on this PDF: {tail[0]}") from None


def _decode_entities(s):
    s = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), s)
    s = re.sub(r'&#x([0-9a-f]+);', lambda m: chr(int(m.group(1), 16)), s, flags=re.I)
    return (s.replace('&lt;', '<').replace('&gt;', '>')
             .replace('&quot;', '"').replace('&apos;', "'")
             .replace('&amp;', '&'))


def _parse_runs(inner):
    """Parse the body of a <text> element into runs of (text, b, i)."""
    runs = []
    b = i = False
    for m in re.finditer(r'<(/?)(b|i)>|([^<]+)', inner, re.I):
        if m.group(3) is not None:
            runs.append(Run(text=_decode_entities(m.group(3)), b=b, i=i))
        elif m.group(2).lower() == 'b':
            b = m.group(1) != '/'
        else:
            i = m.group(1) != '/'
    return runs


def _parse_xml(xml):
    fonts = {}
    for m in re.finditer(
            r'<fontspec id="(\d+)" size="(-?\d+)" family="([^"]*)" color="([^"]*)"( opacity="([\d.]+)")?', xml):
        fonts[m.group(1)] = Font(
            size=int(m.group(2)), family=m.group(3), color=m.group(4).lower(),
            opacity=1.0 if m.group(6) is None else float(m.group(6)))
    pages = []
    for pm in re.finditer(r'<page ([^>]*)>([\s\S]*?)</page>', xml):
        def attr(name, s=pm.group(1)):
            am = re.search(name + r'="(-?\d+)"', s)
            return int(am.group(1)) if am else 0
        items = []
        for tm in re.finditer(
                r'<text top="(-?\d+)" left="(-?\d+)" width="(-?\d+)" height="(-?\d+)" font="(\d+)">([\s\S]*?)</text>',
                pm.group(2)):
            items.append({
                'top': int(tm.group(1)), 'left': int(tm.group(2)),
                'width': int(tm.group(3)), 'height': int(tm.group(4)),
                'font': fonts.get(tm.group(5), Font()),
                'runs': _parse_runs(tm.group(6)),
            })
        images = [
            {'top': int(im.group(1)), 'left': int(im.group(2)),
             'width': int(im.group(3)), 'height': int(im.group(4))}
            for im in re.finditer(
                r'<image top="(-?\d+)" left="(-?\d+)" width="(-?\d+)" height="(-?\d+)"',
                pm.group(2))]
        pages.append({'number': attr('number'), 'width': attr('width'),
                      'height': attr('height'), 'items': items, 'images': images})
    return pages


def _is_noise(item, page):
    font = item['font']
    if font.color == ENGRAVING_COLOR:
        return True                                  # engraving text
    if MUSIC_FONT_RE.search(font.family):
        return True                                  # music glyph fonts
    if EMOJI_FONT_RE.search(font.family) or font.opacity == 0:
        return True
    text = ''.join(r.text for r in item['runs'])
    if not text.strip():
        return True                                  # whitespace filler
    # Page number: a lone integer in the bottom margin, horizontally centered
    # (verse superscripts can also be bare numbers low on the page — keep those).
    if (re.fullmatch(r'\s*\d+\s*', text) and item['top'] > page['height'] * 0.92
            and abs(item['left'] + item['width'] / 2 - page['width'] / 2) < 30):
        return True
    return False


def _build_lines(page):
    """Cluster items into visual lines by vertical position, then order runs
    left-to-right. Superscripts (verse numbers, ordinal "th") sit on the same
    visual line but with a smaller font: mark them sup so the parser/renderer
    can re-create them."""
    items = sorted((it for it in page['items'] if not _is_noise(it, page)),
                   key=lambda it: (it['top'], it['left']))
    clusters = []
    cur = None
    for it in items:
        # An item joins the current line if its vertical center falls within it.
        center = it['top'] + it['height'] / 2
        if cur and cur['top'] - 2 <= center <= cur['bottom'] + 2:
            cur['items'].append(it)
            cur['top'] = min(cur['top'], it['top'])
            cur['bottom'] = max(cur['bottom'], it['top'] + it['height'])
        else:
            cur = {'top': it['top'], 'bottom': it['top'] + it['height'], 'items': [it]}
            clusters.append(cur)
    return [_finish_line(c, page) for c in clusters]


def _finish_line(cluster, page):
    items = sorted(cluster['items'], key=lambda it: it['left'])
    max_h = max(it['height'] for it in items)
    runs = []
    prev_right = None
    for it in items:
        sup = it['height'] <= max_h * 0.78
        for r in it['runs']:
            text = r.text
            if (prev_right is not None and it['left'] - prev_right > 4 and runs
                    and not runs[-1].text.endswith((' ', '\t', '\n'))
                    and not text[:1].isspace()):
                text = ' ' + text
            run = Run(text=text, b=r.b, i=r.i, sup=sup, color=it['font'].color)
            if runs and runs[-1].b == run.b and runs[-1].i == run.i \
                    and runs[-1].sup == run.sup:
                prev = runs[-1]
                if prev.color == run.color:
                    prev.text += run.text
                elif prev.text and not prev.text[-1:].isspace() \
                        and not run.text[:1].isspace():
                    # A color change mid-word. Deliberate rainbow lettering
                    # — single letters, each in its own vivid ink family —
                    # is print art the site reproduces, so those runs stay
                    # split. Every other mid-word change (punctuation in a
                    # different ink, hues drifting within a word) is an
                    # accident that would render as a jarring multi-color
                    # word: absorb the word-part into the open word, colored
                    # by whichever side has more letters; everything after
                    # the first space keeps its own ink.
                    hm = re.match(r'(\S+)([\s\S]*)$', run.text)
                    head, rest = hm.group(1), hm.group(2)
                    prev_word = (prev.text.split() or [''])[-1]
                    head_aln = sum(c.isalnum() for c in head)
                    prev_aln = sum(c.isalnum() for c in prev_word)
                    if head_aln <= 2 and prev_aln <= 2 \
                            and accent_for(prev.color) and accent_for(run.color) \
                            and accent_for(prev.color) != accent_for(run.color):
                        runs.append(run)          # rainbow lettering
                    else:
                        if head_aln > prev_aln and ' ' not in prev.text.strip():
                            prev.color = run.color
                        prev.text += head
                        if rest:
                            runs.append(Run(text=rest, b=run.b, i=run.i,
                                            sup=run.sup, color=run.color))
                else:
                    runs.append(run)
            else:
                runs.append(run)
            prev_right = it['left'] + it['width']
    return Line(
        page=page['number'],
        top=cluster['top'],
        bottom=cluster['bottom'],
        left=items[0]['left'],
        height=max_h,
        runs=runs,
        text=re.sub(r'\s+', ' ', ''.join(r.text for r in runs)).strip(),
    )


def _find_cover_image(pdf_path, out_dir, page1_blank=False):
    """Pick the weekly cover art: the largest embedded image on page 1 that is
    not masthead-shaped (the banner is a fixed ~4:1 strip; covers are
    photo-shaped)."""
    if page1_blank:
        # A scan: page 1 *is* the cover, and pdfimages hands back raw
        # embedded streams that can arrive inverted or mangled (bilevel
        # scans especially). Render the page like every other scan page.
        dest = os.path.join(out_dir, 'cover.jpg')
        render_page_image(pdf_path, 1, dest)
        return dest
    listing = _run(['pdfimages', '-list', pdf_path])
    rows = []
    for line in listing.split('\n')[2:]:
        c = line.split()
        if len(c) < 5 or c[2] == 'smask':
            continue
        rows.append({'page': int(c[0]), 'num': int(c[1]),
                     'width': int(c[3]), 'height': int(c[4])})
    candidates = sorted(
        (r for r in rows if r['page'] == 1 and r['width'] / r['height'] < 3),
        key=lambda r: -(r['width'] * r['height']))
    if not candidates:
        return None
    target = candidates[0]

    prefix = os.path.join(out_dir, 'img')
    _run(['pdfimages', '-all', '-f', '1', '-l', '1', pdf_path, prefix])
    suffix = str(target['num']).zfill(3)
    file = next((f for f in os.listdir(out_dir) if f.startswith(f'img-{suffix}.')), None)
    if not file:
        return None
    if os.path.splitext(file)[1].lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
        # A text guide whose cover art came through in an unusable stream:
        # better no cover than the whole page as one.
        for f in os.listdir(out_dir):
            if f.startswith('img-'):
                os.unlink(os.path.join(out_dir, f))
        return None
    dest = os.path.join(out_dir, 'cover' + os.path.splitext(file)[1])
    os.rename(os.path.join(out_dir, file), dest)
    # Clean up the other page-1 extractions (banner + mask copies).
    for f in os.listdir(out_dir):
        if f.startswith('img-'):
            os.unlink(os.path.join(out_dir, f))
    return dest


def render_page_image(pdf_path, page_num, dest):
    """Render one PDF page to a JPEG (used for flyer/insert pages that are
    designed as posters — their text never reconstructs into clean prose)."""
    out_dir = os.path.dirname(dest) or '.'
    prefix = os.path.join(out_dir, f'.flyer-tmp-{page_num}')
    _run(['pdftoppm', '-jpeg', '-r', '110', '-f', str(page_num), '-l', str(page_num),
          pdf_path, prefix])
    produced = next((f for f in os.listdir(out_dir)
                     if f.startswith(f'.flyer-tmp-{page_num}-') and f.endswith('.jpg')), None)
    if not produced:
        raise RuntimeError(f'pdftoppm produced no image for page {page_num}')
    os.replace(os.path.join(out_dir, produced), dest)


# pdftohtml's XML coordinates are page pixels at its default 1.5 zoom, i.e.
# 108 dpi; rendering a crop at twice that makes every XML unit two pixels.
XML_DPI = 108


def render_page_region(pdf_path, page_num, box, dest):
    """Render one image's region of a page to a JPEG (interstitial photos on
    text pages). Cropping the rendered page — rather than pulling the
    embedded stream — keeps the printed appearance: soft masks, rotations,
    and color spaces all come out exactly as on paper."""
    out_dir = os.path.dirname(dest) or '.'
    prefix = os.path.join(out_dir, f'.photo-tmp-{page_num}')
    scale = 2
    _run(['pdftoppm', '-jpeg', '-r', str(XML_DPI * scale),
          '-f', str(page_num), '-l', str(page_num),
          '-x', str(max(0, box['left']) * scale), '-y', str(max(0, box['top']) * scale),
          '-W', str(box['width'] * scale), '-H', str(box['height'] * scale),
          pdf_path, prefix])
    produced = next((f for f in os.listdir(out_dir)
                     if f.startswith(f'.photo-tmp-{page_num}-') and f.endswith('.jpg')), None)
    if not produced:
        raise RuntimeError(f'pdftoppm produced no image for page {page_num}')
    os.replace(os.path.join(out_dir, produced), dest)


def _has_tesseract():
    return shutil.which('tesseract') is not None


def _load_ppm(path):
    """A pdftoppm P6 image as (width, height, raw RGB bytes) — stdlib-only
    pixel access for ink-color sampling."""
    with open(path, 'rb') as fh:
        data = fh.read()
    m = re.match(rb'P6\s+(?:#[^\n]*\s+)*(\d+)\s+(\d+)\s+(\d+)\s', data)
    if not m or int(m.group(3)) != 255:
        return None
    return int(m.group(1)), int(m.group(2)), data[m.end():]


def _ink_color(img, left, top, width, height):
    """Median color of the dark (ink) pixels inside a word's box — the median
    defeats the white paper and the anti-aliased edge blend, recovering the
    printed ink. None when the box holds too little ink to judge, or when
    most of its ink pixels carry no chroma of their own: genuinely colored
    print is colored through the strokes (measured ≥0.65 on real accents),
    while black text on a noisy scan only picks up color on the JPEG fringe
    — calling that colored is how black words end up randomly accented."""
    w, h, px = img
    left, top = max(0, left), max(0, top)
    rs, gs, bs = [], [], []
    colored = 0
    for y in range(top, min(h, top + height)):
        base = (y * w + left) * 3
        for x in range(min(width, w - left)):
            r, g, b = px[base + 3 * x:base + 3 * x + 3]
            if r + g + b < 480:
                rs.append(r)
                gs.append(g)
                bs.append(b)
                if max(r, g, b) - min(r, g, b) > 40:
                    colored += 1
    if len(rs) < 12 or colored < len(rs) * 0.5:
        return None
    rs.sort()
    gs.sort()
    bs.sort()
    mid = len(rs) // 2
    return f'#{rs[mid]:02x}{gs[mid]:02x}{bs[mid]:02x}'


def _ocr_page(pdf_path, page_num, work_dir):
    """Some pages (GPS notes card, Prayer Journal) are flattened screenshots
    with no text layer. OCR them so the parser can still read the journal
    prayers. Sections of scanned bulletins rely on colored text (accent-ink
    headings, colored refrains), which plain OCR text loses — so tesseract's
    TSV word boxes are sampled against the rendered page for each word's ink
    color. Returns (text, rich): rich aligns 1:1 with text's lines, each a
    list of (word, ink hex or None)."""
    prefix = os.path.join(work_dir, f'ocr-{page_num}')
    _run(['pdftoppm', '-f', str(page_num), '-l', str(page_num), '-r', '300',
          pdf_path, prefix])
    ppm = next((f for f in os.listdir(work_dir)
                if f.startswith(f'ocr-{page_num}-') and f.endswith('.ppm')), None)
    if not ppm:
        return None, None
    ppm_path = os.path.join(work_dir, ppm)
    _run(['tesseract', ppm_path, prefix, 'tsv'])
    with open(prefix + '.tsv', encoding='utf-8') as fh:
        rows = fh.read().split('\n')[1:]
    img = _load_ppm(ppm_path)
    lines = []          # [(block, par, line, [(word, color)])]
    for row in rows:
        c = row.split('\t')
        if len(c) != 12 or c[0] != '5' or not c[11].strip():
            continue
        # Common OCR slip on this material: standalone "I" read as a pipe.
        word = 'I' if c[11] == '|' else c[11]
        color = _ink_color(img, int(c[6]), int(c[7]), int(c[8]), int(c[9])) \
            if img else None
        key = (int(c[1]), int(c[2]), int(c[3]), int(c[4]))
        if lines and lines[-1][0] == key:
            lines[-1][1].append((word, color))
        else:
            lines.append((key, [(word, color)]))
    rich = []
    prev_par = None
    for (_page, block, par, _line), words in lines:
        if prev_par is not None and (block, par) != prev_par:
            rich.append([])                      # paragraph gap → blank line
        rich.append(words)
        prev_par = (block, par)
    text = '\n'.join(' '.join(w for w, _ in words) for words in rich)
    return text, rich


@dataclass
class Extracted:
    pages: list
    cover_path: str | None
    warnings: list


def extract(pdf_path, work_dir, ocr=True):
    os.makedirs(work_dir, exist_ok=True)
    xml_path = os.path.join(work_dir, 'wg.xml')
    warnings = []
    # No -i: the <image> boxes locate interstitial photos on text pages (the
    # extracted image files themselves are ignored — photos are re-rendered
    # from the page, see render_page_region — and vanish with work_dir).
    _run(['pdftohtml', '-xml', '-q', pdf_path, xml_path])
    with open(xml_path, encoding='utf-8') as fh:
        xml = fh.read()
    pages = []
    for p in _parse_xml(xml):
        page = Page(number=p['number'], width=p['width'], height=p['height'],
                    lines=_build_lines(p), images=p['images'])
        if page_is_engraved(p['items'], page.lines):
            page.engraved = True
            page.lines = []          # credits/title residue goes with the score
        pages.append(page)
    if not any(len(p.lines) > 2 for p in pages):
        # No page has real text density: this is a scan. A stray annotation
        # typed onto one page ("Hannah Yi, pianist") must not make the
        # document count as a text guide — clear it and let OCR read the
        # whole page, annotation included.
        for p in pages:
            p.lines = []
    image_pages = [p for p in pages if not p.lines and not p.engraved]
    if image_pages:
        if ocr and _has_tesseract():
            for p in image_pages:
                p.ocr_text, p.ocr_rich = _ocr_page(pdf_path, p.number, work_dir)
        elif ocr:
            nums = ', '.join(str(p.number) for p in image_pages)
            warnings.append(
                f'pages {nums} have no text layer and tesseract is not installed — '
                'content on them (e.g. the Prayer Journal) will be missing. '
                'Install tesseract-ocr.')
    cover_path = _find_cover_image(
        pdf_path, work_dir,
        page1_blank=not (pages and pages[0].lines))
    return Extracted(pages=pages, cover_path=cover_path, warnings=warnings)

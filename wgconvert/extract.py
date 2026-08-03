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
    ocr_text: str | None = None


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
        pages.append({'number': attr('number'), 'width': attr('width'),
                      'height': attr('height'), 'items': items})
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
            run = Run(text=text, b=r.b, i=r.i, sup=sup)
            if runs and runs[-1].b == run.b and runs[-1].i == run.i and runs[-1].sup == run.sup:
                runs[-1].text += run.text
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


def _find_cover_image(pdf_path, out_dir):
    """Pick the weekly cover art: the largest embedded image on page 1 that is
    not masthead-shaped (the banner is a fixed ~4:1 strip; covers are
    photo-shaped)."""
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
        # Scanned guides embed CCITT-fax/JBIG2 streams pdfimages can't hand
        # us as web images — render the whole page instead; on a scan, page 1
        # *is* the cover.
        for f in os.listdir(out_dir):
            if f.startswith('img-'):
                os.unlink(os.path.join(out_dir, f))
        dest = os.path.join(out_dir, 'cover.jpg')
        render_page_image(pdf_path, 1, dest)
        return dest
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


def _has_tesseract():
    return shutil.which('tesseract') is not None


def _ocr_page(pdf_path, page_num, work_dir):
    """Some pages (GPS notes card, Prayer Journal) are flattened screenshots
    with no text layer. OCR them so the parser can still read the journal
    prayers."""
    prefix = os.path.join(work_dir, f'ocr-{page_num}')
    _run(['pdftoppm', '-f', str(page_num), '-l', str(page_num), '-r', '300',
          '-png', pdf_path, prefix])
    png = next((f for f in os.listdir(work_dir)
                if f.startswith(f'ocr-{page_num}-') and f.endswith('.png')), None)
    if not png:
        return None
    _run(['tesseract', os.path.join(work_dir, png), prefix])
    with open(prefix + '.txt', encoding='utf-8') as fh:
        text = fh.read()
    # Common OCR slip on this material: standalone "I" read as a pipe.
    return re.sub(r'(^|\s)\|(?=\s)', r'\1I', text)


@dataclass
class Extracted:
    pages: list
    cover_path: str | None
    warnings: list


def extract(pdf_path, work_dir, ocr=True):
    os.makedirs(work_dir, exist_ok=True)
    xml_path = os.path.join(work_dir, 'wg.xml')
    warnings = []
    # -i: ignore images; -q: quiet
    _run(['pdftohtml', '-xml', '-i', '-q', pdf_path, xml_path])
    with open(xml_path, encoding='utf-8') as fh:
        xml = fh.read()
    pages = [Page(number=p['number'], width=p['width'], height=p['height'],
                  lines=_build_lines(p))
             for p in _parse_xml(xml)]
    image_pages = [p for p in pages if not p.lines]
    if image_pages:
        if ocr and _has_tesseract():
            for p in image_pages:
                p.ocr_text = _ocr_page(pdf_path, p.number, work_dir)
        elif ocr:
            nums = ', '.join(str(p.number) for p in image_pages)
            warnings.append(
                f'pages {nums} have no text layer and tesseract is not installed — '
                'content on them (e.g. the Prayer Journal) will be missing. '
                'Install tesseract-ocr.')
    cover_path = _find_cover_image(pdf_path, work_dir)
    return Extracted(pages=pages, cover_path=cover_path, warnings=warnings)

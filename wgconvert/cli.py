"""wg-convert — Community Church Leisure World worship-guide converter CLI."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile

from .extract import extract, render_page_image, render_page_region
from .parse import parse
from .render import render

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')

EPILOG = """\
Each guide lands in <outdir>/<YYYY-MM-DD>/ as guide.json + cover image
(+ index.html for `convert`). `render` re-renders after hand-edits to
guide.json and expects the cover image next to it.

Requires poppler-utils (pdftohtml, pdfimages, pdftoppm); tesseract-ocr for the
Prayer Journal page, which is a flattened image in the source PDFs.
"""


def load_church(path):
    with open(path or os.path.join(ROOT, 'config', 'church.json'), encoding='utf-8') as fh:
        return json.load(fh)


def report_warnings(name, warnings):
    for w in warnings or []:
        print(f'  ⚠ {name}: {w}', file=sys.stderr)


def write_guide(guide, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'guide.json'), 'w', encoding='utf-8') as fh:
        json.dump(guide, fh, indent=2, ensure_ascii=False)
        fh.write('\n')


def materialize_flyers(pdf, guide, out_dir):
    for fl in guide.get('flyers') or []:
        fl['image'] = f"flyer-{fl['page']}.jpg"
        render_page_image(pdf, fl['page'], os.path.join(out_dir, fl['image']))
    current = {fl['image'] for fl in guide.get('flyers') or []}
    for f in os.listdir(out_dir):
        if re.fullmatch(r'flyer-\d+\.jpg', f) and f not in current:
            os.unlink(os.path.join(out_dir, f))


def materialize_photos(pdf, guide, out_dir):
    """Interstitial photos, cropped from their pages like flyers are rendered
    from theirs; stale crops from a previous conversion are dropped."""
    for n, im in enumerate(guide.get('images') or [], 1):
        im['image'] = f"photo-{im['page']}-{n}.jpg"
        render_page_region(pdf, im['page'], im, os.path.join(out_dir, im['image']))
    current = {im['image'] for im in guide.get('images') or []}
    for f in os.listdir(out_dir):
        if re.fullmatch(r'photo-\d+-\d+\.jpg', f) and f not in current:
            os.unlink(os.path.join(out_dir, f))


def convert_one(pdf, args, church, render_html=True):
    work_dir = tempfile.mkdtemp(prefix='wg-')
    try:
        extracted = extract(pdf, work_dir, ocr=args.ocr)
        guide = parse(extracted)
        slug = guide['dateISO'] or os.path.splitext(os.path.basename(pdf))[0]
        out_dir = os.path.join(args.out, slug)
        os.makedirs(out_dir, exist_ok=True)

        cover_dest = None
        if extracted.cover_path and not guide.get('suppressCover'):
            cover_dest = os.path.join(
                out_dir, 'cover' + os.path.splitext(extracted.cover_path)[1])
            shutil.copyfile(extracted.cover_path, cover_dest)
        materialize_flyers(pdf, guide, out_dir)
        materialize_photos(pdf, guide, out_dir)
        write_guide(guide, out_dir)

        if render_html:
            html = render(guide, church,
                          banner_path=os.path.join(ROOT, 'assets', 'banner.png'),
                          cover_path=cover_dest, flyer_dir=out_dir)
            with open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8') as fh:
                fh.write(html)

        if not args.quiet:
            n = len(guide['warnings'])
            nn = len(guide.get('notes') or [])
            extra = (f", {n} warning(s)" if n else '') + (f", {nn} note(s)" if nn else '')
            target = f'{out_dir}/' if render_html else f'{out_dir}/guide.json'
            print(f"✔ {os.path.basename(pdf)} -> {target}  ({guide['date'] or 'date unknown'}{extra})")
        report_warnings(os.path.basename(pdf), guide['warnings'])
        for note in guide.get('notes') or []:
            print(f'  ℹ {os.path.basename(pdf)}: {note}', file=sys.stderr)
        return 1 if guide['warnings'] else 0
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='wg-convert', description=__doc__, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['convert', 'parse', 'render'])
    ap.add_argument('inputs', nargs='+', help='PDF file(s), or a guide.json for render')
    ap.add_argument('-o', dest='out', default='out', help='output root (default ./out)')
    ap.add_argument('--church', default=None, help='church config json')
    ap.add_argument('--no-ocr', dest='ocr', action='store_false',
                    help='skip OCR of image-only pages (Prayer Journal)')
    ap.add_argument('-q', dest='quiet', action='store_true',
                    help='quiet (suppress per-file progress, keep warnings)')
    args = ap.parse_args(argv)
    church = load_church(args.church)

    if args.command in ('convert', 'parse'):
        warned = 0
        for pdf in args.inputs:
            warned += convert_one(pdf, args, church, render_html=args.command == 'convert')
        return 1 if warned else 0

    # render
    if len(args.inputs) != 1:
        print('render takes exactly one guide.json', file=sys.stderr)
        return 2
    guide_path = args.inputs[0]
    with open(guide_path, encoding='utf-8') as fh:
        guide = json.load(fh)
    out_dir = os.path.dirname(guide_path) or '.'
    cover = next((f for f in os.listdir(out_dir)
                  if re.fullmatch(r'cover\.(jpe?g|png|webp)', f)), None)
    html = render(guide, church,
                  banner_path=os.path.join(ROOT, 'assets', 'banner.png'),
                  cover_path=os.path.join(out_dir, cover) if cover else None,
                  flyer_dir=out_dir)
    html_path = os.path.join(out_dir, 'index.html')
    with open(html_path, 'w', encoding='utf-8') as fh:
        fh.write(html)
    if not args.quiet:
        print(f'✔ {html_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

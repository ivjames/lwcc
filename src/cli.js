'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const { extract } = require('./extract');
const { parse } = require('./parse');
const { render } = require('./render');

const ROOT = path.join(__dirname, '..');

const USAGE = `wg-convert — Community Church Leisure World worship-guide converter

Usage:
  wg-convert convert <pdf> [<pdf>...] [-o <outdir>]   PDF -> guide.json + index.html
  wg-convert parse <pdf> [-o <outdir>]                PDF -> guide.json only
  wg-convert render <guide.json> [-o <outdir>]        guide.json -> index.html
                                                      (after hand-edits; expects
                                                      cover.* next to the json)

Options:
  -o <dir>     output root (default ./out). Each guide lands in <dir>/<YYYY-MM-DD>/
  --church <f> church config json (default config/church.json in the repo)
  --no-ocr     skip OCR of image-only pages (Prayer Journal)
  -q           quiet (suppress per-file progress, keep warnings)

Requires poppler-utils (pdftohtml, pdfimages, pdftoppm); tesseract-ocr for the
Prayer Journal page, which is a flattened image in the source PDFs.
`;

function parseArgs(argv) {
  const args = { cmd: null, inputs: [], out: 'out', church: null, ocr: true, quiet: false };
  const rest = argv.slice(2);
  if (!rest.length || rest[0] === '-h' || rest[0] === '--help') return args;
  args.cmd = rest.shift();
  while (rest.length) {
    const a = rest.shift();
    if (a === '-o') args.out = rest.shift();
    else if (a === '--church') args.church = rest.shift();
    else if (a === '--no-ocr') args.ocr = false;
    else if (a === '-q') args.quiet = true;
    else args.inputs.push(a);
  }
  return args;
}

function loadChurch(p) {
  return JSON.parse(fs.readFileSync(p || path.join(ROOT, 'config', 'church.json'), 'utf8'));
}

function reportWarnings(name, warnings) {
  for (const w of warnings || []) console.error(`  ⚠ ${name}: ${w}`);
}

function convertOne(pdf, args, church) {
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'wg-'));
  try {
    const extracted = extract(pdf, workDir, { ocr: args.ocr });
    const guide = parse(extracted);
    const slug = guide.dateISO || path.basename(pdf, path.extname(pdf));
    const outDir = path.join(args.out, slug);
    fs.mkdirSync(outDir, { recursive: true });

    let coverDest = null;
    if (extracted.coverPath) {
      coverDest = path.join(outDir, 'cover' + path.extname(extracted.coverPath));
      fs.copyFileSync(extracted.coverPath, coverDest);
    }
    const guidePath = path.join(outDir, 'guide.json');
    fs.writeFileSync(guidePath, JSON.stringify(guide, null, 2) + '\n');

    const html = render(guide, church, {
      bannerPath: path.join(ROOT, 'assets', 'banner.png'),
      coverPath: coverDest,
    });
    const htmlPath = path.join(outDir, 'index.html');
    fs.writeFileSync(htmlPath, html);

    if (!args.quiet) {
      console.log(`✔ ${path.basename(pdf)} -> ${outDir}/  (${guide.date || 'date unknown'}${guide.warnings.length ? `, ${guide.warnings.length} warning(s)` : ''})`);
    }
    reportWarnings(path.basename(pdf), guide.warnings);
    return guide.warnings.length ? 1 : 0;
  } finally {
    fs.rmSync(workDir, { recursive: true, force: true });
  }
}

function main() {
  const args = parseArgs(process.argv);
  if (!args.cmd) {
    process.stdout.write(USAGE);
    process.exit(0);
  }
  const church = loadChurch(args.church);

  if (args.cmd === 'convert' || args.cmd === 'parse') {
    if (!args.inputs.length) { console.error('no input PDFs given'); process.exit(2); }
    let warned = 0;
    for (const pdf of args.inputs) {
      if (args.cmd === 'parse') {
        const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'wg-'));
        try {
          const extracted = extract(pdf, workDir, { ocr: args.ocr });
          const guide = parse(extracted);
          const slug = guide.dateISO || path.basename(pdf, path.extname(pdf));
          const outDir = path.join(args.out, slug);
          fs.mkdirSync(outDir, { recursive: true });
          if (extracted.coverPath) {
            fs.copyFileSync(extracted.coverPath, path.join(outDir, 'cover' + path.extname(extracted.coverPath)));
          }
          fs.writeFileSync(path.join(outDir, 'guide.json'), JSON.stringify(guide, null, 2) + '\n');
          if (!args.quiet) console.log(`✔ ${path.basename(pdf)} -> ${outDir}/guide.json`);
          reportWarnings(path.basename(pdf), guide.warnings);
          warned += guide.warnings.length ? 1 : 0;
        } finally {
          fs.rmSync(workDir, { recursive: true, force: true });
        }
      } else {
        warned += convertOne(pdf, args, church);
      }
    }
    process.exit(warned ? 1 : 0);
  }

  if (args.cmd === 'render') {
    if (args.inputs.length !== 1) { console.error('render takes exactly one guide.json'); process.exit(2); }
    const guidePath = args.inputs[0];
    const guide = JSON.parse(fs.readFileSync(guidePath, 'utf8'));
    const dir = path.dirname(guidePath);
    const cover = fs.readdirSync(dir).find(f => /^cover\.(jpe?g|png|webp)$/.test(f));
    const html = render(guide, church, {
      bannerPath: path.join(ROOT, 'assets', 'banner.png'),
      coverPath: cover ? path.join(dir, cover) : null,
    });
    const htmlPath = path.join(dir, 'index.html');
    fs.writeFileSync(htmlPath, html);
    if (!args.quiet) console.log(`✔ ${htmlPath}`);
    process.exit(0);
  }

  console.error(`unknown command: ${args.cmd}\n`);
  process.stdout.write(USAGE);
  process.exit(2);
}

main();

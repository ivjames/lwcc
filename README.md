# lwcc — worship guides for lwcc.lab980.com

The worship-guide site and the app that keeps it fed: upload the week's PDF
at `/admin`, it converts and publishes immediately, and the newest Sunday
becomes the front page.

## The app

- `GET /` — the current (newest) Sunday's guide
- `GET /<YYYY-MM-DD>/` — any published Sunday (permanent URLs)
- `GET /archive` — every published Sunday
- `GET /admin` — the admin area (batch upload with per-file results, review
  panel, guide editor). Every admin page is behind a sign-in: enter the upload
  token once at `/admin/login` and a long-lived HttpOnly cookie (~6 months)
  keeps that browser signed in; `/admin/logout` ends it.
- `GET /admin/history` — the per-file upload results (filename, status,
  warnings, errors), browsable long after the upload page is closed:
  every conversion ever run, newest first, filterable by outcome
  (`?status=ok|warned|failed`). Backed by `uploads.log`; the last few also
  appear on `/admin` as a Recent-uploads card.
- `POST /api/upload` — raw PDF body, authenticated by the admin cookie or an
  `X-Upload-Token` header (for curl). The bytes are spooled and accepted
  immediately; a server-side queue converts one file at a time and publishes
  to `public/<date>/`, so batch uploads are bounded by bandwidth, not OCR —
  the admin page polls `GET /api/status?ids=…` for per-file progress, and
  every outcome (with parser warnings) lands in `/admin/history`. Add
  `?sync=1` to convert before the reply and get the result inline (the old
  behavior). `?date=YYYY-MM-DD` pins the publish date, winning over whatever
  the parser reads — for memorial programs whose printed dates are not the
  service date; the admin upload table has a per-file date field for this.
  Failed conversions keep their PDF in `queue/failed/` and show on `/admin`
  with a Retry button (optionally pinned to a date) — after a parser fix,
  no re-upload is needed. Fails closed when no token is configured.
- `GET /admin/aiscan/<YYYY-MM-DD>` — the AI article scanner: reviews a
  Sunday's parsed guide with Claude for text the OCR pipeline filed under the
  wrong class (announcements vs page directions vs worship content) and
  offers verified, text-preserving repairs — nothing is rewritten, findings
  are moves only, each checked against the stored text before it is applied.
  Findings persist in `public/<date>/aiscan.json`; apply, dismiss, or leave
  them for hand-editing. Requires `ANTHROPIC_API_KEY` in `.env` (scanning
  fails closed without it; `AISCAN_MODEL` overrides the default model). The
  `/admin` panel links every Sunday's scan, badges open findings, and offers
  a scan-all for the unscanned backlog (one API request per Sunday). Scans
  run through their own durable server-side queue — up to `AISCAN_WORKERS`
  (default 10) at a time, markers in `queue/aiscan/` re-enqueued at startup —
  so an `lwcc redeploy` pauses in-flight scans rather than losing them, and
  the admin pages just watch the queue until it drains. `GET /admin/aiscan`
  aggregates matching findings across Sundays in two tiers — identical
  quoted text flagged the same way on two or more guides (the weekly
  masthead filed as an announcement), and same-error-varying-text groups
  (the same misclassification with different words each week, grouped by
  current → proposed and fix op) —
  and applies or dismisses a whole group at once, each fix still verified
  against its own Sunday's stored text (typography-tolerant: a plain-ASCII
  quote matches the printed curly quotes and dashes). Dismissed and skipped
  findings are both reversible — a Reopen button on the group (and per
  Sunday) puts them back to open. "Clear resolved" (per Sunday, or across
  all Sundays from the admin card) archives applied and dismissed findings
  into `resolvedFindings` in aiscan.json — out of the pages and groups, kept
  as history, and preserved across re-scans. Skipped fixes are recoverable:
  "Retry skipped fixes" (per group and per Sunday) reopens them and re-applies
  through a relocation pass — when a stored position went stale (earlier
  fixes, hand edits, a re-convert), the quoted text is searched for across
  the guide and the fix applies only where it matches exactly one target;
  vanished or ambiguous text still refuses with the reason. A group whose
  findings carry no mechanical fix offers "Re-scan for fixes": its Sundays re-scan through the
  queue so the model can pick from the current fix vocabulary, then the
  group returns applyable.
- `GET /healthz` — liveness for the platform `health-check` sweep

Every published Sunday links its printed original: the week-nav strip on a
guide page offers "Original PDF" (`/<date>/original` — an embedded viewer
with a link back to the converted page and a download link) whenever the
source is stored.

Every published Sunday keeps its uploaded PDF as `public/<date>/source.pdf`,
so after a parser upgrade the admin panel's **Re-convert** action re-runs the
converter server-side — no re-upload needed. Sundays uploaded before
retention existed have no stored source; re-upload those once. Re-convert
discards hand-edits to that Sunday (it re-parses the PDF from scratch).

Batch re-conversions are restart-durable: each queued Sunday leaves a
marker in `queue/reconvert/` until its job settles, and the app resumes
survivors on startup — a redeploy mid-sweep pauses the batch instead of
losing it. Conversion workers default to one per core (max 4); set
`CONVERT_WORKERS` in `.env` to override.

**Re-convert, keep edits** merges instead (`wgconvert/merge.py`): the
published guide stays the skeleton — the operator's text, structure, and
classifications win — while any field whose text still matches the printed
PDF (canonically: tags stripped, typography normalized) adopts the fresh
conversion's markup and accent colors, headings with no accent set adopt the
detected one, and the flyer/photo/cover inventory follows the fresh
conversion (photo captions the operator has set win over the fresh parse's).
Edited text simply has no canonical match and passes through untouched. A
"Refresh every Sunday, keep edits" sweep runs the same merge across the
backlog through the server queue; each sweep registers a server-side meter
that the queue banner and the sweep card show live ("37 of 120 done, 3
failed" with a progress bar, in any browser) until its last job settles.

Batch the backlog from a terminal:

```
for f in backlog/*.pdf; do
  curl -X POST --data-binary @"$f" -H "X-Upload-Token: $TOKEN" \
       -H "Content-Type: application/pdf" \
       -H "X-Filename: $(basename "$f")" https://lwcc.lab980.com/api/upload
done
```

(`X-Filename` is optional but makes `/admin/history` show which PDF produced
each result.)

(or convert locally with `bin/wg-convert convert backlog/*.pdf -o public/`
and commit/rsync the output — same result.)

## Deploy on the droplet

`lwcc.lab980.com` is provisioned per lab980 conventions (nginx vhost →
local port, pm2, certbot). First deploy:

```
cd /var/www/lwcc                      # provision-site cloned the repo here
cp .env.example .env                  # then set UPLOAD_TOKEN=$(openssl rand -hex 16)
pm2 start ecosystem.config.cjs && pm2 save
ln -sf /var/www/lwcc/bin/lwcc /usr/local/bin/lwcc
health-check --site lwcc
```

The droplet needs `poppler-utils` and `tesseract-ocr` installed (`apt-get
install -y poppler-utils tesseract-ocr`) for uploads to convert.

**nginx one-time tweak** (the provisioned vhost defaults reject PDF-sized
uploads with a 413 and can time out slow OCR): add inside the `server {`
block with `listen 443` in `/etc/nginx/sites-available/lwcc.lab980.com`:

```
    client_max_body_size 50m;
    proxy_read_timeout 300s;
```

then `nginx -t && systemctl reload nginx`.

The app listens on **8069** (`--port` in `ecosystem.config.cjs`); make sure it
matches the `proxy_pass` port in `/etc/nginx/sites-available/lwcc.lab980.com`
— edit whichever side disagrees. Subsequent deploys: `lwcc redeploy`.

Tests: `python3 test/run.py` (converter) and `python3 test/test_app.py`
(upload flow, end to end).

# Worship-guide converter

Converts Community Church Leisure World's weekly worship-guide PDFs into
self-contained, accessible web pages (one HTML file per Sunday, images
inlined, no external requests). Built to chew through the backlog of past
guides as well as each new week's PDF.

```
wg-convert convert WG_2026_08_02.pdf            # -> out/2026-08-02/{index.html, guide.json, cover.jpg}
wg-convert convert backlog/*.pdf -o site/       # batch a whole folder
```

## How it works

Three stages, each usable on its own:

Scanned bulletins (no text layer) are read too: pages whose OCR reads as
typed prose are structured through the normal parser — order of worship,
prayers, announcements and all — with a note recording the OCR provenance
(the Original PDF link shows the printed page for verification). Art and
poster pages stay images; engraved scores are dropped as everywhere else.
A scan that is mostly art publishes as a facsimile of page images.

1. **extract** (`wgconvert/extract.py`) — `pdftohtml -xml` gives every text run with
   font, color, position, and bold/italic flags. Sheet-music engraving noise
   (Finale exports: note glyphs, lyric syllables, composer credits) is dropped
   by fontspec — engraving text uses the Maestro music fonts and prints in
   `#231f20`, real text in `#000000`/accent colors. Emoji placeholders
   (invisible glyphs, opacity 0) are dropped too. `pdfimages` pulls the weekly
   cover art (largest photo-shaped image on page 1). Pages with no text layer
   — the GPS notes card and the Prayer Journal are flattened screenshots — are
   OCR'd with tesseract at 300 dpi; each recognized word's ink color is
   sampled from the rendered page (median of the dark pixels in its box), so
   scanned bulletins keep the colored text some sections rely on — scans have
   no boldness, so accent-ink headings are their only structural marker.

2. **parse** (`wgconvert/parse.py`) — classifies the extracted lines into a
   structured `guide.json`: date/season, message series, welcome, the order of
   worship (labels, titles, speakers, prayers with their line structure,
   scripture with verse-number superscripts, congregation refrains, stage
   directions), music team, prayer requests, announcements, special events,
   and the prayer journal. Interstitial photos — images set between the
   prose on otherwise-textual pages, which are neither the cover nor a
   full-page flyer — are kept too (`images` in guide.json): the placed-image
   boxes `pdftohtml` reports are filtered down to content-shaped ones (icons,
   off-page background art, full-page backdrops, and panels with text printed
   on them are not photos; a picture *of* printed music — a hymnal snippet
   pasted in as an image — is recognized by its pixels, colorless bilevel
   print with staff lines, and dropped with a note under the
   music-not-reproduced rule), the caption printed under a photo is claimed
   for it rather than leaking into the surrounding section, and each photo is
   published as a crop of its rendered page (`photo-<page>-<n>.jpg`) in a
   Photos section. Nothing is silently dropped; findings are
   two-tier: **warnings** mean content may be lost or wrong (missing
   sections in a text guide, an uncorroborated OCR date — these flag the
   review panel and exit code 1) while **notes** mean content was kept but
   classified loosely (unrecognized label parsed generically, unlabeled
   block kept) — recorded in guide.json and the upload history, no review
   demanded.

3. **render** (`wgconvert/render.py`) — `guide.json` + `config/church.json` +
   `template/guide.css` → `index.html` in the approved design: sticky section
   nav, large-type serif body, skip link, reduced-motion support, banner and
   cover embedded as data URIs.

The intermediate `guide.json` is the correction point for backlog quirks:

```
wg-convert parse odd-week.pdf        # produces guide.json (+ cover image)
$EDITOR out/1999-03-07/guide.json    # fix whatever parsed oddly
wg-convert render out/1999-03-07/guide.json
```

Body text in `guide.json` may carry only `<b>`, `<i>`, `<sup>`, and
`<span class="fc-…">` markup (already escaped by the parser); prayers use
`\n` for their line breaks. Everything else is plain text. The `fc-` spans
carry printed accent inks **exactly**: `fc-rrggbb` classes hold the print's
own hex, and the renderer generates one CSS rule per ink, darkened only as
far as WCAG 4.5:1 against the page background demands (hue preserved — the
print's orange stays orange). The five named classes (`fc-maroon` …) remain
supported for guides published before exact inks. Headings printed in a
single ink get a `color` field ('#rrggbb'); rainbow-lettered headings
instead get `headingHtml`, per-letter ink spans over the Title Case heading
(dropped automatically if the heading text is later edited); prayer-request
names carry `nameColor`. Deliberate rainbow lettering (single letters in
distinct ink families) is preserved run for run; accidental mid-word ink
changes still unify to one color per word.

## Requirements

- Python 3.10+ (stdlib only, no pip dependencies)
- `poppler-utils` (`pdftohtml`, `pdfimages`, `pdftoppm`, `pdfinfo`)
- `tesseract-ocr` — only for the Prayer Journal page; without it the page is
  skipped with a warning

## Repo layout

```
bin/wg-convert       CLI (convert | parse | render)
wgconvert/           extract / parse / render / cli / known_texts (Python package)
template/guide.css   the approved page styles
assets/banner.png    church masthead (shared by every issue)
config/church.json   name, address, phone, service time, vision line
samples/             sample PDF + its parsed guide.json (regression reference)
test/run.py          python3 test/run.py — converts the sample, asserts the structure
```

## Conventions & judgment calls

- **Faithful to the source**: typos in the PDF are preserved, not copyedited.
  Fix them in `guide.json` and re-render if desired.
- Hymn/offertory/reflection items keep title + performer only; engraved music
  is web-hostile and is deliberately not reproduced. Fixed liturgy that exists
  *only* as engraving (the Doxology) is filled in from `wgconvert/known_texts.py`.
- The GPS "Notes from the Service" page is intentionally skipped — it's a
  print-only fill-in card.
- Announcement emoji are assigned by heading keyword in `wgconvert/render.py`
  (`announcementEmoji` / `eventEmoji`); extend the maps as new headings appear.
- En dashes between words become em dashes (house style); curly quotes are
  kept in prose, straightened in titles.

## Deployment note

Per lab980 conventions the output is static — `out/` can be rsynced to any
vhost or committed to a site repo. No server component.

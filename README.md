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
- `GET /healthz` — liveness for the platform `health-check` sweep

Every published Sunday keeps its uploaded PDF as `public/<date>/source.pdf`,
so after a parser upgrade the admin panel's **Re-convert** action re-runs the
converter server-side — no re-upload needed. Sundays uploaded before
retention existed have no stored source; re-upload those once. Re-convert
discards hand-edits to that Sunday (it re-parses the PDF from scratch).

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

1. **extract** (`wgconvert/extract.py`) — `pdftohtml -xml` gives every text run with
   font, color, position, and bold/italic flags. Sheet-music engraving noise
   (Finale exports: note glyphs, lyric syllables, composer credits) is dropped
   by fontspec — engraving text uses the Maestro music fonts and prints in
   `#231f20`, real text in `#000000`/accent colors. Emoji placeholders
   (invisible glyphs, opacity 0) are dropped too. `pdfimages` pulls the weekly
   cover art (largest photo-shaped image on page 1). Pages with no text layer
   — the GPS notes card and the Prayer Journal are flattened screenshots — are
   OCR'd with tesseract at 300 dpi.

2. **parse** (`wgconvert/parse.py`) — classifies the extracted lines into a
   structured `guide.json`: date/season, message series, welcome, the order of
   worship (labels, titles, speakers, prayers with their line structure,
   scripture with verse-number superscripts, congregation refrains, stage
   directions), music team, prayer requests, announcements, special events,
   and the prayer journal. Unrecognized labels are parsed generically and
   reported as warnings — nothing is silently dropped, so drift in older
   guides surfaces immediately (exit code 1 when any file warns).

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

Body text in `guide.json` may carry only `<b>`, `<i>`, `<sup>` markup (already
escaped by the parser); prayers use `\n` for their line breaks. Everything
else is plain text.

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

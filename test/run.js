'use strict';
// Regression test: convert the checked-in sample PDF and assert the parsed
// structure. Content assertions are invariants (not byte-golden) so minor
// poppler/tesseract version drift doesn't break the suite; the checked-in
// samples/WG_2026_08_02.guide.json is the reference for eyeball diffs.

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { extract } = require('../src/extract');
const { parse } = require('../src/parse');
const { render } = require('../src/render');

const SAMPLE = path.join(__dirname, '..', 'samples', 'WG_2026_08_02.pdf');
const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'wg-test-'));

try {
  const extracted = extract(SAMPLE, workDir);
  const g = parse(extracted);

  assert.equal(g.date, 'August 2, 2026');
  assert.equal(g.dateISO, '2026-08-02');
  assert.equal(g.season, '10th Sunday After Pentecost');
  assert.deepEqual(g.series, { title: 'Love Unleashed', by: 'Rev. Johan Dodge' });
  assert.equal(g.warnings.length, 0, `unexpected warnings: ${g.warnings.join('; ')}`);

  assert.ok(g.welcome && /Good Morning and Welcome/.test(g.welcome.body[0].text));

  const labels = g.order.filter(o => o.kind === 'item').map(o => o.label);
  assert.deepEqual(labels, [
    'Call to Worship', 'Prayer of the Day', 'Hymn', 'Prayers of Intercession',
    'Hymn', 'Prayer for Illumination', 'Scripture', 'Message',
    'Questions for Reflection', 'Reflection', 'Invitation to the Offering',
    'Offertory', 'Prayer of Thanksgiving', 'Hymn', 'Blessing', 'Sending Forth',
  ]);
  assert.equal(g.order.filter(o => o.kind === 'stage').length, 6);

  const ctw = g.order.find(o => o.label === 'Call to Worship');
  assert.equal(ctw.title, 'Doxology');
  assert.ok(/Praise God from whom all blessings flow/.test(ctw.body[0].text),
    'Doxology text filled from known-texts');

  const lit = g.order.find(o => o.type === 'litany');
  assert.equal(lit.body.filter(b => b.type === 'refrain').length, 3);

  const scripture = g.order.find(o => o.type === 'scripture');
  assert.deepEqual(scripture.body.filter(b => b.type === 'ref').map(b => b.text),
    ['Romans 9:1-5', 'Matthew 14:13-21']);
  const matthew = scripture.body[3].text;
  for (let v = 13; v <= 21; v++) {
    assert.ok(matthew.includes(`<sup>${v}</sup>`), `verse ${v} marker present`);
  }

  assert.equal(g.musicTeam.length, 4);
  assert.deepEqual(g.musicTeam[0], { name: 'Dave Albulario', role: 'Tenor / Music Dir.' });

  assert.equal(g.prayerRequests.length, 1);
  assert.equal(g.prayerRequests[0].name, 'Patty Jo Schmitz');

  assert.deepEqual(g.announcements.map(a => a.heading), [
    'Flowers & Fellowship', 'Coffee with Pastor', 'August Communion Sunday',
    'Recent LWCC Worship Attendance',
  ]);
  assert.equal(g.announcements[3].kind, 'attendance');

  assert.equal(g.specialEvents.length, 1);
  assert.equal(g.specialEvents[0].heading, 'Duo Concert — TODAY!');
  assert.equal(g.specialEvents[0].note, 'For those who are able, the suggested donation is $10.');

  if (g.journal) {
    assert.ok(/On this new day, O God/.test(g.journal.morning));
    assert.ok(/Hear me at the end of this day/.test(g.journal.evening));
  } else {
    console.warn('  (journal missing — tesseract not installed?)');
  }

  assert.ok(extracted.coverPath, 'cover image extracted');

  // Render smoke test: valid, self-contained, both images inlined.
  const church = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'config', 'church.json'), 'utf8'));
  const html = render(g, church, {
    bannerPath: path.join(__dirname, '..', 'assets', 'banner.png'),
    coverPath: extracted.coverPath,
  });
  assert.ok(html.includes('data:image/png;base64,'), 'banner inlined');
  assert.ok(html.includes('data:image/jpeg;base64,'), 'cover inlined');
  assert.ok(!/src="(?!data:)/.test(html), 'no external resources');
  assert.ok(html.includes('id="journal"') === Boolean(g.journal));

  console.log('all tests passed');
} finally {
  fs.rmSync(workDir, { recursive: true, force: true });
}

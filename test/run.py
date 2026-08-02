"""Regression test: convert the checked-in sample PDF and assert the parsed
structure. Content assertions are invariants (not byte-golden) so minor
poppler/tesseract version drift doesn't break the suite; the checked-in
samples/WG_2026_08_02.guide.json is the reference for eyeball diffs.

Run:  python3 test/run.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from wgconvert import extract, parse, render  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
SAMPLE = os.path.join(ROOT, 'samples', 'WG_2026_08_02.pdf')

work_dir = tempfile.mkdtemp(prefix='wg-test-')
try:
    extracted = extract(SAMPLE, work_dir)
    g = parse(extracted)

    assert g['date'] == 'August 2, 2026'
    assert g['dateISO'] == '2026-08-02'
    assert g['season'] == '10th Sunday After Pentecost'
    assert g['series'] == {'title': 'Love Unleashed', 'by': 'Rev. Johan Dodge'}
    assert not g['warnings'], f"unexpected warnings: {'; '.join(g['warnings'])}"

    assert g['welcome'] and re.search(r'Good Morning and Welcome', g['welcome']['body'][0]['text'])

    labels = [o['label'] for o in g['order'] if o['kind'] == 'item']
    assert labels == [
        'Call to Worship', 'Prayer of the Day', 'Hymn', 'Prayers of Intercession',
        'Hymn', 'Prayer for Illumination', 'Scripture', 'Message',
        'Questions for Reflection', 'Reflection', 'Invitation to the Offering',
        'Offertory', 'Prayer of Thanksgiving', 'Hymn', 'Blessing', 'Sending Forth',
    ], labels
    assert sum(1 for o in g['order'] if o['kind'] == 'stage') == 6

    ctw = next(o for o in g['order'] if o.get('label') == 'Call to Worship')
    assert ctw['title'] == 'Doxology'
    assert re.search(r'Praise God from whom all blessings flow', ctw['body'][0]['text']), \
        'Doxology text filled from known-texts'

    lit = next(o for o in g['order'] if o.get('type') == 'litany')
    assert sum(1 for b in lit['body'] if b['type'] == 'refrain') == 3

    scripture = next(o for o in g['order'] if o.get('type') == 'scripture')
    refs = [b['text'] for b in scripture['body'] if b['type'] == 'ref']
    assert refs == ['Romans 9:1-5', 'Matthew 14:13-21'], refs
    matthew = scripture['body'][3]['text']
    for v in range(13, 22):
        assert f'<sup>{v}</sup>' in matthew, f'verse {v} marker present'

    assert len(g['musicTeam']) == 4
    assert g['musicTeam'][0] == {'name': 'Dave Albulario', 'role': 'Tenor / Music Dir.'}

    assert len(g['prayerRequests']) == 1
    assert g['prayerRequests'][0]['name'] == 'Patty Jo Schmitz'

    assert [a['heading'] for a in g['announcements']] == [
        'Flowers & Fellowship', 'Coffee with Pastor', 'August Communion Sunday',
        'Recent LWCC Worship Attendance',
    ]
    assert g['announcements'][3]['kind'] == 'attendance'

    assert len(g['specialEvents']) == 1
    assert g['specialEvents'][0]['heading'] == 'Duo Concert — TODAY!'
    assert g['specialEvents'][0]['note'] == 'For those who are able, the suggested donation is $10.'

    if g['journal']:
        assert re.search(r'On this new day, O God', g['journal']['morning'])
        assert re.search(r'Hear me at the end of this day', g['journal']['evening'])
    else:
        print('  (journal missing — tesseract not installed?)', file=sys.stderr)

    assert extracted.cover_path, 'cover image extracted'

    # Render smoke test: valid, self-contained, both images inlined.
    with open(os.path.join(ROOT, 'config', 'church.json'), encoding='utf-8') as fh:
        church = json.load(fh)
    html = render(g, church,
                  banner_path=os.path.join(ROOT, 'assets', 'banner.png'),
                  cover_path=extracted.cover_path)
    assert 'data:image/png;base64,' in html, 'banner inlined'
    assert 'data:image/jpeg;base64,' in html, 'cover inlined'
    assert not re.search(r'src="(?!data:)', html), 'no external resources'
    assert ('id="journal"' in html) == bool(g['journal'])

    # ---- second sample: 2025 format (Communion liturgy, text-layer GPS and
    # Prayer Journal pages, hymn-credits block, chapel-style prayer requests)
    g2 = parse(extract(os.path.join(ROOT, 'samples', 'WG_2025_09_07.pdf'), work_dir + '-2'))
    assert g2['dateISO'] == '2025-09-07'
    assert g2['series'] == {'title': 'From Where Does My Help Come?', 'by': 'Rev. Lisa Williams'}
    labels2 = [o['label'] for o in g2['order'] if o['kind'] == 'item']
    for expected in ('Holy Communion | The Eucharist', 'Invitation to Communion',
                     'Blessing of Bread & Wine', 'The Sharing of the Bread',
                     'The Sharing of the Cup', 'Unison Prayer'):
        assert expected in labels2, f'communion label {expected!r} parsed'
    untitled = [o for o in g2['order'] if o['kind'] == 'item' and o['label'] is None]
    assert len(untitled) == 1 and untitled[0]['body'], 'post-stage liturgy kept, not dropped'
    assert [m['name'] for m in g2['musicTeam']] == [
        'Dave Albulario', 'Jennifer Rudy', 'John Fluker', 'Hannah Yi', 'Jim Orr']
    assert 'musicCredits' not in g2, 'credits are discarded, not stored'
    assert len(g2['prayerRequests']) == 1
    assert [a['heading'] for a in g2['announcements']] == [
        'Flowers and Fellowship', 'Visiting Pastor Lisa', 'Coffee with Pastor',
        'LW Korean Community Church Picnic Today!', 'A Course in Miracles',
        'Blessing of the Animals', 'Need a Bible or Devotional?',
        'Recent LWCC Worship Attendance']
    assert g2['journal'] and 'God of all creation' in g2['journal']['morning'], \
        'text-layer Prayer Journal parsed without OCR'
    assert not g2['specialEvents'], 'journal midday note not misread as an event'
    assert len(g2['warnings']) == 1 and 'untitled item' in g2['warnings'][0], g2['warnings']

    print('all tests passed')
finally:
    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.rmtree(work_dir + '-2', ignore_errors=True)

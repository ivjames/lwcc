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

# date-line variants seen in festival editions (no fixture needed)
from wgconvert.parse import DATE_RE  # noqa: E402
assert DATE_RE.match('August 2, 2026')
assert DATE_RE.match('May the 4th, 2025')
_m = DATE_RE.match('April 20, 2025 – EASTER SUNDAY')
assert _m and _m.group(4) == 'EASTER SUNDAY'
assert not DATE_RE.match('Sunday Service: 9:30 AM')

# 2022-23 backlog header: season first, date after the dash
from wgconvert.parse import SEASON_DATE_RE  # noqa: E402
_m = SEASON_DATE_RE.match('Second Sunday of Easter — April 16, 2023')
assert _m and _m.group('season') == 'Second Sunday of Easter'
assert _m.group('month') == 'April' and _m.group('day') == '16' and _m.group('year') == '2023'
assert SEASON_DATE_RE.match('Palm Sunday - April 2, 2023')
assert not SEASON_DATE_RE.match('Sunday Service: 9:30 AM')
assert not SEASON_DATE_RE.match('April 20, 2025 – EASTER SUNDAY')

# 2023-24 label styles: single-run caps labels with inline attribution, and
# title-case music labels — recognized without the modern bold-run structure.
from wgconvert.extract import Run, Line  # noqa: E402
from wgconvert.parse import match_label, kind_for, known_label  # noqa: E402


def _line(text, bold=False, left=50):
    return Line(page=1, top=0, bottom=10, left=left, height=12,
                runs=[Run(text=text, b=bold)], text=text)


_lab = match_label(_line('PRAYER OF CONFESSION – Taylor White'))
assert _lab and _lab['label'] == 'PRAYER OF CONFESSION' and _lab['who'] == 'Taylor White'
assert kind_for(_lab['label']) == 'prayer'
_lab = match_label(_line('ENTRANCE PROCESSIONAL – “All Glory, Laud, and Honor”'))
assert _lab and _lab['label'] == 'ENTRANCE PROCESSIONAL'
assert _lab['title'] == 'All Glory, Laud, and Honor' and kind_for(_lab['label']) == 'music'
_lab = match_label(_line('Hymn: “Let There Be Peace on Earth”'))
assert _lab and _lab['label'] == 'HYMN' and _lab['title'] == 'Let There Be Peace on Earth'
_lab = match_label(_line('Offertory: “Alleluia! Give the Glory” David Albulario'))
assert _lab and _lab['label'] == 'OFFERTORY' and _lab['who'] == 'David Albulario'
assert match_label(_line('You are welcome here.')) is None
assert match_label(_line('THANKS BE TO GOD')) is None, 'unknown caps prose is not a label'

# Centered labels (well right of the margin), hyphenated and junk-prefixed
# variants — accepted only because the vocabulary knows them.
_lab = match_label(_line('ENTRANCE PROCESSIONAL – “All Glory, Laud, and Honor”', left=220))
assert _lab and _lab['label'] == 'ENTRANCE PROCESSIONAL', 'centered label accepted'
_lab = match_label(_line('Hymn: “Hosanna, Loud Hosanna”', left=250))
assert _lab and _lab['label'] == 'HYMN'
assert match_label(_line('REJOICE ALWAYS', left=220)) is None, \
    'centered unknown caps still not a label'
_lab = match_label(_line('HYMN-CAROL: “O Come, All Ye Faithful” / “Adéste Fidéles”'))
assert _lab and kind_for(_lab['label']) == 'music', 'hyphenated hymn label'
_lab = match_label(_line('`HYMN: “Let There Be Peace on Earth”'))
assert _lab and _lab['label'] == 'HYMN', 'stray backtick prefix tolerated'

# Early-2023 Title Case labels — accepted when the vocabulary knows the
# uppercased phrase; prose protects itself (periods break the lookahead,
# long candidates are rejected, dialog turns are not in the vocabulary).
_lab = match_label(_line('Opening Prayer – Kelly Frankiewicz M.Div'))
assert _lab and _lab['label'] == 'OPENING PRAYER' and _lab['who'] == 'Kelly Frankiewicz M.Div'
_lab = match_label(_line('The Sharing of the Cup'))
assert _lab and _lab['label'] == 'THE SHARING OF THE CUP'
assert match_label(_line('Scripture says God loves you.')) is None
assert match_label(_line('People: Let me be Your change agent.')) is None
assert match_label(_line('Pastor: The table is ready.')) is None
assert kind_for('CLOSING HYMN') == 'music' and known_label('CLOSING HYMN')
assert known_label('OPENING WORDS') and known_label('DECLARATION OF FORGIVENESS')

# Engraved score pages: music-font/engraving-color pages are deliberately
# not reproduced — never OCR'd, never flyer-ized, and their credits/title
# residue goes with the score. Pages without engraving marks are untouched.
from types import SimpleNamespace as NS  # noqa: E402
from wgconvert.extract import page_is_engraved, CREDITS_LINE_RE  # noqa: E402
_eng_items = [{'font': NS(color='#231f20', family='Maestro')}]
_txt_items = [{'font': NS(color='#000000', family='Georgia')}]
assert page_is_engraved(_eng_items, [])
assert page_is_engraved(_eng_items, [
    _line('Shepherd Me, O God'),
    _line('Words: Marty Haugen © 1986 GIA Publications, Inc.')])
assert not page_is_engraved(_txt_items, []), 'no engraving marks → image page'
assert not page_is_engraved(_eng_items, [
    _line(f'line {i} of real prose content that keeps this page textual')
    for i in range(5)]), 'pages with real content are never absorbed'
assert CREDITS_LINE_RE.search('Music by John Fluker, arr. D. Albulario')
assert not CREDITS_LINE_RE.search('We pray for the whole world.')

# 2024 festival/stewardship vocabulary and recurring page furniture
from wgconvert.parse import FIXTURE_BLOCK_RE  # noqa: E402
assert kind_for('PRELUDE') == 'music' and kind_for('HOMILY') == 'message'
assert not known_label('PASTOR') and not known_label('CONGREGATION'), \
    'dialog turns are not sections — they stay inside the liturgy body'
assert known_label('STEWARDSHIP MOMENT')
assert known_label('THE CEREMONY OF CANDLE LIGHTING')
assert known_label('LIGHTING OF THE PASCHAL CANDLE')
assert known_label('FLOWERING THE EASTER CROSS') and known_label('EASTER PROCLAMATION')
assert known_label('PRESENTATION OF CERTIFICATES') and known_label('THANKSGIVING FOR WATER')
assert FIXTURE_BLOCK_RE.match('Let us build a house where hands will reach beyond wood and stone')
assert FIXTURE_BLOCK_RE.match('LEISUREWORLDCOMMUNITYCHURCH')
assert FIXTURE_BLOCK_RE.match('Our Vision: To Be the Lantern to Leisure World')
assert FIXTURE_BLOCK_RE.match('continues!')
assert not FIXTURE_BLOCK_RE.match('Our church picnic continues this week')
assert not FIXTURE_BLOCK_RE.match('Let us pray for the needs of the world.')

# Text-layer Prayer Journal pages (2026 winter/spring editions) rebuild the
# OCR-style paragraph blob and reuse parse_journal.
from wgconvert.parse import parse_journal, text_journal_blob  # noqa: E402
_jpage = [_line(t) for t in (
    '\\', 'Prayer Journal',
    'Use this prayer in your time with God each day.',
    'Household Prayer: Morning',
    'Loving God, as this new day dawns, may your Spirit',
    'guide my feet. Amen.',
    'Consider The Upper Room Daily Devotional',
    'as a resource for Midday Prayer.',
    'Household Prayer: Evening',
    'Holy One, thank you for the gift of this day. Amen.',
)]
_j = parse_journal(text_journal_blob(_jpage))
assert _j and _j['subtitle'] == 'Use this prayer in your time with God each day.'
assert _j['morning'].startswith('Loving God') and _j['morning'].endswith('Amen.')
assert _j['midday'].startswith('Consider The Upper Room')
assert _j['evening'].startswith('Holy One')

# Engraved scores scanned into a bulletin: hyphenated-syllable OCR is the
# tell; prose pages never trip it.
from wgconvert.parse import score_page  # noqa: E402
assert score_page('Make me a chan - nel of your peace. Where there is hat - red, '
                  'let me bring your love. in - ju - ry, your par - don, Lord')
assert score_page('Let there be peace on earth, and let it be - gin with me. '
                  'With ev - ery step I take. Music by Sy Miller © 1955 Jan-Lee Music')
assert not score_page('We commend all of life to you, O God, knowing that you '
                      'hear our twenty-first-century prayers and answer them.')
assert not score_page('Anthem: “Shine, Jesus, Shine”\nPRAYER FOR ILLUMINATION')

# Scanned guides (no text layer anywhere): the date comes from page-image
# OCR and every page publishes as an image. Memorial lifespan dates
# ("July 21, 1944 – November 29, 2025") never become the service date.
from wgconvert.extract import Extracted, Page  # noqa: E402
from wgconvert.parse import plausible_service_year  # noqa: E402
assert not plausible_service_year(1944) and plausible_service_year(2025)
_m = DATE_RE.match('Saturday, December 13, 2025')
assert _m and _m.group(2) == '13', 'weekday-prefixed service dates accepted'
_scan = Extracted(pages=[
    Page(number=1, width=612, height=792, lines=[],
         ocr_text='Leisure World Community Church\nin Worship January 22, 2023'),
    Page(number=2, width=612, height=792, lines=[],
         ocr_text='PRAYERS OF INTERCESSION\nhear us as we pray'),
], cover_path=None, warnings=[])
_g = parse(_scan)
assert _g['dateISO'] == '2023-01-22', _g['dateISO']
assert any('page-image OCR' in w for w in _g['warnings'])
assert [f['page'] for f in _g['flyers']] == [1, 2], _g['flyers']
assert not any('no community section' in w or 'no order-of-worship' in w
               or 'no Prayer Journal' in w for w in _g['warnings']), \
    'scan-inherent absences are not review items'
_mem = Extracted(pages=[
    Page(number=1, width=612, height=792, lines=[],
         ocr_text='Taylor White\nJuly 21, 1944 – November 29, 2025\n'
                  'Celebration of Life\nSaturday, December 13, 2025'),
], cover_path=None, warnings=[])
_g = parse(_mem)
assert _g['dateISO'] == '2025-12-13', _g['dateISO']

# poster residue on mixed pages: the lowercase/date fragments between the
# all-caps display blocks ("LEISURE WORLD ... presents ... SUNDAY 3:00 PM")
from wgconvert.parse import poster_residue  # noqa: E402
assert poster_residue(['presents'])
assert poster_residue(['SUNDAY', 'APRIL 27 AT 3:00 PM'])
assert poster_residue(['Ken Aiso Valeria Morgovskaya'])
assert not poster_residue(['Bring a friend to the concert next Sunday.'])
assert not poster_residue(['Special music: John Fluker'])
assert not poster_residue(['If you wish to attend, but are unable to pay '
                           'for a ticket, contact the church office.'])
assert not poster_residue(['presents'] * 4)

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
    gt = [o for o in g2['order'] if o.get('label') == 'The Great Thanksgiving']
    assert len(gt) == 1 and gt[0]['body'], 'eucharistic prayer labeled, not flagged'
    assert g2['flyers'] == [], 'no music or content page adopted as a flyer'
    assert not any(o['kind'] == 'item' and o['label'] is None for o in g2['order'])
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
    assert g2['warnings'] == [], g2['warnings']

    print('all tests passed')
finally:
    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.rmtree(work_dir + '-2', ignore_errors=True)

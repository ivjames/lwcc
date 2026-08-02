"""Fixed liturgical texts that appear in the PDF only as sheet-music
engraving (which the extractor deliberately drops). When an order-of-worship
item has one of these titles and no body of its own, the text is filled in
from here."""

KNOWN_TEXTS = {
    'Doxology':
        "Praise God from whom all blessings flow; praise God, all creatures here below; "
        "praise God above, ye Heav'nly host: praise Father, Son, and Holy Ghost. Amen.",
    'Gloria Patri':
        'Glory be to the Father, and to the Son, and to the Holy Ghost; as it was in the '
        'beginning, is now, and ever shall be, world without end. Amen. Amen.',
}

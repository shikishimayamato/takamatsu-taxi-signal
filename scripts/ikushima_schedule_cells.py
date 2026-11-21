"""Read individual event names from the Ikushima annual schedule."""

import re

COURT_COUNTS = re.compile(r'[①-⑳㉑-㉟㊱-㊿]+')


def subcolumn_entries(page, table, row_index, field, sport, normalize, *, tennis=False):
    """Read each half of the combined venue cell independently."""
    box = table.rows[row_index].cells[field]
    if box is None:
        return
    left, top, right, bottom = box
    middle = (left + right) / 2
    for start, end in ((left, middle), (middle, right)):
        text = page.get_text('text', clip=(start, top, end, bottom), sort=True)
        yield from cell_entries(text, sport, normalize, tennis=tennis)


def cell_entries(text, sport, normalize, *, tennis=False):
    if tennis:
        for chunk in COURT_COUNTS.split(text or ''):
            yield from cell_entries(chunk, sport, normalize)
        return
    pending = ''
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if pending and sport.search(normalize(line)):
            yield pending
            pending = line
        else:
            pending = f'{pending} {line}'.strip()
    if pending:
        yield pending

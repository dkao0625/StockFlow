"""
Safe CSV downloads.

Spreadsheet apps treat a cell that starts with = + - @ (or a tab / carriage
return) as a FORMULA. If a product name or note were "=HYPERLINK(...)", the
spreadsheet could run it when someone opens the export ("CSV injection").
safe_cell() puts a ' in front of such text so it is shown as plain text.

Only pass columns that are safe to share: never password hashes, secret
keys, or session data.
"""

import csv
import io
import re
from datetime import date, datetime

from flask import Response

FORMULA_CHARACTERS = ("=", "+", "-", "@", "\t", "\r")
MAX_EXPORT_ROWS = 50_000


def safe_cell(value):
    """Turn one value into safe CSV text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return value  # real numbers stay numbers (so -3 is still -3)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    if text.startswith(FORMULA_CHARACTERS):
        return "'" + text
    return text


def safe_filename(name):
    """Letters, numbers, dashes, underscores, and dots only."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.")
    return cleaned or "export"


def csv_response(filename, header, rows):
    """A downloadable CSV file. `rows` is any iterable of lists/tuples."""
    buffer = io.StringIO()
    buffer.write("﻿")  # byte-order mark so Excel reads UTF-8 correctly
    writer = csv.writer(buffer)
    writer.writerow([safe_cell(h) for h in header])
    for row in rows:
        writer.writerow([safe_cell(value) for value in row])
    filename = safe_filename(f"{filename}-{date.today().isoformat()}") + ".csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )

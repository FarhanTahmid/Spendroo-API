"""
Pages of an account's transactions by keyset: each page asks for the rows
after the last one seen, so a long history costs the same per page and a row
added meanwhile never shifts the rest.

DRF's CursorPagination positions on the first ordering field alone and falls
back to offsets among equal values; most rows here share their date.
"""
import base64
import binascii
import uuid
from datetime import date, datetime

from django.db.models import Q
from rest_framework.exceptions import ParseError

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# Newest first; the id settles rows of one day made in the same instant (the two legs of a transfer)
ORDERING = ('-occurred_on', '-created_at', '-id')


def encode_cursor(row):
    key = f'{row.occurred_on.isoformat()}|{row.created_at.isoformat()}|{row.id}'
    return base64.urlsafe_b64encode(key.encode()).decode()


def decode_cursor(cursor):
    try:
        day, created_at, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().split('|')
        return date.fromisoformat(day), datetime.fromisoformat(created_at), uuid.UUID(row_id)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ParseError('cursor is not one this endpoint gave out.')


def parse_limit(params):
    try:
        limit = int(params.get('limit', DEFAULT_LIMIT))
    except ValueError:
        raise ParseError('limit must be an integer.')
    if limit < 1:
        raise ParseError('limit must be positive.')
    return min(limit, MAX_LIMIT)


def page(queryset, cursor, limit):
    """Returns (rows, next_cursor); next_cursor is None on the last page."""
    queryset = queryset.order_by(*ORDERING)
    if cursor:
        day, created_at, row_id = decode_cursor(cursor)
        # The first condition lets the index find the place; the second is the exact position
        queryset = queryset.filter(occurred_on__lte=day).filter(
            Q(occurred_on__lt=day)
            | Q(occurred_on=day, created_at__lt=created_at)
            | Q(occurred_on=day, created_at=created_at, id__lt=row_id)
        )
    rows = list(queryset[:limit + 1])
    if len(rows) <= limit:
        return rows, None
    rows = rows[:limit]
    return rows, encode_cursor(rows[-1])

"""
A business's edit history for its owner: who added, changed, removed or
brought back what, and when. Read from the history that database triggers
keep for each tracked table (django-pghistory), so every change is there
however it was made: the API, a sync push, or a bulk update.

Each event is a full copy of the row after the change. What changed is
found by comparing an event with the one before it for the same row.

Pages are keyset, newest first, on (when, kind, event id), so a change made
while paging never shifts the rest. The tables are read separately and
merged, because each keeps its own event ids.
"""
import base64
import binascii
import heapq
import uuid
from itertools import islice
from dataclasses import dataclass, field
from datetime import datetime

from django.contrib.auth import get_user_model
from django.db.models import OuterRef, Subquery
from rest_framework.exceptions import ParseError

from cashbook.models import CashBook, CashBookAdditionalMember, Entry, EntryCategory, EntryField, PaymentMethod
from workspaces.models import Membership

DEFAULT_LIMIT = 30
MAX_LIMIT = 100

CREATED, UPDATED, DELETED, RESTORED = 'created', 'updated', 'deleted', 'restored'


@dataclass(frozen=True)
class Source:
    kind: str
    model: type
    # Fields an owner would call a change. version, updated_at and server_seq
    # move on every save and are never one.
    fields: tuple
    # The column naming the cashbook; None when the row is not in one book
    cashbook_column: str | None = 'cashbook_id'
    # Rows made for a new book without anyone choosing them
    seeded_column: str | None = None
    # Foreign keys shown by name: field -> what kind of name
    names: dict = field(default_factory=dict)

    @property
    def events(self):
        return self.model.pgh_event_model.objects


SOURCES = (
    Source(
        'entry', Entry,
        (
            'entry_type', 'amount_minor', 'entry_date', 'title', 'remarks', 'category_id', 'payment_method_id',
            'custom_values',
        ),
        names={'category_id': 'category', 'payment_method_id': 'payment_method'},
    ),
    Source('cashbook', CashBook, ('book_name', 'description', 'currency'), cashbook_column='pgh_obj_id'),
    Source('category', EntryCategory, ('category_name', 'color'), seeded_column='is_default'),
    Source('payment_method', PaymentMethod, ('payment_method_name',), seeded_column='is_default'),
    # Moving a field up or down is not a change anyone would look for
    Source('field', EntryField, ('name', 'options', 'required', 'show_in_list')),
    Source('access', CashBookAdditionalMember, ('role',)),
    Source('member', Membership, ('role',), cashbook_column=None),
)
KINDS = {source.kind: source for source in SOURCES}


@dataclass(frozen=True)
class Narrow:
    """What the owner narrowed the history to. Every part is optional."""
    cashbook: object = None
    person: object = None
    entry: object = None
    since: object = None  # an aware datetime, inclusive
    until: object = None  # an aware datetime, exclusive


def encode_cursor(at, kind, pgh_id):
    return base64.urlsafe_b64encode(f'{at.isoformat()}|{kind}|{pgh_id}'.encode()).decode()


def decode_cursor(cursor):
    try:
        at, kind, pgh_id = base64.urlsafe_b64decode(cursor.encode()).decode().split('|')
        if kind not in KINDS:
            raise ValueError(kind)
        return datetime.fromisoformat(at), kind, int(pgh_id)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ParseError('cursor is not one this endpoint gave out.')


def _sources(narrow):
    if narrow.entry is not None:
        return [KINDS['entry']]
    if narrow.cashbook is not None:
        return [source for source in SOURCES if source.cashbook_column]
    return list(SOURCES)


def _events(source, workspace, narrow, after):
    events = source.events.filter(workspace_id=workspace.id)
    if narrow.entry is not None:
        events = events.filter(pgh_obj_id=narrow.entry)
    if narrow.cashbook is not None:
        events = events.filter(**{source.cashbook_column: narrow.cashbook})
    if narrow.person is not None:
        events = events.filter(pgh_context__metadata__user=str(narrow.person))
    if narrow.since is not None:
        events = events.filter(pgh_created_at__gte=narrow.since)
    if narrow.until is not None:
        events = events.filter(pgh_created_at__lt=narrow.until)
    if after is not None:
        at, kind, pgh_id = after
        # Kinds at the same instant come in a fixed order: later names first,
        # as heapq.merge(reverse=True) gives them in changes()
        if source.kind < kind:
            events = events.filter(pgh_created_at__lte=at)
        elif source.kind > kind:
            events = events.filter(pgh_created_at__lt=at)
        else:
            events = events.filter(pgh_created_at__lte=at).exclude(pgh_created_at=at, pgh_id__gte=pgh_id)
    before = source.events.filter(pgh_obj_id=OuterRef('pgh_obj_id'), pgh_id__lt=OuterRef('pgh_id'))
    return (
        events.select_related('pgh_context')
        .annotate(previous_id=Subquery(before.order_by('-pgh_id').values('pgh_id')[:1]))
        .order_by('-pgh_created_at', '-pgh_id')
    )


def _what_happened(source, event, previous):
    """(action, [(field, before, after)]), or None when nothing an owner would see changed."""
    if previous is None:
        if source.seeded_column and getattr(event, source.seeded_column):
            return None
        return CREATED, []
    if previous.deleted_at is None and event.deleted_at is not None:
        return DELETED, []
    if previous.deleted_at is not None and event.deleted_at is None:
        return RESTORED, []
    if event.deleted_at is not None:
        return None
    changed = [
        (name, getattr(previous, name), getattr(event, name))
        for name in source.fields if getattr(previous, name) != getattr(event, name)
    ]
    return (UPDATED, changed) if changed else None


def changes(workspace, narrow=Narrow(), cursor=None, limit=DEFAULT_LIMIT):
    """
    Returns (rows, next_cursor), newest first; next_cursor is None on the
    last page. Changes nobody would notice (a version bump, a book's
    ready-made categories) are left out, so a page may cover more events
    than it shows.
    """
    after = decode_cursor(cursor) if cursor else None
    found, last = [], None
    # Read in batches until the page is full or the history runs out
    batch = limit + 1
    while len(found) <= limit:
        # Each table comes newest first; merged, the newest of them all leads
        merged = heapq.merge(
            *(_tagged(source, _events(source, workspace, narrow, after)[:batch]) for source in _sources(narrow)),
            key=lambda pair: (pair[1].pgh_created_at, pair[0].kind, pair[1].pgh_id), reverse=True,
        )
        events = list(islice(merged, batch))
        if not events:
            break
        previous = _previous(events)
        for source, event in events:
            last = (event.pgh_created_at, source.kind, event.pgh_id)
            happened = _what_happened(source, event, previous.get((source.kind, event.previous_id)))
            if happened is not None:
                found.append((source, event, *happened, last))
                if len(found) > limit:
                    break
        if len(events) < batch:
            break
        after = last
        batch = min(batch * 2, MAX_LIMIT * 4)
    more = len(found) > limit
    found = found[:limit]
    next_cursor = encode_cursor(*found[-1][4]) if more else None
    return _rows(workspace, found), next_cursor


def _tagged(source, events):
    return ((source, event) for event in events)


def _previous(events):
    wanted = {}
    for source, event in events:
        if event.previous_id is not None:
            wanted.setdefault(source.kind, set()).add(event.previous_id)
    return {
        (kind, row.pgh_id): row
        for kind, ids in wanted.items()
        for row in KINDS[kind].events.filter(pgh_id__in=ids)
    }


def _rows(workspace, found):
    names = _Names(workspace, found)
    rows = []
    for source, event, action, changed, position in found:
        user_id = event.pgh_context.metadata.get('user') if event.pgh_context_id else None
        row = {
            'id': encode_cursor(*position),
            'at': event.pgh_created_at,
            'kind': source.kind,
            'action': action,
            'object_id': event.pgh_obj_id,
            'cashbook': names.cashbook(_cashbook_of(source, event)),
            'actor': names.person(user_id),
            'subject': names.subject(source, event),
            'changes': [change for name, old, new in changed for change in names.changes(source, name, old, new)],
        }
        if source.kind == 'entry':
            row.update(amount_minor=event.amount_minor, currency=event.currency, entry_type=event.entry_type)
        if source.kind in ('access', 'member'):
            row['role'] = event.role
        rows.append(row)
    return rows


def _changed_values(old, new):
    old, new = old or {}, new or {}
    return [key for key in {**old, **new} if old.get(key) != new.get(key)]


def _field_ids(keys):
    ids = []
    for key in keys:
        try:
            ids.append(uuid.UUID(key))
        except ValueError:
            pass
    return ids


def _cashbook_of(source, event):
    return getattr(event, source.cashbook_column) if source.cashbook_column else None


def _person_of(source, event):
    return {'access': 'member_id', 'member': 'user_id'}.get(source.kind)


class _Names:
    """Everything a page names, looked up at once, deleted rows and former members included."""

    def __init__(self, workspace, found):
        books, categories, methods, people, fields = set(), set(), set(), set(), set()
        for source, event, _, changed, _ in found:
            if source.cashbook_column:
                books.add(getattr(event, source.cashbook_column))
            if event.pgh_context_id and event.pgh_context.metadata.get('user'):
                people.add(event.pgh_context.metadata['user'])
            if column := _person_of(source, event):
                people.add(getattr(event, column))
            for name, old, new in changed:
                if name == 'custom_values':
                    fields.update(_changed_values(old, new))
                    continue
                target = {'category_id': categories, 'payment_method_id': methods}.get(name)
                if target is not None:
                    target.update(value for value in (old, new) if value)
        self.books = dict(
            CashBook.all_objects.filter(workspace=workspace, id__in=books).values_list('id', 'book_name'),
        )
        self.categories = dict(
            EntryCategory.all_objects.filter(workspace=workspace, id__in=categories).values_list('id', 'category_name'),
        )
        self.methods = dict(
            PaymentMethod.all_objects.filter(workspace=workspace, id__in=methods)
            .values_list('id', 'payment_method_name'),
        )
        self.fields = {
            str(row.id): row
            for row in EntryField.all_objects.filter(workspace=workspace, id__in=_field_ids(fields))
        }
        self.people = {
            str(user.pk): user
            for user in get_user_model().objects.filter(pk__in=[p for p in people if p])
        }

    def cashbook(self, book_id):
        if book_id is None:
            return None
        return {'id': book_id, 'name': self.books.get(book_id, '')}

    def person(self, user_id):
        user = self.people.get(str(user_id)) if user_id else None
        if user is None:
            return None
        return {'id': user.pk, 'name': user.full_name, 'email': user.email}

    def subject(self, source, event):
        if source.kind == 'entry':
            return event.title or ''
        if source.kind == 'cashbook':
            return event.book_name
        if source.kind == 'category':
            return event.category_name
        if source.kind == 'payment_method':
            return event.payment_method_name
        if source.kind == 'field':
            return event.name
        person = self.person(getattr(event, _person_of(source, event)))
        return (person['name'] or person['email']) if person else ''

    def changes(self, source, name, old, new):
        """
        A change as the app shows it. A book's own fields are one change each,
        named and in the book's order: "Invoice no: 1041 → 1042".
        """
        if name != 'custom_values':
            return [{
                'field': name.removesuffix('_id'),
                'before': self.value(source, name, old), 'after': self.value(source, name, new),
            }]
        old, new = old or {}, new or {}
        found = [(self.fields.get(key), key) for key in _changed_values(old, new)]
        # A value whose field never existed in this workspace is nobody's change
        found = sorted(((field, key) for field, key in found if field), key=lambda pair: (pair[0].position, pair[0].created_at))
        return [
            {'field': 'custom_value', 'name': field.name, 'kind': field.kind, 'before': old.get(key), 'after': new.get(key)}
            for field, key in found
        ]

    def value(self, source, name, value):
        if value is None:
            return None
        if name == 'category_id':
            return self.categories.get(value, '')
        if name == 'payment_method_id':
            return self.methods.get(value, '')
        if hasattr(value, 'isoformat'):
            return value.isoformat()
        return value

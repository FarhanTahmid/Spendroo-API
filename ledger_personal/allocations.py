"""
How a transaction is filed: under one category, or split into parts that each
have their own. Everything that adds up or picks by category reads it here, so
a split is counted the same way in a list, a total and a report. The app does
the same from its own copy of the data (lib/features/personal/allocations.dart).

A split is stored on the transaction, in `splits`: a list of
{"category_id": <id or null>, "amount_minor": <positive>, "note": <text>}.
The parts add up to the amount; one with no category is money not yet filed.
"""
import uuid

from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import Category

MIN_PARTS = 2
MAX_PARTS = 20
MAX_PART_NOTE = 100
#: The kinds of transaction that have a category at all
PLAIN_KINDS = ('expense', 'income')


def cleaned(splits, kind, amount_minor):
    """The parts as they are stored, or ValidationError saying what is wrong with them."""
    if splits in (None, []):
        return []
    if not isinstance(splits, list):
        raise ValidationError({'splits': 'A split is a list of parts.'})
    if kind not in PLAIN_KINDS:
        raise ValidationError({'splits': 'A transfer cannot be split.'})
    if not MIN_PARTS <= len(splits) <= MAX_PARTS:
        raise ValidationError({'splits': f'A split has {MIN_PARTS} to {MAX_PARTS} parts.'})
    parts = []
    for part in splits:
        if not isinstance(part, dict):
            raise ValidationError({'splits': 'Each part is an object.'})
        amount, category_id, note = part.get('amount_minor'), part.get('category_id'), part.get('note') or ''
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValidationError({'splits': 'Each part needs an amount above zero.'})
        if category_id is not None:
            try:
                category_id = str(uuid.UUID(str(category_id)))
            except ValueError:
                raise ValidationError({'splits': 'A part names its category by id.'})
        if not isinstance(note, str) or len(note) > MAX_PART_NOTE:
            raise ValidationError({'splits': f'A part note is text of up to {MAX_PART_NOTE} characters.'})
        parts.append({'category_id': category_id, 'amount_minor': amount, 'note': note})
    if sum(part['amount_minor'] for part in parts) != abs(amount_minor or 0):
        raise ValidationError({'splits': 'The parts must add up to the amount.'})
    return parts


def check_categories(workspace, splits):
    """Every category a split names must be one of the workspace's own."""
    wanted = {part['category_id'] for part in splits if part['category_id']}
    if not wanted:
        return
    found = Category.objects.filter(workspace=workspace, id__in=wanted).count()
    if found != len(wanted):
        raise ValidationError({'splits': 'A part names a category that does not exist in this workspace.'})


def parts(amount_minor, category_id, splits):
    """(category id or None, amount with the transaction's sign, note) for each part; one for an unsplit row."""
    if not splits:
        return [(category_id, amount_minor, '')]
    sign = -1 if amount_minor < 0 else 1
    return [
        (uuid.UUID(part['category_id']) if part['category_id'] else None, sign * part['amount_minor'], part['note'])
        for part in splits
    ]


def kept(found, category_ids, uncategorised, alive=None):
    """
    Of `found` (from parts()), those a choice of categories keeps: all of them
    when nothing is chosen. `alive` holds the ids of categories that still
    exist; a part filed under one that was deleted reads as uncategorised.
    """
    if not category_ids and not uncategorised:
        return found
    chosen = set(category_ids)
    return [
        part for part in found
        if part[0] in chosen or (uncategorised and (part[0] is None or (alive is not None and part[0] not in alive)))
    ]


def condition(category_ids, uncategorised, type_field='kind'):
    """The transactions with at least one part a choice of categories keeps."""
    either = Q()
    if category_ids:
        either |= Q(category_id__in=category_ids)
        for category_id in category_ids:
            either |= Q(splits__contains=[{'category_id': str(category_id)}])
    if uncategorised:
        # A transfer never has a category; "uncategorised" means income and expenses
        unfiled = Q(category__isnull=True) | Q(category__deleted_at__isnull=False)
        either |= (
            Q(splits=[]) & unfiled & Q(**{f'{type_field}__in': PLAIN_KINDS})
        ) | Q(splits__contains=[{'category_id': None}])
    return either


def split_only(queryset):
    return queryset.exclude(splits=[])


def unsplit_only(queryset):
    return queryset.filter(splits=[])


def categories_of(workspace_id):
    """id -> (name, colour) of the categories that still exist, for naming parts."""
    return {
        row['id']: (row['name'], row['color'])
        for row in Category.objects.filter(workspace_id=workspace_id).values('id', 'name', 'color')
    }

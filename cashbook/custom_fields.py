"""
Values of a book's own fields (EntryField), kept on each entry as
Entry.custom_values: {field id: value as text}.

Every value is text, so phones and reports read it the same way: a number
in plain ASCII ('-1200.5'), a date as 'YYYY-MM-DD', a choice as one of the
field's options. A blank value is no value: its key is dropped.

Only what an edit changes is checked. A value kept from before still passes
after its field was deleted or its option removed, so an old entry never
becomes impossible to edit. Whether a field is required is the app's to ask:
a field made required later must not refuse edits of entries made before.
"""
import re
from datetime import date
from uuid import UUID

from django.core.exceptions import ValidationError

from .models import EntryField

MAX_FIELDS = 20
MAX_TEXT = 500
NUMBER = re.compile(r'-?(0|[1-9]\d{0,14})(\.\d{0,5}[1-9])?')


def cleaned_shape(values):
    """The map with blank values dropped. Refuses anything but {text: text}."""
    if values in (None, ''):
        return {}
    if not isinstance(values, dict):
        raise ValidationError({'custom_values': 'Field values are a map of field id to text.'})
    cleaned = {}
    for key, value in values.items():
        if not isinstance(key, str) or not (value is None or isinstance(value, str)):
            raise ValidationError({'custom_values': 'Field values are a map of field id to text.'})
        if value is not None and value.strip():
            cleaned[key] = value
    if len(cleaned) > MAX_FIELDS:
        raise ValidationError({'custom_values': f'An entry holds at most {MAX_FIELDS} field values.'})
    return cleaned


def changed_keys(old, new):
    """Keys whose value is new or different. A removed key is never checked."""
    old = old or {}
    return [key for key, value in (new or {}).items() if old.get(key) != value]


def values_changed(old, new):
    return (old or {}) != (new or {})


def problem(field, value):
    """Why the value does not fit the field, or None."""
    if field.kind == EntryField.TEXT:
        if len(value) > MAX_TEXT:
            return f'{field.name} is at most {MAX_TEXT} characters.'
        if '\n' in value or '\r' in value:
            return f'{field.name} is a single line.'
    elif field.kind == EntryField.NUMBER:
        if not NUMBER.fullmatch(value):
            return f'{field.name} needs a number.'
    elif field.kind == EntryField.DATE:
        try:
            date.fromisoformat(value)
        except ValueError:
            return f'{field.name} needs a date.'
        if len(value) != 10:
            return f'{field.name} needs a date.'
    elif field.kind == EntryField.CHOICE:
        if value not in field.options:
            return f'{field.name} has no option "{value}".'
    return None


def _live_fields(cashbook, keys):
    return {str(field.id): field for field in EntryField.objects.filter(cashbook=cashbook, id__in=_ids(keys))}


def _ids(keys):
    """Keys that can be field ids at all, so a stray key never breaks the lookup."""
    ids = []
    for key in keys:
        try:
            ids.append(UUID(key))
        except ValueError:
            pass
    return ids


def check_values(cashbook, new, old):
    """Refuses a changed value that is not a live field of the book, or does not fit its field."""
    keys = changed_keys(old, new)
    if not keys:
        return
    fields = _live_fields(cashbook, keys)
    for key in keys:
        field = fields.get(key)
        if field is None:
            raise ValidationError({'custom_values': 'A value is for a field this book does not have.'})
        reason = problem(field, new[key])
        if reason:
            raise ValidationError({'custom_values': reason})


def drop_invalid(cashbook, values):
    """
    For a new entry: keeps only values that fit a live field of the book.
    A phone may have filled a field that was deleted while it was offline;
    the entry still counts, so the value is what goes.
    """
    if not values:
        return {}
    fields = _live_fields(cashbook, values.keys())
    return {
        key: value for key, value in values.items()
        if key in fields and problem(fields[key], value) is None
    }

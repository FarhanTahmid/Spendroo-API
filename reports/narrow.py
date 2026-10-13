"""
What a custom report is narrowed to, beside its dates: kinds of line and
categories. Asked for with ?type=expense,income&category=<id>,<id>,none.

A narrowed report leaves lines out, so it cannot say what was held before or
after the period: it shows money in, money out and their difference instead of
balances, and says at the top what it was narrowed to.
"""
import uuid
from dataclasses import dataclass, replace

from django.db.models import Q
from rest_framework.exceptions import ParseError

from ledger_personal import allocations

LEDGER_TYPES = ('income', 'expense', 'transfer')
CASHBOOK_TYPES = ('cash_in', 'cash_out')
UNCATEGORISED = 'none'
MAX_CATEGORIES = 200


@dataclass(frozen=True)
class Narrow:
    types: tuple = ()
    category_ids: tuple = ()
    #: Lines filed under no category, alongside category_ids
    uncategorised: bool = False
    #: The chosen categories by name, for saying what the report shows
    category_names: tuple = ()

    @property
    def by_category(self):
        return bool(self.category_ids or self.uncategorised)

    @property
    def active(self):
        return bool(self.types or self.by_category)

    def shows(self, kind):
        return not self.types or kind in self.types


EVERYTHING = Narrow()


def _tokens(params, name):
    return [token for value in params.getlist(name) for token in (part.strip() for part in value.split(',')) if token]


def parse(params, allowed_types):
    types = []
    for token in _tokens(params, 'type'):
        if token not in allowed_types:
            raise ParseError(f"type must be among: {', '.join(allowed_types)}.")
        if token not in types:
            types.append(token)
    # Every kind chosen is no choice at all; fewer are said in one order however they were asked for
    types = [] if len(types) == len(allowed_types) else [kind for kind in allowed_types if kind in types]

    category_ids, uncategorised = [], False
    for token in _tokens(params, 'category'):
        if token == UNCATEGORISED:
            uncategorised = True
            continue
        try:
            category_ids.append(uuid.UUID(token))
        except ValueError:
            raise ParseError(f"category must be ids or '{UNCATEGORISED}'.")
    if len(category_ids) > MAX_CATEGORIES:
        raise ParseError(f'category takes up to {MAX_CATEGORIES} ids.')
    return Narrow(tuple(types), tuple(dict.fromkeys(category_ids)), uncategorised)


def named(narrow, categories, name_field):
    """The same choice with its categories looked up in `categories`, the ones it may name."""
    if not narrow.category_ids:
        return narrow
    found = dict(categories.filter(id__in=narrow.category_ids).values_list('id', name_field))
    if len(found) != len(narrow.category_ids):
        raise ParseError('category names a category this report does not have.')
    # A business has one "Sales" per cashbook: say it once
    return replace(narrow, category_names=tuple(dict.fromkeys(found[category_id] for category_id in narrow.category_ids)))


def lines(queryset, narrow, type_field, plain_types=None, splits=False):
    """
    The rows of `queryset` the choice keeps. `plain_types` are the kinds that
    can have a category at all: "uncategorised" means those with none. With
    `splits` the rows are personal transactions, kept when any of their parts is.
    """
    if narrow.types:
        queryset = queryset.filter(**{f'{type_field}__in': narrow.types})
    if narrow.by_category and splits:
        queryset = queryset.filter(allocations.condition(narrow.category_ids, narrow.uncategorised, type_field))
    elif narrow.by_category:
        either = Q()
        if narrow.category_ids:
            either |= Q(category_id__in=narrow.category_ids)
        if narrow.uncategorised:
            without = Q(category__isnull=True) | Q(category__deleted_at__isnull=False)
            if plain_types is not None:
                without &= Q(**{f'{type_field}__in': plain_types})
            either |= without
        queryset = queryset.filter(either)
    return queryset


def describe(narrow, words):
    """(label, what was chosen) for each part of the choice in force."""
    said = []
    if narrow.types:
        said.append((words.t('filter.types'), ', '.join(words.t(f'type.{kind}') for kind in narrow.types)))
    if narrow.by_category:
        names = [*narrow.category_names, *([words.t('row.uncategorised')] if narrow.uncategorised else [])]
        said.append((words.t('filter.categories'), ', '.join(names)))
    return said

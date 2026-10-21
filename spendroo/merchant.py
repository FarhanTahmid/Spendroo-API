"""
Who was paid, or who paid: the merchant (personal) or party (cashbook) on a row.

`tidy_merchant` is what is stored and shown. `normalise_merchant` is the copy
kept beside it for matching (categorisation, subscriptions, capture), so
"Foodpanda", " FOODPANDA " and "foodpanda." all key the same.
"""
import re
import unicodedata

MAX_LENGTH = 100

_SPACES = re.compile(r'\s+')


def tidy_merchant(value):
    """Trimmed, with runs of whitespace collapsed to one space."""
    return _SPACES.sub(' ', value or '').strip()[:MAX_LENGTH]


def normalise_merchant(value):
    """Case-folded, punctuation dropped (except '&', as in "H&M"), spacing collapsed."""
    text = unicodedata.normalize('NFKC', value or '').casefold()
    kept = ''.join(
        char if char == '&' or not unicodedata.category(char).startswith(('P', 'S')) else ' '
        for char in text
    )
    return _SPACES.sub(' ', kept).strip()[:MAX_LENGTH]


def set_merchant(row, save_kwargs):
    """
    For a model's save(): tidies the row's merchant and keeps its key in step,
    also when only some fields are saved.
    """
    row.merchant = tidy_merchant(row.merchant)
    row.merchant_key = normalise_merchant(row.merchant)
    update_fields = save_kwargs.get('update_fields')
    if update_fields is not None and 'merchant' in update_fields:
        save_kwargs['update_fields'] = set(update_fields) | {'merchant_key'}

"""
The `tag_ids` column of transactions and entries: a list of the workspace's
tag ids, as text.
"""
from uuid import UUID

from django.core.exceptions import ValidationError

from .models import Tag

#: At most this many tags on one row
MAX_TAGS = 10


def clean_tag_ids(workspace, value):
    """
    The ids kept, in the order sent, once each. Ids of tags that are not live
    tags of the workspace are dropped: one may have been deleted while the
    phone was offline, and that must not stop the row from syncing.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError({'tag_ids': 'Tags are sent as a list of ids.'})
    wanted = []
    for item in value:
        try:
            tag_id = str(UUID(str(item)))
        except ValueError:
            raise ValidationError({'tag_ids': 'A tag id is not an id.'})
        if tag_id not in wanted:
            wanted.append(tag_id)
    if len(wanted) > MAX_TAGS:
        raise ValidationError({'tag_ids': f'A row carries at most {MAX_TAGS} tags.'})
    live = {str(tag_id) for tag_id in Tag.objects.filter(workspace=workspace, id__in=wanted).values_list('id', flat=True)}
    return [tag_id for tag_id in wanted if tag_id in live]


def tag_ids_matching(workspace, text):
    """Ids of the workspace's live tags whose name contains `text`, for search."""
    return [str(tag_id) for tag_id in Tag.objects.filter(workspace=workspace, name__icontains=text).values_list('id', flat=True)]

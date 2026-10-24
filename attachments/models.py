from django.db import models

from workspaces.models import WorkspaceOwnedModel


class Attachment(WorkspaceOwnedModel):
    """
    What every attachment knows of its file. A subclass adds the row it hangs
    off, who added it and the FileField, and registers itself as a kind
    (kinds.py). Added and removed only through service.py; phones get the
    rows by sync (read-only) and each file through a signed link (storage.py).
    """
    # What the bytes proved the file to be (attachments/files.py), never what the phone said
    content_type = models.CharField(max_length=40, default='', editable=False)
    # Shown to people only; the stored file is named after the row's id
    original_name = models.CharField(max_length=120, blank=True, default='', editable=False)
    # Counted against the owner's attachment storage, and given back on removal
    size_bytes = models.PositiveBigIntegerField(default=0, editable=False)
    # Pages of a PDF; width and height of a photo, or of a PDF's first page in points
    page_count = models.PositiveIntegerField(null=True, blank=True, editable=False)
    width = models.PositiveIntegerField(null=True, blank=True, editable=False)
    height = models.PositiveIntegerField(null=True, blank=True, editable=False)

    class Meta:
        abstract = True
        ordering = ['created_at']

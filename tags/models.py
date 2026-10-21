from django.db import models

from workspaces.models import WorkspaceOwnedModel, hex_color


class Tag(WorkspaceOwnedModel):
    """
    A label of the workspace, put on personal transactions and cashbook
    entries alike. Rows hold the ids they carry in `tag_ids`
    (tags/cleaning.py), so a deleted tag is simply skipped where it is still
    named.

    Names are not unique: two phones may each make "food" offline, and
    refusing the second would quietly take the tag off its rows. The app
    reuses a tag of the same name when one is made.
    """
    MAX_NAME_LENGTH = 40

    name = models.CharField(max_length=MAX_NAME_LENGTH)
    # Chosen by the user; without one the app picks a colour itself
    color = models.CharField(max_length=7, null=True, blank=True, validators=[hex_color])

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

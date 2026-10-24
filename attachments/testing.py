"""Test helpers for anything that stores attachments: a storage of its own per test, and what it holds."""
import os
import shutil
import tempfile

from django.conf import settings
from django.test import override_settings

from billing.catalog.keys import F
from billing.models import UsageCounter


class AttachmentStorageMixin:
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        storages = {
            **settings.STORAGES,
            'bills': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': self.folder}},
        }
        override = override_settings(STORAGES=storages)
        override.enable()
        self.addCleanup(override.disable)

    def files(self):
        return sorted(
            os.path.relpath(os.path.join(root, name), self.folder).replace(os.sep, '/')
            for root, _, names in os.walk(self.folder) for name in names
        )

    def used(self, user):
        counter = UsageCounter.objects.filter(user=user, feature_key=str(F.STORAGE_ATTACHMENTS_MB)).first()
        return counter.used if counter else 0

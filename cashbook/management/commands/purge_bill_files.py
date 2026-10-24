from django.core.management.base import BaseCommand

from attachments.service import purge_files
from workspaces.tenancy import bypass_tenant_context


class Command(BaseCommand):
    help = 'Remove bill and receipt files no live row holds (removed ones, rolled-back uploads). Safe to run any time.'

    def handle(self, *args, **options):
        bypass_tenant_context()
        removed = purge_files()
        for name in removed:
            self.stdout.write(f'  removed {name}')
        self.stdout.write(self.style.SUCCESS(f'{len(removed)} file(s) removed.'))

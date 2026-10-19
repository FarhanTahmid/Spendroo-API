from django.core.management.base import BaseCommand

from workspaces.tenancy import bypass_tenant_context

from cashbook.bills import purge_files


class Command(BaseCommand):
    help = 'Remove bill files no live bill holds (removed bills, rolled-back uploads). Safe to run any time.'

    def handle(self, *args, **options):
        bypass_tenant_context()
        removed = purge_files()
        for name in removed:
            self.stdout.write(f'  removed {name}')
        self.stdout.write(self.style.SUCCESS(f'{len(removed)} file(s) removed.'))

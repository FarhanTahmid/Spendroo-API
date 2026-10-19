from procrastinate.contrib.django import app

from workspaces.tenancy import bypass_tenant_context

from . import bills


# Files normally go right after their bill is removed; this catches the ones that did not
@app.periodic(cron='30 3 * * *')
@app.task(queue='maintenance', queueing_lock='cashbook.purge_bill_files')
def purge_bill_files(timestamp):
    bypass_tenant_context()
    bills.purge_files()

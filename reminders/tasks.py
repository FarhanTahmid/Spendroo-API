from django.db import transaction
from procrastinate.contrib.django import app
from procrastinate.exceptions import AlreadyEnqueued

from notifications import delivery

from . import budgets, engine


# The timestamp is not used: a late run does the same work as a timely one.
# One run at a time: a slow pass is not joined by the next.
@app.periodic(cron='*/15 * * * *')
@app.task(queue='reminders', queueing_lock='reminders.run')
def run_reminders(timestamp):
    engine.run()
    # What there now is to say goes to the phones
    delivery.deliver()


@app.task(queue='reminders')
def evaluate_budgets(workspace_id):
    budgets.evaluate(workspace_id)
    delivery.deliver()


def budgets_changed(workspace_id):
    """
    Look at the workspace's budgets shortly, once however many changes arrive
    together: a sync pushes transactions by the dozen.
    """
    try:
        # On its own savepoint: finding the lock taken must not spoil a transaction this runs inside
        with transaction.atomic():
            evaluate_budgets.configure(
                queueing_lock=f'budgets:{workspace_id}', schedule_in={'seconds': 30},
            ).defer(workspace_id=str(workspace_id))
    except AlreadyEnqueued:
        pass  # already on its way

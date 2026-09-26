"""
Sample MCP Tool Action — reports order counts grouped by blueprint.
"""
from django.db.models import Count

from common.methods import set_progress
from orders.models import BlueprintOrderItem

status = "{{status}}"
top_n = "{{top_n}}"


def run(job, *args, **kwargs):
    qs = BlueprintOrderItem.objects.exclude(blueprint__isnull=True)
    # Test push from vscode plugin
    status_filter = status.strip().upper()
    if status_filter:
        # e.g. SUCCESS, ACTIVE, PENDING, DENIED, FAILURE, CANCELED
        qs = qs.filter(order__status=status_filter)

    counts = (
        qs.values("blueprint__id", "blueprint__name")
        .annotate(order_count=Count("order", distinct=True))
        .order_by("-order_count")
    )

    try:
        limit = int(top_n)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        counts = counts[:limit]

    rows = [
        f"  {c['blueprint__name']}: {c['order_count']}"
        for c in counts
    ]

    scope = f"status={status_filter}" if status_filter else "all statuses"
    if rows:
        message = "Order counts by blueprint ({}):\n{}".format(scope, "\n".join(rows))
    else:
        message = f"No orders found ({scope})."

    set_progress(message)
    return {"status": "SUCCESS", "output_message": message}
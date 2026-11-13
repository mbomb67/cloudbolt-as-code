"""
Refresh CloudBolt Engine Budgets

Recurring job body. For every group that has a CloudBolt Engine budget it
creates the budget if it is still waiting for its tags to appear in billing
data, then reads the budget's performance and the group's daily cost from
CloudBolt Engine and stores the snapshot (including the month-end trend) that
the group's Budgets tab shows. All Engine access is in the cloudbolt_engine
shared module.

One line per group goes to the job output. The job is SUCCESS when every group
refreshed, WARNING when some failed (the failures are listed as errors, the
rest still refreshed), and FAILURE when nothing could be read at all.
"""

from common.methods import set_progress
from shared_modules import cloudbolt_engine
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def run(job, **kwargs):
    try:
        results = cloudbolt_engine.refresh_all()
    except Exception as exc:
        logger.exception("Refreshing CloudBolt Engine budgets failed")
        return "FAILURE", "", "Could not read the group budgets: {}.".format(type(exc).__name__)

    if not results:
        return "SUCCESS", "No groups have a CloudBolt Engine budget.", ""

    lines = []
    failures = []
    for result in results:
        line = "{}: {}".format(result["group"], result["message"])
        lines.append(line)
        set_progress(line)
        if not result["ok"]:
            failures.append(line)

    output = "Refreshed {} of {} group budget(s).\n{}".format(
        len(results) - len(failures), len(results), "\n".join(lines)
    )
    if not failures:
        return "SUCCESS", output, ""
    status = "FAILURE" if len(failures) == len(results) else "WARNING"
    return status, output, "\n".join(failures)

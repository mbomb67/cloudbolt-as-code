"""
Run CIT Tests (MCP Tool Action plugin).

Creates the same functionaltest job that the play button on the CIT Tests
page creates (cscv.views._run_cit_tests) for the named tests, with the
caller as owner, so an agent can run integration tests on demand and poll
the job.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved. Output keys are camelCase so a
synchronous run and an asynchronous fetch_job read identically.
"""
import html
import re

from cscv.models import CITTest
from jobs.models import FunctionalTestParameters, Job
from utilities.models import GlobalPreferences


def _text(value):
    return html.unescape(value or "").strip()


def _split(value):
    return [part for part in re.split(r"[\n,]+", _text(value)) if part.strip()]


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


def _read_inputs():
    raw = {"cit_test_refs": """{{ cit_test_refs }}"""}
    return {"refs": [ref.strip() for ref in _split(raw["cit_test_refs"])]}


def _find_test(ref):
    return (
        CITTest.objects.filter(global_id=ref).first()
        or CITTest.objects.filter(name__iexact=ref).first()
    )


def run(job, *args, **kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    if profile is None:
        return _failure(
            "No calling user profile was supplied. Run this tool through the MCP "
            "server or the API as an authenticated user."
        )
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may run CIT tests.")

    inputs = _read_inputs()
    if not inputs["refs"]:
        return _failure("cit_test_refs is required: CIT-... global ids or exact names, one per line.")

    tests = []
    not_found = []
    for ref in inputs["refs"]:
        test = _find_test(ref)
        if test is None:
            not_found.append(ref)
        elif test not in tests:
            tests.append(test)
    if not tests:
        names = list(CITTest.objects.order_by("name").values_list("name", flat=True)[:30])
        return _failure(
            "No CIT test matches {}. Known names include: {}.".format(
                ", ".join(not_found), ", ".join(names) or "none"
            )
        )

    params = FunctionalTestParameters.objects.create()
    try:
        params.failure_email_address = GlobalPreferences.get().cbadmin_email
    except Exception:  # no admin email configured; the job still runs
        pass
    params.cittests.set([test.id for test in tests])
    params.save()

    if len(tests) == 1:
        label = 'CIT Test "{}"'.format(tests[0].name)
    else:
        label = "Run {} Tests".format(len(tests))
    test_job = Job.objects.create(
        type="functionaltest", job_parameters=params, owner=profile, label=label
    )
    outputs = {
        "jobId": test_job.global_id,
        "tests": [{"id": test.global_id, "name": test.name} for test in tests],
        "notFound": not_found,
        "next": "Poll fetch_job_log with jobId; each test's pass or fail is in the progress log.",
    }
    message = "Created CIT job {} for {} test(s).".format(test_job.global_id, len(tests))
    return {
        "status": "SUCCESS",
        "output_message": message,
        "error_message": "",
        "outputs": outputs,
    }

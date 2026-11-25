"""
POC Onboarding: Remove Environment (MCP Tool Action plugin).

Deletes an environment that was imported by mistake (wrong region, wrong
cluster, a duplicate) so the POC can start over cleanly. Refuses when any
active server still lives in the environment. Synchronous. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

GLOBAL_ID_RE = re.compile(r"^[A-Z]{2,4}-[A-Za-z0-9]{6,16}$")


def _text(value):
    return html.unescape(value or "").strip()


def _failure(message):
    return {"status": "FAILURE", "output_message": "", "error_message": message, "outputs": {"error": message}}


def _success(message, outputs):
    return {"status": "SUCCESS", "output_message": message, "error_message": "", "outputs": outputs}


def _caller(job, kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    return profile


def _read_inputs():
    raw = {"onb_environment": """{{ onb_environment }}"""}
    return {"environment": _text(raw["onb_environment"])}


def _resolve_env(ref):
    from infrastructure.models import Environment

    env = None
    if GLOBAL_ID_RE.match(ref):
        env = Environment.objects.filter(global_id=ref).first()
    elif ref.isdigit():
        env = Environment.objects.filter(id=int(ref)).first()
    if env is None:
        matches = list(Environment.objects.filter(name__iexact=ref))
        if len(matches) > 1:
            return None, "Several environments are named '{}'; pass the ENV- global id.".format(ref)
        env = matches[0] if matches else None
    if env is None:
        return None, "No environment matches '{}'.".format(ref)
    return env, None


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may remove environments.")

    ref = _read_inputs()["environment"]
    if not ref:
        return _failure("onb_environment is required: the ENV- global id or the environment name.")
    env, error = _resolve_env(ref)
    if error:
        return _failure(error)

    active = env.server_set.exclude(status="HISTORICAL").count()
    if active:
        return _failure(
            "Environment '{}' still has {} active server(s); decommission them first (Delete server action), "
            "then remove the environment.".format(env.name, active)
        )
    if env.name.lower() == "unassigned":
        return _failure("The Unassigned environment is built in and cannot be removed.")

    info = {
        "environmentId": env.global_id,
        "name": env.name,
        "handler": env.resource_handler.name if env.resource_handler else None,
        "osBuildsDetached": env.os_builds.count(),
        "groupsDetached": env.groups_served.count(),
    }
    env.delete()
    return _success("Environment '{}' removed.".format(info["name"]), info)

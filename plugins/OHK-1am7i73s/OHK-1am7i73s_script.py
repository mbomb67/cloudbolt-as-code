"""
POC Onboarding: Playbook (MCP Tool Action plugin).

Returns the POC onboarding skill text (the SKILL.md and its reference
sections) from the public cloudbolt-as-code repository, so an MCP client
with no skill support (a desktop chat app, a web UI) receives the same
procedure the Claude Code skill follows. The appliance fetches the Markdown
over HTTPS through CloudBolt's proxy and SSL settings; when GitHub is not
reachable the response says so and gives the URL to open in a browser.
Read-only, synchronous. Any authenticated caller.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html
import re

PLAYBOOK_REPO = "mbomb67/cloudbolt-as-code"
PLAYBOOK_REF = "main"
PLAYBOOK_DIR = ".claude/skills/cb-poc-onboarding"
SECTIONS = {
    "index": ("SKILL.md", "The skill itself: hard rules, the phase loop, when to call which tool"),
    "bootstrap": ("references/bootstrap.md", "Phase 0: connect the MCP client, install the toolkit, first status"),
    "resource-handlers": ("references/resource-handlers.md", "Phase 1: per-technology handler creation, verification and diagnosis"),
    "phases": ("references/phases.md", "Phases 2 to 9: environments, first order, self-service, RBAC, lifecycle, extensibility, wrap-up"),
    "troubleshooting": ("references/troubleshooting.md", "Root-cause playbook: which tool for which failure, common causes and fixes"),
    "test-plan-map": ("references/test-plan-map.md", "Every POC test plan item with its phase, evidence, documentation link and whether it can be skipped"),
}
MAX_CHARS = 80000
TIMEOUT = 10


def _text(value):
    return html.unescape(value or "").strip()


def _failure(message):
    return {
        "status": "FAILURE",
        "output_message": "",
        "error_message": message,
        "outputs": {"error": message},
    }


def _success(message, outputs):
    return {"status": "SUCCESS", "output_message": message, "error_message": "", "outputs": outputs}


def _caller(job, kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    return profile


REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _read_inputs():
    raw = {
        "onb_section": """{{ onb_section }}""",
        "onb_ref": """{{ onb_ref }}""",
    }
    return {
        "section": (_text(raw["onb_section"]) or "index").lower(),
        "ref": _text(raw["onb_ref"]) or PLAYBOOK_REF,
    }


def _url(path, ref):
    return "https://raw.githubusercontent.com/{}/{}/{}/{}".format(PLAYBOOK_REPO, ref, PLAYBOOK_DIR, path)


def _browser_url(path, ref):
    return "https://github.com/{}/blob/{}/{}/{}".format(PLAYBOOK_REPO, ref, PLAYBOOK_DIR, path)


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")

    inputs = _read_inputs()
    section = inputs["section"]
    ref = inputs["ref"]
    if section not in SECTIONS:
        return _failure(
            "Unknown section '{}'. Sections: {}.".format(
                section, "; ".join("{} ({})".format(k, v[1]) for k, v in SECTIONS.items())
            )
        )
    if not REF_RE.match(ref) or "/" in ref:
        return _failure("onb_ref must be a branch or tag name without a slash, got '{}'.".format(ref))
    path, meaning = SECTIONS[section]
    url = _url(path, ref)
    outputs = {
        "section": section,
        "meaning": meaning,
        "source": url,
        "browserUrl": _browser_url(path, ref),
        "ref": ref,
        "sections": [{"section": k, "meaning": v[1]} for k, v in SECTIONS.items()],
    }
    try:
        from utilities.helpers import cb_request

        response = cb_request("GET", url, timeout=TIMEOUT)
    except Exception as exc:
        outputs["error"] = str(exc)[:500]
        outputs["note"] = (
            "The appliance could not reach raw.githubusercontent.com (proxy, firewall or SSL inspection). "
            "Open browserUrl in a browser and paste the text, or clone the repository and read the file."
        )
        return _failure("Could not fetch the playbook section '{}': {}".format(section, str(exc)[:300]))
    if response.status_code == 404:
        outputs["httpStatus"] = 404
        return _failure(
            "The playbook section '{}' is not published on ref '{}' of {} (HTTP 404). Pass onb_ref with the branch "
            "or tag that carries the skill, or read it from the repository.".format(section, ref, PLAYBOOK_REPO)
        )
    if response.status_code != 200:
        outputs["httpStatus"] = response.status_code
        return _failure("GitHub answered HTTP {} for {}.".format(response.status_code, url))
    text = response.text
    outputs["truncated"] = len(text) > MAX_CHARS
    outputs["text"] = text[:MAX_CHARS]
    outputs["characters"] = len(text)
    return _success("Playbook section '{}' ({} characters).".format(section, len(text)), outputs)

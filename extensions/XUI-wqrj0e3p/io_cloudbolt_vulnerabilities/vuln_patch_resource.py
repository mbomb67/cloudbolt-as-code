"""
Installing updates on all servers of a resource.

start_job() runs in the web request or in another job. It checks that patching is on and that no run is live on the
resource, then starts the plugin from vuln_hooks.py (vuln_resource_patch_job.py) with OrchestrationHook.run_as_job.
CloudBolt itself starts the same plugin when a person chooses the Install Updates on All Servers resource action; the
dialog's "Restart if needed" input reaches run_resource_patch as the keyword restart_if_needed.

run_resource_patch() runs inside that job, as the job's owner. It handles the resource's own servers one at a time,
in the order of the resource scan (hostname, then id), and sends each one through vuln_patch._patch_one, so every
rule of the server action holds here too: the per-server start lock, the Unassigned rule, the time limits. On top of
that it:
  1. refuses to start while another run holds the resource (the same marker file as the resource scan, kind
     "patch"), writes that marker, and always clears it at the end;
  2. checks every server again, just before it starts, as the job's owner read fresh: still in the resource, not
     in an Unassigned group, the owner may view it and may run the Install Updates server action on it, and
     vuln_patch.eligibility() passes. A server that fails a check is skipped with a reason and the run goes on;
  3. stops at the first server whose update fails: the servers after it are not attempted and are reported as
     skipped. A server that needs a restart, one with no updates to install and a warning are not failures;
  4. starts no server whose worst case would not end before CloudBolt's 8 hour cancel (vuln_patch.JOB_LIMIT_S), and
     none once the job was asked to stop;
  5. writes the last-run record of the resource (store.set_resource_patch) at the end, also on a cancel.
The resource action's own permission says nothing about the servers, so step 2 is what keeps this action from
granting more than the server action does.
"""
from accounts.models import Group, UserProfile
from infrastructure.models import Server
from utilities.logger import ThreadLogger

from . import vuln_fleet, vuln_hooks, vuln_jobs, vuln_patch, vuln_store as store

logger = ThreadLogger(__name__)

MESSAGE_LEN = 300
ALREADY_RUNNING = "Job {} is already running on this resource."
ACTION_OFF = "The Install Updates on All Servers action is switched off."
IN_FLIGHT = ("The job was canceled while this server was updating. The update on the server may still finish.")
NO_RESOURCE = "This job needs exactly one resource."
NO_OWNER = ("This job has no owner, so nothing was updated. Start it again from the resource's Actions menu.")
NOTHING_UPDATED = "No server could be updated."
# What a skipped server is told, by reason code. The codes that are not here use vuln_fleet.REASONS.
SKIP_TEXT = {
    "no_permission": "the person who started the job may not install updates on this server",
    "left_resource": "no longer in this resource",
}


# ---------------------------------------------------------------- the condition and the offer
def has_candidates(resource):
    """True when the resource has at least one live Linux or Windows server outside an Unassigned group: the
    Display Condition of the resource action, which core runs for every resource page, so it is one query. No
    eligibility and no permission. False on any error."""
    try:
        servers = resource.server_set.exclude(status="HISTORICAL").filter(vuln_fleet.supported_q())
        unassigned = list(Group.unassigned.ids())
        if unassigned:
            servers = servers.exclude(group__in=unassigned)
        return servers.exists()
    except Exception:
        logger.exception("Vulnerabilities: cannot tell whether resource %s has servers to update",
                         getattr(resource, "id", ""))
        return False


def _viewable(profile, resource):
    # infrastructure/models.py Server.objects_for_profile returns a plain queryset: never .distinct() on it.
    return set(Server.objects_for_profile(profile).filter(resource=resource).values_list("id", flat=True))


def _may_patch(profile, server, viewable):
    return server.id in viewable and bool(vuln_hooks.server_patch_permitted(profile, server))


def patchable_ids(profile, resource, visibility, problems=None):
    """The ids, in resource_servers() order, of the resource's servers that profile may view and update (the
    Install Updates server action is permitted on them) and that pass vuln_patch.eligibility(). problems is
    vuln_fleet.prerequisites(), when the caller has it. visibility is the setting, which eligibility reads itself;
    the argument keeps this call shaped like vuln_fleet.scannable_ids."""
    viewable = _viewable(profile, resource)
    if not viewable:
        return []
    if problems is None:
        problems = vuln_fleet.prerequisites(vuln_fleet.default_base_url())
    found = []
    for server in vuln_fleet.resource_servers(resource):
        if _may_patch(profile, server, viewable) and not vuln_patch.eligibility(server, problems):
            found.append(server.id)
    return found


def any_patchable(profile, resource, visibility):
    """True as soon as ONE server passes patchable_ids()'s rules; False when none, and on any error."""
    try:
        viewable = _viewable(profile, resource)
        if not viewable:
            return False
        problems = vuln_fleet.prerequisites(vuln_fleet.default_base_url())
        for server in vuln_fleet.resource_servers(resource):
            if _may_patch(profile, server, viewable) and not vuln_patch.eligibility(server, problems):
                return True
    except Exception:
        logger.exception("Vulnerabilities: could not tell whether resource %s has a server to update",
                         getattr(resource, "id", None))
    return False


# ---------------------------------------------------------------- start
def start_job(owner, resource, restart_if_needed=True, parent_job=None):
    """Start the install job of the resource, run as owner. Returns the Job, or a vuln_jobs.Refusal: 403 when
    patching is off, 409 when a run is live on the resource, 500 when the state cannot be read or CloudBolt did
    not start the job. No permission check, and no server check: the caller has made the first, and the job makes
    both for each server, as owner, when the server's turn comes."""
    if not vuln_patch.patching_enabled():
        return vuln_jobs.Refusal(vuln_patch.PATCHING_OFF, 403)
    try:
        with store.resource_lock(resource.id):
            running = vuln_fleet._live_run(resource.id)
            if running is not None:
                return vuln_jobs.Refusal(ALREADY_RUNNING.format(running[1].id), 409)
            created = vuln_hooks.resource_patch_hook().run_as_job(
                owner=owner,
                resources=[resource],
                parent_job=parent_job,
                context={"restart_if_needed": bool(vuln_patch.parse_restart_flag(restart_if_needed))},
            )
            if not created:
                return vuln_jobs.Refusal("CloudBolt did not start the job; see the application log.", 500)
            job = created[0]
            # From here the job is queued: nothing below may turn into an error.
            try:
                job.label = "Install updates: {}".format(resource.name)
                job.save(update_fields=["label"])
                # resources/models.py Resource.jobs: the job shows on the resource's own Jobs tab.
                resource.jobs.add(job)
            except Exception:
                logger.exception("Vulnerabilities: could not label or link job %s", job.id)
            return job
    except store.StoreError:
        logger.exception("Vulnerabilities: state problem starting an update of resource %s", resource.id)
        return vuln_jobs.Refusal(vuln_fleet.RESOURCE_STATE_ERROR, 500)


# ---------------------------------------------------------------- the job
class _Run:
    """What one job knows about its run: the servers in order, what each one got, and the verdict."""

    def __init__(self, job, resource_id, restart, owner):
        self.job = job
        self.resource_id = resource_id
        self.restart = restart
        self.owner = owner
        self.requested_by = vuln_jobs._requested_by(owner)
        self.order = []
        self.hosts = {}
        self.current = None
        self.results = {}
        self.lines = []
        self.stopped_at = None
        self.status = "warning"
        self.message = "The job was canceled before it finished."
        self.done = False

    def counts(self):
        counts = {"updated": 0, "no_updates": 0, "restart_pending": 0, "failed": 0, "skipped": 0}
        for item in self.results.values():
            counts[item["status"]] += 1
        return counts

    def name(self, server_id):
        """The hostname of a server the owner may view, else "Server <id>"."""
        return self.hosts.get(server_id) or "Server {}".format(server_id)

    def add(self, server_id, status, message):
        self.results[server_id] = {"hostname": self.name(server_id), "status": status,
                                   "message": vuln_jobs.clean(message, vuln_patch.MESSAGE_LEN)}

    def skip(self, server_id, why):
        self.add(server_id, "skipped", "Skipped: {}.".format(why))
        self.lines.append("{}: skipped, {}.".format(self.results[server_id]["hostname"], why))

    def close(self):
        """Every server that got no result is reported as not attempted (a cancel ended the loop)."""
        for server_id in self.order:
            if server_id not in self.results:
                self.skip(server_id, "not attempted, the job was canceled")

    def outputs(self):
        results = {str(server_id): self.results[server_id] for server_id in self.order if server_id in self.results}
        return {"resource_id": self.resource_id, "order": list(self.order), "servers": results,
                "counts": self.counts(), "stopped_at": self.stopped_at}

    def record(self):
        return {
            "at": store.stamp(),
            "by": self.requested_by,
            "job_id": self.job.id,
            "status": self.status,
            "counts": self.counts(),
            "restart_if_needed": bool(self.restart),
            "message": vuln_jobs.clean(self.message, MESSAGE_LEN),
        }


def _one_resource(resources, resource):
    """The one resource the job runs on, or None."""
    if resource is not None:
        return resource
    try:
        found = list(resources) if resources is not None else []
    except Exception:
        logger.exception("Vulnerabilities: could not read the job's resources")
        return None
    return found[0] if len(found) == 1 else None


def run_resource_patch(job, resources=None, resource=None, restart_if_needed=None, profile=None, **kwargs):
    """Plugin entry point (via vuln_resource_patch_job.py). Returns the 4-tuple CloudBolt plugins use: status,
    output, errors, outputs. The resource marker is written first and always cleared at the end, for this job
    only, whatever way it ends; the last-run record is written at the end too, a cancel included. An action an
    admin switched off does not run. When the action does not exist (an API caller on an appliance without it),
    the run goes on: the setting gate and the per-server permission checks still apply."""
    if not vuln_patch.patching_enabled():
        return "FAILURE", "", vuln_patch.PATCHING_OFF, {}
    try:
        state = vuln_hooks.resource_patch_action_state()
    except Exception:
        state = {}
    if state.get("exists") and not state.get("enabled"):
        return "FAILURE", "", ACTION_OFF, {}
    owner = getattr(job, "owner", None)
    if owner is None:
        # A job that is rerun or cloned can lose its owner. Nobody's permissions can be checked then, so
        # nothing may be updated.
        return "FAILURE", NO_OWNER, NO_OWNER, {}
    target = _one_resource(resources, resource)
    if target is None:
        return "FAILURE", "", NO_RESOURCE, {}
    run = _Run(job, target.id, vuln_patch.parse_restart_flag(restart_if_needed), owner)
    try:
        refusal = _take_marker(job, target, run)
    except Exception as exc:
        logger.exception("Vulnerabilities: could not record job %s as the run on resource %s", job.id, target.id)
        message = vuln_fleet.RESOURCE_STATE_ERROR if isinstance(exc, store.StoreError) else (
            "The run could not be recorded: {}".format(vuln_jobs.clean(str(exc), 200)))
        return "FAILURE", "", message, {}
    if refusal:
        return "FAILURE", "", refusal, {}
    try:
        return _execute(job, target, owner, run)
    except Exception as exc:
        logger.exception("Vulnerabilities: the update of resource %s failed", target.id)
        run.close()
        run.status = "failure"
        run.message = "The update of the resource's servers failed: {}".format(vuln_jobs.clean(str(exc), 200))
        run.done = True
        return "FAILURE", "\n".join(run.lines), run.message, run.outputs()
    finally:
        # Runs for a cancel too (a BaseException passes the except above untouched).
        if not run.done:
            run.close()
        try:
            store.set_resource_patch(target.id, run.record())
        except Exception:
            logger.exception("Vulnerabilities: could not record the update of resource %s", target.id)
        try:
            store.clear_resource_run(target.id, job_id=job.id)
        except Exception:
            logger.exception("Vulnerabilities: could not clear the run of resource %s", target.id)


def _take_marker(job, resource, run):
    """Write the marker. Returns a sentence when another job that is still active holds the resource (nothing is
    written then), else "". Raises when the state cannot be read or written: the run's exclusivity is not
    certain, so it does not start."""
    with store.resource_lock(resource.id):
        found = vuln_fleet._live_run(resource.id)
        if found is not None and found[1].id != job.id:
            return ALREADY_RUNNING.format(found[1].id)
        servers = list(vuln_fleet.resource_servers(resource))
        run.order = [server.id for server in servers]
        # Only the servers the owner may view are named anywhere in the job's output, the record or its progress.
        viewable = _viewable(run.owner, resource)
        run.hosts = {server.id: server.hostname for server in servers if server.id in viewable}
        store.set_resource_run(resource.id, {"job_id": job.id, "started": store.stamp(),
                                             "requested_by": run.requested_by, "server_ids": list(run.order),
                                             "kind": vuln_patch.PATCH_KIND})
    return ""


def _execute(job, resource, owner, run):
    try:
        cfg = store.get_settings()
    except Exception:
        cfg = {}
    problems = vuln_fleet.prerequisites(vuln_fleet.default_base_url())
    worst_s = (vuln_patch._minutes(cfg, "patch_timeout_min", vuln_patch.DEFAULT_PATCH_TIMEOUT_MIN)
               + vuln_patch._minutes(cfg, "restart_wait_min", vuln_patch.DEFAULT_RESTART_WAIT_MIN)
               + vuln_patch.RESCAN_MARGIN_MIN) * 60
    began = vuln_patch._clock()
    state = {"profile": owner, "revoked": False}
    total = len(run.order)
    why_not = ""

    def check(server):
        """"" when the server may be updated now, else a reason code. Read fresh just now: the request that
        started the job may be hours old."""
        if getattr(server, "resource_id", None) != resource.id:
            return "left_resource"
        if server.status == "HISTORICAL":
            return "historical"
        if vuln_jobs.in_unassigned_group(server):
            return "unassigned"
        # The owner's roles are read off the loaded profile, and User.is_active is never consulted by them, so
        # read both again here. A profile that is gone or a user that is no longer active has no permission, for
        # this and every remaining server.
        if state["revoked"]:
            return "no_permission"
        fresh = UserProfile.objects.select_related("user").filter(id=owner.id).first()
        user = getattr(fresh, "user", None)
        if fresh is None or user is None or not user.is_active:
            state["revoked"] = True
            return "no_permission"
        state["profile"] = fresh
        if not (vuln_jobs.can_view(fresh, server) and vuln_hooks.server_patch_permitted(fresh, server)):
            return "no_permission"
        return vuln_patch.eligibility(server, problems)

    try:
        for position, server_id in enumerate(run.order):
            if not why_not:
                if vuln_fleet._cancelled(job):
                    why_not = "not started, the job was canceled"
                elif vuln_patch._clock() - began + worst_s > vuln_patch.JOB_LIMIT_S:
                    why_not = ("not started, one more update could take up to {} minutes and the job has to end "
                               "before CloudBolt's 8 hour limit".format(worst_s // 60))
            if why_not:
                run.skip(server_id, why_not)
                continue
            try:
                server = vuln_fleet._fetch_for_scan(server_id)
                if server is None:
                    run.skip(server_id, vuln_fleet.REASONS["gone"])
                    continue
                code = check(server)
            except Exception as exc:
                logger.exception("Vulnerabilities: could not check server %s", server_id)
                run.skip(server_id, "it could not be checked: {}".format(vuln_jobs.clean(str(exc), 200)))
                continue
            name = run.name(server_id)
            if code:
                run.skip(server_id, SKIP_TEXT.get(code) or vuln_fleet.REASONS.get(code, code))
                continue
            vuln_fleet._progress(job, "Server {} of {}: {}".format(position + 1, total, name), position, total)
            run.current = server_id
            try:
                done = vuln_patch._patch_one(job, server, run.restart, cfg)
            except vuln_patch.PatchFailed as exc:
                # The server was not touched: skipped, and the run goes on.
                run.current = None
                run.skip(server_id, "the update could not start: {}".format(vuln_jobs.clean(str(exc), 200)))
                continue
            except Exception as exc:
                logger.exception("Vulnerabilities: the update of server %s failed", server_id)
                done = {"status": "failed", "message": "The update failed: {}".format(vuln_jobs.clean(str(exc), 200)),
                        "warning": True}
            run.current = None
            status = done["status"]
            message = vuln_patch._sentence(name, done) if status != "failed" else done["message"]
            if status == "failed":
                run.add(server_id, "failed", done["message"])
                run.lines.append("{}: failed. {}".format(name, done["message"]))
                run.stopped_at = server_id
                why_not = "not attempted, {} failed".format(name)
            elif status == "restart_pending":
                run.add(server_id, "restart_pending", message)
                run.lines.append(message)
            else:
                # "updated" and "no_updates": neither is a failure or a skip.
                run.add(server_id, "no_updates" if status == "no_updates" else "updated", message)
                run.lines.append(message)
            if done.get("warning") and status != "failed":
                state["warned"] = True
    except BaseException:
        # A cancel (CancelJobException) ends the loop here; _patch_one's own finally cleared the server's marker.
        # The server being updated is neither done nor untouched: its update may still finish on the server.
        if run.current is not None and run.current not in run.results:
            run.add(run.current, "failed", IN_FLIGHT)
            run.lines.append("{}: {}".format(run.name(run.current), IN_FLIGHT))
        vuln_fleet._progress(job, vuln_patch.GUEST_KEEPS_RUNNING)
        raise
    return _finish(run, state.get("warned", False))


def _finish(run, warned):
    """The verdict, as run_patch ends: FAILURE when a server failed and nothing was updated, or when nothing was
    attempted; WARNING for a failure after other servers were updated, a restart pending, a warning or a skip."""
    counts = run.counts()
    ok = counts["updated"] + counts["no_updates"] + counts["restart_pending"]
    attempted = ok + counts["failed"]
    parts = []
    if counts["updated"]:
        parts.append("updated {}".format(_servers(counts["updated"])))
    if counts["no_updates"]:
        parts.append("no updates were available on {}".format(_servers(counts["no_updates"])))
    if counts["restart_pending"]:
        parts.append("{} waiting for a restart".format(_servers(counts["restart_pending"])))
    if counts["failed"]:
        parts.append("{} failed".format(_servers(counts["failed"])))
    if counts["skipped"]:
        parts.append("skipped {}".format(_servers(counts["skipped"])))
    message = ("; ".join(parts) or "nothing was updated").capitalize() + "."
    problem = ""
    if counts["failed"]:
        item = run.results[run.stopped_at]
        problem = "{} failed: {}".format(item["hostname"], item["message"])
        if counts["skipped"]:
            problem += " The run stopped there; the servers after it were not attempted."
    if not attempted:
        problem = NOTHING_UPDATED
    if counts["failed"]:
        status = "FAILURE"
    elif not attempted:
        status = "FAILURE"
    elif warned or counts["failed"] or counts["skipped"] or counts["restart_pending"]:
        status = "WARNING"
    else:
        status = "SUCCESS"
    run.status = status.lower()
    run.message = problem if problem and status == "FAILURE" else message
    run.done = True
    lines = list(run.lines) + [message]
    return status, "\n".join(lines), problem if status != "SUCCESS" else "", run.outputs()


def _servers(count):
    return "{} server{}".format(count, "" if count == 1 else "s")

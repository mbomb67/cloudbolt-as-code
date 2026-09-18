from django.contrib.auth.models import User

from infrastructure.models import Server, ServerExpireParameters
from jobs.models import Job
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


def run(job, *args, **kwargs):
    """
    Recurring job to scan the database for expired servers,
    creating a job to expire them, which in turn will be picked up by
    the job engine to actually run the expire job.
    """
    servers = Server.objects.filter(status__in=["ACTIVE", "PROVFAILED"])

    expirables = Server.get_expired_servers(servers)
    if not expirables:
        return None

    job_params = ServerExpireParameters()
    job_params.save()
    job_params.servers.add(*expirables)

    user = User.objects.filter(is_superuser=True, is_active=True).order_by("id").first()
    if user:
        user_profile = user.userprofile
    else:
        user_profile = None

    # The "expire" job is pretty small.  The main reason for an expire job type is so
    # users can add their own automation in pre-expire and post-expire hook points.
    # TODO: Consider making the expirejob a recurring job so we don't need this expirescan job
    job = Job(
        type="expire", parent_job=job, job_parameters=job_params, owner=user_profile
    )
    job.save()
    msg = f"spawned job {job.id}"

    return "SUCCESS", msg, ""

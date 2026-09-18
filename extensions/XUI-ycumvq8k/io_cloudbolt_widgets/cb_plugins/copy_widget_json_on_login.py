"""
This plugin should be used as an Orchestration action at the SSO User Update
Hook point. It copies the widgets_json field from a specific user to the logging
in user if they don't already have a widgets json field defined.
"""
from accounts.models import UserProfile
from common.methods import set_progress


def run(job, user, **kwargs):
    source_username = "{{ source_username }}"
    src_profile = UserProfile.objects.get(user__username=source_username)
    dst_profile = UserProfile.objects.get(user=user)
    if not dst_profile.widgets_json:
        set_progress(f'Copying widgets_json from {source_username} to '
                     f'{user.username}')
        dst_profile.widgets_json = src_profile.widgets_json
        dst_profile.save()


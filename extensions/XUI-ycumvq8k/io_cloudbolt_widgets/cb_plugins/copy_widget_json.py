from accounts.models import UserProfile

# Username whose dashboard layout is copied to every other user. Edit before running.
SOURCE_USERNAME = "FILL-ME"


def run(job, **kwargs):
    if "FILL-ME" in SOURCE_USERNAME:
        raise ValueError("Set SOURCE_USERNAME in copy_widget_json.py before running.")
    src_profile = UserProfile.objects.get(user__username=SOURCE_USERNAME)
    for dst_profile in UserProfile.objects.all():
        dst_profile.widgets_json = src_profile.widgets_json
        dst_profile.save()


if __name__ == "__main__":
    run(None)

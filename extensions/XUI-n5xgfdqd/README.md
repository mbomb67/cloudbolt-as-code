# Credentials Manager (XUI)

Adds **Admin > Custom Extensions > Credentials Manager**, where CB Admins update the credentials CloudBolt stores for many servers and images in one action, for example after a domain service account's password changes in Active Directory or a vault. Only CloudBolt's records change: nothing runs on the servers and the images themselves are untouched.

- **Credentials:** username (blank keeps each target's), password with confirmation, an SSH key from **Admin > SSH Keys**, and for images a **New VM password** (VMware only: the password CloudBolt sets on each new VM built from the image). Only the fields you fill in change.
- **Filters:** resource technology, resource handler, OS build, OS family, environment, group, blueprint and current username. A target must match every filter; within one filter, any of the picked values. Group and blueprint narrow servers only. Servers that are not Active, or sit in an Unassigned group or environment, are never included.
- **Preview:** counts of what will change, a breakdown per filter value, warnings (SSH key not available on that handler or environment, Windows targets skip the SSH key, no username stored, new VM password on a non-VMware image), and a searchable table of every target with CSV download. Apply asks you to type the number of objects that will change, and refuses if the matching set changed since the preview.
- **History:** each server gets a history event naming the user and the job (a notification event when the server has no creation or onboarding event, because CloudBolt drops change events on those). Images have no history of their own, so each image's event goes on its resource handler's history. The **History** tab lists every update with its result.

The update runs as a job of the plugin "Credentials Manager: Update Credentials", which the page registers on first use; its code is this extension's `update_job.py`. The passwords travel in the job's encrypted parameters, are stripped from the job log as CloudBolt does for password inputs, and are blanked when the job ends, so a job cannot be rerun. One update runs at a time.

Values are written where CloudBolt's own credentials dialogs write them (the `username`, `password` and `key_name` parameters, and `new_password` on images). Setting a server's username or password also removes the legacy `initial_*` and `template_root_password` parameters, as the native dialog does. Remote Scripts with their own saved credentials are not changed.

## Setup

1. Sync `extensions/XUI-n5xgfdqd` from **Admin > Source Control Repos**.
2. The extension adds URL paths, so restart the web server as root:

   ```bash
   systemctl restart httpd
   ```

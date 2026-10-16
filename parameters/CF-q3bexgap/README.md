# Data Disks

Data disks to add to the server, as a JSON list: one entry per disk with drive_letter (Windows) or mount_point (Linux), size_gb, and volume_name.

Parameter `disk_map` (Multi-line Text). Source Control Repos does not sync parameters: the Content Package Manager UI extension writes this folder when it exports a package that lists the parameter, and its Sync creates the parameter, or updates the one with this name.

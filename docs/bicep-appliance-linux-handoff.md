# Bicep compiler on the CloudBolt appliance — notes for the Linux team

## What CloudBolt does

On the first Bicep order, a CloudBolt job downloads Microsoft's Bicep CLI and runs it to compile the template. It is one static Linux binary. No package manager, no root, no PATH or system changes.

| | |
|---|---|
| Source | `https://github.com/Azure/bicep/releases/download/v0.44.1/bicep-linux-x64` (302 redirect to `release-assets.githubusercontent.com`) |
| Size / SHA-256 | 99 MB, `e17dc9a9888184886bb0c0051a3230b83b19f342749999f707bc571c3dfd2f45` (verified against the release) |
| Destination | `/var/opt/cloudbolt/proserv/bicep/0.44.1/bicep`, mode `0755` |
| Runs as | the account that runs the CloudBolt job engine, a supervisord-managed Python process (`supervisorctl status`; normally `cloudbolt`). Not httpd. |
| Command | `bicep build <file> --stdout`. No network needed at compile time. |
| Runtime note | .NET single-file app. At startup it extracts native libraries to `$DOTNET_BUNDLE_EXTRACT_BASE_DIR`, or `$HOME/.net` if unset. That directory must be writable and not `noexec`. |

## What can block it, and the fix

1. **Egress blocked.** Allow HTTPS from the appliance to `github.com` and `release-assets.githubusercontent.com` (older redirects use `objects.githubusercontent.com`). SSL inspection is fine: the download follows CloudBolt's SSL settings, so upload the inspecting CA at Admin > SSL Certificates in CloudBolt rather than exempting the hosts. A failed job names the host, the CA it presented, and whether CloudBolt trusts it; the `Bicep Engine Connectivity Check` recurring job (Run Now) probes every host the same way. Or pre-stage it: put the file at the destination path, `chown` to the job-engine account, `chmod 0755`. CloudBolt checks the SHA-256 and skips the download when it matches.

2. **`noexec` mount.** `findmnt -T /var/opt/cloudbolt/proserv` and check the options. Remount without `noexec`, or tell CloudBolt and the cache can be pointed at another directory. Check the extraction directory from the runtime note the same way.

3. **SELinux denial.** `getenforce`, then `ausearch -m AVC -ts recent | grep bicep`. Label the cache as executable content:

   ```bash
   semanage fcontext -a -t bin_t '/var/opt/cloudbolt/proserv/bicep(/.*)?'
   restorecon -Rv /var/opt/cloudbolt/proserv/bicep
   ```

   If denials continue (for example `execmem` from the .NET runtime), build a targeted module from the audit log:

   ```bash
   ausearch -m AVC -ts recent | audit2allow -M cloudbolt_bicep && semodule -i cloudbolt_bicep.pp
   ```

4. **`$HOME` unset or not writable for the job-engine account.** Add `DOTNET_BUNDLE_EXTRACT_BASE_DIR=/var/opt/cloudbolt/proserv/bicep/.net` to the job engine's `environment=` line in its supervisord program config, create that directory owned by the account, then `supervisorctl update`.

## Verify

Run as the job-engine account:

```bash
sudo -u cloudbolt /var/opt/cloudbolt/proserv/bicep/0.44.1/bicep --version
```

It should print `Bicep CLI version 0.44.1`. If that works, CloudBolt works.

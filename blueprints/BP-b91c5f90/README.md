# Postgres Database

Deploys one Oracle Linux 8 VM, installs PostgreSQL (15-18) from the PGDG repository, creates one database and owning role with the requested password, restricts remote authentication to a client CIDR, and exposes two day-2 actions for testing the connection and running SQL.

## Contents
| Role | ID | Name |
|---|---|---|
| Server tier | — | Linux (OS build "Oracle Linux 8"; hostname `pgdb-<grp>-o-00X`) |
| Build (Remote Script, seq 2) | OHK-29uent0x | Install Postgres |
| Build (plugin, seq 3) | OHK-yw0klpjg | Set Resource Name From Field |
| Day-2 action | RSA-9tfwebk7 | Test Postgres Connection (plugin OHK-wr079u8q) |
| Day-2 action | RSA-jaitfhwp | Run SQL Command (Remote Script OHK-4w05hdd3) |
| Parameter options hook | HPA-qb0w86mi | Generate options for 'Expiration Date' |

Order-form parameters: Postgres Version (15/16/17/18), Database Name, Database Owner, Database Password (PWD), Postgres Port (5432 suggested), Client CIDR, Expiration Date (optional).

## Prerequisites
- An OL8 OS build with outbound HTTPS to `download.postgresql.org` (PGDG repo RPM) and to Oracle EPEL or Fedora EPEL. The script disables the AppStream `postgresql` module.
- The Remote Scripts must run as root (hard check in OHK-29uent0x).
- The `database` resource type on the instance.

## Setup
1. After sync, re-point the server tier's OS build to your Oracle Linux 8 build (the exported href `OSB-pd2spm2e` is instance-specific) and enable the environments the tier may deploy into (`all_environments_enabled` is false).
2. Confirm the run-as credentials on OHK-29uent0x and OHK-4w05hdd3; exported `credentials` values are the placeholder `YOUR_CREDENTIALS`.
3. Optionally adjust defaults in `plugins/OHK-29uent0x/OHK-29uent0x_script.sh`: `LISTEN_ADDRESSES` (`*`), `MAX_CONNECTIONS` (200), `SHARED_BUFFERS` (256MB), `INSTALL_CONTRIB`, `OPEN_FIREWALL`.

## Notes
- Client CIDR is required and becomes the only remote `host` rule in `pg_hba.conf` (`scram-sha-256`) for the new database and owner, alongside `127.0.0.1/32`. Clients outside that network cannot authenticate. The placeholder `172.29.10.0/24` is an example, not a default.
- Postgres listens on all interfaces and the port is opened in firewalld when firewalld is running.
- The database password is a PWD parameter stored on the resource and server and reused by both day-2 actions.
- Run SQL Command executes the input via `psql -v ON_ERROR_STOP=1` on the server; the default value is a sample table create/insert/select.
- Test Postgres Connection (OHK-wr079u8q) connects on port 5432 regardless of the Postgres Port parameter.
- OHK-yw0klpjg renames the resource to `<Database Name> (<hostname>)`, overriding `resource_name_template`.
- No teardown items; deleting the resource deletes the server and nothing else.

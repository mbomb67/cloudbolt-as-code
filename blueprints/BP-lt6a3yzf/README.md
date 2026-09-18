# Request Certificate (Windows CA)

Ad-hoc certificate requests against a Microsoft AD CS Certificate Authority through its Web Enrollment pages (`/certsrv`) over HTTPS with Basic auth. The requester either pastes their own CSR or lets CloudBolt generate a key and CSR; the issued PEM certificate is stored on the resource. No server is provisioned. Demo content.

Operator and lab runbook: [../../docs/windows-ca-cert-request-setup.md](../../docs/windows-ca-cert-request-setup.md).

## Contents
| Role | ID | Name |
|---|---|---|
| Build | OHK-67bw7wgu | Request Certificate (Windows CA) |
| Day-2 action | RSA-7cjsqrwy | Retrieve Pending Certificate (plugin OHK-ul8wbswa) |
| Shared module | SHM-nubxb8sn | windows_ca (Web Enrollment client) |

Order-form inputs: Generate the CSR for me (BOOL, default off), Common Name, Subject Alternative Names, Generated Private Key (ETXT, pre-filled), Your CSR (PEM), Certificate Template (default `CloudBoltWebServer`).

## Prerequisites
- An AD CS CA with the Certification Authority Web Enrollment role, an HTTPS binding, and Basic authentication enabled on `/certsrv` (runbook section 3.6). Enterprise CA: a template with Subject Name "Supply in the request" and a service account granted Read + Enroll. Standalone CA: templates are ignored and requests stay pending unless `RequestDisposition` is set to auto-issue (runbook section 3.8).
- Appliance reachability to the CA on 443/tcp. `requests` and `cryptography` are present on the appliance; no NTLM or Kerberos libraries are needed.
- `any_group_can_deploy` is false: grant deployment permissions to groups after import.

## Setup
1. Create a ConnectionInfo named exactly `Demo Windows CA` (protocol `https`, ip = CA FQDN matching its IIS certificate, port 443, username `DOMAIN\svc`, password), or change `CONNECTION_INFO_NAME` in `shared_modules/SHM-nubxb8sn/SHM-nubxb8sn_script.py`.
2. Re-enter the ConnectionInfo password after every repo sync; the build plugin fails fast when it is blank.
3. Set `CA_CERTS_FILE` in the shared-module config block to a bundle containing the CA root so TLS is validated. As shipped `CA_CERTS_FILE = ""` and `VERIFY_TLS = False` (encrypt-only, lab only). Shared-module edits need a CloudBolt restart.
4. Verify the curl/openssl end-to-end flow in runbook section 6 before exposing the blueprint to users.

## Notes
- With "Generate the CSR for me" on, a fresh private key is pre-filled on every form render; the user must copy it before submitting. The key is also stored encrypted in `windows_ca_private_key` on the resource. Prefer bring-your-own CSR outside demos.
- Manager-approval templates return SUCCESS with status `pending` and the CA Request ID in `windows_ca_request_id`; run Retrieve Pending Certificate until the certificate is issued.
- The resource is renamed to the Common Name; issued PEM, thumbprint, template, and status are stored in `windows_ca_*` custom fields.
- No teardown: deleting the resource does not revoke the certificate.
- Basic auth over HTTPS is a demo posture; runbook section 7 covers hardening.

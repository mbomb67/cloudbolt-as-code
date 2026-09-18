# Windows CA Certificate Request (ad-hoc) — operator & lab setup

This content lets a user **request a certificate from a Microsoft AD CS
(Windows) Certificate Authority on demand**, with no server in the loop. The
CloudBolt appliance submits a CSR directly to the CA's **Certification Authority
Web Enrollment** pages (`/certsrv/certfnsh.asp`) over HTTPS and stores the issued
certificate back on the resource.

It supports two request modes on one form:

- **Bring-your-own CSR** — the user pastes a CSR they already have; CloudBolt
  only submits it and returns the signed certificate. The private key never
  touches CloudBolt.
- **CloudBolt-generated key** — a generated-options plugin pre-fills a freshly
  generated private key into the form (so the user can copy it before
  proceeding); CloudBolt builds the CSR from it, submits, and returns the cert.

> **This doc has two audiences.** §1–§2 are the **recommendation and architecture**.
> §3 is a **from-scratch lab build** of a demo Windows CA in the recommended
> config. §4–§6 are CloudBolt wiring, verification, and hardening. If you already
> have an AD CS CA, skim §1, then jump to §3.6 (Web Enrollment + Basic auth) and §4.

---

## 1. Why this mechanism (and what it is NOT)

AD CS exposes exactly two native enrollment protocols — **MS-WCCE** (DCOM/RPC,
what `certreq.exe` uses) and **MS-WSTEP/MS-XCEP** (the SOAP CES/CEP web services).
**There is no native REST/JSON enrollment API.** ([MS-CERSOD enrollment
overview](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-cersod/444f0375-3cf6-4bdf-b41b-18eed3ba6b54))

For an **ad-hoc request driven from the Linux CloudBolt appliance**, the only
path that runs natively in Python with no Windows intermediary and no
domain-joining the appliance is the **Certification Authority Web Enrollment**
flow — the classic `/certsrv` ASP pages. ([CA Web
Enrollment](https://learn.microsoft.com/en-us/windows-server/identity/ad-cs/certificate-authority-web-enrollment))
The de-facto reference for automating it from Python is the
[`certsrv`](https://github.com/magnuswatn/certsrv) library; this content
implements the same small `certfnsh.asp` POST → `certnew.cer` GET flow inside a
shared module rather than taking a PyPI dependency.

**Auth choice drives everything.** The appliance venv has `requests` and
`cryptography` but **not** `requests-ntlm` or `requests-kerberos`. So:

| `/certsrv` auth scheme | Appliance library needed | Use it? |
|---|---|---|
| **Basic over HTTPS** | `requests` only (present) | ✅ **Recommended for the demo** |
| NTLM | `requests-ntlm` (absent) | Only after confirming/installing the lib |
| Kerberos / Negotiate | `requests-kerberos` + keytab, domain-joined appliance | Production, heavier |
| Client certificate | `requests` native `cert=` (present), needs a bootstrap cert | Possible, more moving parts |

Basic auth is **not** the production-hardened choice (it transmits the password,
base64-encoded, to the server on every request — encrypted only by the
surrounding TLS). For a **demo it is the right call** because it works with the
stock appliance. §6 covers moving to Integrated auth or CES/CEP for production.

---

## 2. Recommended demo architecture

```
 CloudBolt appliance (Linux)                 Windows Server (lab)
 ┌──────────────────────────┐                ┌─────────────────────────────┐
 │ Blueprint order          │                │ AD DS  (domain controller)  │
 │  → build plugin (OHK-*)  │   HTTPS 443    │ AD CS  (Enterprise Root CA) │
 │  → SHM windows_ca client │ ─────────────► │  + Web Enrollment (/certsrv)│
 │     POST certfnsh.asp    │  Basic auth    │  + IIS HTTPS binding        │
 │     GET  certnew.cer     │ ◄───────────── │  + WebServer-style template │
 │  → store cert in ETXT    │   issued cert  │  + enroll svc account       │
 └──────────────────────────┘                └─────────────────────────────┘
```

| Decision | Recommendation | Why |
|---|---|---|
| CA type | **Enterprise Root CA** | Supports certificate templates + auto-issuance; mirrors real customers. (Standalone alternative in §3.8.) |
| Topology (lab) | **Single Windows Server** acting as DC + Enterprise CA | Smallest representative footprint for a demo. |
| Role service | **Certification Authority Web Enrollment** | Provides the `/certsrv` pages the appliance posts to. |
| Transport | **HTTPS only** | Web Enrollment requires SSL/TLS; protects Basic credentials in transit. |
| `/certsrv` auth | **Basic authentication** (demo) | Works with the stock appliance (`requests`), no NTLM/Kerberos libs. |
| Template | Duplicate of **Web Server**, subject **"Supply in the request"**, **auto-issue** | CSR subject is honored; no manual approval for a smooth demo. |
| Appliance → CA cred | A domain **service account** with **Enroll** on the template, stored in a CloudBolt **`ConnectionInfo`** | Standard repo credential pattern; gated, never exposed to end users. |

---

## 3. Lab build — stand up the demo CA from scratch

> Run the elevated PowerShell steps **on the Windows Server**. These commands set
> up a **lab**; do not point them at production AD. Every Microsoft-tool step is
> linked to its official doc — follow the linked page for prompts/options this
> guide summarizes.

### 3.1 Base VM

- Windows Server 2019/2022/2025, static IP, set the hostname (e.g. `ca01`)
  **before** promoting it. A 2 vCPU / 4 GB VM is plenty for a demo.

### 3.2 Promote to a domain controller (new forest)

```powershell
Install-WindowsFeature AD-Domain-Services -IncludeManagementTools
Install-ADDSForest -DomainName "lab.example.com" -InstallDns
# Reboots automatically. After reboot, log in as LAB\Administrator.
```
Docs: [Install-ADDSForest](https://learn.microsoft.com/en-us/powershell/module/addsdeployment/install-addsforest).
(Skip this section entirely if you use the Standalone alternative in §3.8.)

### 3.3 Install AD CS + Web Enrollment role services

```powershell
Install-WindowsFeature ADCS-Cert-Authority, ADCS-Web-Enrollment -IncludeManagementTools
```
Docs: [Install AD CS](https://learn.microsoft.com/en-us/windows-server/identity/ad-cs/).

### 3.4 Configure the CA (Enterprise Root)

```powershell
Install-AdcsCertificationAuthority -CAType EnterpriseRootCA `
  -CACommonName "Lab Enterprise Root CA" -KeyLength 2048 -HashAlgorithmName SHA256 `
  -ValidityPeriod Years -ValidityPeriodUnits 10
```
Docs: [Install-AdcsCertificationAuthority](https://learn.microsoft.com/en-us/powershell/module/adcsdeployment/install-adcscertificationauthority).

### 3.5 Configure Web Enrollment

```powershell
Install-AdcsWebEnrollment
```
Docs: [Install-AdcsWebEnrollment](https://learn.microsoft.com/en-us/powershell/module/adcsdeployment/install-adcswebenrollment).
This creates the `/certsrv` application under the IIS **Default Web Site**.

### 3.6 Bind IIS to HTTPS and enable Basic auth on `/certsrv`

Web Enrollment **requires** an HTTPS binding. Issue a server-auth cert to the box
(it can come from the CA you just installed) and bind it:

1. **Get a server certificate.** In `mmc` → Certificates (Local Computer) →
   Personal → All Tasks → **Request New Certificate** → select the *Computer* (or
   *Web Server*) template → enroll. (Or via `certreq`.) Docs:
   [Request certs with the Certificates snap-in](https://learn.microsoft.com/en-us/windows-server/networking/core-network-guide/cncg/server-certs/request-a-server-authentication-certificate).
2. **Add the HTTPS binding** in IIS Manager → *Default Web Site* → **Bindings** →
   Add → `https` / port 443 → select the cert. Docs:
   [Add a binding](https://learn.microsoft.com/en-us/iis/manage/configuring-security/how-to-set-up-ssl-on-iis).
3. **Enable Basic auth** (the demo-enabling step — `/certsrv` ships with Windows
   Integrated auth only):
   ```powershell
   Install-WindowsFeature Web-Basic-Auth
   ```
   Then IIS Manager → *Default Web Site* → **CertSrv** → **Authentication** →
   **enable Basic Authentication**. Leave Windows Auth enabled too if you like;
   the appliance will explicitly use Basic. Docs:
   [IIS Basic Authentication](https://learn.microsoft.com/en-us/iis/configuration/system.webserver/security/authentication/basicauthentication/).
4. **Force HTTPS** for `/certsrv`: IIS Manager → CertSrv → **SSL Settings** →
   *Require SSL*.

### 3.7 Create the certificate template and service account

1. **Duplicate the Web Server template.** `certtmpl.msc` → right-click **Web
   Server** → **Duplicate Template**. Name it e.g. `CloudBoltWebServer`. Confirm
   **Subject Name = "Supply in the request"** (Web Server's default — required so
   the submitted CSR's subject is honored). Docs:
   [Certificate template concepts](https://learn.microsoft.com/en-us/windows-server/identity/ad-cs/certificate-template-concepts).
2. **Permissions:** on the template's **Security** tab, grant your service
   account (below) **Read + Enroll**. Do **not** grant Autoenroll (not needed).
3. **Publish it:** `certsrv.msc` → *Certificate Templates* → New → **Certificate
   Template to Issue** → pick `CloudBoltWebServer`.
4. **Service account:** create a domain user, e.g. `LAB\svc-cbca`, with a strong
   password. Its only privilege need is **Enroll** on that template (granted in
   step 2). This is the identity the appliance authenticates as over Basic auth.

> **Auto-issue check:** the Web Server template issues immediately (no manager
> approval) by default. Keep it that way for the demo. If you later test the
> *pending* path, enable "CA certificate manager approval" on the template's
> *Issuance Requirements* tab — the content surfaces a day-2 "retrieve pending
> certificate" action keyed on the returned Request ID.

### 3.8 Lighter alternative — Standalone CA (no domain)

If you don't want to stand up a domain for the demo:

```powershell
Install-WindowsFeature ADCS-Cert-Authority, ADCS-Web-Enrollment -IncludeManagementTools
Install-AdcsCertificationAuthority -CAType StandaloneRootCA `
  -CACommonName "Lab Standalone Root CA" -KeyLength 2048 -HashAlgorithmName SHA256
Install-AdcsWebEnrollment
```
Trade-offs to know:

- **No certificate templates.** Omit the `CertificateTemplate` attribute on
  submit; the subject comes entirely from the CSR.
- **Requests are pending by default.** To auto-issue for a smooth demo:
  ```powershell
  certutil -setreg policy\RequestDisposition 1
  Restart-Service certsvc
  ```
  Docs: [certutil](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/certutil)
  (`-setreg policy\RequestDisposition`). Otherwise approve each request in
  `certsrv.msc` → *Pending Requests* and use the day-2 retrieve action.

Still do §3.6 (HTTPS + Basic auth) for the Standalone box.

---

## 4. Wire up CloudBolt

### 4.1 ConnectionInfo (the appliance → CA credential)

Create a **ConnectionInfo** the shared module resolves by name (mirrors the
`SHM-jlguerjr` / `SHM-eybr4hgz` pattern):

| Field | Value |
|---|---|
| **Name** | `Demo Windows CA` (must match `CONNECTION_INFO_NAME` in the SHM config block) |
| **Protocol** | `https` |
| **IP / hostname** | CA FQDN, e.g. `ca01.lab.example.com` (must match the IIS cert subject) |
| **Port** | `443` |
| **Username** | `LAB\svc-cbca` (or `svc-cbca@lab.example.com`) |
| **Password** | the service account password |

> **Secrets after sync:** repo syncs redact `ConnectionInfo` passwords. After
> every sync, re-enter the password in the CloudBolt UI. The build plugin fails
> fast with a clear "re-enter credentials" message when it's blank.

### 4.2 Appliance trust of the CA's IIS certificate

The appliance must trust the HTTPS cert on `/certsrv`. Either:

- **Recommended:** export the CA root cert and point the SHM config
  `CA_CERTS_FILE` at a bundle containing it (same pattern as the AD-DNS content),
  so TLS is validated; **or**
- **Lab shortcut:** set the SHM config `VERIFY_TLS = False`. This is encrypt-only
  (vulnerable to active MITM) and is for self-signed lab CAs only — never
  production.

### 4.3 Content inventory (what the blueprint ships)

| Piece | Role |
|---|---|
| `BP-*` "Request Certificate (Windows CA)" | Order form (§5) |
| `SHM-* windows_ca` | Pure `certfnsh.asp` client (submit CSR, fetch cert/chain, detect pending) + `get_ca_client()` seam resolving the ConnectionInfo |
| `OHK-*` build plugin | Branches on `generate_csr`, builds/forwards the CSR, submits, stores the cert |
| `OHK-*` generated-options plugin | Pre-fills `generated_pk` with a fresh private key as `initial_value` |
| `RSA-*` (optional) | "Retrieve pending certificate" day-2 action keyed on the Request ID |

---

## 5. Order form and request flow

| Field | Type | Shown when | Purpose |
|---|---|---|---|
| `common_name` | `STR` | `generate_csr == True` | Subject CN for the CloudBolt-built CSR |
| `subject_alt_names` | `STR` | `generate_csr == True` | Optional SANs (comma-separated) |
| `certificate_template` | `STR` | always | Template name, default `CloudBoltWebServer` |
| `generate_csr` | `BOOL` | always | "Generate the CSR for me?" Default **False** |
| `generated_pk` | `ETXT` | `generate_csr == True` | Generated-options plugin pre-fills a fresh **private key** as `initial_value`; user copies it before submit |
| `customer_csr` | `TXT` | `generate_csr == False` | User pastes their own PEM CSR |
| `issued_certificate` | `ETXT` | (output) | Issued PEM cert written back after the job |

**Flow:**

1. If `generate_csr == True`: the build plugin reads the submitted `generated_pk`
   (the exact key the user saw/copied) and builds a CSR from it for
   `common_name` + SANs using `cryptography`.
   If `generate_csr == False`: it uses `customer_csr` verbatim.
2. **Branches merge:** submit the CSR to `/certsrv/certfnsh.asp` (Basic auth, with
   `CertificateTemplate:<certificate_template>`), parse the Request ID, GET
   `certnew.cer?ReqID=N&Enc=b64`.
3. Write the issued PEM into `issued_certificate`; persist thumbprint / serial /
   NotAfter as resource fields. If the CA returns *pending*, store the Request ID
   and surface the day-2 retrieve action.

> **Conditional visibility** is done with CloudBolt field dependencies. Get the
> dependency entry-name suffix right (`_a<hookid>`) — malformed suffixes are
> silently dropped on import, so verify visibility on the live instance.

> **Private-key handling (demo posture).** With `generate_csr == True` the key is
> generated server-side, shown to the user via `initial_value`, and persisted
> encrypted in the `generated_pk` (ETXT) field. That's acceptable for a demo. For
> production, prefer **bring-your-own CSR** (key never touches CloudBolt), or
> generate client-side and never persist. Call this out when demoing.

---

## 6. Verification

**A. Reach the pages over HTTPS with Basic auth** (from the appliance):
```bash
curl -sk -u 'LAB\svc-cbca:<password>' https://ca01.lab.example.com/certsrv/ | head
# Expect HTTP 200 and the "Microsoft Active Directory Certificate Services" page.
```

**B. End-to-end issue, by hand** (proves the template + permissions before wiring
the plugin):
```bash
# 1. Make a test key + CSR
openssl req -newkey rsa:2048 -nodes -keyout test.key -out test.csr \
  -subj "/CN=test1.lab.example.com"

# 2. Submit via certfnsh.asp (URL-encode the CSR + template attribute)
CSR=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(open('test.csr').read()))")
curl -sk -u 'LAB\svc-cbca:<password>' \
  --data "Mode=newreq&CertRequest=${CSR}&CertAttrib=CertificateTemplate:CloudBoltWebServer&TargetStoreFlags=0&SaveCert=yes" \
  https://ca01.lab.example.com/certsrv/certfnsh.asp -D - -o resp.html
# Find the ReqID in resp.html (location: certnew.cer?ReqID=N...)

# 3. Download the issued cert
curl -sk -u 'LAB\svc-cbca:<password>' \
  "https://ca01.lab.example.com/certsrv/certnew.cer?ReqID=<N>&Enc=b64" -o issued.cer
openssl x509 -in issued.cer -noout -subject -issuer -dates
```
If step B issues a certificate, the SHM client will too. (The exact `certfnsh.asp`
field/response shape is the Web Enrollment UI, not a formally documented API —
this is why we verify against the real CA rather than assume.)

---

## 7. Production hardening (beyond the demo)

- **Drop Basic auth.** Move `/certsrv` to **Windows Integrated** (Negotiate/
  Kerberos) and add `requests-kerberos` (+ a keytab, domain-join the appliance),
  or switch to **client-certificate** auth. Make the SHM client's auth pluggable.
- **Prefer CES/CEP** ([CES](https://learn.microsoft.com/en-us/windows-server/identity/ad-cs/certificate-enrollment-web-service))
  for a standards-based, supported enrollment surface if the environment runs it.
- **Validate TLS** (`CA_CERTS_FILE` set, never `VERIFY_TLS=False`).
- **Least privilege:** the service account needs only **Enroll** on the one
  template — nothing else.
- **Reconsider key custody:** default to bring-your-own-CSR; if CloudBolt
  generates keys, define retention/rotation and who can read the ETXT field.

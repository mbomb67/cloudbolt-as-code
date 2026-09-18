# Test template: Storage Account

A minimal, generic Bicep template for exercising the CloudBolt Bicep deployment
engine end to end. In real use this lives in a customer's GitHub repo; for
testing, push this folder to a sandbox repo the engine's "GitHub" ConnectionInfo
can read.

## What it exercises

| Engine feature | Parameter |
|---|---|
| Required input (no default) | `storageAccountName` |
| Length constraints (`@minLength`/`@maxLength`) | `storageAccountName` |
| Expression default → pin-by-omission (AE3) | `location` (`resourceGroup().location`) |
| `@allowed` → dropdown, literal default → accept-and-pin | `sku`, `accountKind`, `accessTier` |
| Untyped `object` → JSON free-text input | `tags` |
| Output-driven `resource.name` | output `resourceName` |

## Run it two ways

**Generic blueprint (no scaffold).** Order the `Bicep Deployment` blueprint
(`BP-nibk4erf`) and supply:

- Environment: a sandbox Azure environment
- Resource Group: a sandbox RG
- Repository: `<owner>/<repo>` where this folder lives
- Template Path: `docs/examples/bicep/storage-account/main.bicep` (or wherever you pushed it)
- Ref: a branch/tag/SHA (pin a SHA/tag for anything beyond a quick test)
- Parameters (JSON): e.g. `{"storageAccountName": "cbteststorage001"}`
  (the others have defaults; `location` resolves via `resourceGroup().location`)

The job pauses at the what-if approval gate — review and Continue to deploy.

**Scaffolded blueprint (typed inputs).** Run the `scaffold-bicep` skill against
this template. `sku`/`accountKind`/`accessTier` become dropdowns,
`storageAccountName` a required field, `location` is offered for
pin-by-omission, and `main.bicepparam` supplies the proposed defaults during
curation.

## Day-2

- **Update Bicep Deployment** — change `sku`/`accessTier` etc. (Standard_LRS →
  Standard_ZRS makes a clean what-if Modify).
- **Drift Check** — read-only what-if; reports no drift on an unchanged deployment.
- **Delete the resource** — teardown deletes the stack (delete-all).

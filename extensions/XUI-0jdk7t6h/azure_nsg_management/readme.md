# Azure NSG Management (XUI)

Adds a **Security Rules** tab to Azure Network Security Group resources that
loosely mirrors the Azure portal NSG overview.

Companion to the **Azure Network Security Group** blueprint (`BP-3fdhnw54`).

## What it shows

The tab renders two tables — **Inbound Security Rules** and **Outbound
Security Rules** — each sorted by priority and matching the portal columns:
Priority, Name, Port, Protocol, Source, Destination, Action.

- Each table has an **Add Security Rule** button in its top-right corner.
- Each rule row ends with an **Actions** column:
  - Custom rules get **Edit** and **Delete** links (open dialogs).
  - Azure's built-in default rules (`AllowVnetInBound`, `DenyAllInBound`, …)
    are read-only and show a **Default** marker instead.

Rules are read live from Azure on each load, and add/edit/delete apply directly
against Azure via the `azure.mgmt.network` SDK.

## How it attaches

The tab is registered via `@tab_extension(model=Resource, title="Security Rules")`.
Its delegate only displays the tab when the resource carries an
`azure_network_security_group_id` custom field — the value the blueprint's
build (`OHK-7987st2p`) and discovery (`OHK-kdne1t5s`) plugins stamp on NSG
resources — so it never appears on non-NSG network resources.

## Azure connection

The extension resolves the resource's `AzureARMHandler` from the stored
`azure_rh_id`, then uses `handler.get_api_wrapper().network_client` — the same
path the NSG discovery plugin uses. No resource handler is ever exposed to the
end user; the tab operates only on the already-provisioned resource.

## Tables

Uses CloudBolt's default DataTables integration: each `<table data-table
data-table-source="…">` is initialised with `c2.dataTables.init(...)` and pulls
rows asynchronously from a JSON endpoint that returns the standard
`sEcho / iTotalRecords / iTotalDisplayRecords / aaData` payload.

## Files

| File | Purpose |
|---|---|
| `views.py` | Tab, JSON source, add/edit/delete dialogs, Azure helpers |
| `forms.py` | `SecurityRuleForm` (create/edit) |
| `urls.py` | `xui_urlpatterns` for the JSON source and dialogs |
| `templates/nsg_overview_tab.html` | Two-table Overview layout |

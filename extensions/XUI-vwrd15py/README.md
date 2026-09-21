# Technology Reference (XUI)

Adds a read-only **Technology Reference** tab to every Resource Handler page and to every Environment that has a resource handler. For that handler's technology it lists the custom fields the technology seeds, declares, reads or writes; the actions filtered to the technology or naming its handler class in code, with every place each action is used; and the field dependencies controlled by those fields. Everything is discovered live from the instance, and **Download Excel** exports the three tables as one `.xlsx`.

## Prerequisites
- CloudBolt 8.6 or later.
- Nothing else: the `.xlsx` is written without `openpyxl` or `xlsxwriter`.

## Setup
1. Sync the repository and confirm the extension is enabled under Admin > Extensions.
2. Optional: list fields the code scan cannot see (referenced only through a variable) in `TECHNOLOGY_REFERENCE_FIELD_OVERRIDES` in `customer_settings.py`, keyed by the technology's `type_slug` or `"*"`, each entry a dict with at least `name` (plus `label`, `type`, `description` as desired).

## Notes
- Access follows the page it sits on: CloudBolt admins, global viewers, and users with the manage permission on the handler or environment. The export URL applies the same check.
- Fields are found from the technology's `*_minimal` seed module, the handler's `tech_parameter_fields` and `special_fields`, and a bytecode scan of the handler package. VMware's seeds live in `initialize.cb_minimal` and are not read, so they appear only when another method finds them.
- Fields marked **Create to enable** are referenced by the handler code but do not exist yet; create them under Admin > Parameters.
- The Actions tab reads the source of every plug-in with a module file on each load, so it can take a few seconds on instances with many actions.

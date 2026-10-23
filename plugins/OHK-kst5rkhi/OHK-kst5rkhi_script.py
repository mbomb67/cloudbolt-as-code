"""
POC Onboarding: Install Content Library Item (MCP Tool Action plugin).

Downloads an item from CloudBolt's Content Library (content.cloudbolt.app)
and imports it, the way the library page does, so the POC can add the
Jupyter Notebooks UI extension, the tmp cleanup recurring job, the Resize
VM and Ad-Hoc Script server actions or any other item without clicking
through the UI. With onb_cl_item empty it lists the collection's items.
Runs as a job because it downloads from the internet. Admin only.

Every declared input is read from its rendered template token in
_read_inputs(); keep each one referenced that way or CloudBolt's token scan
deletes the input when the script is saved.
"""
import html

COLLECTIONS = {
    "blueprints": ("servicecatalog.api.v3.serializers", "ServiceBlueprintSerializer"),
    "server-actions": ("cbhooks.api.v3.serializers", "ServerActionSerializer"),
    "resource-actions": ("cbhooks.api.v3.serializers", "ResourceActionSerializer"),
    "orchestration-actions": ("cbhooks.api.v3.serializers", "OrchestrationActionSerializer"),
    "rules": ("cbhooks.api.v3.serializers", "TriggerPointSerializer"),
    "recurring-jobs": ("jobs.api.v3.serializers", "RecurringJobSerializer"),
    "ui-extension-packages": ("extensions.api.v3.serializers", "UIExtensionSerializer"),
    "cb-applets": ("extensions.api.v3.serializers", "CbAppletSerializer"),
}
MAX_LIST = 300


def _text(value):
    return html.unescape(value or "").strip()


def _bool(value, default):
    text = _text(value).lower()
    if not text:
        return default
    return text in ("true", "1", "yes", "y", "on")


def _failure(message):
    return {"status": "FAILURE", "output_message": "", "error_message": message, "outputs": {"error": message}}


def _success(message, outputs):
    return {"status": "SUCCESS", "output_message": message, "error_message": "", "outputs": outputs}


def _caller(job, kwargs):
    profile = kwargs.get("profile")
    if profile is None and job is not None:
        profile = job.owner
    return profile


def _portal_url():
    try:
        from portals.models import PortalConfig

        portal = PortalConfig.get_current_portal()
        url = portal.site_url if portal else None
    except Exception:
        url = None
    return (url or "").rstrip("/")


def _progress(job, message):
    if job is None:
        return
    try:
        from common.methods import set_progress

        set_progress(message)
    except Exception:
        pass


def _read_inputs():
    raw = {
        "onb_cl_collection": """{{ onb_cl_collection }}""",
        "onb_cl_item": """{{ onb_cl_item }}""",
        "onb_cl_replace": """{{ onb_cl_replace }}""",
    }
    return {
        "collection": _text(raw["onb_cl_collection"]).lower().replace("_", "-"),
        "item": _text(raw["onb_cl_item"]),
        "replace": _bool(raw["onb_cl_replace"], False),
    }


def _item_name(item):
    return item.get("display-name") or item.get("name") or item.get("label") or item.get("base_action_name") or item.get("id") or ""


def _serializer(collection):
    import importlib

    module_path, class_name = COLLECTIONS[collection]
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls()


def _describe(obj, base):
    row = {"model": type(obj).__name__}
    for attr in ("global_id", "name", "label"):
        value = getattr(obj, attr, None)
        if value:
            row[attr] = value
    try:
        row["url"] = base + obj.get_absolute_url()
    except Exception:
        pass
    return row


def run(job, *args, **kwargs):
    profile = _caller(job, kwargs)
    if profile is None:
        return _failure("No calling user profile was supplied. Run this tool through the MCP server as an authenticated user.")
    if not profile.is_cbadmin:
        return _failure("Only CloudBolt admins may install Content Library items.")

    inputs = _read_inputs()
    if inputs["collection"] not in COLLECTIONS:
        return _failure("onb_cl_collection must be one of: {}.".format(", ".join(COLLECTIONS)))

    try:
        from utilities.cb_http import export_resource, get_content_library_connection, get_exportable_content, import_resource_from_download_package
    except Exception as exc:
        return _failure("This CloudBolt version does not expose the Content Library helpers: {}".format(str(exc)[:200]))

    collection = inputs["collection"]
    _progress(job, "Reading the Content Library index for {}".format(collection))
    try:
        connection = get_content_library_connection(collection)
    except Exception as exc:
        return _failure("No Content Library connection: {}".format(str(exc)[:300]))
    try:
        index = get_exportable_content(connection, collection=collection)
    except Exception as exc:
        return _failure(
            "Could not read the Content Library: {}: {}. The appliance needs outbound HTTPS to content.cloudbolt.app "
            "(proxy under Admin > Miscellaneous Settings, inspecting CA under Admin > SSL Certificates).".format(type(exc).__name__, str(exc)[:400])
        )
    items = index.get(collection, []) if isinstance(index, dict) else list(index or [])
    items = [i for i in items if isinstance(i, dict)]
    base = _portal_url()

    if not inputs["item"]:
        rows = [{"name": _item_name(i), "id": i.get("id"), "version": i.get("version"), "description": (i.get("description") or "")[:200]} for i in items[:MAX_LIST]]
        rows.sort(key=lambda r: (r["name"] or "").lower())
        return _success(
            "{} item(s) in the {} collection.".format(len(rows), collection),
            {"collection": collection, "count": len(items), "items": rows, "libraryUrl": base + "/library/", "next": "Call again with onb_cl_item set to one of the names (or ids) to install it."},
        )

    wanted = inputs["item"].lower()
    match = next((i for i in items if _item_name(i).lower() == wanted or str(i.get("id") or "").lower() == wanted), None)
    if match is None:
        close = [_item_name(i) for i in items if wanted in _item_name(i).lower()][:15]
        return _failure("No item named '{}' in the {} collection. {}".format(
            inputs["item"], collection, "Close matches: {}.".format(", ".join(close)) if close else "Call with onb_cl_item empty to list the collection."))
    package_url = match.get("package-url") or match.get("package_url")
    if not package_url:
        return _failure("The index entry for '{}' has no package URL.".format(_item_name(match)))

    _progress(job, "Downloading {} from the Content Library".format(_item_name(match)))
    try:
        response = export_resource(connection, package_url)
    except Exception as exc:
        return _failure("Download of '{}' failed: {}: {}".format(_item_name(match), type(exc).__name__, str(exc)[:400]))
    _progress(job, "Importing {}".format(_item_name(match)))
    try:
        serializer = _serializer(collection)
        replace = inputs["replace"] or collection in ("ui-extension-packages", "cb-applets")
        resource = import_resource_from_download_package(response, serializer, replace_existing=replace)
    except Exception as exc:
        text = "{}: {}".format(type(exc).__name__, str(exc)[:600])
        hint = ""
        if "already exist" in text.lower() or "duplicate" in text.lower():
            hint = " It already exists; pass onb_cl_replace=true to refresh it."
        return _failure("Import of '{}' failed: {}.{}".format(_item_name(match), text, hint))

    imported = _describe(resource, base) if resource is not None else {"note": "The importer returned no object; check the collection's page in the UI."}
    outputs = {
        "collection": collection,
        "item": {"name": _item_name(match), "id": match.get("id"), "version": match.get("version")},
        "imported": imported,
        "libraryUrl": base + "/library/",
    }
    if collection == "ui-extension-packages":
        outputs["next"] = "UI extensions add URL routes: restart the web server (systemctl restart httpd) or wait for the next restart before opening it, then follow the extension's documentation cell."
    elif collection == "recurring-jobs":
        outputs["next"] = "Set the schedule and inputs on the recurring job, then run it once with run_recurring_job."
    elif collection in ("server-actions", "resource-actions"):
        outputs["next"] = "The action is available on servers (fetch_server_actions); set Requires approval with onb_action_approval if the test calls for it."
    elif collection == "rules":
        outputs["next"] = "Enable the rule under Actions > Rules and run Execute Rules there (test plan 4.2.3)."
    return _success("Installed '{}' from the {} collection.".format(_item_name(match), collection), outputs)

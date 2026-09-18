"""
CloudBolt "Generated Parameter Options" plugin for the `node_size` parameter.

Attach this plug-in to the `node_size` custom field (Parameter > Options >
Generated) and declare `os_build` as a REGENOPTIONS controller of `node_size`
(a FieldDependency: dependent=node_size, controlling=os_build, type=REGENOPTIONS
-- see the setup notes at the bottom). When the order form renders node_size,
CloudBolt calls:

    get_options_list(field, control_value=None, form_data=None,
                     form_prefix=None, **kwargs)

With `os_build` as the sole controller, CloudBolt re-hydrates the submitted
os_build pk into an **OSBuild instance** and passes it as `control_value`
(confirmed in platform source: api/parameter_helper_methods.py and
common/methods.py convert the os_build control value to an OSBuild before the
plugin runs). `kwargs["environment"]` is the selected Environment instance (or
None); on the classic order-form path `form_data`/`form_prefix` carry the rest
of the submitted form.

What it does
------------
1. Starts from the customer-controlled ALLOWED_NODE_SIZES allow-list -- the one
   knob a customer edits to constrain which VM sizes are ever offered.
2. Resolves the selected OS Build's Azure image and reads that image's processor
   architecture from Azure (see architecture note below).
3. Returns only the eligible allowed sizes: an Arm64 image -> the "arm" bucket;
   an x64 image -> the "amd" and "x64" buckets merged (AMD and Intel VMs both
   run on x64 images).

If no OS Build is selected yet, or the architecture can't be determined, it
returns the full allow-list so the field is never empty and orders stay
orderable (fail-open).

Architecture source
-------------------
Azure tags a VM *image* with an architecture of only `x64` or `Arm64`; there is
no "AMD" image trait (AMD-vs-Intel is a VM *size* distinction). CloudBolt does
not persist an architecture field on AzureARMImage, so architecture is read via
a **live Azure SDK lookup** that CloudBolt already wraps:
`AzureARMHandler.get_api_wrapper()._get_virtual_machine_image(publisher, offer,
sku, version, region).architecture` -> `ArchitectureTypes` value ("x64" or
"Arm64", exact casing). This is a per-image network round-trip, so results are
cached per (publisher/offer/sku/version, region) for the life of the call and
the lookup fails open. Custom/private images (no marketplace publisher/offer/
sku) fall back to a name heuristic.

Entry point: get_options_list(...) -> {"options": [(value, label)], "override": True, ...}
"""

from infrastructure.models import Environment
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

PARAM_NAME = "node_size"

# ---------------------------------------------------------------------------
# CUSTOMER-EDITABLE ALLOW-LIST
# ---------------------------------------------------------------------------
# The full set of VM sizes this environment may ever offer, bucketed by
# processor architecture. The plugin never returns a size outside these lists.
# Seeded with 4 common, low-cost example sizes per architecture. Values are
# Azure VM size names exactly as Azure expects them.
#   - "arm": Arm64 (Ampere Altra) sizes -- require an Arm64 OS image.
#   - "amd": AMD EPYC x64 sizes (the "a" suffix, e.g. D2as) -- run on x64 images.
#   - "x64": Intel x64 sizes -- run on x64 images.
ALLOWED_NODE_SIZES = {
    "arm": [
        "Standard_B2pts_v2",   # burstable, 2 vCPU  -- lowest-cost Arm64
        "Standard_D2pls_v5",   # 2 vCPU, 4 GiB
        "Standard_D2ps_v5",    # 2 vCPU, 8 GiB
        "Standard_D4pls_v5",   # 4 vCPU, 8 GiB
    ],
    "amd": [
        "Standard_B2as_v2",    # burstable, 2 vCPU  -- lowest-cost AMD
        "Standard_D2as_v5",    # 2 vCPU, 8 GiB
        "Standard_D4as_v5",    # 4 vCPU, 16 GiB
        "Standard_E2as_v5",    # 2 vCPU, 16 GiB (memory-optimized)
    ],
    "x64": [
        "Standard_B2s",        # burstable, 2 vCPU  -- lowest-cost Intel
        "Standard_D2s_v5",     # 2 vCPU, 8 GiB
        "Standard_D4s_v5",     # 4 vCPU, 16 GiB
        "Standard_E2s_v5",     # 2 vCPU, 16 GiB (memory-optimized)
    ],
}


def _resolve_environment(kwargs, form_data=None, form_prefix=None):
    """Return the selected Environment instance, or None.

    Prefers the `environment` kwarg (an Environment instance on the API/classic
    paths); falls back to reading `<form_prefix>-environment` out of the posted
    form_data, mirroring the native storage-type hook.
    """
    candidate = kwargs.get("environment")
    if isinstance(candidate, Environment):
        return candidate
    if candidate not in (None, ""):
        try:
            return Environment.objects.get(id=int(candidate))
        except (Environment.DoesNotExist, ValueError, TypeError):
            pass
    if form_data and form_prefix:
        env_id = form_data.get(f"{form_prefix}-environment")
        if isinstance(env_id, list):
            env_id = env_id[0] if env_id else None
        if env_id:
            try:
                return Environment.objects.get(id=int(env_id))
            except (Environment.DoesNotExist, ValueError, TypeError):
                pass
    return None


def _resolve_os_build(control_value, form_data=None, form_prefix=None):
    """Resolve the selected OS Build to an OSBuild instance, or None.

    With os_build as the sole REGENOPTIONS controller, `control_value` is already
    an OSBuild instance (CloudBolt re-hydrates the pk before calling us). We keep
    a defensive fallback for the classic form_data path where the raw pk appears.
    """
    from externalcontent.models import OSBuild

    if isinstance(control_value, OSBuild):
        return control_value

    candidate = control_value
    if candidate in (None, "") and form_data and form_prefix:
        candidate = form_data.get(f"{form_prefix}-os_build")
    if isinstance(candidate, (list, tuple)):
        candidate = candidate[0] if candidate else None
    if candidate in (None, ""):
        return None
    try:
        return OSBuild.objects.get(id=int(candidate))
    except (OSBuild.DoesNotExist, ValueError, TypeError):
        logger.warning("node_size options: could not resolve OS Build from %r", control_value)
        return None


def _get_azure_image(os_build, rh, env):
    """Return the AzureARMImage (OSBuildAttribute) this RH/env uses for os_build, or None."""
    try:
        osba = os_build.osba_for_resource_handler(
            rh, environment=env, region=(getattr(env, "node_location", "") or "")
        )
    except Exception as exc:  # noqa: BLE001 -- data errors must not break the form
        logger.warning("node_size options: osba lookup failed for %s: %s", os_build, exc)
        return None
    if osba is None:
        return None
    # osba_for_resource_handler returns the concrete OSBuildAttribute; for Azure
    # that is an AzureARMImage. Cast if the mixin exposes it, else use as-is.
    cast = getattr(osba, "cast", None)
    return cast() if callable(cast) else osba


# Per-call cache: (publisher, offer, sku, version, region) -> "x64" | "Arm64" | None
_ARCH_CACHE = {}


def _architecture_from_azure(rh, image):
    """Live-lookup the image architecture via CloudBolt's Azure wrapper. Fails open (None)."""
    publisher = getattr(image, "publisher", None)
    offer = getattr(image, "offer", None)
    sku = getattr(image, "sku", None)
    version = getattr(image, "version", None)
    region = getattr(image, "region", None) or getattr(rh, "location", None)

    # Marketplace lookup needs publisher/offer/sku; custom images (image_id/blob)
    # don't have these -> caller falls back to the name heuristic.
    if not (publisher and offer and sku):
        return None

    key = (publisher, offer, sku, version, region)
    if key in _ARCH_CACHE:
        return _ARCH_CACHE[key]

    arch = None
    try:
        wrapper = rh.get_api_wrapper()
        # CloudBolt wraps virtual_machine_images.get(...); the returned
        # VirtualMachineImage exposes a flat `architecture` attribute
        # (ArchitectureTypes: "x64" | "Arm64").
        getter = getattr(wrapper, "_get_virtual_machine_image", None) or getattr(
            wrapper, "get_virtual_machine_image", None
        )
        if getter is not None:
            vm_image = getter(publisher, offer, sku, version, region)
            arch = getattr(vm_image, "architecture", None)
    except Exception as exc:  # noqa: BLE001 -- fail open on any SDK/network error
        logger.warning("node_size options: architecture lookup failed for %r: %s", key, exc)
        arch = None

    _ARCH_CACHE[key] = arch
    return arch


def _detect_architecture(rh, image):
    """Return "arm" or "x64" for the image, or None if undeterminable.

    Prefers the authoritative live Azure lookup; falls back to a name heuristic
    on the image reference for custom images or when the lookup fails.
    """
    if not image:
        return None

    arch = _architecture_from_azure(rh, image)
    if arch:
        return "arm" if "arm" in str(arch).lower() else "x64"

    # Heuristic fallback (custom images / lookup failure).
    haystack = " ".join(
        str(getattr(image, attr, "") or "")
        for attr in ("publisher", "offer", "sku", "version", "template_name")
    ).lower()
    if "arm64" in haystack or "aarch64" in haystack:
        return "arm"
    if haystack.strip():
        return "x64"
    return None


def _dedupe(sizes):
    seen = []
    for size in sizes:
        if size not in seen:
            seen.append(size)
    return seen


def _all_allowed_sizes():
    return sorted(_dedupe(s for sizes in ALLOWED_NODE_SIZES.values() for s in sizes))


def _sizes_for_architecture(architecture):
    """Allowed sizes eligible for the detected image architecture.

    Arm64 image -> "arm" bucket. x64 image -> "amd" + "x64" merged (AMD and Intel
    VMs both run x64 images). None -> full allow-list (fail-open).
    """
    if architecture == "arm":
        return list(ALLOWED_NODE_SIZES.get("arm", []))
    if architecture == "x64":
        return _dedupe(list(ALLOWED_NODE_SIZES.get("amd", [])) + list(ALLOWED_NODE_SIZES.get("x64", [])))
    return _all_allowed_sizes()


def _as_result(sizes):
    """Return CloudBolt's rich options dict. override=True makes our list authoritative."""
    options = [(size, size) for size in sizes]
    return {
        "options": options,
        "override": True,
        "initial_value": options[0] if options else "",
    }


def get_options_list(field, control_value=None, form_data=None, form_prefix=None, **kwargs):
    """Generate architecture-matched node-size options from ALLOWED_NODE_SIZES."""
    env = _resolve_environment(kwargs, form_data, form_prefix)
    os_build = _resolve_os_build(control_value, form_data, form_prefix)

    # No OS Build selected yet -> offer the full allow-list (fail-open).
    if os_build is None:
        return _as_result(_all_allowed_sizes())

    image = None
    if env is not None and getattr(env, "resource_handler", None):
        rh = env.resource_handler.cast()
        image = _get_azure_image(os_build, rh, env)
        architecture = _detect_architecture(rh, image)
    else:
        architecture = None

    if architecture is None:
        logger.info(
            "node_size options: architecture undetermined for OS Build %s; offering all allowed sizes.",
            os_build,
        )
        return _as_result(_all_allowed_sizes())

    sizes = _sizes_for_architecture(architecture)
    if not sizes:
        return {"options": [("", f"------ No allowed {architecture} sizes configured ------")],
                "override": True, "initial_value": ""}
    return _as_result(sizes)


# ---------------------------------------------------------------------------
# SETUP NOTES (wiring os_build -> node_size)
# ---------------------------------------------------------------------------
# For `control_value` to arrive as the selected OSBuild, add a REGENOPTIONS
# FieldDependency on the (classic) provision-server order form:
#   dependent-field = node_size, controlling-field = os_build,
#   dependency-type = REGENOPTIONS.
# CloudBolt renders os_build as a CF-backed choice field
# (add_os_build_cf_choice_field_if_needed) so it participates in the dependency
# graph and the regen JS re-runs this plugin whenever os_build changes. Without
# the dependency, control_value is empty and the plugin degrades to the full
# ALLOWED_NODE_SIZES union. See docs/agents/metadata-schemas.md -> "Parameter
# dependencies (field_dependency_*_set)".

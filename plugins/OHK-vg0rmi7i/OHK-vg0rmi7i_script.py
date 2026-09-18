"""
Azure Resource Manager Rate Hook — "Compute Server Rate" hook point.

Estimates the cost of an Azure VM and its associated resources for the order /
blueprint cost preview, using the customer's NEGOTIATED prices when available and
falling back to public RETAIL (list) prices otherwise. All pricing logic lives in
the `azure_pricing` shared module; this hook only resolves the order inputs and
assembles the rate_dict.

Source strategy (see shared_modules/azure_pricing):
  - EA / MCA: negotiated unit prices from the Azure Price Sheet, downloaded and
    cached on disk by the "Azure Price Sheet Refresh" recurring job (the handler
    service principal must hold a billing-scope role). Keyed by meterId.
  - CSP / Partner, or EA/MCA with no usable sheet: public Azure Retail Prices API
    (the same data behind the Azure Pricing Calculator).
  - Neither reachable: fall back to CloudBolt's default_compute_rate.

rate_dict contract (CloudBolt costs.utils.validate_rate / RateBreakdown):
  - Only "Hardware", "Software", "Extra" survive; values must be Decimal; nested
    {label: Decimal} is summed at any depth; labels are free-form display text.
  - The engine does NOT apply quantity or the time unit — the hook bakes both in.
  We populate:
    Hardware -> Node Cost, OS Disk, Data Disk N, Public IP
    Software -> RHEL/SLES License (separate per-vCPU Azure meter; not on AHB/BYOS)
  and keep default_compute_rate's "Software"->"Applications" and "Extra"
  (admin-configured) rates. We drop the default "Software"->"OS Build" line so an
  admin OS-build rate does not double-count the Azure-sourced OS license. Windows
  licensing is bundled in the Windows compute meter (Hardware), unless AHB.

A line that falls back to list price while negotiated data was expected is
labelled "… (list price)" so the basis is visible in the cost tooltip.
"""
import re

from decimal import Decimal

from costs.utils import default_compute_rate
from resourcehandlers.azure_arm.models import AzureARMHandler, AzureARMImage
from utilities.logger import ThreadLogger
from utilities.models import GlobalPreferences

from shared_modules.azure_pricing import get_vm_cost_components

logger = ThreadLogger(__name__)

# Data-disk order params follow the convention disk_<N>_size (disk_1_size, ...),
# all priced at the single storage_account_type_arm selection (as is the OS disk).
_DATA_DISK_RE = re.compile(r"^disk_(\d+)_size$")


def _truthy(value):
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _region_from_environment(environment, cfvs):
    """Resolve the ARM region name (e.g. 'eastus') from the environment's
    node_location, falling back to a node_location order CFV. CloudBolt may
    store the display name ('East US'); normalize to the ARM form."""
    raw = None
    try:
        raw = environment.get_cfv_for_custom_field("node_location")
        raw = getattr(raw, "value", raw)
    except Exception:
        raw = None
    if not raw:
        raw = getattr(environment, "node_location", None)
    if not raw:
        for cfv in cfvs:
            if cfv.field.name == "node_location" and cfv.value:
                raw = cfv.value
                break
    if not raw:
        return None
    return str(raw).strip().lower().replace(" ", "")


# Heuristics for finding a data disk inside a parameter-collection value set.
_SIZE_KEY_RE = re.compile(r"(disk.*size|size.*disk|^size$|disk_size)", re.IGNORECASE)
_TYPE_KEY_RE = re.compile(r"(storage_account_type|disk_type|storage_type)", re.IGNORECASE)
_SECRET_KEY_RE = re.compile(r"(password|secret|key|token|credential)", re.IGNORECASE)


def _cfv_pairs(cfvs):
    """Yield (name, value) for a list of CustomFieldValue objects, defensively."""
    for cfv in cfvs or []:
        try:
            yield cfv.field.name, cfv.value
        except Exception:
            continue


def _pcvs_field_maps(pcvss):
    """Yield a {name: value} dict for each parameter-collection value set. Data
    disks are commonly modeled as repeating pcvss rather than flat CFVs, so each
    pcvs is one repeating group (e.g. one data disk)."""
    for pcvs in pcvss or []:
        inner = getattr(pcvs, "cfvs", None)
        if inner is None:
            inner = getattr(pcvs, "custom_field_values", None)
        try:
            seq = inner.all() if hasattr(inner, "all") else inner
        except Exception:
            seq = None
        yield {name: value for name, value in (_cfv_pairs(seq) if seq else [])}


def _sanitize(fields):
    """Redact secret-looking values before logging a field map."""
    return {k: ("***" if _SECRET_KEY_RE.search(k) else v) for k, v in fields.items()}


def _find_disk_in_map(fields):
    """From a pcvs field map, return (size_gb, type_override) if it looks like a
    data disk, else (None, None). Conservative: requires a numeric size AND a
    disk-ish signal (a storage-type field, or any key containing 'disk')."""
    size = None
    for name, value in fields.items():
        if _SIZE_KEY_RE.search(name):
            candidate = _int(value)
            if candidate:
                size = candidate
                break
    dtype = None
    for name, value in fields.items():
        if _TYPE_KEY_RE.search(name) and value:
            dtype = value
            break
    disk_like = bool(dtype) or any("disk" in n.lower() for n in fields)
    return (size if (size and disk_like) else None), dtype


def _collect_inputs(cfvs, pcvss):
    """Pull VM-shaping inputs from the order CFVs and parameter-collection value
    sets. Returns raw pieces (the caller assembles the disk list so it can resolve
    os_disk_size==0 to the image default):
        (node_size, disk_type, os_disk_size, disk_size_total, data_disks,
         has_public_ip, ahb)
    disk_size_total is CloudBolt's `disk_size` — the SUM of all DATA disk sizes
    (OS disk excluded). data_disks (from flat disk_<N>_size CFVs or pcvss) is only
    a fallback for blueprints that don't populate `disk_size`.
    """
    node_size = disk_type = os_disk_size = None
    disk_size_total = None  # CloudBolt's `disk_size`: SUM of all DATA disk sizes
    flat_disk_n = {}        # N -> size_gb from flat disk_<N>_size CFVs (fallback)
    data_disks = []         # fallback per-disk list of (size_gb, type_override)
    has_public_ip = ahb = False

    for name, value in _cfv_pairs(cfvs):
        if name == "node_size" and value:
            node_size = value
        elif name == "storage_account_type_arm" and value:
            disk_type = value
        elif name == "os_disk_size_arm" and value not in (None, ""):
            os_disk_size = _int(value)
        elif name == "disk_size" and value not in (None, ""):
            # CloudBolt aggregates all DATA disk sizes into `disk_size` (the OS
            # disk is NOT included — it's `os_disk_size_arm`).
            disk_size_total = _int(value)
        elif "public_ip" in name:
            has_public_ip = has_public_ip or _truthy(value)
        elif "hybrid_benefit" in name or name in ("ahb", "azure_hybrid_benefit"):
            ahb = ahb or _truthy(value)
        else:
            match = _DATA_DISK_RE.match(name)
            if match and _int(value):
                flat_disk_n[int(match.group(1))] = _int(value)
    for n in sorted(flat_disk_n):
        data_disks.append((flat_disk_n[n], None))

    # Fallback only: some blueprints model data disks as repeating pcvss.
    pcvs_dumps = []
    for fields in _pcvs_field_maps(pcvss):
        pcvs_dumps.append(fields)
        size, dtype = _find_disk_in_map(fields)
        if size:
            data_disks.append((size, dtype))

    # --- Diagnostics (grep azure_pricing).
    logger.info(
        f"[azure_pricing] rate hook inputs: node_size={node_size!r}, "
        f"storage_account_type_arm={disk_type!r}, os_disk_size_arm={os_disk_size!r}, "
        f"disk_size(total data disks)={disk_size_total!r}, "
        f"flat_disk_N_size={dict(sorted(flat_disk_n.items()))}, "
        f"pcvss_disks={len(data_disks) - len(flat_disk_n)}, "
        f"public_ip={has_public_ip}, ahb={ahb}")
    if pcvs_dumps:
        logger.info(f"[azure_pricing] rate hook: pcvss field maps="
                    f"{[_sanitize(f) for f in pcvs_dumps]}")
    return (node_size, disk_type, os_disk_size, disk_size_total, data_disks,
            has_public_ip, ahb)


# Marketplace base OS-disk sizes, used only when os_disk_size_arm is 0 ('keep base
# disk') AND CloudBolt has no recorded template size. Most Azure Linux marketplace
# images ship a 30 GiB OS disk; Windows Server images 127 GiB.
_DEFAULT_OS_DISK_GB = {"windows": 127, "linux": 30}


def _image_total_disk_size(rh, os_build, environment):
    """The template/base OS disk size (GB) recorded on the OS build's Azure image
    (AzureARMImage.total_disk_size), or None if not recorded.

    IMPORTANT: CloudBolt FOLDS this template size into the aggregate `disk_size`
    field, so we use it both to size a 'keep base disk' OS disk and to subtract the
    template contribution out of `disk_size` (otherwise the OS disk is counted
    twice). Uses the RH's env-aware lookup, then the by-id image lookup as a
    fallback (the image shares the OS build's id)."""
    image = None
    try:
        image = rh.get_osba_for_osb_and_env(os_build, environment, raise_on_none=False)
    except Exception as exc:
        logger.warning(f"[azure_pricing] rate hook: get_osba raised: {exc!r}")
    if image is None or _int(getattr(image, "total_disk_size", None)) is None:
        try:
            image = AzureARMImage.objects.filter(id=os_build.id).first() or image
        except Exception as exc:
            logger.warning(f"[azure_pricing] rate hook: AzureARMImage by-id lookup "
                           f"raised: {exc!r}")
    size = _int(getattr(image, "total_disk_size", None)) if image is not None else None
    logger.info(f"[azure_pricing] rate hook: template total_disk_size={size!r} "
                f"(image {'found' if image is not None else 'not found'}).")
    return size


def compute_rate(group, environment, resource_technology, cfvs, pcvss, os_build,
                 apps, quantity=1, **kwargs):
    """
    Find a rate for the selected VM and return a rate_dict. Returns the default
    rate_dict unchanged whenever Azure pricing cannot be determined, so cost
    previews degrade gracefully rather than showing zero.
    """
    # Start from the default so admin-configured Software/Extra rates flow
    # through. override_defaults is test-only (absent in production); when set,
    # start from an empty dict and return only Azure-sourced rates.
    logger.debug(f'[azure_pricing] cfvs={cfvs}, pcvss={pcvss}, '
                 f'os_build={os_build}, apps={apps}, kwargs={kwargs}')
    override_defaults = kwargs.pop("override_defaults", False)
    if override_defaults:
        default_dict = {}
    else:
        default_dict = default_compute_rate(
            group, environment, resource_technology, cfvs, pcvss, os_build, apps,
            quantity, **kwargs,
        )

    node_size, disk_type, os_disk_size, disk_size_total, data_disks, \
        has_public_ip, ahb = _collect_inputs(cfvs, pcvss)
    if not node_size:
        logger.warning("[azure_pricing] rate hook: no node_size on the order; "
                       "returning default rate.")
        return default_dict

    region = _region_from_environment(environment, cfvs)
    if not region:
        logger.warning("[azure_pricing] rate hook: could not resolve an ARM region; "
                       "returning default rate.")
        return default_dict

    rh = environment.resource_handler
    if not rh:
        rh = AzureARMHandler.objects.first()
    if not rh:
        return default_dict
    rh = rh.cast()

    try:
        is_windows = os_build.os_family.name == "Windows"
    except AttributeError:
        is_windows = False

    # Template/base OS disk size recorded on the image. CloudBolt folds this into
    # the aggregate `disk_size`, so we (a) size a 'keep base disk' OS disk with it
    # and (b) subtract it from `disk_size` so the OS disk isn't counted twice.
    template_disk = _image_total_disk_size(rh, os_build, environment)

    # OS disk: rely on os_disk_size_arm; 0/unset means "keep base disk" = template
    # size (marketplace-base default only if the template size is unknown).
    if os_disk_size:
        effective_os_disk = os_disk_size
    elif template_disk:
        effective_os_disk = template_disk
    else:
        effective_os_disk = _DEFAULT_OS_DISK_GB["windows" if is_windows else "linux"]
        logger.info(f"[azure_pricing] rate hook: os_disk_size_arm=0 and no template "
                    f"size; OS disk defaulted to {effective_os_disk}GB.")

    # Data disks = `disk_size` aggregate MINUS the template OS disk CloudBolt folds
    # into it (avoids double-counting the OS disk).
    data_total = None
    if disk_size_total:
        if template_disk:
            data_total = disk_size_total - template_disk
            logger.info(f"[azure_pricing] rate hook: data disks = disk_size "
                        f"{disk_size_total} - template {template_disk} = {data_total}.")
        else:
            data_total = disk_size_total
            logger.warning(f"[azure_pricing] rate hook: disk_size={disk_size_total} but "
                           f"template size unknown — not subtracting; OS disk may be "
                           f"double-counted.")
        if data_total is not None and data_total <= 0:
            data_total = None  # disk_size was just the template; no data disks

    # Assemble lines. All use the global storage_account_type_arm unless a fallback
    # per-disk field carried its own type. Data disks priced as one aggregate line
    # (exact for per-GiB types; approximate for tiered — per-disk split unknown).
    disks = []
    if not disk_type:
        logger.warning("[azure_pricing] rate hook: storage_account_type_arm absent; "
                       "no disks priced (compute still prices).")
    else:
        if effective_os_disk:
            disks.append((disk_type, effective_os_disk, "OS Disk"))
        if data_total:
            disks.append((disk_type, data_total, "Data Disks"))
        elif not disk_size_total:
            # No aggregate at all — fall back to any per-disk fields.
            for i, (size, type_override) in enumerate(data_disks, start=1):
                dtype = type_override or disk_type
                if dtype and size:
                    disks.append((dtype, size, f"Data Disk {i}"))
    logger.info(f"[azure_pricing] rate hook: disk lines to price="
                f"{[(d[2], d[0], d[1]) for d in disks]}")

    rate_time_unit = GlobalPreferences.get().rate_time_unit

    try:
        components = get_vm_cost_components(
            rh=rh,
            region=region,
            arm_sku=node_size,
            is_windows=is_windows,
            os_build=os_build,
            disks=disks,
            has_public_ip=has_public_ip,
            ahb=ahb,
            quantity=quantity,
            rate_time_unit=rate_time_unit,
        )
    except Exception as exc:
        logger.exception(f"[azure_pricing] rate hook: pricing engine failed: "
                         f"{exc}; returning default rate.")
        return default_dict

    basis = components.get("basis")
    agreement = components.get("agreement_type") or "unknown agreement"
    if basis == "none":
        logger.info(f"[azure_pricing] rate hook: ROUTE=default_compute_rate — no "
                    f"negotiated or retail price available for {node_size} in "
                    f"{region}; returning CloudBolt default rate.")
        return default_dict

    if basis == "negotiated":
        logger.info(f"[azure_pricing] rate hook: ROUTE=negotiated ({agreement}) — "
                    f"priced {node_size} in {region} from the cached Azure Price "
                    f"Sheet.")
    elif basis == "mixed":
        logger.info(f"[azure_pricing] rate hook: ROUTE=negotiated+retail "
                    f"({agreement}) — priced {node_size} in {region} from the Azure "
                    f"Price Sheet with public retail (list price) fallback for some "
                    f"meters.")
    else:  # "list"
        logger.info(f"[azure_pricing] rate hook: ROUTE=retail (list price) — priced "
                    f"{node_size} in {region} from the public Azure Retail Prices "
                    f"API (no negotiated sheet for this subscription).")

    # Override Hardware entirely with Azure-sourced values (so we never show
    # CloudBolt's default CPU/disk rates alongside Azure's).
    rate_dict = dict(default_dict)
    rate_dict["Hardware"] = {k: Decimal(v) for k, v in components["hardware"].items()}

    # Merge OS license into Software, dropping the default "OS Build" line to
    # avoid double-counting an admin OS-build rate with the Azure license meter.
    software = dict(default_dict.get("Software") or {})
    software.pop("OS Build", None)
    for label, value in components["software"].items():
        software[label] = Decimal(value)
    rate_dict["Software"] = software

    return rate_dict

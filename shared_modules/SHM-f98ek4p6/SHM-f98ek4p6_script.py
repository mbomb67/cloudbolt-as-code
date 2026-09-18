"""
Shared module for importing VM disk images and metadata into OpenShift
Virtualization (KubeVirt).

Reads a manifest directory produced by
``vmware_export.cold_export_via_nfc`` and:

1. Uploads each QCOW2 disk image to a DataVolume via the CDI upload
   proxy REST API.
2. Constructs a KubeVirt ``VirtualMachine`` resource from the manifest
   metadata (CPU, memory, firmware, NICs, controllers, tags).
3. Creates the VM via the Kubernetes API.

Connection can be established via a CloudBolt ``OVirtHandler`` resource
handler or by supplying an OpenShift API URL and bearer token directly.

Usage::

    from shared_modules.openshift_import import OpenShiftImporter

    # Option A -- from a CloudBolt OVirtHandler
    importer = OpenShiftImporter.from_handler(handler, namespace="my-project")

    # Option B -- standalone
    importer = OpenShiftImporter(
        api_url="https://api.ocp.example.com:6443",
        token="sha256~...",
        namespace="my-project",
        storage_class="ocs-storagecluster-ceph-rbd",
    )

    result = importer.import_vm("/var/tmp/my-vm")
"""

import json
import math
import os
import subprocess
import time

import requests
import urllib3
from common.methods import set_progress
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

MANIFEST_FILE = "manifest.json"
UPLOAD_CHUNK_SIZE = 64 * 1024 * 1024  # 64 MiB
DV_UPLOAD_READY_TIMEOUT = 300
DV_SUCCEEDED_TIMEOUT = 1800
VM_RUNNING_TIMEOUT = 600
POLL_INTERVAL = 5

CDI_PROXY_ROUTE_NAMESPACES = [
    "openshift-cnv",
    "cdi",
]
CDI_PROXY_ROUTE_NAMES = [
    "cdi-uploadproxy",
    "cdi-uploadproxy-openshift-cnv",
]

K8S_VM_API = "apis/kubevirt.io/v1"
# The start/stop/restart subresources live in a SEPARATE API group from the
# VirtualMachine object and require their own RBAC grant.
K8S_VM_SUBRESOURCE_API = "apis/subresources.kubevirt.io/v1"
K8S_CDI_API = "apis/cdi.kubevirt.io/v1beta1"
K8S_CDI_UPLOAD_API = "apis/upload.cdi.kubevirt.io/v1beta1"
K8S_ROUTE_API = "apis/route.openshift.io/v1"


class OpenShiftImportError(Exception):
    """Raised when a VM import operation fails."""


class OpenShiftImporter:
    """Import VM disks and configuration into OpenShift Virtualization.

    Parameters
    ----------
    api_url : str
        OpenShift / Kubernetes API base URL
        (e.g. ``https://api.ocp.example.com:6443``).
    token : str
        Bearer token for API authentication.
    namespace : str
        Target namespace / project for the VM and its DataVolumes.
    storage_class : str or None
        StorageClass name for DataVolume PVCs.  ``None`` uses the cluster
        default.
    access_mode : str or None
        PVC access mode.  ``None`` (default) omits it so CDI infers the mode
        from the StorageClass's StorageProfile -- which selects
        ``ReadWriteMany`` on backends that support it, making the VM
        live-migratable.  Pass an explicit value to force one (e.g.
        ``"ReadWriteOnce"``).
    volume_mode : str or None
        PVC volume mode (default ``Block`` -- recommended for KubeVirt).
        ``None`` lets CDI infer it from the StorageProfile too.
    cdi_proxy_url : str or None
        CDI upload proxy URL.  Auto-discovered from the cluster if
        ``None``.
    verify_ssl : bool
        Whether to verify TLS certificates (default ``False``).
    """

    def __init__(
        self,
        api_url,
        token,
        namespace,
        storage_class=None,
        access_mode=None,
        volume_mode="Block",
        cdi_proxy_url=None,
        verify_ssl=False,
    ):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.namespace = namespace
        self.storage_class = storage_class
        self.access_mode = access_mode
        self.volume_mode = volume_mode
        self.cdi_proxy_url = cdi_proxy_url
        self.verify_ssl = verify_ssl

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        })
        self._session.verify = self.verify_ssl
        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_handler(cls, handler, namespace, **kwargs):
        """Create an importer from a CloudBolt ``OVirtHandler``.

        Extracts the API URL and obtains an OAuth token from the
        handler's wrapper client.

        Parameters
        ----------
        handler : OVirtHandler
            A cast ``OVirtHandler`` instance.
        namespace : str
            Target OpenShift namespace / project.
        **kwargs
            Forwarded to ``__init__`` (``storage_class``, etc.).
        """
        wrapper = handler.get_api_wrapper()
        client = wrapper.client

        api_url = getattr(client, "openshift_base_api_url", None)
        if not api_url:
            protocol = getattr(handler, "protocol", "https")
            port = getattr(handler, "port", 6443)
            api_url = f"{protocol}://{handler.ip}:{port}"

        token = client.get_oauth_token()
        if isinstance(token, dict):
            token = token.get("access_token", token)

        verify_ssl = kwargs.pop("verify_ssl", False)

        return cls(
            api_url=api_url,
            token=token,
            namespace=namespace,
            verify_ssl=verify_ssl,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Kubernetes API helper
    # ------------------------------------------------------------------

    def _api_request(self, method, path, **kwargs):
        """Execute an API request against the OpenShift / K8s API.

        Returns the parsed JSON response body.  Raises
        ``OpenShiftImportError`` on non-2xx status codes unless
        ``raise_for_status=False`` is passed.
        """
        url = f"{self.api_url}/{path.lstrip('/')}"
        raise_for = kwargs.pop("raise_for_status", True)
        resp = self._session.request(method, url, **kwargs)
        if raise_for and not resp.ok:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("message", resp.text[:500])
            except Exception:
                detail = resp.text[:500]
            raise OpenShiftImportError(
                f"{method.upper()} {url} failed ({resp.status_code}): {detail}"
            )
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    # ------------------------------------------------------------------
    # CDI upload proxy discovery
    # ------------------------------------------------------------------

    def _discover_cdi_proxy(self):
        """Auto-discover the CDI upload proxy URL from cluster routes.

        Sets ``self.cdi_proxy_url`` if found.  Raises
        ``OpenShiftImportError`` if discovery fails and no URL was
        provided.
        """
        if self.cdi_proxy_url:
            return

        for ns in CDI_PROXY_ROUTE_NAMESPACES:
            for name in CDI_PROXY_ROUTE_NAMES:
                path = f"{K8S_ROUTE_API}/namespaces/{ns}/routes/{name}"
                try:
                    route = self._api_request("GET", path, raise_for_status=False)
                    if isinstance(route, dict) and "spec" in route:
                        host = route["spec"].get("host")
                        if host:
                            tls = route["spec"].get("tls")
                            scheme = "https" if tls else "http"
                            self.cdi_proxy_url = f"{scheme}://{host}"
                            logger.info(
                                "Discovered CDI upload proxy: %s",
                                self.cdi_proxy_url,
                            )
                            return
                except Exception:
                    continue

        raise OpenShiftImportError(
            "Could not auto-discover CDI upload proxy URL.  "
            "Pass cdi_proxy_url explicitly."
        )

    # ------------------------------------------------------------------
    # DataVolume lifecycle
    # ------------------------------------------------------------------

    def _datavolume_exists(self, name):
        """Return the DataVolume dict if it exists, else ``None``."""
        path = (
            f"{K8S_CDI_API}/namespaces/{self.namespace}/datavolumes/{name}"
        )
        resp = self._session.get(f"{self.api_url}/{path}")
        if resp.status_code == 404:
            return None
        if resp.ok:
            return resp.json()
        return None

    def _create_datavolume(self, name, size_bytes):
        """Create a DataVolume with ``upload`` source."""
        # CDI requires the PVC to be LARGER than the uncompressed virtual size
        # (its check is availableSize < virtualSize), so a break-even request
        # fails. Add a small headroom over the rounded-up virtual size.
        size_gi = math.ceil(size_bytes / (1024 ** 3)) + 2
        size_str = f"{size_gi}Gi"

        dv_body = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "annotations": {
                    "cdi.kubevirt.io/storage.usePopulator": "false",
                    "cdi.kubevirt.io/storage.bind.immediate.requested": "true",
                },
            },
            "spec": {
                "source": {"upload": {}},
                "storage": {
                    "resources": {
                        "requests": {"storage": size_str},
                    },
                },
            },
        }
        storage = dv_body["spec"]["storage"]
        # Leaving accessModes unset lets CDI fill it from the StorageClass's
        # StorageProfile, which selects ReadWriteMany on backends that support
        # it -- the requirement for a live-migratable VM. Only pin a mode when
        # one was explicitly requested.
        if self.access_mode:
            storage["accessModes"] = [self.access_mode]
        if self.volume_mode:
            storage["volumeMode"] = self.volume_mode
        if self.storage_class:
            storage["storageClassName"] = self.storage_class

        path = f"{K8S_CDI_API}/namespaces/{self.namespace}/datavolumes"
        logger.info("Creating DataVolume %s/%s (%s)", self.namespace, name, size_str)
        return self._api_request("POST", path, json=dv_body)

    def _wait_dv_phase(self, name, target_phase, timeout=DV_UPLOAD_READY_TIMEOUT):
        """Poll a DataVolume until it reaches *target_phase*.

        Returns the final DataVolume dict.  Raises on timeout.
        """
        path = (
            f"{K8S_CDI_API}/namespaces/{self.namespace}/datavolumes/{name}"
        )
        deadline = time.time() + timeout
        last_phase = None
        while time.time() < deadline:
            dv = self._api_request("GET", path)
            phase = dv.get("status", {}).get("phase", "Unknown")
            if phase != last_phase:
                logger.info("DataVolume %s phase: %s", name, phase)
                last_phase = phase
            if phase == target_phase:
                return dv
            if phase in ("Failed",):
                conditions = dv.get("status", {}).get("conditions", [])
                msg = next(
                    (c.get("message", "") for c in conditions if c.get("type") == "Ready"),
                    str(conditions),
                )
                raise OpenShiftImportError(
                    f"DataVolume {name} failed: {msg}"
                )
            time.sleep(POLL_INTERVAL)

        raise OpenShiftImportError(
            f"DataVolume {name} did not reach phase {target_phase!r} "
            f"within {timeout}s (last phase: {last_phase})"
        )

    # ------------------------------------------------------------------
    # CDI upload token + streaming upload
    # ------------------------------------------------------------------

    def _request_upload_token(self, pvc_name):
        """Request a CDI upload token for the given PVC.

        Returns the token string.
        """
        token_name = f"{pvc_name}-upload-token"
        token_body = {
            "apiVersion": "upload.cdi.kubevirt.io/v1beta1",
            "kind": "UploadTokenRequest",
            "metadata": {
                "name": token_name,
                "namespace": self.namespace,
            },
            "spec": {
                "pvcName": pvc_name,
            },
        }
        path = (
            f"{K8S_CDI_UPLOAD_API}/namespaces/{self.namespace}"
            f"/uploadtokenrequests"
        )
        result = self._api_request("POST", path, json=token_body)
        upload_token = result.get("status", {}).get("token")
        if not upload_token:
            raise OpenShiftImportError(
                f"UploadTokenRequest for {pvc_name} did not return a token: "
                f"{json.dumps(result, indent=2)}"
            )
        return upload_token

    def _stream_upload(self, upload_token, qcow2_path):
        """Stream a QCOW2 file to the CDI upload proxy."""
        file_size = os.path.getsize(qcow2_path)
        logger.info(
            "Uploading %s (%.1f GB) to CDI proxy %s",
            qcow2_path,
            file_size / (1024 ** 3),
            self.cdi_proxy_url,
        )
        upload_url = f"{self.cdi_proxy_url}/v1beta1/upload"

        def _file_reader():
            uploaded = 0
            with open(qcow2_path, "rb") as f:
                while True:
                    chunk = f.read(UPLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    uploaded += len(chunk)
                    if file_size > 0:
                        pct = uploaded / file_size * 100
                        if pct % 10 < (UPLOAD_CHUNK_SIZE / file_size * 100):
                            set_progress("Upload progress: %.0f%%" % pct)
                    yield chunk

        resp = requests.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {upload_token}",
                "Content-Type": "application/octet-stream",
            },
            data=_file_reader(),
            verify=self.verify_ssl,
        )
        if not resp.ok:
            raise OpenShiftImportError(
                f"CDI upload failed ({resp.status_code}): {resp.text[:500]}"
            )
        logger.info("CDI upload completed successfully")

    # ------------------------------------------------------------------
    # High-level disk upload
    # ------------------------------------------------------------------

    def _upload_disk(self, qcow2_path, dv_name, size_bytes):
        """Upload a single QCOW2 disk image to a DataVolume.

        Handles the full lifecycle: create DV -> wait UploadReady ->
        get token -> stream upload -> wait Succeeded.

        Idempotent: skips upload if the DataVolume already exists and
        has phase ``Succeeded``.
        """
        existing = self._datavolume_exists(dv_name)
        if existing:
            phase = existing.get("status", {}).get("phase", "")
            if phase == "Succeeded":
                logger.info(
                    "DataVolume %s already exists (phase Succeeded) -- skipping upload",
                    dv_name,
                )
                return
            logger.warning(
                "DataVolume %s exists with phase %s -- will attempt upload",
                dv_name,
                phase,
            )
        else:
            self._create_datavolume(dv_name, size_bytes)

        self._wait_dv_phase(dv_name, "UploadReady", timeout=DV_UPLOAD_READY_TIMEOUT)

        upload_token = self._request_upload_token(dv_name)
        self._stream_upload(upload_token, qcow2_path)

        self._wait_dv_phase(dv_name, "Succeeded", timeout=DV_SUCCEEDED_TIMEOUT)
        logger.info("Disk %s uploaded and ready", dv_name)

    # ------------------------------------------------------------------
    # VirtualMachine spec builder
    # ------------------------------------------------------------------

    def _build_vm_spec(
        self,
        manifest,
        vm_name,
        dv_names,
        network_map=None,
        cpu_count=None,
        memory_mb=None,
        run_strategy="Manual",
        disk_bus="virtio",
    ):
        """Construct a KubeVirt VirtualMachine resource dict.

        Parameters
        ----------
        manifest : dict
            Parsed manifest.json.
        vm_name : str
            Name for the VirtualMachine resource.
        dv_names : dict
            Mapping of ``disk_key -> datavolume_name``.
        network_map : dict or None
            Mapping of source network name to OpenShift network. Each
            value is either ``None`` (use pod network) or a string in
            the form ``"namespace/nad-name"`` or just ``"nad-name"``
            (uses VM namespace).
        cpu_count : int or None
            Override CPU count (default from manifest).
        memory_mb : int or None
            Override memory in MB (default from manifest).
        run_strategy : str
            KubeVirt RunStrategy.
        disk_bus : str
            Disk bus type.  ``"virtio"`` (default) is correct because virt-v2v
            installs the virtio-blk driver during conversion.  Use ``"sata"``
            only for a guest that was NOT converted (raw copy) and lacks
            virtio drivers.
        """
        vm_meta = manifest.get("vm", {})

        cpus = cpu_count or vm_meta.get("num_cpus", 2)
        cores_per_socket = vm_meta.get("cores_per_socket", 1)
        mem = memory_mb or vm_meta.get("memory_mb", 2048)
        firmware = vm_meta.get("firmware", "bios")

        # -- Disks and volumes ----------------------------------------
        disks = []
        volumes = []
        # Boot order goes on the boot disk ONLY. Marking multiple disks with a
        # bootOrder makes firmware try each in turn; pointing that at a
        # non-bootable data disk can turn a clean failure into a boot loop.
        # Disks iterate in sorted key order, so position 0 is the boot disk.
        for position, disk_key in enumerate(sorted(dv_names.keys(), key=int)):
            dv_name = dv_names[disk_key]
            # Name the in-VM disk/volume after its PVC ("<vm>-disk<N>"), NOT the
            # VMware device label: that label is the NFC lease device key
            # (e.g. ".../ParaVirtualSCSIController0:0"), which sanitises to noise
            # like "vmXparavirtualscsicontroller00". Reusing dv_name keeps the
            # in-VM disk name and its backing PVC identical in the console.
            vol_name = dv_name

            disk_entry = {
                "name": vol_name,
                "disk": {
                    "bus": disk_bus,
                },
            }
            if position == 0:
                disk_entry["bootOrder"] = 1

            disks.append(disk_entry)
            volumes.append({
                "name": vol_name,
                "persistentVolumeClaim": {
                    "claimName": dv_name,
                },
            })

        # Cloud-init is intentionally omitted for imported VMs that
        # already have a configured OS.  Add it post-import if needed.

        # -- Network interfaces and networks --------------------------
        # KubeVirt admission rejects more than one interface on the pod network,
        # so at most one source NIC can map to it; additional NICs MUST map to a
        # Multus NAD (via network_map). We fail loudly rather than silently
        # dropping a NIC -- shipping a multi-NIC VM as single-NIC is data-loss-
        # grade. NIC model is virtio (paravirtualised): virt-v2v installs the
        # virtio-net driver during conversion, so the guest uses the fast virtio
        # device instead of an emulated one. The source MAC is preserved on
        # bridge/multus (L2) interfaces, where it is meaningful; on a masqueraded
        # pod interface the guest sits behind NAT so a preserved MAC buys nothing.
        nics_meta = vm_meta.get("nics", [])
        interfaces = []
        networks = []
        pod_network_used = False

        if not nics_meta:
            interfaces.append({
                "name": "default",
                "model": "virtio",
                "masquerade": {},
            })
            networks.append({"name": "default", "pod": {}})
        else:
            for idx, nic in enumerate(nics_meta):
                nic_name = f"nic-{idx}"
                src_network = nic.get("network_name", "")
                mac = nic.get("mac_address")

                target = None
                if network_map and src_network in network_map:
                    target = network_map[src_network]

                if target is None:
                    if pod_network_used:
                        raise OpenShiftImportError(
                            "VM {} has more than one NIC destined for the pod "
                            "network, which KubeVirt does not allow. Map the "
                            "extra source network {!r} to a Multus "
                            "NetworkAttachmentDefinition via network_map, or "
                            "explicitly drop it.".format(vm_name, src_network)
                        )
                    interfaces.append({
                        "name": nic_name,
                        "model": "virtio",
                        "masquerade": {},
                    })
                    networks.append({"name": nic_name, "pod": {}})
                    pod_network_used = True
                else:
                    nad_ns, nad_name = _parse_nad_ref(target, self.namespace)
                    iface = {
                        "name": nic_name,
                        "model": "virtio",
                        "bridge": {},
                    }
                    if mac:
                        iface["macAddress"] = mac
                    interfaces.append(iface)
                    networks.append({
                        "name": nic_name,
                        "multus": {
                            "networkName": (
                                f"{nad_ns}/{nad_name}"
                                if nad_ns != self.namespace
                                else nad_name
                            ),
                        },
                    })

        # -- Firmware / bootloader ------------------------------------
        firmware_spec = {}
        if firmware.lower() == "efi":
            firmware_spec["bootloader"] = {"efi": {"secureBoot": False}}
        else:
            firmware_spec["bootloader"] = {"bios": {}}

        # -- Labels and annotations -----------------------------------
        labels = {
            "vm.kubevirt.io/name": vm_name,
            "app": vm_name,
        }
        # Only the standard KubeVirt OS hint is set here. CloudBolt's own tags
        # (group, owner, cost-center, ...) are NOT stamped manually as
        # annotations -- they are applied out-of-band by the resource handler's
        # update_tags() after the server is re-registered onto OpenShift.
        annotations = {}
        guest_id = vm_meta.get("guest_id", "")
        if guest_id:
            annotations["vm.kubevirt.io/os"] = guest_id

        annotation_text = vm_meta.get("annotation", "")

        # -- Assemble the VM body -------------------------------------
        vm_body = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {
                "name": vm_name,
                "namespace": self.namespace,
                "labels": labels,
                "annotations": annotations,
            },
            "spec": {
                "runStrategy": run_strategy,
                "template": {
                    "metadata": {
                        "labels": labels,
                    },
                    "spec": {
                        "domain": {
                            "cpu": {
                                "cores": cpus,
                                "threads": 1,
                                "sockets": 1,
                            },
                            "memory": {
                                "guest": f"{mem}Mi",
                            },
                            "firmware": firmware_spec,
                            "devices": {
                                "disks": disks,
                                "interfaces": interfaces,
                            },
                        },
                        "networks": networks,
                        "volumes": volumes,
                        "terminationGracePeriodSeconds": 180,
                        # Live-migrate on node drain WHEN the VM is actually
                        # migratable (RWX storage + a migratable NIC binding),
                        # otherwise fall back to shutdown -- WITHOUT the "not
                        # migratable" warning that plain LiveMigrate raises. This
                        # is decided per-VM by KubeVirt at eviction time, so it
                        # tracks real migratability (storage AND network), not
                        # just the storage class. Without this the VM inherits
                        # the cluster default (LiveMigrate), which warns on
                        # non-migratable VMs (e.g. RWO/EBS-backed disks).
                        "evictionStrategy": "LiveMigrateIfPossible",
                    },
                },
            },
        }

        if cores_per_socket and cores_per_socket > 1:
            vm_body["spec"]["template"]["spec"]["domain"]["cpu"]["sockets"] = (
                cpus // cores_per_socket or 1
            )
            vm_body["spec"]["template"]["spec"]["domain"]["cpu"]["cores"] = (
                cores_per_socket
            )

        if annotation_text:
            vm_body["metadata"]["annotations"]["description"] = annotation_text

        return vm_body

    # ------------------------------------------------------------------
    # VM creation
    # ------------------------------------------------------------------

    def _vm_exists(self, vm_name):
        """Return the VM dict if it already exists, else ``None``."""
        path = (
            f"{K8S_VM_API}/namespaces/{self.namespace}"
            f"/virtualmachines/{vm_name}"
        )
        resp = self._session.get(f"{self.api_url}/{path}")
        if resp.status_code == 404:
            return None
        if resp.ok:
            return resp.json()
        return None

    def _create_vm(self, vm_body):
        """POST a VirtualMachine resource to the cluster."""
        vm_name = vm_body["metadata"]["name"]
        logger.info("Creating VirtualMachine %s/%s", self.namespace, vm_name)
        path = f"{K8S_VM_API}/namespaces/{self.namespace}/virtualmachines"
        return self._api_request("POST", path, json=vm_body)

    def _start_vm(self, vm_name):
        """Start the VM via the ``start`` subresource.

        Uses the subresource rather than patching ``runStrategy`` to Always: the
        subresource starts the VM while leaving its declared run strategy intact,
        whereas a merge-patch to Always converts the VM's *policy* (a guest-
        initiated shutdown would then be undone, and stopping would require a
        further patch to Halted).

        The caller builds the VM with the requested run strategy and only calls
        this for strategies that do not auto-start, so this is the single start
        path -- no double-start.
        """
        logger.info("Starting VirtualMachine %s/%s", self.namespace, vm_name)
        path = (
            f"{K8S_VM_SUBRESOURCE_API}/namespaces/{self.namespace}"
            f"/virtualmachines/{vm_name}/start"
        )
        return self._api_request(
            "PUT",
            path,
            data=json.dumps({}),
            # The action subresource returns no JSON body, so the session's
            # global "Accept: application/json" makes virt-api answer 406 Not
            # Acceptable. Accept anything (as virtctl/curl do) for this call.
            headers={"Accept": "*/*", "Content-Type": "application/json"},
        )

    def _wait_vm_running(self, vm_name, timeout=VM_RUNNING_TIMEOUT):
        """Poll the VirtualMachineInstance until it reaches ``Running``.

        Returns the VMI dict once running.  Raises on timeout or
        terminal failure states.
        """
        vmi_path = (
            f"{K8S_VM_API}/namespaces/{self.namespace}"
            f"/virtualmachineinstances/{vm_name}"
        )
        deadline = time.time() + timeout
        last_phase = None
        while time.time() < deadline:
            resp = self._session.get(f"{self.api_url}/{vmi_path}")
            if resp.status_code == 404:
                if last_phase != "Pending":
                    logger.info("VMI %s not yet created, waiting...", vm_name)
                    last_phase = "Pending"
                time.sleep(POLL_INTERVAL)
                continue
            if not resp.ok:
                time.sleep(POLL_INTERVAL)
                continue
            vmi = resp.json()
            phase = vmi.get("status", {}).get("phase", "Unknown")
            if phase != last_phase:
                logger.info("VMI %s phase: %s", vm_name, phase)
                last_phase = phase
            if phase == "Running":
                return vmi
            if phase in ("Failed", "Unknown"):
                conditions = vmi.get("status", {}).get("conditions", [])
                msg = next(
                    (c.get("message", "") for c in conditions
                     if c.get("status") == "False"),
                    str(phase),
                )
                raise OpenShiftImportError(
                    f"VMI {vm_name} entered {phase} state: {msg}"
                )
            time.sleep(POLL_INTERVAL)

        raise OpenShiftImportError(
            f"VMI {vm_name} did not reach Running within {timeout}s "
            f"(last phase: {last_phase})"
        )

    # ------------------------------------------------------------------
    # Manifest loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_manifest(manifest_dir):
        """Load and validate the manifest.json from a VM directory."""
        manifest_path = os.path.join(manifest_dir, MANIFEST_FILE)
        if not os.path.exists(manifest_path):
            raise OpenShiftImportError(
                f"Manifest not found at {manifest_path}.  "
                f"Run vmware_export.cold_export_via_nfc first."
            )
        with open(manifest_path, "r") as f:
            manifest = json.load(f)

        if not manifest.get("disks"):
            raise OpenShiftImportError(
                "Manifest contains no disks -- nothing to import."
            )
        return manifest

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def import_vm(
        self,
        manifest_dir,
        vm_name=None,
        network_map=None,
        cpu_count=None,
        memory_mb=None,
        run_strategy="Manual",
        start_vm=False,
        disk_bus="virtio",
    ):
        """Import a VM from a manifest directory into OpenShift.

        Parameters
        ----------
        manifest_dir : str
            Path to the directory containing ``manifest.json`` and the
            QCOW2 disk images.
        vm_name : str or None
            Name for the VirtualMachine resource.  Defaults to the
            source VM name from the manifest (sanitised for K8s).
        network_map : dict or None
            Mapping of source VMware network names to OpenShift network
            targets.  Each value is either ``None`` (use pod network)
            or a ``"namespace/nad-name"`` string referencing a Multus
            NetworkAttachmentDefinition.  Example::

                {
                    "VM Network": None,          # pod network
                    "Management": "default/mgmt-br",
                }

        cpu_count : int or None
            Override CPU core count (default from manifest).
        memory_mb : int or None
            Override memory in MiB (default from manifest).
        run_strategy : str
            KubeVirt RunStrategy (default ``"Manual"``).
        start_vm : bool
            If ``True``, start the VM immediately after creation.
        disk_bus : str
            Disk bus type.  ``"virtio"`` (default) -- virt-v2v installs the
            virtio-blk driver during conversion.  ``"sata"`` only for an
            unconverted (raw-copy) guest without virtio drivers.

        Returns
        -------
        dict
            Summary of the import with keys ``vm_name``, ``namespace``,
            ``uid`` (the created VirtualMachine's ``metadata.uid``),
            ``disks_uploaded``, ``vm_created``, and ``started``.
        """
        manifest = self._load_manifest(manifest_dir)
        vm_meta = manifest.get("vm", {})

        # Sanitise whatever name we're given (callers pass server.hostname,
        # which may contain dots/uppercase); fall back to the manifest name.
        vm_name = _sanitise_k8s_name(
            vm_name or vm_meta.get("name", os.path.basename(manifest_dir))
        )

        logger.info(
            "Starting VM import: %s -> %s/%s",
            manifest_dir,
            self.namespace,
            vm_name,
        )

        # -- Discover CDI proxy ---------------------------------------
        self._discover_cdi_proxy()

        # -- Upload disks ---------------------------------------------
        disks = manifest.get("disks", {})
        dv_names = {}

        for disk_key, disk_info in disks.items():
            if disk_info.get("removed"):
                logger.info("Skipping removed disk %s", disk_key)
                continue

            qcow2_file = _resolve_disk_file(manifest_dir, disk_info)
            if not qcow2_file:
                raise OpenShiftImportError(
                    f"No QCOW2 file found for disk {disk_key}.  "
                    f"Re-run vmware_export.cold_export_via_nfc, or ensure "
                    f"the QCOW2 file exists in {manifest_dir}."
                )

            capacity_bytes = disk_info.get("capacity_bytes", 0)
            if not capacity_bytes:
                # Derive from the QCOW2 VIRTUAL size, never the compressed
                # on-disk size: a -c compressed image is a fraction of virtual
                # size, so file-size estimates under-provision the PVC and CDI
                # fails with ErrLargerPVCRequired.
                capacity_bytes = _qcow2_virtual_size(qcow2_file)

            dv_name = _disk_name(vm_name, disk_key)
            self._upload_disk(qcow2_file, dv_name, capacity_bytes)
            dv_names[disk_key] = dv_name

        if not dv_names:
            raise OpenShiftImportError(
                "No disks were uploaded -- nothing to create a VM from."
            )

        # -- Check for existing VM ------------------------------------
        existing_vm = self._vm_exists(vm_name)
        if existing_vm:
            raise OpenShiftImportError(
                f"VirtualMachine {self.namespace}/{vm_name} already exists.  "
                f"Delete it first or pass a different vm_name."
            )

        # -- Build and create VM --------------------------------------
        vm_body = self._build_vm_spec(
            manifest=manifest,
            vm_name=vm_name,
            dv_names=dv_names,
            network_map=network_map,
            cpu_count=cpu_count,
            memory_mb=memory_mb,
            run_strategy=run_strategy,
            disk_bus=disk_bus,
        )

        created_vm = self._create_vm(vm_body)
        # Capture the KubeVirt VirtualMachine's metadata.uid from the create
        # response. This is the value CloudBolt's OVirtHandler uses as a
        # server's resource_handler_svr_id (its
        # resource_handler_svr_id_key_for_new_vm is "uuid", sourced from
        # metadata.uid), so callers that re-register the CB Server record onto
        # OpenShift need it.
        vm_uid = ""
        if isinstance(created_vm, dict):
            vm_uid = created_vm.get("metadata", {}).get("uid", "") or ""
        logger.info("VirtualMachine %s/%s created (uid: %s)",
                    self.namespace, vm_name, vm_uid or "unknown")

        started = False
        if start_vm:
            # "Always"/"RerunOnFailure" bring the VM up on their own; the
            # /start subresource is only needed (and only valid) for strategies
            # that stay stopped until explicitly started.
            if run_strategy not in ("Always", "RerunOnFailure"):
                self._start_vm(vm_name)
            vmi = self._wait_vm_running(vm_name)
            guest_ip = ""
            for iface in vmi.get("status", {}).get("interfaces", []):
                ip = iface.get("ipAddress", "")
                if ip:
                    guest_ip = ip
                    break
            logger.info(
                "VirtualMachine %s/%s is running (IP: %s)",
                self.namespace, vm_name, guest_ip or "not yet assigned",
            )
            started = True

        summary = {
            "vm_name": vm_name,
            "namespace": self.namespace,
            "uid": vm_uid,
            "disks_uploaded": list(dv_names.values()),
            "vm_created": True,
            "started": started,
        }
        if started:
            summary["guest_ip"] = guest_ip
        logger.info("Import complete: %s", json.dumps(summary, indent=2))
        return summary


# ======================================================================
# Module-level helpers
# ======================================================================


def _sanitise_k8s_name(name):
    """Convert a string to a valid Kubernetes resource name.

    Lowercase, alphanumeric and hyphens only, max 63 chars, must start
    and end with an alphanumeric character.
    """
    safe = []
    for ch in name.lower():
        if ch.isalnum():
            safe.append(ch)
        elif ch in ("-", "_", " ", "."):
            safe.append("-")
        # else: skip
    result = "-".join(part for part in "".join(safe).split("-") if part)
    return result[:63].strip("-") or "imported-vm"


def _disk_name(vm_name, disk_key):
    """Return the name for a VM disk and its backing PVC/DataVolume.

    Format ``<vm_name>-disk<N>`` (e.g. ``myvm-disk0``). The same name is used
    for both the PVC/DataVolume and the in-VM disk/volume so they read
    identically in the OpenShift console. KubeVirt volume/disk names are
    DNS-1123 labels capped at 63 characters; if the VM name is long, only the
    VM portion is truncated -- the unique ``-disk<N>`` suffix is always kept.
    ``vm_name`` is already sanitised by the caller and ``disk_key`` is an
    integer-like string, so the result needs no further sanitisation.
    """
    suffix = f"-disk{disk_key}"
    prefix = vm_name[: 63 - len(suffix)].rstrip("-") or "vm"
    return f"{prefix}{suffix}"


def _parse_nad_ref(target, default_namespace):
    """Parse a NetworkAttachmentDefinition reference.

    Accepts ``"namespace/nad-name"`` or just ``"nad-name"``.
    Returns ``(namespace, name)``.
    """
    if "/" in target:
        parts = target.split("/", 1)
        return parts[0], parts[1]
    return default_namespace, target


def _qcow2_virtual_size(qcow2_path):
    """Return the virtual (uncompressed) size in bytes via ``qemu-img info``."""
    result = subprocess.run(
        ["qemu-img", "info", "--output=json", qcow2_path],
        check=True, capture_output=True, text=True,
    )
    return int(json.loads(result.stdout)["virtual-size"])


def _resolve_disk_file(manifest_dir, disk_info):
    """Find the QCOW2 file for a disk from its manifest chain entry.

    Returns the file named by the disk's own chain, or ``None``. There is NO
    scan-any-qcow2 fallback: returning an arbitrary file would upload the wrong
    disk's data for a multi-disk VM. A missing file is surfaced by the caller.
    """
    chain = disk_info.get("chain", [])
    if chain:
        candidate = os.path.join(manifest_dir, chain[-1].get("file", ""))
        if os.path.isfile(candidate):
            return candidate
    return None

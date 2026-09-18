"""
This Shared module hosts common methods used for communicating with VMware
"""
import time

from pyVmomi import vim
from resourcehandlers.vmware.models import VsphereResourceHandler
from resourcehandlers.vmware.tools import tasks
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)


class VMwareConnection(object):
    def __init__(self, rh, server=None):
        """
        Initialize the VMware connection object.
        :param rh: Required. Resource Handler object
        """
        rh = rh.cast()
        if type(rh) is not VsphereResourceHandler:
            raise Exception("Resource Handler is not a VMware Resource Handler")
        self.rh = rh
        (self.search_index, self.service_instance,
         self.content) = self.get_vc_info()

    def get_or_create_scsi_controller(self, server, bus_number):
        """
        Get or create a SCSI controller on the VM object.
        :param server: Server object
        :param bus_number: SCSI controller name
        :return: SCSI controller object
        """
        vc_vm = self.get_vc_vm_from_server(server)
        scsi_controller_obj = self.get_scsi_controller(vc_vm, bus_number)
        if not scsi_controller_obj:
            self.create_scsi_controller(vc_vm, bus_number)
            scsi_controller_obj = self.get_scsi_controller(vc_vm, bus_number)
        return scsi_controller_obj

    def get_vc_vm_from_server(self, server):
        """
        Get the vCenter VM object from the server object.
        :param server:
        :param search_index:
        :return:
        """
        # First refresh info to be sure the server has vmwareserverinfo
        server.refresh_info()
        vm = self.search_index.FindByUuid(
            None, server.vmwareserverinfo.instance_uuid, True, True
        )
        if not vm:
            raise Exception(
                f"VM not found in vCenter for server {server.hostname}")
        return vm

    def get_vc_info(self):
        pyvmomi_wrapper = self.rh.get_api_wrapper()
        # Get the pyvmomi server object
        service_instance = pyvmomi_wrapper._get_connection()
        content = service_instance.RetrieveContent()
        search_index = content.searchIndex
        return search_index, service_instance, content

    @staticmethod
    def list_scsi_controllers_for_vc_vm(vc_vm):
        """
        List all SCSI controllers for a vCenter VM. set_vc_vm must be run first.
        :param vc_vm: vCenter VM object
        :return: List of SCSI controller objects
        """
        scsi_controllers = []
        for device in vc_vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualSCSIController):
                scsi_controllers.append(device)
        return scsi_controllers

    def get_scsi_controller(self, vc_vm, bus_number):
        """
        Get the SCSI controller object from the vCenter VM. set_vc_vm must be
        run first.
        :param vc_vm: vCenter VM object
        :param bus_number: Bus number of the SCSI controller
        :return: SCSI controller object
        """
        for device in self.list_scsi_controllers_for_vc_vm(vc_vm):
            if device.busNumber == bus_number:
                return device
        return None

    def create_scsi_controller(self, vc_vm, bus_number):
        """
        Create a new SCSI controller on the vCenter VM. set_vc_vm must be
        run first.
        :param vc_vm: vCenter VM object
        :param bus_number: Bus number of the SCSI controller
        :return: SCSI controller object
        """
        devices = []
        spec = vim.vm.ConfigSpec()

        controller = vim.vm.device.VirtualDeviceSpec()
        controller.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        controller.device = vim.vm.device.ParaVirtualSCSIController()
        controller.device.busNumber = bus_number
        controller.device.hotAddRemove = True
        controller.device.sharedBus = 'noSharing'
        controller.device.scsiCtlrUnitNumber = 7
        devices.append(controller)

        spec.deviceChange = devices
        task = vc_vm.ReconfigVM_Task(spec=spec)
        tasks.wait_for_tasks(self.service_instance, [task])
        return None

    def get_highest_bus_number(self, vc_vm):
        """
        Get the highest SCSI bus number from the SCSI controllers on the vCenter VM.
        :param vc_vm: vCenter VM object
        :return: Highest SCSI bus number
        """
        scsi_controllers = self.list_scsi_controllers_for_vc_vm(vc_vm)
        bus_numbers = [controller.busNumber for controller in scsi_controllers]
        return max(bus_numbers)

    def create_disk(self, server, scsi_controller_obj, disk_size,
                    datastore_cluster_name):
        """
        Create a new disk on the SCSI controller.
        :param server: Server object
        :param scsi_controller_obj: SCSI controller object
        :param disk_size: Disk size
        :param datastore_cluster_name: Datastore cluster name
        :return: Disk object
        """
        vc_vm = self.get_vc_vm_from_server(server)
        datastore_cluster = self.get_datastore_cluster_by_name(
            datastore_cluster_name
        )
        ds = self.get_recommended_datastore(datastore_cluster)
        disk_unit_number = self.get_disk_unit_number_for_scsi_controller(
            vc_vm, scsi_controller_obj
        )
        disk_number = len(self.get_vm_disks(vc_vm)) + 1

        devices = []
        spec = vim.vm.ConfigSpec()

        disk = vim.vm.device.VirtualDeviceSpec()
        disk.fileOperation = "create"
        disk.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        disk.device = vim.vm.device.VirtualDisk()
        disk.device.capacityInKB = disk_size * 1024 * 1024
        disk.device.controllerKey = scsi_controller_obj.key
        disk.device.unitNumber = disk_unit_number
        disk.device.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        disk.device.backing.thinProvisioned = True
        disk.device.backing.diskMode = 'persistent'
        disk.device.backing.datastore = ds
        disk.device.backing.fileName = (
            f'[{ds.name}]/{vc_vm.name}/{vc_vm.name}_'
            f'{disk_number}.vmdk')
        devices.append(disk)

        spec.deviceChange = devices
        task = vc_vm.ReconfigVM_Task(spec=spec)
        tasks.wait_for_tasks(self.service_instance, [task])
        return

    @staticmethod
    def get_vm_disks(vc_vm):
        """
        Get the disks attached to the vCenter VM.
        :param vc_vm: vCenter VM object
        :return: List of disk objects
        """
        disks = []
        for device in vc_vm.config.hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                disks.append(device)
        return disks

    def get_disks_for_scsi_controller(self, vc_vm, scsi_controller_obj):
        """
        Get the disks attached to the SCSI controller.
        :param vc_vm: vCenter VM object
        :param scsi_controller_obj: SCSI controller object
        :return:
        """
        disks = self.get_vm_disks(vc_vm)
        scsi_controller_disks = []
        for disk in disks:
            if disk.controllerKey == scsi_controller_obj.key:
                scsi_controller_disks.append(disk)
        return scsi_controller_disks

    def get_disk_unit_number_for_scsi_controller(self, vc_vm,
                                                 scsi_controller_obj):
        """
        Get the unit number of the disk to be created.
        :param scsi_controller_obj: SCSI controller object
        :param vc_vm: vCenter VM object
        :return: Unit number of the disk
        """
        disk_unit_numbers = [
            disk.unitNumber for disk in
            self.get_disks_for_scsi_controller(vc_vm, scsi_controller_obj)
        ]
        unit_number = max(disk_unit_numbers) + 1 if disk_unit_numbers else 0
        if unit_number == 7:
            unit_number += 1
        if unit_number >= 16:
            print("we don't support this many disks")
        return unit_number

    @staticmethod
    def get_cluster_from_vm(vc_vm):
        """
        Get the cluster object from the vCenter VM.
        :param vc_vm: vCenter VM object
        :return: Cluster object
        """
        return vc_vm.resourcePool.parent

    @staticmethod
    def get_datastore_clusters_available_in_cluster(cluster):
        """
        Get the datastore clusters available in the cluster.
        :param cluster: Cluster object
        :return: List of datastore cluster objects
        """
        datastore_clusters = []
        for child in cluster.datastore:
            parent = child.parent
            if isinstance(parent, vim.StoragePod):
                if parent.summary.name and parent not in datastore_clusters:
                    datastore_clusters.append(parent)
        return datastore_clusters

    def list_all_datastore_clusters(self):
        """
        List all datastore clusters available in the vCenter.
        :return: List of datastore cluster objects
        """
        obj_view = self.content.viewManager.CreateContainerView(
            self.content.rootFolder, [vim.StoragePod], True
        )
        datastore_clusters = obj_view.view
        obj_view.Destroy()
        return datastore_clusters

    def get_datastore_cluster_by_name(self, datastore_cluster_name):
        """
        Get the datastore cluster object by name.
        :param datastore_cluster_name: Datastore cluster name
        :return: Datastore cluster object
        """
        datastore_clusters = self.list_all_datastore_clusters()
        for datastore_cluster in datastore_clusters:
            if datastore_cluster.summary.name == datastore_cluster_name:
                return datastore_cluster
        raise Exception(
            f"Datastore cluster not found: {datastore_cluster_name}")

    @staticmethod
    def return_storage_placement_for_datastore_cluster(datastore_cluster):
        """
        Return the storage placement for the datastore cluster.
        :param datastore_cluster: Datastore cluster object
        :return: Storage placement object
        """
        return datastore_cluster.configuration.placement

    def get_recommended_datastore(self, ds_cluster):
        """
        Function to return Storage DRS recommended datastore from datastore
        cluster. If a datastore cluster is not SDRS enabled, it will return
        datastore with most free space.
        Args:
            datastore_cluster_obj: datastore cluster managed object

        :param ds_cluster:
        :return: Name of recommended datastore from the given datastore cluster
        """
        # Check if Datastore Cluster provided by user is SDRS ready
        sdrs_config = ds_cluster.podStorageDrsEntry.storageDrsConfig
        sdrs_status = sdrs_config.podConfig.enabled
        if sdrs_status:
            # We can get storage recommendation only if SDRS is enabled on given
            # datastorage cluster
            pod_sel_spec = vim.storageDrs.PodSelectionSpec()
            pod_sel_spec.storagePod = ds_cluster
            storage_spec = vim.storageDrs.StoragePlacementSpec()
            storage_spec.podSelectionSpec = pod_sel_spec
            storage_spec.type = 'create'

            try:
                rec = self.content.storageResourceManager.RecommendDatastores(
                    storageSpec=storage_spec
                )
                rec_action = rec.recommendations[0].action[0]
                return rec_action.destination.name
            except Exception:
                # There is some error, so we fall back to general workflow
                pass
        datastore = None
        datastore_freespace = 0
        for ds in ds_cluster.childEntity:
            if (isinstance(ds, vim.Datastore) and
                    ds.summary.freeSpace > datastore_freespace):
                # If datastore field is provided, filter destination datastores
                datastore = ds
                datastore_freespace = ds.summary.freeSpace
        if datastore:
            return datastore
        return None

    def get_vm_advanced_info_by_key(self, vc_vm, key):
        """
        Get the advanced info of the vCenter VM by key.
        :param vc_vm: vCenter VM object
        :param key: Key of the advanced info. eg. guestinfo.appInfo
        :return: Advanced info object
        """
        for item in self.get_vm_advanced_info(vc_vm):
            if item.key == key:
                return item.value

    def get_vm_advanced_info(self, vc_vm):
        """
        Get the advanced info of the vCenter VM.
        :param vc_vm: vCenter VM object
        :return: Advanced info object
        """
        return vc_vm.config.extraConfig

    def set_vm_advanced_info(self, vc_vm, key, value):
        """
        Set the advanced info of the vCenter VM.
        :param vc_vm: vCenter VM object
        :param key: Key of the advanced info. eg. guestinfo.myNewKey
        :param value: Value of the advanced info
        :return: None
        """
        extra_config = vc_vm.config.extraConfig
        extra_config.append(vim.option.OptionValue(key=key, value=value))
        spec = vim.vm.ConfigSpec(extraConfig=extra_config)
        task = vc_vm.ReconfigVM_Task(spec=spec)
        tasks.wait_for_tasks(self.service_instance, [task])

    def add_nic_to_vm(self, vm, port_group, nic_poweron=True):
        """
        Adds a new NIC to a VM and connects it to the specified port group.
        :param vm: Virtual Machine object
        :param port_group: Portgroup to connect the NIC to
        :param nic_poweron: Whether to power on the NIC after adding (default is True)
        """
        from resourcehandlers.vmware import pyvmomi_wrapper
        si = self.service_instance

        # Create NIC backing info
        nic_backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
            port=vim.dvs.PortConnection(
                portgroupKey=port_group.key,
                switchUuid=port_group.config.distributedVirtualSwitch.uuid
            )
        )

        # Create NIC device
        nic_device = vim.vm.device.VirtualVmxnet3(
            key=-1,
            backing=nic_backing,
            connectable=vim.VirtualDeviceConnectInfo(
                startConnected=nic_poweron,
                allowGuestControl=True,
                connected=nic_poweron
            )
        )

        # Create device change spec
        dev_spec = vim.VirtualDeviceConfigSpec(
            operation=vim.VirtualDeviceConfigSpecOperation.add,
            device=nic_device
        )

        # Create VM config spec
        vm_config_spec = vim.vm.ConfigSpec(deviceChange=[dev_spec])

        # Reconfigure VM
        task = vm.ReconfigVM_Task(spec=vm_config_spec)
        pyvmomi_wrapper.wait_for_tasks(si, [task])

        logger.info(f"NIC added to VM '{vm.name}' and connected to portgroup "
                    f"'{port_group.name}'")

    def get_portgroup_by_name(self, portgroup_name):
        """
        Get the network object by name.
        :param portgroup_name: Port Group name
        :return: Network object
        """
        content = self.service_instance.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.dvs.DistributedVirtualPortgroup], True)
        for pg in container.view:
            if pg.name == portgroup_name:
                container.Destroy()
                return pg
        container.Destroy()
        raise ValueError(f"Portgroup '{portgroup_name}' not found")

    def shutdown_guest(self, server, vc_vm=None, timeout=600,
                       allow_hard_poweroff=False, poll_interval=10):
        """
        Source-VM shutdown for cold migration. Attempts a graceful guest
        shutdown and confirms ``poweredOff`` before returning; escalates to a
        hard power-off only when ``allow_hard_poweroff`` is True (an explicit
        operator opt-in).

        Returns a dict::

            {"powered_off": bool,
             "method": "already-off" | "graceful" | "hard" | None,
             "elapsed_seconds": float,
             "error": str | None}

        Never raises; a failed shutdown is reported as ``powered_off=False`` with
        an ``error`` so the caller can abort before touching disks.
        """
        vc_vm = vc_vm or self.get_vc_vm_from_server(server)
        start = time.time()

        if vc_vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOff:
            return {"powered_off": True, "method": "already-off",
                    "elapsed_seconds": 0.0, "error": None}

        tools_running = (getattr(vc_vm.guest, "toolsRunningStatus", "")
                         == "guestToolsRunning")
        if tools_running:
            try:
                vc_vm.ShutdownGuest()  # asynchronous, no task; poll powerState
            except Exception as e:
                logger.warning("ShutdownGuest call failed on %s: %s", vc_vm.name, e)
            deadline = start + timeout
            while time.time() < deadline:
                vc_vm = self.get_vc_vm_from_server(server)
                if vc_vm.runtime.powerState == \
                        vim.VirtualMachinePowerState.poweredOff:
                    return {"powered_off": True, "method": "graceful",
                            "elapsed_seconds": round(time.time() - start, 1),
                            "error": None}
                time.sleep(poll_interval)
            if not allow_hard_poweroff:
                return {"powered_off": False, "method": None,
                        "elapsed_seconds": round(time.time() - start, 1),
                        "error": "graceful shutdown timed out after {}s and hard "
                                 "power-off was not authorized".format(timeout)}
        elif not allow_hard_poweroff:
            return {"powered_off": False, "method": None,
                    "elapsed_seconds": round(time.time() - start, 1),
                    "error": "VMware Tools not running and hard power-off was not "
                             "authorized"}

        try:
            tasks.wait_for_tasks(self.service_instance, [vc_vm.PowerOffVM_Task()])
        except Exception as e:
            return {"powered_off": False, "method": "hard",
                    "elapsed_seconds": round(time.time() - start, 1),
                    "error": "hard power-off failed: {}".format(e)}
        return {"powered_off": True, "method": "hard",
                "elapsed_seconds": round(time.time() - start, 1), "error": None}

    def rename_vm(self, vc_vm, new_name):
        """Rename a vCenter VM.

        Used post-migration to mark the retired source VM (e.g. append a
        ``-migrated`` suffix) so it is unmistakably the decommissioned source
        and not a live duplicate. Idempotent: returns immediately if the VM is
        already named ``new_name``.

        Uses the vSphere Web Services API ``ManagedEntity.Rename_Task(newName)``
        (VirtualMachine inherits it), the same task-based pattern as
        ``PowerOffVM_Task``/``ReconfigVM_Task`` above.
        """
        if vc_vm.name == new_name:
            return new_name
        logger.info("Renaming vCenter VM '%s' -> '%s'", vc_vm.name, new_name)
        tasks.wait_for_tasks(
            self.service_instance, [vc_vm.Rename_Task(newName=new_name)]
        )
        return new_name

    def get_datacenter_name(self, vc_vm):
        """Walk up the inventory tree to find the Datacenter name for a VM."""
        current = vc_vm.parent
        while current:
            if isinstance(current, vim.Datacenter):
                return current.name
            current = getattr(current, "parent", None)
        raise ValueError(
            f"Could not determine datacenter for VM {vc_vm.name}"
        )

    def get_vcenter_host(self):
        """Return the vCenter hostname/IP from the resource handler."""
        return (
            getattr(self.rh, "ip", None)
            or getattr(self.rh, "hostname", None)
        )

    # ------------------------------------------------------------------
    # VM Metadata Capture
    # ------------------------------------------------------------------

    def get_vm_metadata(self, vc_vm):
        """Capture comprehensive VM metadata for migration and restore.

        Returns a dict suitable for serialisation into a manifest that
        future restore modules (AWS, Proxmox, OpenShift) can consume to
        recreate the VM with matching resources.

        :param vc_vm: vCenter VM ManagedObject
        :return: dict with vm config, NICs, controllers, tags, attributes
        """
        cfg = vc_vm.config
        hw = cfg.hardware

        metadata = {
            "name": vc_vm.name,
            "instance_uuid": cfg.instanceUuid,
            "bios_uuid": cfg.uuid,
            "guest_id": cfg.guestId,
            "guest_full_name": cfg.guestFullName,
            "hardware_version": cfg.version,
            "firmware": getattr(cfg, "firmware", "bios"),
            "num_cpus": hw.numCPU,
            "cores_per_socket": hw.numCoresPerSocket,
            "memory_mb": hw.memoryMB,
            "annotation": cfg.annotation or "",
            "nics": self._collect_nic_info(hw),
            "scsi_controllers": self._collect_controller_info(hw),
            "custom_attributes": self._collect_custom_attributes(vc_vm),
            "tags": self._collect_tags(vc_vm),
        }
        return metadata

    @staticmethod
    def _collect_nic_info(hw):
        """Extract NIC configuration from VM hardware."""
        nics = []
        for dev in hw.device:
            if not isinstance(dev, vim.vm.device.VirtualEthernetCard):
                continue
            nic = {
                "type": type(dev).__name__.rsplit(".", 1)[-1],
                "mac_address": dev.macAddress,
                "address_type": getattr(dev, "addressType", None),
                "label": dev.deviceInfo.label if dev.deviceInfo else None,
                "connected": (
                    dev.connectable.connected
                    if dev.connectable else None
                ),
                "start_connected": (
                    dev.connectable.startConnected
                    if dev.connectable else None
                ),
            }
            backing = dev.backing
            dvp = vim.vm.device.VirtualEthernetCard
            if isinstance(
                backing, dvp.DistributedVirtualPortBackingInfo
            ):
                nic["network_type"] = "distributed"
                nic["portgroup_key"] = (
                    backing.port.portgroupKey if backing.port else None
                )
                nic["switch_uuid"] = (
                    backing.port.switchUuid if backing.port else None
                )
                try:
                    nic["network_name"] = (
                        backing.port.portgroupKey if backing.port else None
                    )
                except Exception:
                    pass
            elif isinstance(backing, dvp.NetworkBackingInfo):
                nic["network_type"] = "standard"
                nic["network_name"] = getattr(backing, "deviceName", None)
            else:
                nic["network_type"] = "unknown"
            nics.append(nic)
        return nics

    @staticmethod
    def _collect_controller_info(hw):
        """Extract SCSI/NVMe controller configuration from VM hardware."""
        controllers = []
        for dev in hw.device:
            if isinstance(dev, vim.vm.device.VirtualSCSIController):
                controllers.append({
                    "type": type(dev).__name__.rsplit(".", 1)[-1],
                    "bus_number": dev.busNumber,
                    "sharing": getattr(dev, "sharedBus", "noSharing"),
                    "key": dev.key,
                })
        return controllers

    def _collect_custom_attributes(self, vc_vm):
        """Read VM custom attributes, resolving field IDs to names."""
        attrs = {}
        try:
            field_defs = {
                f.key: f.name
                for f in self.content.customFieldsManager.field
            }
            for cv in (vc_vm.customValue or []):
                name = field_defs.get(cv.key, f"field_{cv.key}")
                attrs[name] = cv.value
        except Exception as e:
            logger.warning("Could not read custom attributes: %s", e)
        return attrs

    def _collect_tags(self, vc_vm):
        """Fetch vSphere tags for a VM via the REST API.

        Returns a list of ``"category:tag_name"`` strings.  If the REST
        API is unavailable or the call fails, returns an empty list with
        a warning.
        """
        import requests

        host = self.get_vcenter_host()
        username = getattr(self.rh, "serviceaccount", None) or ""
        password = getattr(self.rh, "servicepasswd", None) or ""
        if not host or not username:
            return []

        base_url = f"https://{host}"
        session = requests.Session()
        session.verify = False

        try:
            resp = session.post(
                f"{base_url}/api/session",
                auth=(username, password),
                timeout=15,
            )
            resp.raise_for_status()
            token = resp.json()
            session.headers["vmware-api-session-id"] = token
        except Exception as e:
            logger.warning("Could not create vSphere REST session for "
                           "tag retrieval: %s", e)
            return []

        try:
            vm_moref = vc_vm._moId
            resp = session.post(
                f"{base_url}/api/cis/tagging/tag-association"
                f"?action=list-attached-tags",
                json={"object_id": {
                    "id": vm_moref,
                    "type": "VirtualMachine",
                }},
                timeout=15,
            )
            resp.raise_for_status()
            tag_ids = resp.json()
            if not tag_ids:
                return []

            tags = []
            tag_cache = {}
            cat_cache = {}
            for tag_id in tag_ids:
                if tag_id not in tag_cache:
                    r = session.get(
                        f"{base_url}/api/cis/tagging/tag/{tag_id}",
                        timeout=10,
                    )
                    r.raise_for_status()
                    tag_cache[tag_id] = r.json()
                tag_obj = tag_cache[tag_id]
                cat_id = tag_obj.get("category_id", "")
                if cat_id and cat_id not in cat_cache:
                    r = session.get(
                        f"{base_url}/api/cis/tagging/category/{cat_id}",
                        timeout=10,
                    )
                    r.raise_for_status()
                    cat_cache[cat_id] = r.json()
                cat_name = cat_cache.get(cat_id, {}).get("name", cat_id)
                tag_name = tag_obj.get("name", tag_id)
                tags.append(f"{cat_name}:{tag_name}")
            return tags
        except Exception as e:
            logger.warning("Could not retrieve tags for VM %s: %s",
                           vc_vm.name, e)
            return []
        finally:
            try:
                session.delete(
                    f"{base_url}/api/session", timeout=5,
                )
            except Exception:
                pass
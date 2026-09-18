# Migrate VM to OpenShift

Server action that cold-migrates a vCenter-managed VM into OpenShift Virtualization. It powers the VM off, exports its disks through vCenter over port 443 (vSphere Export API, no VDDK and no ESXi:902), runs `virt-v2v` on the CloudBolt appliance to convert the guest to KVM and write QCOW2, uploads the disks through the CDI upload proxy, creates the KubeVirt VirtualMachine, and re-homes the same CloudBolt server record onto the OpenShift environment so its history is kept.

## Contents
| Role | ID | Name |
|---|---|---|
| Plugin | OHK-kx3qcgmy | Migrate VM to OpenShift |
| Shared module | SHM-so5r7cau | vmware_export |
| Shared module | SHM-newmivt6 | vmware |
| Shared module | SHM-f98ek4p6 | openshift_import |

## Prerequisites
- CloudBolt 8.6 or later.
- `virt-v2v` on the CloudBolt appliance (`dnf install -y virt-v2v`). Windows guests also need `virtio-win` at `/usr/share/virtio-win` and `libguestfs-winsupport`. Without KVM on the appliance, libguestfs falls back to slower software emulation.
- Free space under `/var/tmp` on the appliance for the exported VMDKs plus the converted QCOW2 images; the scratch directory is removed after the run.
- The appliance can reach vCenter on 443 and the OpenShift API and CDI upload proxy route.
- An OpenShift Virtualization resource handler with an environment whose namespace (`ovirt_namespace`) is set and which the server's group can use. The target namespace is chosen by picking that environment.
- The source server must be managed by a VMware resource handler.

## Setup
1. Sync the repo, then restart CloudBolt so the shared modules are loaded.
2. No ConnectionInfo is involved; the action authenticates with the VMware and OpenShift resource handlers' stored credentials.

## Notes
- Cold, single-pass migration: the VM stays powered off from export through import. There is no warm or incremental transfer. The action is flagged dangerous and shows a confirmation dialog.
- CPU and memory for the new VM come from the CloudBolt server record, falling back to the exported vCenter values.
- `virt-v2v` installs virtio drivers, removes VMware Tools, and resets NICs to DHCP because the source static IP was valid only on the old network. `qemu-guest-agent` is installed best-effort.
- Network Name maps every source NIC to one NetworkAttachmentDefinition; blank uses the pod network. Storage Class blank uses the cluster default. Start VM waits for the VM to reach Running.
- After a successful import the source vCenter VM is powered off and renamed with a `-migrated` suffix; it is not deleted. Failures in that step or in re-registering the server record are reported as WARNING with remediation guidance, not as a failed migration.
- If the export or import fails the job returns FAILURE; the source VM is not renamed and may be left powered off.

"""
Shared module for cold-exporting VMware VMs for migration to OpenShift.

Exports each disk of a (powered-off) VM through vCenter over 443 only, using the
vSphere Export API (``ExportVm`` / ``HttpNfcLease``) -- no VDDK and no ESXi:902 --
then runs ``virt-v2v`` once over all of the guest's disks to both convert the
guest OS to run on KVM (virtio drivers, initramfs/bootloader fixups, VMware
Tools removal) and write KVM-ready QCOW2, and writes a ``manifest.json`` the
OpenShift importer (``shared_modules.openshift_import``) consumes. virt-v2v
reads the exported VMDKs directly, so there is no separate ``qemu-img`` step.
After conversion, ``virt-customize`` (best effort) installs + enables the
qemu-guest-agent and resets the guest's NICs to DHCP, since the source static
IP was valid on the old network only.

Cold single-pass only: the CDI upload path has no checkpoint/consolidation, so
there is no warm/incremental transfer here. The export consolidates any existing
snapshot automatically (it exports the current running point).

Progress is reported to the job (``set_progress``): the real export percent is
fed to the lease keepalive so the vCenter UI's transfer bar stays accurate, and
virt-v2v's phase messages are surfaced during the conversion.

Prerequisites:
    - ``virt-v2v`` (libguestfs) on the appliance. With no KVM on the appliance,
      libguestfs falls back to slower TCG software emulation automatically.
      Windows guests also need virtio-win at ``/usr/share/virtio-win`` (or
      ``$VIRTIO_WIN``); Linux guests do not.
    - The appliance can reach vCenter on 443 (it manages the vCenter); the
      export is pinned to the vCenter host so it never needs an ESXi host or 902.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

import requests

from common.methods import set_progress
from pyVmomi import vim
from utilities.logger import ThreadLogger

logger = ThreadLogger(__name__)

MANIFEST_FILE = "manifest.json"
MANIFEST_FORMAT_VERSION = 1

NFC_DOWNLOAD_CHUNK = 8 * 1024 * 1024
NFC_LEASE_READY_TIMEOUT = 120
NFC_KEEPALIVE_INTERVAL = 30
# Minimum seconds between job-progress messages, so a fast link does not spam
# the job log with one line per 8 MiB chunk.
NFC_PROGRESS_INTERVAL = 30

# virt-v2v prints timestamped phase lines like "[  12.3] Converting ...".
_V2V_PHASE_RE = re.compile(r"^\[\s*[\d.]+\]\s*(.+)$")

# Best-effort DHCP reset installed via ``virt-customize --firstboot`` after the
# virt-v2v conversion (virt-v2v cannot reconfigure networking, and virt-v2v 1.42
# on RHEL/OEL 8 has no --firstboot of its own). The source
# VM's static IP -- if any -- was valid on the old network only; DHCP is the safe
# default on the new OpenShift network. Runs once, as root, on the guest's first
# boot. Linux-focused across the common network stacks; on Windows the shell
# script does not run and the fresh virtio adapter defaults to DHCP anyway. A
# stack that is not present is simply skipped, so this is safe on any guest.
_DHCP_FIRSTBOOT_SCRIPT = """#!/bin/sh
# CloudBolt migration: reset wired interfaces to DHCP after the move to KVM.
set +e

# NetworkManager (RHEL/CentOS 8+, Fedora, many Ubuntu installs): switch every
# ethernet connection profile to DHCP and clear its old static addressing.
if command -v nmcli >/dev/null 2>&1; then
    # Existing ethernet connection profiles -> DHCP, clearing old static addrs.
    nmcli -t -f NAME,TYPE connection show 2>/dev/null \\
        | awk -F: '$2 ~ /ethernet/ {print $1}' \\
        | while IFS= read -r con; do
            nmcli connection modify "$con" \\
                ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns "" \\
                ipv6.method auto 2>/dev/null
        done
    # Ethernet devices with NO active connection -- the common post-migration
    # case, since the new virtio NIC has no profile at all. Create a DHCP
    # profile (autoconnecting) and bring it up now.
    nmcli -t -f DEVICE,TYPE,STATE device status 2>/dev/null \\
        | awk -F: '$2=="ethernet" && $3!="connected" {print $1}' \\
        | while IFS= read -r dev; do
            nmcli connection add type ethernet ifname "$dev" \\
                con-name "dhcp-$dev" autoconnect yes \\
                ipv4.method auto ipv6.method auto 2>/dev/null
            nmcli connection up "dhcp-$dev" 2>/dev/null \\
                || nmcli device connect "$dev" 2>/dev/null
        done
fi

# netplan (Ubuntu 18.04+ server).
if [ -d /etc/netplan ]; then
    cat > /etc/netplan/99-cloudbolt-dhcp.yaml <<'YAML'
network:
  version: 2
  ethernets:
    cloudbolt-en:
      match:
        name: "en*"
      dhcp4: true
      dhcp6: true
    cloudbolt-eth:
      match:
        name: "eth*"
      dhcp4: true
      dhcp6: true
YAML
    chmod 600 /etc/netplan/99-cloudbolt-dhcp.yaml 2>/dev/null
    netplan apply 2>/dev/null
fi

# Legacy ifcfg (RHEL/CentOS 7): force DHCP and drop static/MAC pinning.
if [ -d /etc/sysconfig/network-scripts ]; then
    for f in /etc/sysconfig/network-scripts/ifcfg-*; do
        [ -f "$f" ] || continue
        case "$f" in */ifcfg-lo) continue ;; esac
        sed -i -e 's/^BOOTPROTO=.*/BOOTPROTO=dhcp/' \\
               -e '/^IPADDR=/d' -e '/^NETMASK=/d' -e '/^PREFIX=/d' \\
               -e '/^GATEWAY=/d' -e '/^HWADDR=/d' "$f" 2>/dev/null
        grep -q '^BOOTPROTO=' "$f" 2>/dev/null || echo 'BOOTPROTO=dhcp' >> "$f"
    done
fi

# systemd-networkd.
if [ -d /etc/systemd/network ]; then
    cat > /etc/systemd/network/99-cloudbolt-dhcp.network <<'NET'
[Match]
Name=en* eth*
[Network]
DHCP=yes
NET
fi

# Drop stale persistent-net udev rules that pin NIC names to old MACs.
rm -f /etc/udev/rules.d/70-persistent-net.rules 2>/dev/null
exit 0
"""


def _download_nfc(url, cookie, dest_path, on_progress=None):
    """Stream an NFC lease device URL to a local file, authenticating with the
    vCenter session cookie. TLS verification is disabled (vCenter self-signed);
    the lease also carries an ``sslThumbprint`` for strict verification later.

    If ``on_progress`` is given it is called after each chunk with
    ``(downloaded_bytes, total_bytes)``. ``total_bytes`` is the response
    ``Content-Length`` when the server sends it, else ``0`` (unknown) -- the
    stream-optimized export is thin/compressed, so this is the true wire size,
    NOT the virtual disk capacity. Returns the number of bytes written.
    """
    requests.packages.urllib3.disable_warnings()
    with requests.get(url, headers={"Cookie": cookie}, stream=True,
                      verify=False, timeout=(30, 3600)) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=NFC_DOWNLOAD_CHUNK):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        on_progress(downloaded, total)
    return downloaded


def _convert_guest_with_virt_v2v(output_dir, vmdk_paths, out_name):
    """Convert exported VMDK disks to KVM-ready QCOW2 with ``virt-v2v``.

    Runs virt-v2v ONCE over all of the guest's disks (``-i disk`` treats the
    files as one guest's disks, in order), doing both the guest-OS conversion
    (virtio drivers, initramfs/bootloader fixups, VMware Tools removal) and the
    format conversion to QCOW2 -- so this fully replaces the old ``qemu-img``
    copy. virt-v2v reads the VMDKs directly.

    ``-on out_name`` fixes the output basename, so the converted disks are
    written to ``output_dir`` as ``<out_name>-sda``, ``<out_name>-sdb`` ... in
    the SAME order as ``vmdk_paths`` (documented virt-v2v behaviour): index 0 ->
    ``-sda``, 1 -> ``-sdb``, etc. Returns those filenames (basenames), disk-index
    ordered.

    After conversion the guest's NICs are reset to DHCP as a best-effort,
    non-fatal step (see ``_inject_dhcp_firstboot``).

    Raises ``RuntimeError`` if virt-v2v is missing, exits non-zero, or does not
    produce an expected output file.
    """
    cmd = ["virt-v2v", "-i", "disk"]
    cmd += list(vmdk_paths)
    cmd += ["-o", "disk", "-of", "qcow2", "-os", output_dir, "-on", out_name]

    logger.info("Running guest conversion: %s", " ".join(cmd))
    set_progress("Converting guest to run on KVM (virt-v2v)...")

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "virt-v2v is not installed on the appliance. Install the "
            "libguestfs/virt-v2v tooling (and virtio-win for Windows guests) "
            "to enable guest conversion."
        )

    # Surface virt-v2v's timestamped phase lines to the job, throttled. Under
    # TCG (no KVM on the appliance) this step is the slow one, so the phase
    # messages are the operator's signal that it is progressing.
    last_emit = time.time()
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        logger.info("virt-v2v: %s", line)
        m = _V2V_PHASE_RE.match(line)
        if m and (time.time() - last_emit) >= NFC_PROGRESS_INTERVAL:
            last_emit = time.time()
            set_progress("virt-v2v: %s" % m.group(1)[:120])
    err = proc.stderr.read()
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(
            "virt-v2v conversion failed (exit %d): %s"
            % (proc.returncode, (err or "").strip()[:800]))

    converted = []
    for i in range(len(vmdk_paths)):
        fname = "%s-sd%s" % (out_name, chr(ord("a") + i))
        if not os.path.isfile(os.path.join(output_dir, fname)):
            raise RuntimeError(
                "Expected virt-v2v output '%s' not found in %s; the tool "
                "may name disks differently in this version."
                % (fname, output_dir))
        converted.append(fname)

    # Install the guest agent + reset NICs to DHCP on the converted disk(s).
    # Separate tool: virt-v2v 1.42 (RHEL/OEL 8) has no --install/--firstboot of
    # its own. Best-effort -- never blocks the migration.
    _customize_guest(output_dir, converted)

    return converted


def _customize_guest(output_dir, converted_files):
    """Best-effort guest customization on the converted disk(s) via
    ``virt-customize``:

      * install + enable ``qemu-guest-agent`` so OpenShift can report the VM's
        IP and manage it. Done OFFLINE here (appliance-side), so it does NOT
        depend on the guest getting network/DHCP at first boot;
      * install a firstboot script that resets the NICs to DHCP.

    A SEPARATE step because virt-v2v 1.42 (RHEL/OEL 8) has no ``--install`` or
    ``--firstboot`` of its own. Non-fatal: if virt-customize is missing, or the
    run fails (e.g. the guest's package repos are unreachable from the
    appliance), the VM is still migrated -- just without these tweaks, which can
    then be applied by hand inside the guest.
    """
    firstboot_path = os.path.join(output_dir, ".cloudbolt-firstboot-dhcp.sh")
    with open(firstboot_path, "w") as fb:
        fb.write(_DHCP_FIRSTBOOT_SCRIPT)

    cmd = ["virt-customize"]
    for fname in converted_files:
        cmd += ["-a", os.path.join(output_dir, fname)]
    cmd += [
        # Install the guest agent offline (uses the appliance's network via the
        # guest's own repos) and enable it to start on boot.
        "--install", "qemu-guest-agent",
        "--run-command", "systemctl enable qemu-guest-agent",
        # Reset NICs to DHCP on first boot.
        "--firstboot", firstboot_path,
    ]

    set_progress("Customizing guest (guest agent + DHCP) via virt-customize...")
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "virt-customize failed (exit %d); VM migrated without the "
                "guest-agent/DHCP tweaks: %s", result.returncode,
                (result.stdout or "").strip()[-800:])
            set_progress("Guest customization failed (non-fatal).")
        else:
            logger.info("Guest agent installed + DHCP firstboot set on the "
                        "converted guest.")
    except FileNotFoundError:
        logger.warning(
            "virt-customize not found; skipping guest-agent/DHCP setup. Install "
            "it with 'dnf install -y /usr/bin/virt-customize' to enable it.")
        set_progress("virt-customize not installed; skipped guest customization.")
    finally:
        try:
            os.remove(firstboot_path)
        except OSError:
            pass


def cold_export_via_nfc(server, path="/var/tmp"):
    """Cold-export a VM's disks and convert them for OpenShift import.

    Thin wrapper around ``_cold_export_impl`` that guarantees the scratch dir is
    removed from the appliance if the export/convert fails partway -- so a failed
    migration never leaves files behind. On success it returns the output
    directory for the importer to consume (the caller removes it afterwards).
    """
    created = {}
    try:
        return _cold_export_impl(server, path, created)
    except Exception:
        if created.get("dir"):
            shutil.rmtree(created["dir"], ignore_errors=True)
        raise


def _cold_export_impl(server, path, created):
    """Cold-export a VM's disks over vCenter:443 only -- no VDDK, no ESXi:902.

    Powers the VM off, opens an ``ExportVm`` HttpNfcLease, downloads each disk
    (stream-optimized VMDK) through vCenter, then runs ``virt-v2v`` once over all
    of the disks to produce KVM-ready QCOW2, and writes a ``manifest.json``
    compatible with the OpenShift importer. The export consolidates any snapshot
    automatically (it exports the current running point). Returns the output
    directory.

    :param server: CloudBolt Server object (a VMware VM)
    :param path:   Parent directory; a ``<vm_name>/`` subdirectory is used
    """
    from shared_modules.vmware import VMwareConnection

    rh = server.resource_handler
    if not rh:
        raise ValueError("Server has no resource handler")
    conn = VMwareConnection(rh.cast())
    vc_vm = conn.get_vc_vm_from_server(server)
    si = conn.service_instance

    logger.info("Powering off %s for cold export", vc_vm.name)
    set_progress("Powering off %s for cold export..." % vc_vm.name)
    shut = conn.shutdown_guest(server, vc_vm=vc_vm, timeout=600,
                               allow_hard_poweroff=True)
    if not shut["powered_off"]:
        raise RuntimeError(
            "Could not power off %s: %s" % (vc_vm.name, shut["error"]))
    vc_vm = conn.get_vc_vm_from_server(server)

    output_dir = os.path.join(path, vc_vm.name)
    os.makedirs(output_dir, exist_ok=True)
    created["dir"] = output_dir  # lets the wrapper clean up on any failure below

    logger.info("Opening export lease for %s", vc_vm.name)
    lease = vc_vm.ExportVm()
    deadline = time.time() + NFC_LEASE_READY_TIMEOUT
    while lease.state == vim.HttpNfcLease.State.initializing:
        if time.time() > deadline:
            raise RuntimeError(
                "Export lease for %s did not become ready" % vc_vm.name)
        time.sleep(2)
    if lease.state == vim.HttpNfcLease.State.error:
        raise RuntimeError(
            "Export lease error for %s: %s" % (vc_vm.name, lease.error))

    info = lease.info
    host = conn.get_vcenter_host()
    cookie = si._stub.cookie  # 'vmware_soap_session=...'

    disk_urls = [du for du in info.deviceUrl if getattr(du, "disk", False)]
    total_disks = len(disk_urls)

    # Overall export percent, shared with the keepalive thread so vCenter's
    # lease-progress bar reflects the real transfer. A single int scalar; the
    # GIL makes the assignment/read atomic across the two threads.
    export_state = {"pct": 0}

    # Keep the lease alive during the (possibly long) download and report the
    # real percent to vCenter instead of a fixed value.
    stop = threading.Event()

    def _keepalive():
        while not stop.wait(NFC_KEEPALIVE_INTERVAL):
            try:
                lease.HttpNfcLeaseProgress(export_state["pct"])
            except Exception:
                break

    ka = threading.Thread(target=_keepalive, daemon=True)
    ka.start()

    exported = []  # (vmdk_path, label) in disk-index order
    try:
        for disk_index, du in enumerate(disk_urls):
            disk_num = disk_index + 1
            # NFC device URLs return '*' for the connected host; pin to the
            # vCenter host so the download stays on 443 and never reaches an
            # ESXi host. If a URL comes back with an explicit ESXi host instead
            # of '*', this environment cannot export 443-only and it will fail
            # here -- that is the signal, not a silent wrong path.
            url = du.url.replace("*", host)
            # du.key is a lease-scoped id containing '/' and ':' (e.g.
            # "/vm-38770/ParaVirtualSCSIController0:0") -- NOT the integer disk
            # key, and not safe as a filename or as a manifest/DataVolume key.
            # Use a positional index; keep the raw key only as a label.
            vmdk_path = os.path.join(output_dir, "disk_%d.vmdk" % disk_index)

            logger.info("Exporting disk %d (%s) of %s via vCenter (443)",
                        disk_index, du.key, vc_vm.name)
            set_progress("Exporting disk %d/%d from vCenter (443)..."
                         % (disk_num, total_disks))

            # Throttle state for this disk's download messages.
            emit = {"t": time.time()}

            def _on_progress(downloaded, total, _i=disk_index, _num=disk_num,
                             _emit=emit):
                # Feed the real percent to the lease keepalive (capped < 100
                # until the whole export completes).
                frac = (downloaded / total) if total else 0.0
                export_state["pct"] = int(
                    min(99, ((_i + frac) / total_disks) * 100))
                now = time.time()
                if now - _emit["t"] < NFC_PROGRESS_INTERVAL:
                    return
                _emit["t"] = now
                gb = downloaded / (1024 ** 3)
                if total:
                    set_progress(
                        "Disk %d/%d: exported %.1f of %.1f GB (%.0f%%)"
                        % (_num, total_disks, gb, total / (1024 ** 3),
                           frac * 100))
                else:
                    # No Content-Length: the truthful report is a running
                    # byte count, not a percent of the virtual size.
                    set_progress("Disk %d/%d: exported %.1f GB"
                                 % (_num, total_disks, gb))

            _download_nfc(url, cookie, vmdk_path, on_progress=_on_progress)

            # Download for this disk is done; reflect it in the lease progress.
            export_state["pct"] = int(min(99, (disk_num / total_disks) * 100))
            exported.append((vmdk_path, du.key))
            size_gb = os.path.getsize(vmdk_path) / (1024 ** 3)
            logger.info("Disk %d exported -> %s (%.1f GB on disk)", disk_index,
                        os.path.basename(vmdk_path), size_gb)
            set_progress("Disk %d/%d: exported (%.1f GB on disk)"
                         % (disk_num, total_disks, size_gb))

        export_state["pct"] = 100
        lease.HttpNfcLeaseProgress(100)
        lease.HttpNfcLeaseComplete()
    except Exception:
        try:
            lease.HttpNfcLeaseAbort()
        except Exception:
            pass
        raise
    finally:
        stop.set()

    # -- Guest conversion (replaces qemu-img) -------------------------------
    # One virt-v2v pass over ALL exported disks: converts the guest OS to run on
    # KVM AND writes KVM-ready QCOW2, reading the VMDKs directly. The source
    # VMDKs are kept until conversion succeeds (so a failure is retriable/
    # debuggable), then removed. Peak scratch during this step is the VMDKs plus
    # the converted images; for a single-disk VM that is one of each.
    out_name = re.sub(r"[^A-Za-z0-9._-]", "-", vc_vm.name).strip("-") or "vm"
    vmdk_paths = [vp for (vp, _label) in exported]
    converted_files = _convert_guest_with_virt_v2v(output_dir, vmdk_paths,
                                                    out_name)

    disks = {}
    for disk_index, (vmdk_path, label) in enumerate(exported):
        disks[str(disk_index)] = {
            "label": label,
            # 0 -> the importer derives the true virtual size from the
            # converted QCOW2 (qemu-img info), which is exact per disk.
            "capacity_bytes": 0,
            "chain": [{"file": converted_files[disk_index]}],
        }

    for vmdk_path, _label in exported:
        try:
            os.remove(vmdk_path)
        except OSError:
            pass

    manifest = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "source": {
            "type": "vmware",
            "vcenter_host": host,
            "vm_moref": vc_vm._moId,
        },
        "vm": conn.get_vm_metadata(vc_vm),
        "disks": disks,
        "cold_export": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": "nfc-export-443",
        },
    }
    with open(os.path.join(output_dir, MANIFEST_FILE), "w") as f:
        json.dump(manifest, f, indent=2)

    logger.info("Cold export complete for %s -> %s", vc_vm.name, output_dir)
    set_progress("Cold export complete: %d disk(s) ready for import."
                 % total_disks)
    return output_dir

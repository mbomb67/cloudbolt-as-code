#!/bin/bash
#
# Configure Data Disks (Oracle Linux): finds each disk CloudBolt attached inside
# the guest, then creates an LVM volume and an XFS file system on it and mounts
# it at the requested mount point.
#
# Build item of the "Oracle Linux Server with Data Disks" blueprint. It runs in
# the guest after the Attach Data Disks (Linux) plug-in, which saved the ordered
# disks on the server's data_disk_layout parameter: one entry per disk with its
# mount point, size, volume name and where the hypervisor attached it (on
# vCenter the SCSI controller bus, unit number, and VMDK UUID; on Azure the data
# disk LUN). CloudBolt fills that parameter in below before it sends the script
# to the guest.
#
# Each entry is matched to one unused guest disk (no partitions, no signature),
# trying in turn:
#   1. Serial number. With disk.EnableUUID = TRUE on the VM, the guest reports
#      the VMDK UUID as the disk's SERIAL and WWN.
#   2. LUN. An Azure data disk's LUN is the L in the disk's H:C:T:L address.
#   3. SCSI target. The vCenter unit number is the T in the disk's H:C:T:L
#      address.
#   4. Attach order. CloudBolt adds the disks in table order, so the next
#      unused disk in H:C:T:L order is taken.
# Every match must also agree on size. If any entry cannot be matched, or a
# mount point or volume group is already in use, the script stops before it
# changes any disk.
#
# Each matched disk becomes the only physical volume of volume group
# vg_<volume name>, with one logical volume lv_<volume name> using all of it,
# formatted XFS with the volume name as the label and mounted by UUID from
# /etc/fstab with nofail. An entry whose volume group is already mounted at the
# requested mount point is reported as already configured, so the script can
# run again. Runs as root (the Remote Script's run-as credentials).
#
# References:
#   lsblk (HCTL, SERIAL, WWN):  https://man7.org/linux/man-pages/man8/lsblk.8.html
#   blkid -p (exit 2 = no signature found): https://man7.org/linux/man-pages/man8/blkid.8.html
#   LVM on Oracle Linux:        https://docs.oracle.com/en/learn/ol-lvm/
#   mkfs.xfs:                   https://man7.org/linux/man-pages/man8/mkfs.xfs.8.html
#   XFS label, 12 characters:   https://man7.org/linux/man-pages/man8/xfs_admin.8.html
#   fstab:                      https://man7.org/linux/man-pages/man5/fstab.5.html
#   Guest disk WWN = VMDK UUID: https://access.redhat.com/solutions/93943
#   Azure LUN = last HCTL field: https://learn.microsoft.com/en-us/azure/virtual-machines/linux/add-disk

# Policy: change these for your environment.
FS_MOUNT_OPTIONS="defaults,nofail"
WAIT_FOR_DISKS_SECONDS=180   # how long to wait for hot-added disks to appear in the guest
SIZE_TOLERANCE_GB=1

set -u -o pipefail

fail() {
    echo "ERROR: $*"
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail "This script must run as root. Check the Remote Script's run-as credentials."

# Quoted here-document: the shell expands nothing inside it.
LAYOUT_JSON=$(cat <<'CB_DATA_DISK_LAYOUT'
{{ server.data_disk_layout }}
CB_DATA_DISK_LAYOUT
)
[ -n "$(printf '%s' "$LAYOUT_JSON" | tr -d '[:space:]')" ] ||
    fail "The server has no data_disk_layout parameter. The Attach Data Disks (Linux) build item must run first."

# dnf needs Python, so python3 (Oracle Linux 9) or platform-python (Oracle Linux 8) is always present.
PY=$(command -v python3 || true)
if [ -z "$PY" ] && [ -x /usr/libexec/platform-python ]; then PY=/usr/libexec/platform-python; fi
[ -n "$PY" ] || fail "python3 is required to read the data disk layout."

# One line per entry: disk|mount_point|size_gb|volume_name|scsi_bus|scsi_unit|disk_uuid|lun
ENTRIES=$(printf '%s' "$LAYOUT_JSON" | "$PY" -c '
import json, sys
rows = json.load(sys.stdin)
for r in sorted(rows, key=lambda r: int(r["disk"])):
    vals = [r.get(k) for k in ("disk", "mount_point", "size_gb", "volume_name", "scsi_bus", "scsi_unit", "disk_uuid", "lun")]
    print("|".join("" if v is None else str(v) for v in vals))
') || fail "data_disk_layout is not valid JSON."
[ -n "$ENTRIES" ] || fail "data_disk_layout lists no disks."

hex_id() {
    # Lower-case hex digits only, without a leading 0x.
    printf '%s' "${1#0x}" | tr -cd '0-9A-Fa-f' | tr 'A-F' 'a-f'
}

size_ok() {
    # $1 bytes, $2 GB
    local diff=$(( $1 / 1073741824 - $2 ))
    [ "${diff#-}" -le "$SIZE_TOLERANCE_GB" ]
}

already_configured() {
    # $1 volume name, $2 mount point
    [ "$(findmnt -n -o SOURCE --mountpoint "$2" 2>/dev/null)" = "/dev/mapper/vg_$1-lv_$1" ]
}

rescan_scsi() {
    local scan
    for scan in /sys/class/scsi_host/host*/scan; do
        [ -w "$scan" ] && echo "- - -" > "$scan"
    done
    udevadm settle >/dev/null 2>&1
    return 0
}

# Unused whole disks, one per line: name|size_bytes|hctl|serial|wwn
unused_disks() {
    local name type rc
    while read -r name type; do
        [ "$type" = "disk" ] || continue
        # Anything below the disk in the tree (partitions, LVM, multipath) means it is in use.
        [ "$(lsblk -n -o NAME "/dev/$name" | wc -l)" -eq 1 ] || continue
        rc=0
        blkid -p "/dev/$name" >/dev/null 2>&1 || rc=$?
        [ "$rc" -eq 2 ] || continue
        echo "$name|$(lsblk -dbn -o SIZE "/dev/$name" | tr -d ' ')|$(lsblk -dn -o HCTL "/dev/$name" | tr -d ' ')|$(lsblk -dn -o SERIAL "/dev/$name" | tr -d ' ')|$(lsblk -dn -o WWN "/dev/$name" | tr -d ' ')"
    done < <(lsblk -dn -o NAME,TYPE)
    return 0
}

is_claimed() {
    case " $CLAIMED " in *" $1 "*) return 0 ;; esac
    return 1
}

# Each finder prints matching device names from CANDIDATES that are not claimed yet.
find_by_serial() {
    # $1 normalized VMDK UUID
    local name bytes hctl serial wwn
    while IFS='|' read -r name bytes hctl serial wwn; do
        [ -n "$name" ] || continue
        is_claimed "$name" && continue
        if [ "$(hex_id "$serial")" = "$1" ] || [ "$(hex_id "$wwn")" = "$1" ]; then echo "$name"; fi
    done <<< "$CANDIDATES"
    return 0
}

find_by_hctl_field() {
    # $1 HCTL field (3 = target, 4 = LUN), $2 value, $3 size GB
    local name bytes hctl serial wwn
    while IFS='|' read -r name bytes hctl serial wwn; do
        [ -n "$name" ] || continue
        is_claimed "$name" && continue
        if [ -n "$hctl" ] && [ "$(echo "$hctl" | cut -d: -f"$1")" = "$2" ] && size_ok "$bytes" "$3"; then echo "$name"; fi
    done <<< "$CANDIDATES"
    return 0
}

find_by_order() {
    # $1 size GB; candidates in H:C:T:L order
    local name bytes hctl serial wwn
    while IFS='|' read -r name bytes hctl serial wwn; do
        [ -n "$name" ] || continue
        is_claimed "$name" && continue
        if size_ok "$bytes" "$1"; then echo "$name"; fi
    done < <(printf '%s\n' "$CANDIDATES" | sort -t'|' -k3,3V)
    return 0
}

candidate_field() {
    # $1 device name, $2 field number (2 size, 3 hctl, 4 serial)
    printf '%s\n' "$CANDIDATES" | awk -F'|' -v n="$1" -v f="$2" '$1 == n { print $f }'
}

echo "Requested data disks:"
while IFS='|' read -r disk mp size_gb vname bus unit uuid lun; do
    echo "  disk $disk: $mp $size_gb GB '$vname'"
done <<< "$ENTRIES"

# Pre-flight: sort entries into done and pending, and check pending targets are free.
PENDING=""
DONE=""
while IFS='|' read -r disk mp size_gb vname bus unit uuid lun; do
    if already_configured "$vname" "$mp"; then
        DONE+="$disk|$mp|$size_gb|$vname"$'\n'
        continue
    fi
    vgs "vg_$vname" >/dev/null 2>&1 && fail "Disk $disk: volume group vg_$vname already exists but is not mounted at $mp."
    mountpoint -q "$mp" 2>/dev/null && fail "Disk $disk: $mp is already a mount point."
    [ -e "$mp" ] && [ ! -d "$mp" ] && fail "Disk $disk: $mp exists and is not a directory."
    [ -d "$mp" ] && [ -n "$(ls -A "$mp")" ] && fail "Disk $disk: $mp is not empty; mounting over it would hide its contents."
    PENDING+="$disk|$mp|$size_gb|$vname|$bus|$unit|$uuid|$lun"$'\n'
done <<< "$ENTRIES"
PENDING_COUNT=$(printf '%s' "$PENDING" | grep -c . || true)

# Wait for the hot-added disks to show up.
CANDIDATES=""
if [ "$PENDING_COUNT" -gt 0 ]; then
    deadline=$(( $(date +%s) + WAIT_FOR_DISKS_SECONDS ))
    while :; do
        rescan_scsi
        CANDIDATES=$(unused_disks)
        count=$(printf '%s' "$CANDIDATES" | grep -c . || true)
        [ "$count" -ge "$PENDING_COUNT" ] && break
        [ "$(date +%s)" -ge "$deadline" ] &&
            fail "Found $count unused disks but $PENDING_COUNT are still to configure after waiting $WAIT_FOR_DISKS_SECONDS seconds."
        echo "Waiting for disks: $count of $PENDING_COUNT unused disks visible"
        sleep 10
    done
fi

# Match every pending entry before changing anything.
CLAIMED=""
PLAN=""
while IFS='|' read -r disk mp size_gb vname bus unit uuid lun; do
    [ -n "$disk" ] || continue
    match=""
    how=""
    nuuid=$(hex_id "$uuid")
    if [ "$(printf '%s' "$nuuid" | wc -c)" -ge 16 ]; then
        found=$(find_by_serial "$nuuid")
        if [ "$(printf '%s' "$found" | grep -c .)" -eq 1 ]; then match=$found; how="serial number = VMDK UUID"; fi
    fi
    if [ -z "$match" ] && [ -n "$lun" ]; then
        found=$(find_by_hctl_field 4 "$lun" "$size_gb")
        if [ "$(printf '%s' "$found" | grep -c .)" -eq 1 ]; then match=$found; how="LUN $lun = Azure LUN"; fi
    fi
    if [ -z "$match" ] && [ -n "$unit" ]; then
        found=$(find_by_hctl_field 3 "$unit" "$size_gb")
        if [ "$(printf '%s' "$found" | grep -c .)" -eq 1 ]; then match=$found; how="SCSI target $unit = vCenter unit"; fi
    fi
    if [ -z "$match" ]; then
        match=$(find_by_order "$size_gb" | head -n 1)
        [ -n "$match" ] && how="attach order and size"
    fi
    [ -n "$match" ] || fail "Disk $disk ($mp $size_gb GB): no unused guest disk of that size was found."
    size_ok "$(candidate_field "$match" 2)" "$size_gb" ||
        fail "Disk $disk: /dev/$match matched by $how is $(( $(candidate_field "$match" 2) / 1073741824 )) GB, but $size_gb GB was ordered."
    CLAIMED="$CLAIMED $match"
    vcenter="n/a"
    if [ -n "$unit" ]; then vcenter="SCSI($bus:$unit)"; elif [ -n "$lun" ]; then vcenter="LUN $lun"; fi
    PLAN+="$disk|$mp|$size_gb|$vname|$match|$(candidate_field "$match" 3)|$vcenter|$how"$'\n'
done <<< "$PENDING"

if [ -n "$PLAN" ] && [ ! -e /etc/fstab.cloudbolt-data-disks.bak ]; then
    cp -p /etc/fstab /etc/fstab.cloudbolt-data-disks.bak || fail "Could not back up /etc/fstab."
fi

while IFS='|' read -r disk mp size_gb vname dev hctl vcenter how; do
    [ -n "$disk" ] || continue
    vg="vg_$vname"
    lv="lv_$vname"
    echo "Disk $disk: /dev/$dev (H:C:T:L $hctl, matched by $how) -> $mp '$vname'"
    pvcreate -y "/dev/$dev" || fail "pvcreate /dev/$dev failed."
    vgcreate "$vg" "/dev/$dev" || fail "vgcreate $vg failed."
    lvcreate -y -n "$lv" -l 100%FREE "$vg" || fail "lvcreate $vg/$lv failed."
    mkfs.xfs -L "$vname" "/dev/$vg/$lv" || fail "mkfs.xfs on /dev/$vg/$lv failed."
    fs_uuid=$(blkid -s UUID -o value "/dev/$vg/$lv")
    [ -n "$fs_uuid" ] || fail "Could not read the file system UUID of /dev/$vg/$lv."
    mkdir -p "$mp" || fail "Could not create $mp."
    if ! grep -q "UUID=$fs_uuid" /etc/fstab; then
        printf 'UUID=%s %s xfs %s 0 0\n' "$fs_uuid" "$mp" "$FS_MOUNT_OPTIONS" >> /etc/fstab || fail "Could not update /etc/fstab."
    fi
    systemctl daemon-reload >/dev/null 2>&1
    mount "$mp" || fail "mount $mp failed."
    command -v restorecon >/dev/null 2>&1 && restorecon -R "$mp"
done <<< "$PLAN"

echo
printf '%-5s %-16s %-8s %-13s %-10s %-10s %-10s %-30s %s\n' "Disk" "Mount point" "Size GB" "Volume" "Hypervisor" "Guest" "H:C:T:L" "Matched by" "Result"
while IFS='|' read -r disk mp size_gb vname dev hctl vcenter how; do
    [ -n "$disk" ] || continue
    printf '%-5s %-16s %-8s %-13s %-10s %-10s %-10s %-30s %s\n' "$disk" "$mp" "$size_gb" "$vname" "$vcenter" "/dev/$dev" "$hctl" "$how" "formatted"
done <<< "$PLAN"
while IFS='|' read -r disk mp size_gb vname; do
    [ -n "$disk" ] || continue
    printf '%-5s %-16s %-8s %-13s %-10s %-10s %-10s %-30s %s\n' "$disk" "$mp" "$size_gb" "$vname" "" "" "" "" "already configured"
done <<< "$DONE"

echo
echo "Mounted data volumes:"
while IFS='|' read -r disk mp size_gb vname bus unit uuid lun; do
    df -h --output=source,fstype,size,avail,target "$mp" | tail -n 1
done <<< "$ENTRIES"
exit 0

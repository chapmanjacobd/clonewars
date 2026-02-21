#!/usr/bin/env bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

DEFAULT_PARALLEL=20
DRY_RUN=false
POSITIONAL_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            POSITIONAL_ARGS+=("$1")
            shift
            ;;
    esac
done

set -- "${POSITIONAL_ARGS[@]}"

usage() {
    echo "Usage: sudo $0 [-n] <source_device> [batch|sequential] [parallel]"
    exit 1
}

[[ $# -lt 1 ]] && usage

SOURCE="$1"
MODE="${2:-batch}"
PARALLEL="${3:-$DEFAULT_PARALLEL}"

[[ ! -b "$SOURCE" ]] && { echo "Invalid source device"; exit 1; }

############################################
# Identify source partitions
############################################
BOOT_PART=$(lsblk -ln -o NAME,FSTYPE "$SOURCE" | awk '$2=="vfat"{print $1}')
ROOT_PART=$(lsblk -ln -o NAME,FSTYPE "$SOURCE" | awk '$2 ~ /^ext/{print $1}')

[[ -z "$BOOT_PART" || -z "$ROOT_PART" ]] && {
    echo "Could not detect boot (vfat) and root (ext) partitions"
    exit 1
}

BOOT_DEV="/dev/$BOOT_PART"
ROOT_DEV="/dev/$ROOT_PART"

echo "Source: $SOURCE"
echo "Boot:   $BOOT_DEV"
echo "Root:   $ROOT_DEV"
[[ "$DRY_RUN" == "true" ]] && echo "MODE:   DRY RUN (No changes will be made)"
echo

############################################
# Extract source layout info
############################################
SRC_BOOT_START=$(lsblk -b -no START "$BOOT_DEV")
SRC_BOOT_SIZE=$(lsblk -b -no SIZE "$BOOT_DEV")
SRC_ROOT_START=$(lsblk -b -no START "$ROOT_DEV")
BOOT_SIZE_SECTORS=$((SRC_BOOT_SIZE / 512))

echo "Calculating minimum filesystem size..."
block_size=$(dumpe2fs -h "$ROOT_DEV" 2>/dev/null | awk -F: '/Block size/ {gsub(/ /,""); print $2}')
min_blocks=$(resize2fs -P "$ROOT_DEV" 2>/dev/null | awk '{print $NF}')

[[ -z "$block_size" || -z "$min_blocks" ]] && {
    echo "Failed to determine filesystem minimum size"
    exit 1
}

min_fs_bytes=$((min_blocks * block_size))
root_start_bytes=$((SRC_ROOT_START * 512))
required_total_bytes=$((root_start_bytes + min_fs_bytes))

total_human=$(numfmt --to=iec --suffix=B "$required_total_bytes")
echo "Minimum space required: $total_human ($min_fs_bytes fs)"
echo

############################################
# Detect new devices continuously
############################################
BASELINE=$(lsblk -dn -o NAME)
declare -A SEEN
TARGETS=()

echo "Insert microSD cards. Press ENTER when finished."
while true; do
    if read -t 1 -n 1 -s; then break; fi

    udevadm settle
    CURRENT=$(lsblk -dn -o NAME)
    DIFF=$(comm -13 <(echo "$BASELINE" | sort) <(echo "$CURRENT" | sort))

    for d in $DIFF; do
        if [[ -z "${SEEN[$d]:-}" ]]; then
            SEEN[$d]=1
            TARGETS+=("$d")
        fi
    done
    echo -ne "\rTotal devices detected: ${#TARGETS[@]}    "
done

[[ ${#TARGETS[@]} -eq 0 ]] && { echo -e "\nNo targets found."; exit 1; }

echo -e "\n\nTargets:"
printf '  /dev/%s\n' "${TARGETS[@]}"
echo

############################################
# Dry Run Logic (Exit here if -n)
############################################
if [[ "$DRY_RUN" == "true" ]]; then
    echo "Dry run enabled. Validating target sizes..."
    for t in "${TARGETS[@]}"; do
        disk="/dev/$t"
        # blockdev can fail if device is unstable; || echo 0 prevents script death
        tgt_bytes=$(blockdev --getsize64 "$disk" 2>/dev/null || echo 0)
        if (( tgt_bytes < required_total_bytes )); then
            echo "[-] $disk: TOO SMALL ($((tgt_bytes/1024/1024)) MB)"
        else
            echo "[+] $disk: OK ($((tgt_bytes/1024/1024)) MB)"
        fi
    done
    echo -e "\nDry run complete. No data was written."
    exit 0
fi

read -rp "Press ENTER to begin cloning..."

############################################
# Clone function
############################################
clone_target() {
    local disk="/dev/$1"
    echo "-----"
    echo "Processing $disk"

    # Robust unmount
    mount | grep "^${disk}" | cut -d' ' -f1 | xargs -r umount || true

    tgt_bytes=$(blockdev --getsize64 "$disk" 2>/dev/null || echo 0)
    if (( tgt_bytes < required_total_bytes )); then
        echo "Skipping $disk — too small"
        return
    fi

    echo "Rebuilding partition table..."
    wipefs -a "$disk"

    sfdisk "$disk" <<EOF
label: dos
unit: sectors
${disk}1 : start=${SRC_BOOT_START}, size=${BOOT_SIZE_SECTORS}, type=c
${disk}2 : start=${SRC_ROOT_START}, type=83
EOF

    # Force kernel to see new partitions
    udevadm settle
    partprobe "$disk"
    sleep 2

    NEW_BOOT="${1}1" # Typical for sdb -> sdb1
    [[ ! -b "/dev/$NEW_BOOT" ]] && NEW_BOOT="${1}p1" # Typical for mmcblk0 -> mmcblk0p1

    NEW_ROOT="${1}2"
    [[ ! -b "/dev/$NEW_ROOT" ]] && NEW_ROOT="${1}p2"

    echo "Copying boot partition to /dev/$NEW_BOOT..."
    dd if="$BOOT_DEV" of="/dev/$NEW_BOOT" bs=4M conv=fsync status=progress

    echo "Restoring root filesystem to /dev/$NEW_ROOT..."
    e2image -ra "$ROOT_DEV" "/dev/$NEW_ROOT"

    echo "Expanding /dev/$NEW_ROOT..."
    echo ", +" | sfdisk -N2 "$disk"
    udevadm settle
    partprobe "$disk"

    resize2fs "/dev/$NEW_ROOT"
    echo "Finished $disk"
}

############################################
# Execute
############################################
if [[ "$MODE" == "sequential" ]]; then
    for t in "${TARGETS[@]}"; do
        clone_target "$t"
    done
else
    running=0
    for t in "${TARGETS[@]}"; do
        clone_target "$t" &
        ((running++))
        if [[ $running -ge $PARALLEL ]]; then
            wait -n
            ((running--))
        fi
    done
    wait
fi

echo -e "\nAll cloning complete."

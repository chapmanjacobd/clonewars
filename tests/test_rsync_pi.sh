#!/bin/bash
set -e
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}Test: clone_rsync.py with Pi Layout (FAT32+EXT4)${NC}"
cd "$(dirname "$0")/.."

cleanup() {
    if mountpoint -q mnt_src; then sudo umount -l mnt_src || true; fi
    if mountpoint -q mnt_tgt; then sudo umount -l mnt_tgt || true; fi
    for img in source_rsync_pi.img target_rsync_pi.img; do
        if [ -f "$img" ]; then
            LOOP_DEVS=$(sudo losetup -j "$img" | cut -d: -f1)
            for dev in $LOOP_DEVS; do sudo losetup -d "$dev" || true; done
        fi
    done
    sudo rm -rf .tmp mnt_src mnt_tgt source_rsync_pi.img target_rsync_pi.img
}
trap cleanup EXIT

truncate -s 500M source_rsync_pi.img
echo "label: dos
unit: sectors
source_rsync_pi.img1 : start=2048, size=131072, type=c
source_rsync_pi.img2 : start=133120, type=83" | sfdisk source_rsync_pi.img

LOOP_SRC=$(sudo losetup -fP --show source_rsync_pi.img)
sudo mkfs.vfat "${LOOP_SRC}p1"
sudo mkfs.ext4 -F "${LOOP_SRC}p2"

mkdir -p mnt_src
sudo mount "${LOOP_SRC}p2" mnt_src
sudo bash -c "echo 'rsync pi data' > mnt_src/os.txt"
sudo umount mnt_src

truncate -s 500M target_rsync_pi.img
(
  sleep 3
  sudo losetup -f target_rsync_pi.img
  sleep 3
  echo ""
  sleep 2
  echo ""
) | sudo python3 clone_rsync.py "${LOOP_SRC}"

LOOP_TGT=$(sudo losetup -fP --show target_rsync_pi.img)
mkdir -p mnt_tgt
sudo mount "${LOOP_TGT}p2" mnt_tgt
if grep -q "rsync pi data" mnt_tgt/os.txt; then
    echo -e "${GREEN}PASSED${NC}"
else
    echo -e "${RED}FAILED${NC}"
    exit 1
fi

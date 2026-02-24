#!/bin/bash
set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}Starting Cloning Tests...${NC}"

# Ensure we are in the root directory
cd "$(dirname "$0")/.."

# Function to cleanup loop devices
cleanup() {
    echo "Cleaning up..."
    # Unmount if mounted
    if mountpoint -q mnt_src; then sudo umount -l mnt_src || true; fi
    if mountpoint -q mnt_tgt; then sudo umount -l mnt_tgt || true; fi
    
    # Detach all loop devices associated with our test images
    for img in source.img target.img android_source.img android_target.img; do
        if [ -f "$img" ]; then
            LOOP_DEVS=$(sudo losetup -j "$img" | cut -d: -f1)
            for dev in $LOOP_DEVS; do
                sudo losetup -d "$dev" || true
            done
        fi
    done
    sudo rm -rf .tmp mnt_src mnt_tgt source.img target.img android_source.img android_target.img
}

trap cleanup EXIT

# 1. Test Case: Boot (FAT32) + Root (EXT4) - Typical Raspberry Pi layout
echo -e "${GREEN}Test Case 1: Boot (FAT32) + Root (EXT4)${NC}"

# Create a 500MB source image
truncate -s 500M source.img
echo "label: dos
unit: sectors
source.img1 : start=2048, size=131072, type=c
source.img2 : start=133120, type=83" | sfdisk source.img

LOOP_SRC=$(sudo losetup -fP --show source.img)
sudo mkfs.vfat "${LOOP_SRC}p1"
sudo mkfs.ext4 -F "${LOOP_SRC}p2"

# Add data to root
mkdir -p mnt_src
sudo mount "${LOOP_SRC}p2" mnt_src
sudo bash -c "echo 'system data' > mnt_src/os.txt"
sudo umount mnt_src

# Create target image (slightly smaller to test shrinking)
truncate -s 450M target.img

echo "Testing clone_dd.py..."
# We use a subshell to simulate insertion and keypresses
(
  sleep 3
  sudo losetup -f target.img
  sleep 3
  echo "" # First Enter: Finish detection
  sleep 2
  echo "" # Second Enter: Start cloning
) | sudo python3 clone_dd.py --skip-zerofill "${LOOP_SRC}"

# Verify target
LOOP_TGT=$(sudo losetup -j target.img | cut -d: -f1 | head -n1)
# The script might have detached it or not. Let's ensure it's attached with partitions.
if [ -z "$LOOP_TGT" ]; then
    LOOP_TGT=$(sudo losetup -fP --show target.img)
else
    sudo partprobe "$LOOP_TGT"
fi

mkdir -p mnt_tgt
sudo mount "${LOOP_TGT}p2" mnt_tgt
if grep -q "system data" mnt_tgt/os.txt; then
    echo -e "${GREEN}clone_dd.py verification PASSED${NC}"
else
    echo -e "${RED}clone_dd.py verification FAILED${NC}"
    exit 1
fi
sudo umount mnt_tgt
sudo losetup -d "$LOOP_TGT"

echo "Testing clone_rsync.py..."
# Reset target
truncate -s 450M target.img
(
  sleep 3
  sudo losetup -f target.img
  sleep 3
  echo "" 
  sleep 2
  echo ""
) | sudo python3 clone_rsync.py "${LOOP_SRC}"

# Verify target
LOOP_TGT=$(sudo losetup -fP --show target.img)
sudo mount "${LOOP_TGT}p2" mnt_tgt
if grep -q "system data" mnt_tgt/os.txt; then
    echo -e "${GREEN}clone_rsync.py verification PASSED${NC}"
else
    echo -e "${RED}clone_rsync.py verification FAILED${NC}"
    exit 1
fi
sudo umount mnt_tgt
sudo losetup -d "$LOOP_TGT"

# 2. Test Case: Android MicroSD (Large FAT32/exFAT partition)
echo -e "${GREEN}Test Case 2: Android MicroSD (Single FAT32 partition)${NC}"

truncate -s 500M android_source.img
echo "label: dos
unit: sectors
android_source.img1 : start=2048, type=c" | sfdisk android_source.img

LOOP_AND_SRC=$(sudo losetup -fP --show android_source.img)
sudo mkfs.vfat "${LOOP_AND_SRC}p1"

mkdir -p mnt_src
sudo mount "${LOOP_AND_SRC}p1" mnt_src
sudo bash -c "echo 'photos and videos' > mnt_src/dcim.txt"
sudo umount mnt_src

truncate -s 500M android_target.img

echo "Testing clone_dd.py with Android-like layout..."
(
  sleep 3
  sudo losetup -f android_target.img
  sleep 3
  echo ""
  sleep 2
  echo ""
) | sudo python3 clone_dd.py --skip-zerofill "${LOOP_AND_SRC}"

# Verify
LOOP_AND_TGT=$(sudo losetup -fP --show android_target.img)
sudo mount "${LOOP_AND_TGT}p1" mnt_tgt
if grep -q "photos and videos" mnt_tgt/dcim.txt; then
    echo -e "${GREEN}Android clone_dd.py verification PASSED${NC}"
else
    echo -e "${RED}Android clone_dd.py verification FAILED${NC}"
    exit 1
fi
sudo umount mnt_tgt
sudo losetup -d "$LOOP_AND_TGT"

echo "Testing clone_rsync.py with Android-like layout..."
truncate -s 500M android_target.img
(
  sleep 3
  sudo losetup -f android_target.img
  sleep 3
  echo ""
  sleep 2
  echo ""
) | sudo python3 clone_rsync.py "${LOOP_AND_SRC}"

# Verify
LOOP_AND_TGT=$(sudo losetup -fP --show android_target.img)
sudo mount "${LOOP_AND_TGT}p1" mnt_tgt
if grep -q "photos and videos" mnt_tgt/dcim.txt; then
    echo -e "${GREEN}Android clone_rsync.py verification PASSED${NC}"
else
    echo -e "${RED}Android clone_rsync.py verification FAILED${NC}"
    exit 1
fi
sudo umount mnt_tgt
sudo losetup -d "$LOOP_AND_TGT"

echo -e "${GREEN}All tests completed successfully!${NC}"

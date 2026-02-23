#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor


def run_cmd(cmd, shell=False, capture=False):
    res = subprocess.run(
        cmd, shell=shell, check=True, text=True, capture_output=capture
    )
    return res.stdout.strip() if capture else res


def get_layout(args):
    # Detect base source disk (e.g., /dev/sdb)
    source_disk = run_cmd(
        f"lsblk -no PKNAME {args.source} | head -n1", shell=True, capture=True
    )
    if not source_disk:  # If args.source is already a disk, not a partition
        source_disk = args.source.replace("/dev/", "")
    source_disk_path = f"/dev/{source_disk}"

    # Get all partitions with their names and fstypes
    parts = run_cmd(
        f"lsblk -ln -o NAME,FSTYPE {source_disk_path}", shell=True, capture=True
    ).splitlines()

    partition_list = []
    for line in parts:
        parts_split = line.split()
        if len(parts_split) < 1:
            continue
        name = parts_split[0]
        fstype = parts_split[1] if len(parts_split) > 1 else ""

        if name != source_disk:
            if not fstype:
                try:
                    fstype = run_cmd(
                        f"blkid -s TYPE -o value /dev/{name}", shell=True, capture=True
                    )
                except Exception:
                    fstype = ""
            partition_list.append({"name": name, "fstype": fstype})

    boot_dev = None
    root_dev = None
    root_fstype = None

    if len(partition_list) == 1:
        # Single partition case
        root_dev = f"/dev/{partition_list[0]['name']}"
        root_fstype = partition_list[0]["fstype"]
    else:
        # Try to find boot (vfat) and root (ext)
        for p in partition_list:
            if p["fstype"] == "vfat" and not boot_dev:
                boot_dev = f"/dev/{p['name']}"
            elif p["fstype"].startswith("ext") and not root_dev:
                root_dev = f"/dev/{p['name']}"
                root_fstype = p["fstype"]

        # Fallback: if we didn't find the classic pair, use the last partition as root
        if not root_dev and partition_list:
            root_dev = f"/dev/{partition_list[-1]['name']}"
            root_fstype = partition_list[-1]["fstype"]

    if not root_dev:
        print(f"DEBUG: lsblk output for {source_disk_path}:")
        print(
            run_cmd(
                f"lsblk -ln -o NAME,FSTYPE {source_disk_path}", shell=True, capture=True
            )
        )
        print(f"Could not detect partitions on {source_disk_path}")
        sys.exit(1)

    # Get UUID of source root
    root_uuid = run_cmd(f"blkid -s UUID -o value {root_dev}", shell=True, capture=True)
    root_start = int(run_cmd(["lsblk", "-b", "-no", "START", root_dev], capture=True))

    boot_start = 0
    boot_size_sectors = 0
    if boot_dev:
        boot_start = int(
            run_cmd(["lsblk", "-b", "-no", "START", boot_dev], capture=True)
        )
        boot_size = int(run_cmd(["lsblk", "-b", "-no", "SIZE", boot_dev], capture=True))
        boot_size_sectors = boot_size // 512

    # Estimation of required total bytes
    if root_fstype.startswith("ext"):
        block_size = int(
            run_cmd(
                f"dumpe2fs -h {root_dev} 2>/dev/null | awk -F: '/Block size/ {{gsub(/ /,\"\"); print $2}}'",
                shell=True,
                capture=True,
            )
        )
        min_blocks = int(
            run_cmd(
                f"resize2fs -P {root_dev} 2>/dev/null | awk '{{print $NF}}'",
                shell=True,
                capture=True,
            )
        )
        min_fs_bytes = min_blocks * block_size
    else:
        # For non-ext, just use the current partition size as estimate
        min_fs_bytes = int(
            run_cmd(["lsblk", "-b", "-no", "SIZE", root_dev], capture=True)
        )

    required_total_bytes = (root_start * 512) + min_fs_bytes

    # Get Disk ID (label-id) to preserve PARTUUIDs
    disk_id = run_cmd(
        f"sfdisk -d {source_disk_path} | grep 'label-id' | awk '{{print $2}}'",
        shell=True,
        capture=True,
    )

    return {
        "source_disk": source_disk_path,
        "disk_id": disk_id,
        "boot_dev": boot_dev,
        "root_dev": root_dev,
        "root_uuid": root_uuid,
        "root_fstype": root_fstype,
        "boot_start": boot_start,
        "boot_size_sectors": boot_size_sectors,
        "root_start": root_start,
        "required_total_bytes": required_total_bytes,
        "is_single_partition": boot_dev is None,
    }


def get_partition_nodes(disk):
    try:
        out = subprocess.check_output(
            ["lsblk", "-ln", "-o", "NAME", disk], text=True
        ).splitlines()
        # Filter to only keep partitions (children of the disk)
        partitions = []
        for line in out:
            p = line.strip()
            if p and f"/dev/{p}" != disk:
                partitions.append(f"/dev/{p}")
        return partitions
    except Exception as e:
        print(f"Error resolving partitions for {disk}: {e}")
        return []


def clone_target(args, layout, src_mnt, target):
    disk = f"/dev/{target}"
    dst_mnt = os.path.abspath(f"./.tmp/dst_{target}")
    verbose = args.verbose
    if verbose:
        print(f"Processing {disk}")
    try:
        tgt_bytes = int(run_cmd(["blockdev", "--getsize64", disk], capture=True))
        if tgt_bytes < layout["required_total_bytes"]:
            print(f"Skipping {disk} - too small")
            return

        # 1. Prepare Target
        subprocess.run(
            f"mount | grep '^{disk}' | cut -d' ' -f1 | xargs -r umount",
            shell=True,
            capture_output=not verbose,
        )
        run_cmd(["wipefs", "-a", disk], capture=not verbose)

        # 2. Rebuild Partition Table
        if layout["is_single_partition"]:
            sfdisk_input = (
                f"label: dos\nlabel-id: {layout['disk_id']}\nunit: sectors\n"
                f"{disk}1 : start={layout['root_start']}, type=83\n"
            )
        else:
            sfdisk_input = (
                f"label: dos\nlabel-id: {layout['disk_id']}\nunit: sectors\n"
                f"{disk}1 : start={layout['boot_start']}, size={layout['boot_size_sectors']}, type=c\n"
                f"{disk}2 : start={layout['root_start']}, type=83\n"
            )

        subprocess.run(
            ["sfdisk", disk],
            input=sfdisk_input,
            text=True,
            check=True,
            capture_output=not verbose,
        )
        run_cmd(["udevadm", "settle"], capture=not verbose)
        run_cmd(["partprobe", disk], capture=not verbose)
        time.sleep(1)

        partitions = get_partition_nodes(disk)
        if layout["is_single_partition"]:
            if len(partitions) < 1:
                raise RuntimeError(f"Could not find partitions on {disk}")
            p_root = partitions[0]
            p_boot = None
        else:
            if len(partitions) < 2:
                raise RuntimeError(f"Could not find 2 partitions on {disk}")
            p_boot = partitions[0]
            p_root = partitions[1]

        # 3. Format Root and copy Boot
        if verbose:
            print(f"[{target}] Formatting {p_root} as {layout['root_fstype']}...")

        fstype = layout["root_fstype"]
        uuid = layout["root_uuid"]

        if fstype.startswith("ext"):
            run_cmd(["mkfs.ext4", "-q", "-F", "-m", "1", "-U", uuid, p_root], capture=not verbose)
        elif fstype == "ntfs":
            run_cmd(["mkfs.ntfs", "-Q", "-F", p_root], capture=not verbose)
        elif fstype == "vfat" or fstype == "fat32":
            run_cmd(["mkfs.vfat", p_root], capture=not verbose)
        elif fstype == "exfat":
            run_cmd(["mkfs.exfat", p_root], capture=not verbose)
        else:  # Default to ext4 if unknown
            run_cmd(
                [
                    "mkfs.ext4",
                    "-q",
                    "-F",
                    "-T",
                    "largefile",
                    "-m",
                    "0",
                    "-e",
                    "continue",
                    p_root,
                ],
                capture=not verbose,
            )

        if p_boot and layout["boot_dev"]:
            if verbose:
                print(f"[{target}] Streaming boot partition to {p_boot}...")
            run_cmd(
                [
                    "dd",
                    f"if={layout['boot_dev']}",
                    f"of={p_boot}",
                    "bs=4M",
                    "conv=sparse,fsync",
                ],
                capture=not verbose,
            )

        # 4. Sync Files via Rsync/Fpsync
        if verbose:
            print(f"[{target}] Syncing files from shared source...")
        os.makedirs(dst_mnt, exist_ok=True)
        try:
            # Mount with appropriate options if needed
            run_cmd(["mount", p_root, dst_mnt], capture=not verbose)

            if args.fpsync:
                cmd = [
                    "fpsync",
                    "-n",
                    str(args.fpsync),
                    "-v",
                    "-o",
                    r"-lptgoDHAX --numeric-ids --inplace --filter=-x\ security.selinux",
                ]
            else:
                if layout["root_fstype"].startswith("ext"):
                    cmd = [
                        "rsync",
                        "-aHAX",
                        "--numeric-ids",
                        "--inplace",
                        "--filter=-x security.selinux",
                        "--one-file-system",
                    ]
                else:
                    # Simpler flags for FAT/NTFS/exFAT to avoid permission errors
                    cmd = [
                        "rsync",
                        "-rtv",
                        "--inplace",
                        "--one-file-system",
                    ]

            run_cmd(cmd + [f"{src_mnt}/", f"{dst_mnt}/"], capture=not verbose)
        finally:
            subprocess.run(
                ["umount", "-l", dst_mnt],
                stderr=subprocess.DEVNULL,
                capture_output=not verbose,
            )
            if os.path.exists(dst_mnt):
                os.rmdir(dst_mnt)

        run_cmd(["blockdev", "--flushbufs", disk], capture=not verbose)
        if verbose:
            print(f"[{disk}] Complete")
    except Exception as e:
        print(f"FAILED {disk}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fpsync", type=int, help="Use fpsync with N workers instead of rsync"
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="Number of parallel clones (1 = sequential)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("-n", "--dry-run", action="store_true")

    parser.add_argument("source")
    args = parser.parse_args()

    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable] + sys.argv)

    loop_dev = None
    if os.path.isfile(args.source):
        if args.verbose:
            print(f"Setting up loop device for {args.source}...")
        loop_dev = run_cmd(["losetup", "-fP", "--show", args.source], capture=True)
        run_cmd(["udevadm", "settle"])
        run_cmd(["partprobe", loop_dev])
        # Use the loop device as the source for the rest of the script
        args.source = loop_dev

    try:
        layout = get_layout(args)
        print(f"Source: {layout['source_disk']}")
        print(f"Required: {layout['required_total_bytes'] / 1e9:.2f} GB")

        baseline = set(run_cmd(["lsblk", "-dn", "-o", "NAME"], capture=True).split())
        targets = []
        print("Insert cards. Press Enter when ready.")

        import select

        while True:
            if select.select([sys.stdin], [], [], 0.1)[0]:
                sys.stdin.readline()
                break
            current = set(run_cmd(["lsblk", "-dn", "-o", "NAME"], capture=True).split())
            for dev in current - baseline:
                if dev not in targets and dev != layout["source_disk"].replace(
                    "/dev/", ""
                ):
                    try:
                        # Filter out empty slots (e.g., multi-card readers without media)
                        size = int(
                            run_cmd(
                                ["blockdev", "--getsize64", f"/dev/{dev}"], capture=True
                            )
                        )
                        if size > 0:
                            targets.append(dev)
                    except Exception:
                        continue
            print(f"\rDetected: {len(targets)}", end="", flush=True)

        if not targets:
            print("\nNo targets.")
            return

        print(f"\nTargets:\n{'\n'.join(['/dev/' + t for t in targets])}")

        if args.dry_run:
            print("\nDry run results:")
            for t in targets:
                size = int(
                    run_cmd(["blockdev", "--getsize64", f"/dev/{t}"], capture=True)
                )
                status = "OK" if size >= layout["required_total_bytes"] else "TOO SMALL"
                print(f"  /dev/{t}: {status} ({size / 1e9:.2f} GB)")
            return

        input("\nPress Enter to start... or ctrl-c to cancel")

        # Mount source root partition once
        src_mnt = os.path.abspath("./.tmp/shared_src_root")
        os.makedirs(src_mnt, exist_ok=True)
        if args.verbose:
            print(f"Mounting source {layout['root_dev']} to {src_mnt}...")
        run_cmd(["mount", "-o", "ro", layout["root_dev"], src_mnt])

        try:
            if args.threads == 1:
                for t in targets:
                    clone_target(args, layout, src_mnt, t)
            else:
                with ProcessPoolExecutor(max_workers=args.threads) as executor:
                    # Map the shared source mount to all processes
                    executor.map(
                        clone_target,
                        [args] * len(targets),
                        [layout] * len(targets),
                        [src_mnt] * len(targets),
                        targets,
                    )
        finally:
            if args.verbose:
                print("Cleaning up source mount...")
            subprocess.run(["umount", src_mnt], stderr=subprocess.DEVNULL)
            if os.path.exists(src_mnt):
                os.rmdir(src_mnt)
    finally:
        if loop_dev:
            if args.verbose:
                print(f"Detaching {loop_dev}...")
            run_cmd(["losetup", "-d", loop_dev])

    print("Final Sync")
    os.sync()
    print("Done")


if __name__ == "__main__":
    main()

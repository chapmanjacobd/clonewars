#!/usr/bin/env python3
import argparse
import math
import os
import subprocess
import sys
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

    # Filter out the disk itself (first entry usually, but let's be safe)
    # lsblk -ln -o NAME,FSTYPE /dev/sdb gives:
    # sdb
    # sdb1 vfat
    # sdb2 ext4

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

    # Get index for resizepart (e.g., /dev/sdb2 -> 2)
    root_idx = run_cmd(f"lsblk -no PARTN {root_dev}", shell=True, capture=True)

    return {
        "source_disk": source_disk_path,
        "boot_dev": boot_dev,
        "root_dev": root_dev,
        "root_fstype": root_fstype,
        "root_idx": root_idx,
        "root_start": int(
            run_cmd(["lsblk", "-b", "-no", "START", root_dev], capture=True)
        ),
        "root_size": int(
            run_cmd(["lsblk", "-b", "-no", "SIZE", root_dev], capture=True)
        ),
    }


def shrink_source(args, layout):
    root_dev = layout["root_dev"]
    fstype = layout["root_fstype"]

    if not fstype or not fstype.startswith("ext"):
        if args.verbose:
            print(f"Skipping shrink for non-ext filesystem: {fstype}")
        return (layout["root_start"] * 512) + layout["root_size"]

    print(f"Shrinking Source: {root_dev} ({fstype})")

    # 1. Zero-fill root
    if not args.skip_zerofill:
        tmp_mnt = os.path.abspath("./.tmp/shrink_mnt")
        os.makedirs(tmp_mnt, exist_ok=True)
        try:
            run_cmd(["mount", root_dev, tmp_mnt])
            print("Zero-filling unused blocks...")
            subprocess.run(
                f"cat /dev/zero > {tmp_mnt}/zero.fill 2>/dev/null || true", shell=True
            )
            run_cmd(["rm", "-f", f"{tmp_mnt}/zero.fill"])
            run_cmd(["umount", tmp_mnt])
        except Exception as e:
            print(f"Warning during zero-fill: {e}")

    # 2. Shrink FS
    run_cmd(["e2fsck", "-p", "-f", root_dev])
    run_cmd(["resize2fs", "-M", root_dev])

    # 3. Calculate new size
    block_size = int(
        run_cmd(
            f"tune2fs -l {root_dev} | grep '^Block size' | awk '{{print $NF}}'",
            shell=True,
            capture=True,
        )
    )
    block_count = int(
        run_cmd(
            f"tune2fs -l {root_dev} | grep '^Block count' | awk '{{print $NF}}'",
            shell=True,
            capture=True,
        )
    )

    # Add 200MB safety buffer
    fs_size_bytes = (block_size * block_count) + (200 * 1024 * 1024)
    root_start_bytes = layout["root_start"] * 512
    new_end_byte = root_start_bytes + fs_size_bytes

    # 4. Shrink Partition
    print(f"Resizing partition {layout['root_idx']} to end at {new_end_byte} bytes")
    cmd = [
        "parted",
        "---pretend-input-tty",
        "---pretend-input-tty",
        layout["source_disk"],
        "unit",
        "B",
        "resizepart",
        layout["root_idx"],
        str(new_end_byte),
    ]
    yes_proc = subprocess.Popen(["yes", "Yes"], stdout=subprocess.PIPE)
    subprocess.run(
        cmd, stdin=yes_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    yes_proc.terminate()

    run_cmd(["udevadm", "settle"])

    return new_end_byte


def clone_target(args, layout, cutoff_byte, target):
    dest_disk = f"/dev/{target}"
    verbose = args.verbose
    if verbose:
        print(f"Processing {dest_disk}")
    try:
        # 1. Unmount targets
        subprocess.run(
            f"mount | grep '^{dest_disk}' | cut -d' ' -f1 | xargs -r umount",
            shell=True,
            capture_output=not verbose,
        )

        # 2. Copy using DD
        # bs=4M is efficient for SD cards. count is calculated to stop after shrunken root.
        copy_count = math.ceil(cutoff_byte / (4 * 1024 * 1024))
        if verbose:
            print(f"[{target}] Copying {copy_count * 4}MB from source...")
        run_cmd(
            [
                "dd",
                f"if={layout['source_disk']}",
                f"of={dest_disk}",
                "bs=4M",
                f"count={copy_count}",
                "conv=sparse,fsync",
            ],
            capture=not verbose,
        )

        # 3. Fix Partition Table and Expand Target (Always expand target)
        if verbose:
            print(f"[{target}] Expanding partition to 100%...")
        run_cmd(
            ["parted", "-s", dest_disk, "resizepart", layout["root_idx"], "100%"],
            capture=not verbose,
        )
        run_cmd(["udevadm", "settle"], capture=not verbose)

        # Identify partition node on target using PARTN
        root_tgt_name = run_cmd(
            f"lsblk -ln -o NAME,PARTN {dest_disk} | awk '$2==\"{layout['root_idx']}\" {{print $1}}'",
            shell=True,
            capture=True,
        )
        if not root_tgt_name:
            print(
                f"[{target}] FAILED: Could not find partition {layout['root_idx']} on target"
            )
            return
        root_tgt = f"/dev/{root_tgt_name}"

        if layout["root_fstype"] and layout["root_fstype"].startswith("ext"):
            run_cmd(["e2fsck", "-p", "-f", root_tgt], capture=not verbose)
            run_cmd(["resize2fs", root_tgt], capture=not verbose)

        if verbose:
            print(f"[{target}] Complete")
    except Exception as e:
        print(f"FAILED {dest_disk}: {e}")


def restore_source(layout):
    print("Restoring Source Disk")
    run_cmd(
        [
            "parted",
            "-s",
            layout["source_disk"],
            "resizepart",
            layout["root_idx"],
            "100%",
        ]
    )
    run_cmd(["udevadm", "settle"])
    if layout["root_fstype"] and layout["root_fstype"].startswith("ext"):
        run_cmd(["resize2fs", layout["root_dev"]])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-zerofill",
        action="store_true",
        help="Skip zero-filling the source filesystem",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="Number of parallel clones (1 = sequential)",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Dry run: show what would be done without modifying disks",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    parser.add_argument("source", help="/dev/sdb or /dev/sdb2")
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
        source_end = (layout["root_start"] * 512) + layout["root_size"]

        print(f"Source: {layout['source_disk']}")
        print(f"Required: {source_end / 1e9:.2f} GB")

        # Detection loop
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
            print("\nNo targets found. Exiting.")
            return

        print(f"\nTargets:\n{'\n'.join(['/dev/' + t for t in targets])}")

        # Find smallest target size
        min_target_size = min(
            int(run_cmd(["blockdev", "--getsize64", f"/dev/{t}"], capture=True))
            for t in targets
        )
        # Shrink if source partition end is within 64MB of or exceeds target capacity
        needs_shrink = source_end > (min_target_size - 64 * 1024 * 1024)

        if args.dry_run:
            if needs_shrink:
                # Estimate minimum size using resize2fs -P
                block_size = int(
                    run_cmd(
                        f"tune2fs -l {layout['root_dev']} | grep '^Block size' | awk '{{print $NF}}'",
                        shell=True,
                        capture=True,
                    )
                )
                min_blocks = int(
                    run_cmd(
                        f"resize2fs -P {layout['root_dev']} 2>/dev/null | awk '{{print $NF}}'",
                        shell=True,
                        capture=True,
                    )
                )
                # Include the 200MB safety buffer used in shrink_source
                required = (
                    (layout["root_start"] * 512)
                    + (min_blocks * block_size)
                    + (200 * 1024 * 1024)
                )
            else:
                required = source_end

            print(
                f"\nDry run results (Required: {required / 1e9:.2f} GB, Needs Shrink: {needs_shrink}):"
            )
            for t in targets:
                size = int(
                    run_cmd(["blockdev", "--getsize64", f"/dev/{t}"], capture=True)
                )
                status = "OK" if size >= required else "TOO SMALL"
                print(f"  /dev/{t}: {status} ({size / 1e9:.2f} GB)")
            return

        # Phase preparation
        if needs_shrink:
            cutoff_byte = shrink_source(args, layout)
        else:
            cutoff_byte = source_end

        input("\nPress Enter to start... or ctrl-c to cancel")

        # Clone Phase
        try:
            if args.threads == 1:
                for t in targets:
                    clone_target(args, layout, cutoff_byte, t)
            else:
                with ProcessPoolExecutor(max_workers=args.threads) as executor:
                    executor.map(
                        clone_target,
                        [args] * len(targets),
                        [layout] * len(targets),
                        [cutoff_byte] * len(targets),
                        targets,
                    )
        finally:
            # Expansion Phase (Ensure source is restored even if clone fails)
            if needs_shrink:
                restore_source(layout)
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

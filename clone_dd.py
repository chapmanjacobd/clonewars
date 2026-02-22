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

    boot_part = run_cmd(
        f"lsblk -ln -o NAME,FSTYPE {source_disk_path} | awk '$2==\"vfat\"{{print $1}}'",
        shell=True,
        capture=True,
    )
    root_part = run_cmd(
        f"lsblk -ln -o NAME,FSTYPE {source_disk_path} | awk '$2 ~ /^ext/{{print $1}}'",
        shell=True,
        capture=True,
    )

    if not boot_part or not root_part:
        print(f"Could not detect partitions on {source_disk_path}")
        sys.exit(1)

    boot_dev, root_dev = f"/dev/{boot_part}", f"/dev/{root_part}"

    # Get index for resizepart (e.g., /dev/sdb2 -> 2)
    root_idx = run_cmd(f"lsblk -no PARTN {root_dev}", shell=True, capture=True)

    return {
        "source_disk": source_disk_path,
        "boot_dev": boot_dev,
        "root_dev": root_dev,
        "root_idx": root_idx,
        "root_start": int(
            run_cmd(["lsblk", "-b", "-no", "START", root_dev], capture=True)
        ),
        "root_size": int(
            run_cmd(["lsblk", "-b", "-no", "SIZE", root_dev], capture=True)
        ),
    }


def shrink_source(args, layout):
    print(f"--- Shrinking Source: {layout['root_dev']} ---")
    root_dev = layout["root_dev"]

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

        # Identify partition node on target (p2 or 2)
        out = run_cmd(
            ["lsblk", "-ln", "-o", "NAME", dest_disk], capture=True
        ).splitlines()
        p2 = f"/dev/{out[2].strip()}"  # index 0 is disk, 1 is boot, 2 is root

        run_cmd(["e2fsck", "-p", "-f", p2], capture=not verbose)
        run_cmd(["resize2fs", p2], capture=not verbose)

        if verbose:
            print(f"[{target}] Finished")
    except Exception as e:
        print(f"FAILED {dest_disk}: {e}")


def restore_source(layout):
    print("--- Restoring Source Disk ---")
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

    layout = get_layout(args)
    source_end = (layout["root_start"] * 512) + layout["root_size"]

    print(f"Source: {layout['source_disk']}")
    print(f"Required: {source_end / 1e9:.2f} GB")

    # Detection loop
    baseline = set(run_cmd(["lsblk", "-dn", "-o", "NAME"], capture=True).split())
    targets = []
    print("Insert cards. Press Enter when finished.")

    import select

    while True:
        if select.select([sys.stdin], [], [], 0.1)[0]:
            sys.stdin.readline()
            break
        current = set(run_cmd(["lsblk", "-dn", "-o", "NAME"], capture=True).split())
        for dev in current - baseline:
            if dev not in targets and dev != layout["source_disk"].replace("/dev/", ""):
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
            size = int(run_cmd(["blockdev", "--getsize64", f"/dev/{t}"], capture=True))
            status = "OK" if size >= required else "TOO SMALL"
            print(f"  /dev/{t}: {status} ({size / 1e9:.2f} GB)")
        return

    # Phase preparation
    if needs_shrink:
        cutoff_byte = shrink_source(args, layout)
    else:
        cutoff_byte = source_end

    input("\nPress Enter to start...")

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

    os.sync()
    print("Done")


if __name__ == "__main__":
    main()

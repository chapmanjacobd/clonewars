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

    boot_part = run_cmd(
        f"lsblk -ln -o NAME,FSTYPE {source_disk_path} | awk '$2==\"vfat\"{{print $1}}' | head -n1",
        shell=True,
        capture=True,
    )
    root_part = run_cmd(
        f"lsblk -ln -o NAME,FSTYPE {source_disk_path} | awk '$2 ~ /^ext/{{print $1}}' | head -n1",
        shell=True,
        capture=True,
    )

    if not boot_part or not root_part:
        print(f"DEBUG: lsblk output for {source_disk_path}:")
        print(run_cmd(f"lsblk -ln -o NAME,FSTYPE {source_disk_path}", shell=True, capture=True))
        print("Could not detect boot (vfat) and root (ext) partitions")
        sys.exit(1)

    boot_dev = f"/dev/{boot_part}"
    root_dev = f"/dev/{root_part}"

    # Get UUID of source root
    root_uuid = run_cmd(f"blkid -s UUID -o value {root_dev}", shell=True, capture=True)

    boot_start = int(run_cmd(["lsblk", "-b", "-no", "START", boot_dev], capture=True))
    boot_size = int(run_cmd(["lsblk", "-b", "-no", "SIZE", boot_dev], capture=True))
    root_start = int(run_cmd(["lsblk", "-b", "-no", "START", root_dev], capture=True))

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
        "boot_start": boot_start,
        "boot_size_sectors": boot_size // 512,
        "root_start": root_start,
        "required_total_bytes": required_total_bytes,
    }


def get_partition_nodes(disk):
    try:
        out = subprocess.check_output(
            ["lsblk", "-ln", "-o", "NAME", disk], text=True
        ).splitlines()
        partitions = [f"/dev/{p.strip()}" for p in out if f"/dev/{p.strip()}" != disk]
        if len(partitions) < 2:
            raise RuntimeError(f"Could not find at least 2 partitions on {disk}")
        return partitions[0], partitions[1]
    except Exception as e:
        print(f"Error resolving partitions for {disk}: {e}")
        return None, None


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

        p1, p2 = get_partition_nodes(disk)

        # 3. Format Root with Source UUID and copy Boot
        if verbose:
            print(f"[{target}] Formatting {p2} with UUID {layout['root_uuid']}...")
        run_cmd(
            ["mkfs.ext4", "-q", "-F", "-U", layout["root_uuid"], p2],
            capture=not verbose,
        )
        run_cmd(["tune2fs", "-m", "1", p2], capture=not verbose)

        if verbose:
            print(f"[{target}] Streaming boot partition to {p1}...")
        run_cmd(
            [
                "dd",
                f"if={layout['boot_dev']}",
                f"of={p1}",
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
            run_cmd(["mount", p2, dst_mnt], capture=not verbose)

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
                cmd = [
                    "rsync",
                    "-aHAX",
                    "--numeric-ids",
                    "--inplace",
                    "--filter=-x security.selinux",
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
            print("\nNo targets.")
            return

        print(f"\nTargets:\n{'\n'.join(['/dev/' + t for t in targets])}")

        if args.dry_run:
            print("\nDry run results:")
            for t in targets:
                size = int(run_cmd(["blockdev", "--getsize64", f"/dev/{t}"], capture=True))
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

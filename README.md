# CloneWars

`clone_rsync.py` will use `rsync` to copy the ext4 file tree without needing to shrink

`clone_dd.py` will shrink the source filesystem and then use `dd` to copy the ext4 filesystem

## Benchmark

Comparison of cloning a 64GB device/partition to a 32GB device/partition (with 5GB of data):

| Method | Speed | Notes |
| :--- | :--- | :--- |
| `clone_dd.py` | 5m 19s | Shrink and expand source when the source is too large (can take a bit of time at startup if the source device has a lot of free space) |
| `clone_dd.py --skip-zerofill` | 5m 19s | Direct `dd` of shrunken partition. |
| `clone_rsync.py` | 5m | File-based copy; overhead from many small files. |
| `clone_rsync.py --fpsync 8` | 4m 55s | Parallel file-based copy; reduces latency of small files. |

## Things that don't work

`e2image`, `fsarchiver`, and `partclone` either don't work when the target is smaller or they require intermediate storage space

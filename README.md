# CloneWars

`clone_rsync.py` will use `rsync` to copy the ext4 file tree without needing to shrink

`clone_dd.py` will shrink the source filesystem and then use `dd` to copy the ext4 filesystem

## Things that don't work

`e2image`, `fsarchiver`, and `partclone` either don't work when the target is smaller or they require intermediate storage space

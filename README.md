# CloneWars

`clone_dd.py` (recommended) will shrink the source filesystem and then use `dd` to copy the ext4 filesystem

`clone_rsync.py` will use `rsync` to copy the ext4 file tree without needing to shrink (more of a proof of concept to try and understand how commercial duplicators are able to write to smaller devices)

## Usage

1. Start the program

    Insert the source USB drive:

        ./clone_dd.py /dev/sdc

    Or reference an image file:

        ./clone_dd.py raspios.img

2. Insert the USB Hub(s) with up to 5 daisy chains of powered USB Hubs (and up to the 32~96 limit of the [CPU](https://en.wikipedia.org/wiki/Southbridge_(computing)#Current_status))

3. Wait for the screen to update with the number of devices that you expect

4. Press enter to start batch cloning

## Benchmark

Comparison of cloning a 64GB device/partition to a 32GB device/partition (with 5GB of data):

| Method | Speed | Notes |
| :--- | :--- | :--- |
| `clone_dd.py` | 5m59s | Shrink and expand source when the source is too large (can take ~20mins at startup if the source device has a lot of free space) |
| `clone_dd.py --skip-zerofill` | 5m19s | Direct `dd` of shrunken partition |
| `clone_rsync.py` | 5m | File-based copy |
| `clone_rsync.py --fpsync 8` | 4m55s | Parallel file-based copy; reduces latency of many small files |

## Things that don't work

`e2image`, `fsarchiver`, and `partclone` either don't work when the target is smaller or they require intermediate storage space

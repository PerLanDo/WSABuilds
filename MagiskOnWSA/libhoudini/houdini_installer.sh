#!/usr/bin/env bash
set -Eeuo pipefail

# Keep WSA 2407's Android 13 Houdini runtime and only bypass its expiry branch.
# A replacement VHDX is built beside the original and atomically swapped in only
# after every filesystem, binary, and image check succeeds.

WORK_DIR="$(pwd)"
ARTIFACT_FOLDER="${1:-}"

abort() {
    echo "Error: $*" >&2
    exit 1
}

if [[ -z "$ARTIFACT_FOLDER" ]]; then
    abort "artifact folder not provided (usage: $0 <artifact_folder>)"
fi
if [[ "$ARTIFACT_FOLDER" == */* || "$ARTIFACT_FOLDER" == *\\* || "$ARTIFACT_FOLDER" == "." || "$ARTIFACT_FOLDER" == ".." ]]; then
    abort "artifact folder must be a single directory name"
fi

WSA_PATH="$WORK_DIR/output/$ARTIFACT_FOLDER"
SOURCE_VHDX="$WSA_PATH/vendor.vhdx"
RAW_IMAGE="$WSA_PATH/vendor.houdini.$$.img"
PATCHED_VHDX="$WSA_PATH/vendor.houdini.$$.vhdx"
MOUNT_BASE="$(mktemp -d "$WORK_DIR/mount_houdini.XXXXXX")"
MOUNT_DIR="$MOUNT_BASE/vendor"
MOUNTED=false

cleanup() {
    local status=$?
    set +e
    if [[ "$MOUNTED" == true ]] && mountpoint -q "$MOUNT_DIR"; then
        sync
        sudo umount "$MOUNT_DIR"
    fi
    rm -f -- "$RAW_IMAGE" "$PATCHED_VHDX"
    rmdir "$MOUNT_DIR" 2>/dev/null || true
    rmdir "$MOUNT_BASE" 2>/dev/null || true
    return "$status"
}
trap cleanup EXIT

run_e2fsck() {
    local status
    set +e
    e2fsck "$@"
    status=$?
    set -e
    if (( status >= 4 )); then
        abort "e2fsck failed with status $status"
    fi
}

for tool in e2fsck mountpoint python3 qemu-img resize2fs; do
    command -v "$tool" >/dev/null 2>&1 || abort "required tool not found: $tool"
done
[[ -d "$WSA_PATH" ]] || abort "WSA output directory not found: $WSA_PATH"
[[ -f "$SOURCE_VHDX" ]] || abort "vendor.vhdx not found: $SOURCE_VHDX"
[[ ! -e "$RAW_IMAGE" && ! -e "$PATCHED_VHDX" ]] || abort "temporary output path already exists"

mkdir "$MOUNT_DIR"

echo "=== Patching WSA 2407 API-33 Houdini in $SOURCE_VHDX ==="
echo "Converting vendor.vhdx to a working raw image..."
qemu-img convert -f vhdx -O raw "$SOURCE_VHDX" "$RAW_IMAGE"

echo "Preparing the ext4 filesystem for safe writes..."
run_e2fsck -pf "$RAW_IMAGE"
qemu-img resize -f raw "$RAW_IMAGE" +128M
resize2fs "$RAW_IMAGE"
run_e2fsck -fy -E unshare_blocks "$RAW_IMAGE"

echo "Mounting the working vendor image..."
sudo mount -t ext4 -o loop,rw "$RAW_IMAGE" "$MOUNT_DIR"
MOUNTED=true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sudo python3 "$SCRIPT_DIR/patch_libhoudini.py" --vendor-dir "$MOUNT_DIR"
sync

echo "Unmounting and validating the patched filesystem..."
sudo umount "$MOUNT_DIR"
MOUNTED=false
run_e2fsck -pf "$RAW_IMAGE"
resize2fs -M "$RAW_IMAGE"
run_e2fsck -pf "$RAW_IMAGE"

echo "Creating and validating the replacement vendor.vhdx..."
qemu-img convert -f raw -O vhdx "$RAW_IMAGE" "$PATCHED_VHDX"
qemu-img check -f vhdx "$PATCHED_VHDX"

# Both files are in the same directory, so this replacement is atomic. Until
# this line succeeds, the source vendor.vhdx remains untouched and recoverable.
mv -f -- "$PATCHED_VHDX" "$SOURCE_VHDX"
rm -f -- "$RAW_IMAGE"
rmdir "$MOUNT_DIR"
rmdir "$MOUNT_BASE"
trap - EXIT

echo "=== Houdini patch completed successfully ==="

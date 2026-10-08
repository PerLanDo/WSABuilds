# TikTok and ARM Translation Fix

## Symptoms

On x64 WSA 2407 builds, ARM applications such as TikTok, TikTok Lite, and
Shopee can close shortly after launch or stop at the splash screen. Collected
tombstones show failures in ART and Scudo allocator paths while translated ARM
code is active.

## Cause

WSA 2407.40000.4.0 is based on Android 13 (API 33). The previous build process
worked around the expiry check in Microsoft's Houdini translator by replacing
the original runtime with files from Google Play Games for PC. Those replacement
ARM libraries identify themselves as Android 14 (API 34).

Mixing that API-34 ARM userspace with WSA's API-33 x86_64 userspace is the
leading explanation for the native allocator corruption. The Houdini bundles
extracted from Google Play Games releases 26.8 and 26.9 were also byte-for-byte
identical, so updating that package did not resolve the mismatch.

## Fix

The x64 build now retains Microsoft's original API-33 translation runtime and
patches only the expired conditional branch in each original `libhoudini.so`:

| Binary | File offset | Original instruction | Replacement |
| --- | ---: | --- | --- |
| `vendor/lib64/libhoudini.so` | `0xe6723` | `jae` to the expiry failure path | six `NOP` bytes |
| `vendor/lib/libhoudini.so` | `0x88297` | `jae` to the expiry failure path | six `NOP` bytes |

The patcher is intentionally limited to the known Microsoft WSA 2407 binaries.
It verifies the Android API level, exact unpatched hashes, nearby instruction
bytes, and final patched hashes. An unknown or API-34 runtime causes the build to
stop without replacing the source `vendor.vhdx`.

The image workflow writes a temporary raw image, validates the ext4 filesystem,
patches it, creates and checks a replacement VHDX, and only then atomically
replaces the build output. It no longer modifies `system.vhdx` or copies the
roughly 600 MB replacement runtime into the package.

## Validation

The patch has been verified against pristine `vendor.vhdx` from WSA
2407.40000.4.0:

- Both source hashes match the expected Microsoft API-33 binaries.
- Both patched hashes match the expected outputs.
- A second patch pass is idempotent.
- API-34 input is rejected.
- The rebuilt VHDX passes `qemu-img check` and its ext4 filesystem passes a
  read-only `e2fsck` verification.

- Application-level testing confirmed:
  - The CI build candidate package was installed and registered with live user data preserved.
  - WSA booted normally and Google Play Store operated without issues.
  - TikTok (`com.ss.android.ugc.trill`) successfully launched through its `SplashActivity`, initialized its `MainActivity` and `MainRootFragment`, and remained fully usable.
  - Exercised over multiple sessions across 6+ minutes with zero crashes.
  - Diagnostics verified zero new tombstones (0 new vs 9 baseline), zero new ANRs (0 new vs 4 baseline), and zero Scudo allocator or ART SIGSEGV crashes in logcat.


# The radeon-9700 branch

QEMU plus an **ATI Radeon 9700 PRO** for the `mac99` machine: Mac OS X's own ATI
drivers run it unmodified, giving OpenGL, Quartz Extreme and Core Image, with the
card's 3D engine translated to Metal on the host. It also carries the PowerMac
**Screamer** sound chip and a few Cocoa and PowerPC fixes.

It is a short series of commits on a QEMU release tag, rebased onto each new
release. [QemuMac](https://github.com/matthewdeaves/QemuMac) builds it
(`./install-deps.sh`, the Radeon choice) and turns the card on with
`DISPLAY_GPU="radeon9700"`.

## What the commits add

| Area | Files |
|---|---|
| The card: PCI, CP ring and PM4, GART, 2D, R300 3D | `hw/display/ppc_mac_gpu*.{c,h,m}`, `hw/display/r300/`, `include/hw/display/ppc_mac_gpu*.h` |
| AGP capability and GART on the host bridge | `hw/pci-host/uninorth.c` |
| Screamer sound | `hw/audio/screamer.c`, `hw/misc/macio/macio.c` |
| Cocoa: window sized to the guest, no crash on guest shutdown | `ui/cocoa.m` |
| PowerPC: host-FPU fast path, inline FPRF, inline lmw/stmw, inline lfs/stfs conversion | `target/ppc/` |
| Offline tests for the 3D translation | `tests/r300/` |

More performance work on the card: the texture cache now validates per-page
against VRAM write generation (rehash only the pages a bound texture actually
had written, not the whole chain), per-draw Metal lookups (pixel-format
alignment, sampler states, texture cache scan) are cached instead of
recomputed, a repeated vertex index is transformed once per draw instead of
once per occurrence, and the vertex-program interpreter decodes each
instruction once per draw instead of once per vertex (~2x on the
interpreter). 2D blits from system RAM go a page at a time. Bring-up and
rate logging are off by default; set `PPCGPU_DIAG`, `PPCGPU_RATE` or
`R300_DRAWLOG` to get them back.

The device sources came from
[linuxkid473/poweremu-qemu](https://github.com/linuxkid473/poweremu-qemu) (branch
`r300`), itself built on [Spartan0285/PowerEmu](https://github.com/Spartan0285/PowerEmu),
and Screamer from UTM's QEMU. Commit authors are kept.

## Testing

```bash
tests/r300/run.sh          # macOS: vertex programs, MSL translation (compiled by Metal), formats, raster
```

A real check is a Tiger guest with the card: System Profiler shows "ATI Radeon 9700
Pro" with Quartz Extreme and Core Image *Supported*. QemuMac's
`tests/ci/macos-radeon-build.sh` builds this branch, runs the tests above and boots
the card.

## Moving to a new QEMU release

```bash
git fetch upstream --tags
git rebase --onto vX.Y.Z vOLD radeon-9700
ninja -C build qemu-system-ppc && tests/r300/run.sh
git push --force-with-lease origin radeon-9700
```

Conflicts come almost only from QEMU's own API changes (the console, audio,
qdev and vmstate APIs moved between 10.0 and 11.1); the device's files are new
and do not conflict. Debug switches are listed at the top of
`hw/display/ppc_mac_gpu.c` and in its `trace` property.

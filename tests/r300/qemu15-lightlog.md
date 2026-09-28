# qemu#15 dynamic-light capture

This is instrumentation, not a rendering fix. The disproven `TXO_ENDIAN`
mapping remains reverted. No live rocket capture was possible in the sandbox.

## Boundaries being measured

Quake II's game-side dlight RGB and world origin are not objects in this
emulator. `r300_render()` receives draw packets/registers, translates texture
addresses to VRAM or copies GART bytes, and hands the assembled packet to
`metal_draw_r300()`. `set_textures()` supplies the endian/decode/swizzle state;
`set_uniforms()` supplies fragment constants and blend state. The driver's
texture environment is represented by the US fragment program.

`r300_texture()` selects a VRAM view, a GART upload, or the converted/mipmapped
texture cache. It does not itself identify a texture as a lightmap. A moving
light baked by the guest CPU into an atlas should appear as changing texels
on repeated binds of one address. A constant-driven light should instead
show up in constants/varyings consumed by the shader. The capture must decide
which actually happens; there is no inferred source RGB labelled as fact.

## Capture

1. Acquire `qemu-tiger3d` with
   `old-mac-build-host/scripts/pick-bench-host.sh --acquire qemu-tiger3d qemu15-lightlog`.
   Check the result before using the VM. Release the claim with that script's
   `--release qemu-tiger3d` when done.
2. Boot the instrumented binary from this checkout using the normal QemuMac
   binary-selection workflow. Set `R300_LIGHTLOG` to an absolute output path
   in this checkout, for example `build/qemu15-light.log`, in the **QEMU host
   process environment**. A running VM must be restarted to pick it up.
   `R300_DRAWLOG=<another-path>` is optional for comparison; `PPCGPU_DIAG`
   is not needed. Do not use the same file for both logs.
3. Launch Quake II with `+set gl_dynamic 1 +set gl_flashblend 0 +map base1`.
   Use `give all`, select the rocket launcher, and keep the base1 viewpoint.
   Capture a no-shot screendump with `qemu-vm.sh screendump <absolute-path>`.
4. Fire at the nearby wall. Capture the flying projectile and impact, including
   the ceiling and underside of the inclined beam. Record host times for the
   shot/captures alongside the evidence. A blaster muzzle flash is insufficient.
5. Stop the short capture and retain the log, screenshots, QEMU version/binary
   identity, and guest renderer identity. Log output is uncapped and can be
   large; texel scans and file I/O can slow the game. They add no GPU waits,
   but a timing-sensitive failure may still change under instrumentation.

The sandbox acquisition attempt failed before acquiring a claim: process
listing was denied and QEMU could not unlink the VM monitor socket. No host
claim was obtained and no live capture was run.

## Records and predictions

All submitted draws are logged, including single-texture and untextured draws,
so the diagnostic does not assume a lightmap unit number. `D<number>` uses the
same counter as `R300_DRAWLOG`.

`TIME` records give host epoch and monotonic microseconds for screenshot/shot
correlation. `RESULT` flushes the stream after each submitted draw.

- `DRAW`: output format/endian register, color/alpha blend registers, channel
  mask, blend color, fog and PVS controls. `CONST` includes all 32 fragment
  constants, including zero, in raw float24 and decoded form.
- `US`/`MSL`: guest texture instructions and raw ALU words, then the translated
  combine/blend/output code, once per shader ID. `STATE` dumps registers/PVS
  memory when PVS uploads change. `VERT` contains the first three transformed
  vertices and all their varyings; these are not the light's world origin.
- `TEX`: raw TX_OFFSET/FORMAT0/1/2, TXO_ENDIAN, effective decode selector,
  channel swizzle, filter flags, size/pitch, VRAM versus GART, dirty generation.
- `UPLOAD`: PM4 HOSTDATA subrects and the start of MMIO HOST_DATA uploads with
  raw source words. Direct guest aperture writes and other blit paths are not
  individually traced; their results are visible in `PIX`/`TEXELS`.
- `BIND`: actual Metal binding success/format and chosen path.
  `CACHE`: full-cache lookup/reupload decision for the immediately enclosing
  draw and matching texture address. `RESULT`: renderer return value.
- `PIX`/`TEXELS`: every changed level-zero RGBA8 texel, its old/new memory-order
  bytes and coordinates, hash and exclusive bounding box. The first observation
  is explicitly a baseline. Snapshots use 64 bounded slots; address/layout/source
  collisions restart the baseline. Compare only `baseline=0` changes. Row padding
  is excluded. Non-RGBA8, non-2D, malformed, or >1 MiB level-zero textures are
  explicitly skipped, as is memory with pending GPU writes. No silent readback
  or synchronization is introduced. Higher mips are not sampled by this probe.

| Hypothesis | Evidence to look for |
| --- | --- |
| CPU lightmap relight | Moving/localized `PIX` changes in an atlas while shader/constant setup stays stable; shader samples that unit and combines it with the base texture. |
| Texture decode/channel error | Relight bytes change, but the logged decode and TX swizzle route a component into the wrong sampled channel. For RGBA8 the current decode is 0=raw, 1=ABGR, 2=GRBA, 3=BGRA, followed by signed/gamma conversion and `swz`. Compare these decoded old/new texels, not raw bytes called RGB. Merely observing endian 1/3 does not justify the already-disproven fix. |
| Bad source/upload bytes | Bytes are already inconsistent before sampling, or logged upload words disagree with the resulting texels after accounting for the actual upload storage order. Atlas RGB includes baked lighting and scaling, so it need not equal the rocket's source color. |
| Stale texture cache | A non-baseline level-zero byte/hash change accompanies a full-cache hit that skips upload, after accounting for intervening binds/reuploads of the same address/layout. `vram-view` has no copied full texture to become stale. |
| Per-draw color/position path | Atlas bytes remain unchanged while consumed `CONST`, PVS state, or vertex varyings change with the rocket. Shader instructions identify whether those values are colors, coordinates, or unrelated state. |
| Combine/blend/output error | Decoded lightmap colors are plausible, but guest US versus translated MSL or color/alpha blending/output channel selection produces the cast. Untextured draws let the same capture investigate a separate overlay/viewmodel problem. |

If the relevant reads are skipped or the capture cannot distinguish these,
extend that specific boundary before proposing a fix. Absence of `PIX` changes
on an unsupported/busy texture is not evidence of constant-driven lighting.

A later fix needs the same base1/give-all/rocket test before and after, with
the beam underside and ceiling captured during projectile flight and impact,
plus a no-shot frame. Verify the warm moving light survives without the cast;
do not accept a disappearance caused by disabled dynamic lighting. Run
`sh tests/r300/run.sh` on a host with Metal access before claiming validation.

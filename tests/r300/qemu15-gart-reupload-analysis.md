# qemu#15: GART lightmap re-upload investigation, 2026-09-28

The strongest specific candidate is the unchanged `td->host_data` passed to
`replaceRegion` in `hw/display/ppc_mac_gpu_metal.m:7780-7781`. The RGBA8 GART
path feeds system-memory bytes into a decode convention written for the
CPU-aperture representation of VRAM. It does not normalize those bytes to
that representation. The neighboring raw-format GART path explicitly does
that normalization, reversing each dword at line 7734. The DXT path also
distinguishes GART from VRAM. RGBA8 does neither.

This is a concrete inconsistency with a mechanism that affects newly patched
lightmap rectangles while leaving existing VRAM texels alone. It is not yet
a verified rendering defect. The captured source component contract and the
completed dynamic-atlas contents still need to be established together.
No change to `set_textures()` is proposed here.

There is also a useful explanation for the second disproven candidate:
changing RGBA8 decode to zero at **both** sampling stages leaves the final
dynamic-lightmap RGB exactly unchanged for the captured upload/world pair.
The first stage writes different bytes into the atlas, but the second stage
interprets those bytes differently and cancels that change. This does not
predict that the entire frame stays identical, since base textures and other
draws can also be affected. It does explain why that experiment cannot rule
out a missing conversion confined to GART uploads.

The investigation used checkout `0549e16c4d6f0b4ec521fc1a02176cd8e2ad72c9`,
which was clean on entry. I read `qemu15-lightlog.md`,
`qemu15-recheck2-analysis.md`, the relevant device/backend/shader code and
existing upload fixtures. I also read the weapon-cycle evidence README and
viewed `wc-quake04.png` and `wc-quake05.png`. The earlier analysis's temporary
shader-221 extract was inspected to check its copy operation. The historical
draw/texel measurements below come from that earlier analysis and its
extracts, not a new capture. No VM access, renderer execution, code edits or
commits were performed.

The new screenshots establish a no-attack reproduction during weapon
cycling, followed by recovery by the next screenshot about 40 client frames
later. They do not establish that the corruption lasted exactly one rendered
frame, or independently identify the guest event as a dynamic light. The
floor, beam and background show a broad magenta cast in `wc-quake04`; the
near left wall and visible weapon also retain substantial brown/orange.
That is compatible with many affected lightmap surfaces and does not prove
a uniform fullscreen overlay. The viewmodel still needs its own matched
draw comparison. The old rocket capture's yellow viewmodel used vertex
lighting rather than the world atlas.

The actual patch path is a textured draw, not a special lightmap upload API.
There is no backend branch for a rocket or dynamic light. Guest submission
of a small GART texture and an atlas render target selects this route:

| Boundary | Code in this checkout | What happens |
| --- | --- | --- |
| Describe patch | `r300_draw.c:655-746`, `set_textures` | W8Z8Y8X8 selects RGBA8. Endian 0 selects decode 1. Pitch and dimensions come from the texture registers. This runs before GART resolution. |
| Read system memory | `ppc_mac_gpu.c:3187-3197`, `r300_read_raw:2306-2330` | Allocate the complete texture span and copy bytes through the GART page mapping without reversing them. |
| Create sampled patch | `ppc_mac_gpu_metal.m:7773-7782` | Create an RGBA8Unorm texture and upload `host_data` unchanged with the specified row pitch. This is the candidate boundary. |
| Sample/copy patch | `r300_us.c:536-570`; captured shader 221 | Apply decode, then TX swizzle. The fragment program copies sampled RGBA. |
| Store atlas rectangle | `r300_draw.c:491-496`, `r300_us.h:151-166`, `r300_us.c:875-909` | Output selectors and `rt_swap32` pack the result into the VRAM-backed render target. |
| Bind atlas to world | `ppc_mac_gpu_metal.m:7784-7793`; captured shader 222 | Use a direct VRAM view. Decode and swizzle again, then multiply lightmap RGB by base RGB and two. |

The GART copy has its own allocation for each draw. The single-level RGBA8
path creates a new Metal texture rather than reusing a staging texture. The
atlas render pass uses load/store actions at `ppc_mac_gpu_metal.m:8017-8018`;
the patch is limited by geometry and scissor. There is no separate CPU
subrect-copy loop here that could accidentally use the atlas pitch for the
source. The patch's destination is VRAM, so the GART-render-target scratch
copy-out path is not involved.

The reason to suspect this boundary, beyond the resulting hue, is the
existing representation contract elsewhere in the emulator:

- `r300_texture_raw`, `ppc_mac_gpu_metal.m:7688-7735`, says GART dwords must
  be reversed to reach VRAM's side of the aperture, and implements that
  conversion only when `td->host_data` is non-null.
- `r300_dxt_bytes`, `r300_draw.c:584-595`, uses a different byte permutation
  for GART and VRAM. `tests/r300/test_features.c:504` checks that both sources
  reach the same BC stream. That supports the memory-source distinction;
  it does not establish RGBA8 hardware behavior by itself.
- The 2D system-memory copy path calls `r300_upload_word` at
  `ppc_mac_gpu.c:5422`, with equivalent calls in the other 2D copy paths.
  For four-byte pixels and source-swap 0, that helper reverses each dword
  before storing VRAM's CPU view. `tests/r300/test_upload.c:8-16` explicitly
  tests source bytes `[R,G,B,0]` becoming VRAM bytes `[0,B,G,R]`.
- The single-level RGBA8 path instead says "upload as it lies" at line
  7774. The multi-level RGBA8 path's default `r300_level_bytes` branch at
  lines 7437-7444 also copies bytes without a source-dependent conversion.
  That is an adjacent consistency concern, but the captured patch has one
  level and does not take that branch.

These local contracts explain how an initial 2D upload can produce correct
static texels while a later 3D GART patch leaves bytes in a different order.
The supplied capture does not identify the original upload of every static
atlas, so this distinction is a supported mechanism, not a claim that the
entire initial-upload history was observed. Nor is the bug necessarily
exclusive to dynamic lights: any upload using the same source/format route
could be affected. Relighting makes it visible by selecting or updating
that route.

The 2D source-swap register at `0x15d4` belongs to the 2D copy path. It is
not an extra texture-sampling control to apply blindly to these 3D patches.
Likewise, changing `r300_read_raw` globally would also change vertex/index
reads and discard its explicit raw-byte contract. The candidate is a
missing distinction in the RGBA8 GART texture boundary, not a general
reversal of everything read from system RAM.

For the captured upload, let `p = [p0,p1,p2,p3]` be one texel's bytes in
ascending memory addresses. The relevant state is:

```text
patch: fmt=0c, source=GART, endian=0, decode=1, swz=2,1,0,3, flags=0
copy:  shader 221, outfmt=00001b00, pitch=00c30080
       cblend=0, chanmask=f, rop=0, alpha=0, fog=0
world: source=VRAM, endian=0, decode=1, swz=0,1,2,5, flags=0
```

`00001b00` selects `[B,G,R,A]` for output components C0..C3.
`00c30080` selects COLOR_ENDIAN 0, and the existing `r300_cb_swap32` rule
sets `rt_swap32=1`. Thus the copy operation composes as follows:

```text
GART raw bytes                  [p0,p1,p2,p3]
patch decode 1                  [p3,p2,p1,p0]
patch swizzle / copied RGBA      [p1,p2,p3,p0]
output selectors B,G,R,A         [p3,p2,p1,p0]
reversed store / atlas bytes    [p0,p1,p2,p3]
world decode 1 and XYZ1          RGB = [p3,p2,p1]
```

The measured six-texel control, D525416 to D526124, agrees with that byte
identity. It does not validate the identity as the correct transfer between
system-memory and CPU-aperture representations. For example, source
`2b292625` becomes atlas `2b292625`, and the world reads RGB `(37,38,41)`.
If the source contract is RGBA byte order, the intended RGB is `(43,41,38)`.

With the rejected unconditional decode-zero change:

```text
GART raw bytes                  [p0,p1,p2,p3]
patch decode 0                  [p0,p1,p2,p3]
patch swizzle / copied RGBA      [p2,p1,p0,p3]
output selectors B,G,R,A         [p0,p1,p2,p3]
reversed store / atlas bytes    [p3,p2,p1,p0]
world decode 0 and XYZ1          RGB = [p3,p2,p1]
```

That is the same final RGB. Reversing only the GART representation before
the existing decode-one path would instead write `[p3,p2,p1,p0]` into the
atlas and let the unchanged world decode read `[p0,p1,p2]`. This is a
counterfactual used to distinguish the mechanisms, not a verified fix.
The first rejected candidate touched nonzero TXO_ENDIAN arms, whereas
these captured patch and atlas binds both have endian zero.

| Sample from the earlier capture | Source bytes | Current world RGB | World RGB with both decodes zero | World RGB with GART-only representation reversal |
| --- | --- | --- | --- | --- |
| D525298 wall patch `(1,0)` | `5d371358` | `88,19,55` | `88,19,55` | `93,55,19` |
| D526156 wall patch `(1,4)` | `cca513cb` | `203,19,165` | `203,19,165` | `204,165,19` |
| D528075 beam patch `(0,0)` | `89760c86` | `134,12,118` | `134,12,118` | `137,118,12` |

The readable static wall texel at `(97,37)` was `ff13375d`, yielding
`93,55,19`. The first patch above addresses that location and contains
the same three values in its first three bytes. This is stronger evidence
than merely finding an interpretation that looks warm. The later patches
would raise red and green while keeping blue low under the hypothesized
source contract; the current chain instead sends the rising second byte
to blue and the low third byte to green. The fourth source byte is not
independently identified as alpha or padding by LIGHTLOG, so its intended
role must remain a hypothesis.

I checked the competing stride and partial-write explanations:

- D528075's `format0=8000200e` and `format2=0000000f` describe 15x5 active
  texels with a 16-texel pitch. Allocation is 320 bytes, source row pitch
  is 64, and the final active byte is offset 315. `r300_read_raw`,
  `r300_tex_layout`, `replaceRegion` and the PIX scanner agree on that
  layout. The 4-byte padding at each row end is not scanned as a texel.
  Its source span `10c2bd60..10c2bea0` does not cross a GART page.
- The destination is 128 pixels wide at four bytes per pixel, giving
  512-byte rows. The recorded patch covers `[64,79) x [26,31)`; the world
  beam draw D528076 samples that rectangle. There is no apparent source
  versus destination pitch mix-up in the binding code.
- `chanmask=f`, disabled blending/ROP and shader 221's RGBA copy provide
  no intended component-preserving partial write for the captured patch.
  A dropped rectangle or bad scissor remains testable, but does not
  naturally explain the demonstrated channel permutation. The 2x3
  D525416 control verifies byte identity across all three rows, though it
  cannot exclude a problem restricted to other sizes or coordinates.
- The atlas uses `vram-view`, not the copied full-texture cache. A base
  texture's `CACHE decision=hit-generation` says nothing about atlas
  freshness. Read/write conflict checks at
  `ppc_mac_gpu_metal.m:7953-7992` and fence chaining at lines 6270-6299
  explicitly order atlas writes and subsequent sampling. I found no
  specific missing dependency in this sequence. That is not GPU
  validation, and `skipped=gpu-busy` is not proof of an ordering bug.

Output packing is an alternative boundary that can reverse the same four
bytes. Its current rule is shared with render-to-texture cases, including
Quake warp textures, and has explicit round-trip fixtures. Those fixtures
encode the implementation's contract rather than prove real hardware
behavior. The GART-specific inconsistency is the stronger lead because
it explains the memory-source difference without changing existing VRAM
consumers or general render-target storage.

The next live capture should resolve the following questions, in order.
These are proposed measurements, not instrumentation added in this pass.

1. Capture the short `give all`/`weapnext` reproduction from before the
   first visible transition through recovery, with a verified running
   binary and its decode behavior recorded. Keep complete draw blocks
   and US/MSL definitions. Identify whole game-target frames and their
   compositor copies; do not use only a small draw window around a
   screenshot time. Submission timestamps alone cannot identify the
   completed frame. Preserve the no-attack trigger; rocket fire is not
   required for this capture.

2. Match world geometry before, during and after the cast through `VERT`
   clip positions and consumed UVs. For each atlas writer and consumer,
   retain `DRAW` target/pitch/outfmt/cblend/ablend/chanmask/alpha/rop/fog,
   `TEX` address/source/format0/1/2/endian/decode/swz/flags/size/pitch/levels,
   `BIND` path/format/ok/gpu_busy, `RESULT`, `CONST`, and full shader text.
   Shader IDs and addresses can change between runs; identify the copy
   and world shaders by their operations. Historic anchors are
   D525298 -> D525299, D526156 -> D526157, D528075 -> D528076, plus the
   readable D525416 -> D526124 control.

3. Capture both sides of one completed atlas write **before another
   patch can overwrite it**. Existing LIGHTLOG logs source `host_data`
   and skips a busy destination; it does not log the actual sampled
   Metal texel or schedule a post-completion atlas snapshot. A narrowly
   scoped diagnostic needs a retained source snapshot and an ordered
   destination copy after that upload, including a one-texel border.
   Read that copy only after completion. A forced wait can simplify an
   isolated diagnostic but may suppress timing bugs, so retain a normal
   unsynchronized repro as well. Record all vertices or the complete
   destination rectangle/scissor, since current VERT logs only three.
   Distinguish original guest-memory bytes, bytes passed to Metal and
   final atlas bytes explicitly; current PIX is not a post-upload Metal
   readback.

4. Compare every texel at `source + y*source_pitch + 4*x` against
   `atlas + (dst_y+y)*atlas_pitch + 4*(dst_x+x)`, and verify that the
   surrounding border stays unchanged. On the current decode-one code,
   the prediction is atlas bytes equal source bytes. A GART-normalized
   path with unchanged decode predicts per-texel reversal instead.
   Row-dependent shifts, padding appearing as pixels, old destination
   components or writes outside the rectangle would redirect the
   investigation to stride/coverage/order. Current `baseline=1` is
   useful as a complete source snapshot, but is not a temporal change;
   reconstruct non-baseline snapshots from their preceding baseline.

5. Establish the intended source order independently. The best controlled
   guest test uploads an asymmetric known RGB/RGBA pattern normally,
   then updates a subrectangle with the same logical colors and the same
   GL format/type/unpack state. Force or confirm the same GART patch
   route and compare sampled colors before/after. Use distinct channels
   and row markers, including an odd-width padded case such as 15x5.
   If tracing the actual guest instead, record the lightmap buffer's
   format/type, row stride and bytes at the upload call, plus any driver
   conversion into the GART patch. For the initial 2D route, additionally
   log source/destination addresses, pitches, rectangle and `0x15d4`
   source-swap value. LIGHTLOG's `UPLOAD` records currently cover only
   PM4/MMIO HOSTDATA, so they cannot establish all of that 2D history.

Confirmation requires the intended source contract, the observed patch
transfer and the world sample to agree on the missing conversion. If the
known source RGB is `[p0,p1,p2]`, the current path stores `p`, and the
consumer reads `[p3,p2,p1]`, this identifies the defect rather than merely
its color. If the guest contract instead expects `[p3,p2,p1]`, the proposed
GART normalization is wrong. If completed atlas bytes already have the
correct representation while the GPU samples a wrong value, investigate
the Metal view/dependency boundary. If GART bytes already disagree with
the guest buffer, compare translated physical pages and cached versus
uncached reads upstream of `r300_texture`.

Finally, capture the viewmodel and compositor from that same bad frame.
Compare the viewmodel's base `TEXELS` hash and consumed `VERT` colors,
US/MSL, constants and output state. Check the compositor's source target,
sampling and output state. An atlas-only correction cannot explain an
independently corrupted vertex-lit viewmodel. If the world lightmap samples
are correct but every object becomes magenta, this hypothesis does not
account for that event and the common output/compositor path takes priority.

Offline validation in this pass checked the byte permutations on 1,024
single-channel basis cases, all four byte positions at values 0..255. The
current and both-decodes-zero chains yielded identical final lightmap RGB
in every case; the GART-only normalization counterfactual yielded the first
three source bytes. The numerical examples and 15x5 allocation/stride
calculation also passed. These are arithmetic checks, not Metal or live
rendering validation. Only this new report was added.

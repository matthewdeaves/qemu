# qemu#15: analysis of the 2026-09-28 captured rocket shot

The capture confirms two mechanisms in the hypothesis table: surface lighting
changes through uploaded lightmap texels, and the yellow viewmodel changes through
consumed vertex colours. It does **not** uniquely establish which emulator defect
causes the world's incorrect hue. The evidence narrows that question to the
source-byte/component-order contract across GART upload, atlas storage and texture
sampling. No rendering fix is verified or proposed here.

The distinction matters. The world shader receives or is predicted to receive
magenta lightmap values before its ordinary multiply. The gun receives large red
and green vertex multipliers with little blue. A shared change in a blend constant,
fog colour or base-texture bytes is not needed to explain these observations.

## Scope and provenance

Read-only evidence directory:
`/Users/matt/oldmac/evidence/qemu15-recheck2-20260928/`.
The raw file was 26,499,005,223 bytes when inspected. Analysis used `timing.log`,
`all-times.txt`, the supplied small slice, the PNGs, complete raw draw blocks
D523300-D529999, and earlier US/MSL definitions. No VM or host was accessed.

The checkout was on `radeon-9700` at
`babffca260c675a2fcda7cccc6f405bd53707d0c`. This is analysis-checkout provenance;
it is not a verified identity for the captured binary. The captured MSL is the
shader evidence. Local source was used to understand probe semantics, binding and
uniform derivation, with an observed upload/readback control checked below.

The screenshot title identifies Yamagi Quake II 5.11. No guest source, source
light RGB, driver-side format conversion trace or reference-hardware output is
included in this evidence. The existing spec explicitly says source dlight RGB
is unavailable. The ceiling pickup is not used as evidence of a defect.

## Timing and complete draw sequences

`TIME` records describe submission time, not GPU completion or presentation.
The clear/game-target/compositor sequence gives useful frame-sized groups, but
there are no presentation fences in this log. A timestamp alone cannot identify
exactly which completed image a screendump read.

Counting the first `timing.log` line as fire and the remaining eight as dump1..8
produces the following brackets. These differ from the supplied screenshot labels.

| Event | Epoch microseconds from timing.log | Bracketing draws |
| --- | ---: | --- |
| Fire | 1790581982256824 | D524240 / D524241 |
| Dump 1 | 1790581982697574 | D525092 / D525093 |
| Dump 2 | 1790581983052301 | D525472 / D525473 |
| Dump 3 | 1790581983318682 | D526029 / D526030 |
| Dump 4 | 1790581983891267 | D527011 / D527012 |
| Dump 5 | 1790581984416773 | D527969 / D527970 |
| Dump 6 | 1790581984687455 | D528285 / D528286 |
| Dump 7 | 1790581984925359 | D528846 / D528847 |
| Dump 8 | 1790581985453150 | D529746 / D529747 |

In particular, 1790581983320817 and 1790581984686178 are the log times of
D526030 and D528285, not the corresponding `timing.log` entries. Decoded PNG
pixels are identical for fireB-2/fireB-3 and for fireB-6/fireB-7. The images
therefore do not represent eight distinct rendered game frames.

| Game-target sequence, including initial clear | Following compositor draws | Interpretation from draw content |
| --- | --- | --- |
| D523399-D524240 | D524241-D524249 | Entirely before fire command |
| D524250-D525092 | D525093-D525101 | Before visible rocket response |
| D525102-D526029 | D526030-D526038 | Raised gun, strong yellow vertex lighting |
| D526039-D527011 | D527012-D527020 | Gun lowered somewhat, still strongly yellow |
| D527021-D527969 | D527970-D527978 | Following transition |
| D527979-D528866 | D528867-D528875 | Gun recovered, beam dynamically relit |
| D528876-D529746 | D529747-D529755 | Later recovery |

These groups have about 840-970 draws each. A window of 40-80 draws around
an anchor would still miss most world geometry. D526030 is a compositor copy
sampling the 1024x768 game target `00939000`; D528285 is a 3D draw using shader
223 and a 256x64 texture, not a HUD digit. Thus the previous small slice's HUD
conclusion does not describe every draw it contains. Its unchanged HUD hash is
valid but does not decide the lightmap question.

## Actual world geometry and stable base textures

The level is rendered to `rt=00939000`. Matched draws use shader 222, with t0
as the repeating base texture and t1 as a 128x128 lightmap atlas. The logged
clip positions, base UVs and lightmap UVs identify repeated geometry across phases.

| Surface / evidence | Before fire | First strong flash | Following flash | Gun recovered / beam lit |
| --- | --- | --- | --- | --- |
| Left wall at its visible right edge, t0 `01759000`, 128x128 | D523475 | D525299 | D526157 | D528055 |
| Diagonal beam face, t0 `01753000`, 64x64 | D523489 | D525316 | D526227 | D528076 |
| Wall t1 | `0182d000` | `01976000` | `01976000` | `0182d000` |
| Beam t1 | `0182d000` | `0182d000` | `01976000` | `01976000` |

For example, the first beam triangle projects in game-target coordinates to
approximately `(585,383), (528,383), (682,19)`. The game's visible content begins
44 screenshot pixels below the top edge. This puts the triangle on the long
slanted face. The wall triangle projects to `(409,199), (409,489), (377,494)`,
at the near wall's right edge. Only the first three transformed vertices are
logged; these identify a surface portion, not every triangle in the draw.

At D523475/D526157/D528055, and separately D523489/D526227/D528076, all three
logged clip positions and consumed v0/v1/v2 values are identical. Pipeline fields
are identical apart from batch vertex counts on the beam. All 32 fragment
constants are zero in each selected draw. Fog stays enabled with the same
`fog_rgba`; blend colour is zero and `cblend=2726000e` has blending disabled.
The shader does not consume the garbage-looking unused v3..v9 values sometimes
present in these records.


```text
DRAW D523489 msl=222 verts=18+0 warn=0 tx_enable=3 rt=00939000 pitch=00d10400 outfmt=00001b01 cblend=2726000e ablend=27260000 chanmask=f alpha=4aa rop=0 blend_rgba=0,0,0,0 fog=1 fog_rgba=0.497556,0.497556,0.549365,0 pvs_ctrl=00a00c00 pvs_const=000c0000 vap=00000002
TEXELS D523489 t0 addr=01753000 baseline=0 hash=b1a8aad96d6fd65d changed=0 bbox=0,0-0,0
TEXELS D523489 t1 addr=0182d000 baseline=0 hash=2166559950daa538 changed=0 bbox=0,0-0,0
DRAW D526227 msl=222 verts=12+0 warn=0 tx_enable=3 rt=00939000 pitch=00d10400 outfmt=00001b01 cblend=2726000e ablend=27260000 chanmask=f alpha=4aa rop=0 blend_rgba=0,0,0,0 fog=1 fog_rgba=0.497556,0.497556,0.549365,0 pvs_ctrl=00a00c00 pvs_const=000c0000 vap=00000002
TEXELS D526227 t0 addr=01753000 baseline=0 hash=b1a8aad96d6fd65d changed=0 bbox=0,0-0,0
TEXELS D526227 t1 skipped=gpu-busy
DRAW D528076 msl=222 verts=12+0 warn=0 tx_enable=3 rt=00939000 pitch=00d10400 outfmt=00001b01 cblend=2726000e ablend=27260000 chanmask=f alpha=4aa rop=0 blend_rgba=0,0,0,0 fog=1 fog_rgba=0.497556,0.497556,0.549365,0 pvs_ctrl=00a00c00 pvs_const=000c0000 vap=00000002
TEXELS D528076 t0 addr=01753000 baseline=0 hash=b1a8aad96d6fd65d changed=0 bbox=0,0-0,0
TEXELS D528076 t1 skipped=gpu-busy
```

The base-texture level-zero hashes match in all listed phases:

| Base texture | Hash |
| --- | --- |
| Wall `01759000` | `1556235269460033` |
| Beam `01753000` | `b1a8aad96d6fd65d` |
| Gun `011cf000` | `a05496896850d8bc` |

The base textures also retain decode, swizzle, filters and layout. `now` changes
as other textures are written; that is not a base-texture content change.
Several TEXELS records have `baseline=1` because the 64 snapshot slots collide.
Their whole-texture `changed` counts mean a new observation, not a relight event.
Higher mip levels are outside this probe.


```text
TEX D526227 t0 bound=1 tx_offset=0175300c endian=0 format0=1801f83f format1=00088a0c format2=00003fff fmt=0c addr=01753000 source=VRAM kind=0 size=64x64 pitch=256 levels=7 decode=1 swz=0,1,2,5 flags=20 filter=00205e00,00000000 gen=1137 now=1303
TEX D526227 t1 bound=1 tx_offset=0197600c endian=0 format0=0003f87f format1=00088a0c format2=00003fff fmt=0c addr=01976000 source=VRAM kind=0 size=128x128 pitch=512 levels=1 decode=1 swz=0,1,2,5 flags=0 filter=10001400,00000000 gen=0 now=1303
BIND D526227 t0 addr=01753000 path=full-cache ok=1 metal_format=70 rowel=1 gpu_busy=0
BIND D526227 t1 addr=01976000 path=vram-view ok=1 metal_format=70 rowel=1 gpu_busy=1
```

The enclosing cache record for D526227 is:


```text
CACHE addr=01753000 gen=1137 now=1303 decision=hit-generation
```

This hit is for the unchanged base texture. The lightmap takes `vram-view`,
which does not keep a copied full-texture cache. There is no evidence here of
a changed level-zero base texture hidden behind a stale full-cache hit.

## World shader: where the lightmap enters

Shader 222 was first printed at D124862. It remains the same shader before,
during and after the shot. Its captured guest US is:


```text
US D124862 id=222 BEGIN
US config 00000008 offset 00060012, 1 node(s)
 node 0
  tex16 op3 t0 <- unit1 t0
  tex17 op3 t1 <- unit0 t1
  alu18 rgb 1c020040 08050200  alpha 01020080 00040509

US END
```

Selected lines from that captured MSL, in original order:


```text
    t[0] = vin[2];
    t[1] = vin[0];
    t[2] = vin[1];
    t[0] = r300_tex(tex1, smp1, u, 1, t[0], true, 0.0, 0u);
    t[1] = r300_tex(tex0, smp0, u, 0, t[1], true, 0.0, 0u);
        float4 cs0 = t[0]; float4 as0 = t[0];
        float4 cs1 = t[1]; float4 as1 = t[2];
        float3 rA = (cs0.rgb);
        float3 rB = (cs1.rgb);
        float3 rC = (float3(0.0));
        float3 rr = rA * rB + rC;
        rr = rr * 2.0; ar = ar;
        oc[0].rgb = rr.rgb;
```

Thus RGB is `2 * sampled_lightmap.rgb * sampled_base.rgb`, followed by
fog and target packing/clamping. RGB constants are not part of that product.
The captured US has two projected texture reads and the matching multiply/scale
ALU words. There is no observed before/during shader replacement or a new
magenta overlay equation. The record does not log `aux.x`, so it does not prove
identical fog factors, but fog colour/control themselves do not change.

## Uploads, measured atlas changes and component routing

The dynamic atlas `01976000` is written by shader 221 from small GART-resident
RGBA8-layout subrects. The following draw samples that atlas as t1. Small upload
textures here are lightmap patches, not HUD icons. Every observed bind of this
dynamic atlas in D523300-D529999 is GPU-busy; none supplies a readable TEXELS
hash. It would be wrong to call this atlas unchanged.

The later beam draw D528076 follows this 15x5 upload to atlas rectangle
`[64,79) x [26,31)`. Its v2 coordinates address the same rectangle.


```text
DRAW D528075 msl=221 verts=6+0 warn=0 tx_enable=1 rt=01976000 pitch=00c30080 outfmt=00001b00 cblend=00000000 ablend=27260000 chanmask=f alpha=0 rop=0 blend_rgba=0,0,0,0 fog=0 fog_rgba=0.497556,0.497556,0.549365,0 pvs_ctrl=00a00c00 pvs_const=000c0000 vap=00000102
TEX D528075 t0 bound=1 tx_offset=10c2bd60 endian=0 format0=8000200e format1=0000a60c format2=0000000f fmt=0c addr=10c2bd60 source=GART kind=0 size=15x5 pitch=64 levels=1 decode=1 swz=2,1,0,3 flags=0 filter=00000a92,00000000 gen=0 now=0
BIND D528075 t0 addr=10c2bd60 path=gart-upload ok=1 metal_format=70 rowel=1 gpu_busy=0
TEXELS D528075 t0 addr=10c2bd60 baseline=1 hash=dd99dc28d70e3afb changed=75 bbox=0,0-15,5
```


```text
PIX D528075 t0 xy=0,0 old=none new=89760c86
PIX D528075 t0 xy=0,4 old=none new=2d1a0c27
```

There is also direct, non-baseline evidence that an atlas changes. D525416
uploads a 2x3 patch at `(5,0)` into the readable atlas `0182d000`. At D526124,
all six resulting texels equal those source bytes exactly. Two representative
source/destination lines are:


```text
PIX D525416 t0 xy=0,0 old=none new=2b292625
PIX D525416 t0 xy=1,0 old=none new=1e1c1917
PIX D526124 t1 xy=5,0 old=3c393736 new=2b292625
PIX D526124 t1 xy=6,0 old=201d1a19 new=1e1c1917
```


```text
TEXELS D526124 t0 addr=01817000 baseline=1 hash=44c14eb27d80ad1b changed=16384 bbox=0,0-128,128
TEXELS D526124 t1 addr=0182d000 baseline=0 hash=642f3c2b1f473dbc changed=323 bbox=0,0-115,68
```

D526124 is `baseline=0`: 323 changed atlas texels, bounding box
`[0,115) x [0,68)`. This differs from a baseline restart. The six-texel control
also tests the upload/storage reconstruction against bytes actually read back
by the probe, without a new GPU readback or VM session.

The captured shader helpers say:


```text
static float4 r300_tpost(float4 raw, constant R300FSUniforms &u, uint unit)
{
    uint k = u.tex_info[unit].y;
    return r300_tfix(k == 1 ? raw.abgr : k == 2 ? raw.grba : k == 3 ? raw.bgra : raw, u, unit);
}
```

For the GART uploads, `decode=1 swz=2,1,0,3` maps memory-order bytes
`[p0,p1,p2,p3]` to shader RGBA `[p1,p2,p3,p0]`. Shader 221 copies that sample.
Its `outfmt=00001b00` supplies B,G,R,A output selectors. The local uniform
builder derives a reversed store for `pitch=00c30080`, COLOR_ENDIAN 0; the
captured `r300_pack` performs that reversal. The composed upload is byte
identity, `[p0,p1,p2,p3] -> [p0,p1,p2,p3]`. The six measured texels above agree.

The world atlas bind then uses `decode=1 swz=0,1,2,5`, so the same bytes become
sampled RGB `[p3,p2,p1]` and alpha 1. Values below are integer components on a
0-255 scale before filtering/base multiplication/fog, not reported game dlight
RGB. Dynamic-atlas values are predictions from the observed upload path, not
reads of the busy destination.

| Surface/sample | Logged memory bytes | Sampled RGB | Observation or prediction |
| --- | --- | --- | --- |
| Static wall atlas `(97,37)`, D524309 | `ff13375d` | `(93,55,19)` | Readable atlas |
| First flash wall patch `(1,0)`, D525298, destined for `(97,37)` | `5d371358` | `(88,19,55)` | Predicted dynamic atlas |
| Following flash wall patch `(1,4)`, D526156 | `cca513cb` | `(203,19,165)` | Predicted dynamic atlas |
| Baseline beam atlas `(64,26)`, D524309 | `2e1c0c28` | `(40,12,28)` | Readable atlas |
| Recovery beam patch `(0,0)`, D528075, destined for `(64,26)` | `89760c86` | `(134,12,118)` | Predicted dynamic atlas |

The beam example raises red by 94 and blue by 90 while green stays 12, before
the world shader multiplies by the base. It explains why the relight is magenta
and follows the affected surface. The baseline beam sample is already somewhat
magenta, consistent with the screenshot; the rocket intensifies it.


```text
PIX D524309 t1 xy=97,37 old=none new=ff13375d
PIX D525298 t0 xy=1,0 old=none new=5d371358
PIX D526156 t0 xy=1,4 old=none new=cca513cb
PIX D524309 t1 xy=64,26 old=none new=2e1c0c28
PIX D528075 t0 xy=0,0 old=none new=89760c86
```

These examples establish the actual permutation and its colour consequence.
They do **not** establish that p0 was meant to be red, p3 was meant to be alpha,
or that changing the generic TXO_ENDIAN rule is correct. The uploaded bytes can
be made to look warm by another interpretation, but that alone is not evidence
for that interpretation. Static and updated subrects using different apparent
component orders is the useful remaining lead.

## Yellow gun: a separate consumed vertex-colour path

The viewmodel is t0 `011cf000`, 256x256, shader 224. The first draw in each of
its compared batches is D523688, D525475, D526457 and D528301. The first triangle
has matching base UVs. Its small clip w and screen position identify the gun,
not one of the distant animated models.

The complete 65,536 level-zero texels at D523688, D525475 and D528301 compare
byte-for-byte equal. All four phase hashes match. Sampling state is unchanged,
and the enclosing cache records are `hit-generation`, gen 1079. The consumed
v1 colour changes substantially:

| Draw | First vertex v1 RGB |
| --- | --- |
| D523688, before fire | `(0.706445,0.405682,0.0909286)` |
| D525475, raised gun | `(1.95154,1.65738,0.0889302)` |
| D526457, following flash | `(1.63398,1.33322,0.0909286)` |
| D528301, recovery | `(0.729735,0.419056,0.0939263)` |

Exact first-vertex records follow. Unused varyings are retained in these quotes.


```text
VERT D523688 v0 clip=6.4107,-8.49288,3.79706,16.6507 v0=0.54223,0.509146,0,1 v1=0.706445,0.405682,0.0909286,1 v2=0.825506,0.690538,0,1 v3=-2.18949e+22,1.66755e-43,0,0 v4=-2.18951e+22,1.66755e-43,0,0 v5=-2.18815e+22,1.66755e-43,0,0 v6=-2.18804e+22,1.66755e-43,0,0 v7=-2.18811e+22,1.66755e-43,0,0 v8=-2.18809e+22,1.66755e-43,0,0 v9=-5.23876e+22,1.66755e-43,0,0
VERT D525475 v0 clip=7.39468,1.44486,2.3001,11.6633 v0=0.54223,0.509146,0,1 v1=1.95154,1.65738,0.0889302,1 v2=0.825506,0.690538,0,1 v3=-2.18949e+22,1.66755e-43,0,0 v4=-2.18951e+22,1.66755e-43,0,0 v5=-2.18815e+22,1.66755e-43,0,0 v6=-2.18804e+22,1.66755e-43,0,0 v7=-2.18811e+22,1.66755e-43,0,0 v8=-2.18809e+22,1.66755e-43,0,0 v9=-5.23876e+22,1.66755e-43,0,0
VERT D528301 v0 clip=6.17201,-9.08426,3.85158,16.8323 v0=0.54223,0.509146,0,1 v1=0.729735,0.419056,0.0939263,1 v2=0.825506,0.690538,0,1 v3=-2.18949e+22,1.66755e-43,0,0 v4=-2.18951e+22,1.66755e-43,0,0 v5=-2.18815e+22,1.66755e-43,0,0 v6=-2.18804e+22,1.66755e-43,0,0 v7=-2.18811e+22,1.66755e-43,0,0 v8=-2.18809e+22,1.66755e-43,0,0 v9=-5.23876e+22,1.66755e-43,0,0
```

Shader 224 was printed at D124895. Its routes are `t[0] = vin[0]` and
`t[1] = vin[1]`; it samples t0, then multiplies sampled RGB by v1 RGB with
no factor of two. Its US and selected MSL lines are:


```text
US D124895 id=224 BEGIN
US config 00000008 offset 0001400b, 1 node(s)
 node 0
  tex10 op3 t0 <- unit0 t0
  alu11 rgb 1c020040 00050200  alpha 01020040 00040509

US END
MSL D124895 id=224 BEGIN
```


```text
    t[0] = vin[0];
    t[1] = vin[1];
        float3 rA = (cs0.rgb);
        float3 rB = (cs1.rgb);
        float3 rr = rA * rB + rC;
        rr = rr; ar = ar;
```

The strong red/green multipliers and very low blue explain bright yellow and
clipping. This confirms the per-draw colour path for the gun. It does not prove
that the guest intended every numerical value or that PVS translation is fully
correct. The log exposes transformed outputs rather than the game's lighting
calculation. A fix to world lightmap upload should not be assumed to fix or
require changing the gun.

## Hypothesis-table verdicts

| Spec row | Verdict for this capture |
| --- | --- |
| CPU lightmap relight | Confirmed surface-lightmap upload/update mechanism: localized GART patches, atlas writes, a real non-baseline atlas diff, stable world shader/constants, and matched surfaces switching onto/off the dynamic atlas. The game-side CPU lighting calculation and source dlight RGB are not logged. This identifies how the moving lighting arrives, not the defective implementation. |
| Texture decode/channel error | The permutation and predicted magenta inputs are demonstrated. A *wrong* decode/swizzle relative to intended source order is not uniquely confirmed. Dynamic destination reads are skipped, and intended source components are absent. A blanket endian change is unsupported. |
| Bad source/upload bytes | Not confirmed or ruled out upstream. The six-texel control shows no source/destination byte disagreement in that upload path. It does not show whether GART bytes were already wrongly laid out before R300_LIGHTLOG sees them. |
| Stale texture cache | No positive evidence for the matched level-zero textures. Base hashes stay stable; lightmaps bind through vram-view. Higher mips and a distinct GPU synchronization defect are not validated by these probes. |
| Per-draw color/position path | Confirmed for the yellow gun through consumed v1. The matched wall/beam samples have white v1, zero CONSTs, stable blend/fog settings and stable consumed geometry at matched camera poses. Unlogged fog aux and guest-side inputs limit broader claims. |
| Combine/blend/output error | No demonstrated defect in the world/gun multiply or blending. The world path already predicts magenta before its multiply. Upload output packing participates in the byte contract, so an output-storage error is not independently excluded. Correctness against real hardware cannot be inferred just because US and generated MSL look consistent. |

There is therefore no uniquely confirmed *defect row*. Calling the entire event
a constant-colour bug would discard the lightmap evidence; calling it a proven
endian bug would invent the missing intended source layout.

## What a fix would need, and what remains unverified

The remaining boundary is the component order expected by the guest upload
versus the bytes R300 reads from GART and writes into an atlas. Relevant local
code is `r300_read_raw` / GART texture assembly in `hw/display/ppc_mac_gpu.c`,
`set_textures` and `set_uniforms` in `hw/display/r300/r300_draw.c`, and
`r300_tpost` / output packing plus `r300_cb_swap32` in `r300_us.c` / `r300_us.h`.
`r300_texture` in `ppc_mac_gpu_metal.m` uploads the GART bytes unchanged for
this RGBA8 path. These are boundaries to distinguish, not a proposal to edit
all of them or to toggle TXO_ENDIAN globally.

To select a fix, establish the source layout from a guest upload/driver trace
or an independently verified hardware contract, and observe the dynamic atlas
after its writes complete. The present log cannot supply that skipped read.
No further live capture was requested, and none was run. Any eventual fix still
needs the spec's before/after rocket test with moving warm lighting retained,
plus the R300 test suite on a Metal-capable host. There is no rendering change
in this analysis.

## Extraction and checks

The initial raw scan was scoped to record types and D524000-D528999 with
`LC_ALL=C grep -E`; shader headers were also selected. Full draw blocks were
then extracted with bounded seeks around ordered `TIME D...` records, so PIX,
unnumbered CACHE records and multiline STATE/US/MSL bodies were retained. A
D-prefixed grep alone loses CACHE records and most shader source.

For this exact raw file, zero-based byte intervals, end exclusive, are:

- D523300-D523999: `[17673865839,17718531420)`.
- D524000-D529999: `[17718531420,18078271537)`.

The earlier US/MSL blocks were found by their literal record markers; shaders
221, 222 and 224 were printed at D124861, D124862 and D124895. The raw capture
was never loaded whole. All intermediate extracts and scripts were under
`/tmp/qemu15-analysis/`, outside the evidence directory.

Offline assertions passed for the selected base hashes, complete gun snapshots,
matching wall/beam pipeline/constants/consumed vertices, the component permutation
on 1,024 byte-basis cases, and all six source/destination control texels. PNGs
were inspected directly, and decoded-pixel equality checked for the duplicate
screenshots. No renderer test or Metal/live verification was run: no rendering
code changed, and offline arithmetic does not validate GPU execution.

Only this analysis note was added. No `hw/display/` files, evidence files,
commits, branches, issues or releases were changed.

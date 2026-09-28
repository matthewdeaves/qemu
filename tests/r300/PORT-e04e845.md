# GPU vertex program port from poweremu

Port of `poweremu/r300` commit `e04e8450eaf1bb3be26fd975065e65ad458929ce`
onto `radeon-9700` at `70fed3035f59fd2fdf9391cf5dfe44550b88a2a0`.
The upstream commit and the parent-to-fork differences were inspected before
editing. No commit, VM access, bench reservation, installation or deployment.

## Adapted code

- The PVS translator is `r300_pvs_to_msl`, adapted from upstream's
  `r300_pvs_to_glsl`. It emits native Metal types, comparisons, `select`,
  `rsqrt`, float bit casts and constant address-space parameters. It keeps
  the upstream opcode translation, swizzles, abs/negate, saturation, dual
  and macro instructions, and A0-relative constants.
- `r300_draw.c` generates the viewport transform, colour/texture routing,
  fog and user clip distances. It packs each distinct input index once
  and assembles a triangle index buffer. Shader sources have stable cache
  ownership and IDs, following the fork's fragment translation convention.
  Uniform values are excluded from the source key. Unsupported translations
  are cached too. A cache hit allocates no source text or key.
- `R300State.gpu_vs` and `PPCMacGPURenderer.r300_gpu_vs` replace upstream's
  GLSL capability flag. Only Metal enables the capability. Points, lines,
  polygon mode, flat/solid and two-sided colours, flow control, rectangle
  lists and bypass draws keep the prepared interpreter. AA resolves also
  keep CPU vertices because this fork computes their bounds on the CPU.
- Metal binds input vertices at vertex buffer 0, sample shift at 1 and
  PVS uniforms at 2. The existing arena holds the inputs and indices.
  The rate log includes the GPU vertex-program share and guards division
  by zero. Light logging no longer dereferences absent CPU vertices.
- The prepared interpreter's dual-issue `cv[4]` is initialized to zero.
  Upstream fixed the equivalent raw interpreter operand `cc[4]`.
- Vertex functions compile separately with `MTLMathModeSafe`, through
  `r300_metal_vertex_options`. Fragment compilation retains its existing
  options, including on the forced CPU path.

## Reused fork infrastructure and skipped upstream machinery

- `r300_us_msl_cached` and its stable `msl`/`msl_id` ownership remain intact.
  No second fragment cache, GLSL generator, shaderc, SPIRV-Cross or Vulkan
  backend was introduced. This fork removed the upstream SPIR-V backend,
  so the upstream `r300_spirv.c` binding change is expressed directly as
  Metal buffer attributes instead.
- `r300_pipeline` still uses the existing locked `GHashTable` and BQL
  release around compilation. Its key now includes both shader IDs.
  The losing compiler in a concurrent duplicate insertion releases its
  owned pipeline, matching this file's manual reference counting.
- `r300_metal_cache.{h,m}` still owns persistent pipeline archive storage.
  Its digest now includes fragment source, optional vertex source, stage
  boundaries and a safe-math policy version. Cold/warm archive tests now
  distinguish the same fragments paired with two different vertex shaders.
  There is no parallel archive directory or cache implementation.
- Existing page-based GART/AGP blit mapping, `r300_upload_word`, VRAM page
  generations, texture page hashes, prepared PVS instructions and CPU
  post-transform reuse were preserved. Upstream's differently named
  blit/cache machinery was not imported.
- Contrary to the initial task description, this HEAD already tracks
  `tests/r300/run.sh`, QE fixtures and CPU/Metal regression tests. The
  existing native-MSL runner was extended with upstream's 450-line GPU
  test, adapted to native MSL and this fork's names, plus packet-selection
  regressions. The runner keeps the existing tests and now writes build
  and temporary files under the checkout by default.

## Safety switch verification

The production selection gate in `r300_draw.c` reads `R300_CPU_VS` once per
process. As upstream does, any presence disables GPU vertex programs;
`R300_CPU_VS=1` is the supported release switch.

`test_vs_build` builds the captured QE packet with Metal capability enabled.
Without the switch it asserts a generated shader, four packed vertices and
six indices. In a separate `R300_CPU_VS=1` process it asserts that the packet
has CPU vertices and no GPU shader, then compares every position, varying
and fog value against a capability-disabled interpreter build. Both runs
pass, including under AddressSanitizer and UndefinedBehaviorSanitizer.
`run.sh` also runs the GPU rendering comparison under `R300_CPU_VS=1`.

## Review and fix pass

A second Codex `--high` review (framed as a GPU/CPU emulator correctness
change) found three defects in the initial draft, all now fixed:

1. `tests/r300/test_gpuvs.m`'s `close_enough()` accepted any NaN/Inf pair as
   matching, so the "0 differ" GPU-vs-interpreter and QE-pixel results didn't
   actually cover special values. Rewritten: NaN only matches NaN, infinities
   only match via exact equality (same sign), everything else keeps the
   original relative tolerance.
2. `r300_metal_cache.h` set `mathMode = MTLMathModeSafe` but left
   `mathFloatingPointFunctions` at its `Fast` default, undermining the safe
   math the port's own PVS `_DX`/`_FF` opcodes depend on. Added
   `r300_metal_vertex_options()`, which sets `mathMode` +
   `mathFloatingPointFunctions = Precise` under `@available(macOS 15.0, *)`,
   falling back to `fastMathEnabled = NO` pre-15. The archive cache key is
   now a versioned SHA256 (stage boundaries + a safe-math policy tag) so no
   pre-port fast-math archive can satisfy a safe vertex-program pipeline.
3. Two `R300_DUMP` debug-dump loops in `ppc_mac_gpu.c`'s `r300_render()`
   dereferenced `pkt.verts` unconditionally; a successful GPU-VS packet nulls
   it. Both loops now guard on `pkt.verts` first, matching the existing
   light-logging path.

A third, independent Codex `--high` review of just the fix diff confirmed
all three fixes correct, both cache callers updated, the versioned key
correctly invalidates pre-port archives, `R300_CPU_VS` opt-out semantics
intact (no launcher/release script sets it), and every `MTLMathMode`-family
call site covered by the macOS-15 guard. No further findings.

## Validation status

- `ninja -C build qemu-system-ppc`: PASS.
- Full `./tests/r300/run.sh`, run with real Metal access after the fix pass:
  **PASS, all R300 tests**, including the checks the broken comparator had
  been masking: 400 random PVS programs against the interpreter (0 differ),
  QE quad and QE quad+fog pixel comparisons (0 of 47360 pixels differ each),
  `R300_CPU_VS=1` interpreter fallback verified equal to the GPU path, and
  cold/warm archive reuse across the new versioned cache key.

The GPU test retains upstream's per-component tolerance of
`1e-3 + 1e-3 * max(abs(cpu), abs(gpu))` for the finite case, checks every
captured pixel and its coverage, and defaults to 400 random programs with 64
inputs each. Passing `5000` requests the longer diagnostic run. Upstream
documented one denormal flush at 5000; that observation has not been
reproduced locally here.

Not yet done: the per-install `bench-compare.sh` A/B and VALID+frame-check
bundles for all 5 games, blocked on `qemu-tiger3d` being claimed by other
ports' loop tickets this round. No guest performance or VM rendering claim
is made until that runs.

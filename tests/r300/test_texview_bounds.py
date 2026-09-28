#!/usr/bin/env python3
"""qemu#22 regression: r200_view() must never hand Metal a bytesPerRow that
doesn't cover a full row of the width it claims -- Metal aborts the whole
process on that ('_mtlValidateStrideTextureParameters: failed assertion
Texture Descriptor Validation Linear texture: bytesPerRow must be greater
or equal to ...'), and a guest just needs to program an inconsistent
pitch/width pair to reach it (ordinary GPU command submission, no special
access needed).

Extracts the real mtl_bytes_per_element / r200_view_fits from production
code (hw/display/ppc_mac_gpu_metal.m) -- the same technique test_cp_reads.py
and test_2d_packet_headers.py use -- so the decision under test is the
actual shipped control flow, not a second implementation of it. Both
functions are pure (no Metal calls, no device-state mutation), which is
what makes this extraction possible without a real MTLDevice.

Ported from linuxkid473/poweremu-qemu f0e414a1ca9c8fab27b05f3086099feb9cf41bf2
("ppc-mac-gpu metal: validate VRAM texture views instead of letting Metal
abort"): a sampled texture whose pitch is too small for its width is
shrunk to what fits; a render target that doesn't fit gets no view at all
(the caller already treats a nil r200_view() as "skip this draw" -- see
r200_metal_warn/r300_metal_warn call sites around every r200_view() call).

Before this fix, r200_view() passed k.width/k.height and k.pitch/k.offset
straight through to -[MTLBuffer newTextureWithDescriptor:offset:bytesPerRow:]
with no check at all. Every existing call site validates alignment, and
most (R200's texture units and depth buffer, R300's colour buffer `ck[k]`)
also validate total VRAM bounds -- but metal_draw_r200()'s own colour
render target (`rk`) checks alignment only, never bounds, so a guest could
still reach Metal's abort through that one path. r200_view_in_bounds()
closes that gap the same way r200_view_fits() closes the pitch-vs-width
one: validated once, inside r200_view() itself, so no call site's
completeness matters.

Run: python3 tests/r300/test_texview_bounds.py
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'hw/display/ppc_mac_gpu_metal.m').read_text()

bpe_start = source.index('static uint32_t mtl_bytes_per_element(MTLPixelFormat pf)')
bpe_end = source.index('\n}\n', bpe_start) + len('\n}\n')
bpe_code = source[bpe_start:bpe_end]
assert 'MTLPixelFormatBGRA8Unorm' in bpe_code and 'return 4;' in bpe_code, \
    'mtl_bytes_per_element no longer covers BGRA8Unorm -- update this slice'

fits_start = source.index('static bool r200_view_fits(MTLPixelFormat pf, uint32_t pitch,')
fits_end = source.index('static id<MTLTexture> r200_view(PPCMacGPUMetalState *st,', fits_start)
fits_code = source[fits_start:fits_end]
assert 'return false;' in fits_code and '*width = pitch / bpe;' in fits_code, \
    'r200_view_fits no longer shrinks-or-refuses -- update this slice'
assert 'static bool r200_view_in_bounds(' in fits_code and \
    '*height = h;' in fits_code, \
    'r200_view_in_bounds no longer shrinks-or-refuses -- update this slice'

stub = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

/* Distinct opaque values -- the real Metal enum numbers don't matter here,
 * only that mtl_bytes_per_element's switch can tell them apart. */
typedef int MTLPixelFormat;
#define MTLPixelFormatR8Unorm      1
#define MTLPixelFormatR8Uint       2
#define MTLPixelFormatRG8Unorm     3
#define MTLPixelFormatR16Uint      4
#define MTLPixelFormatBGRA8Unorm   5
#define MTLPixelFormatRGBA8Unorm   6
#define MTLPixelFormatR32Uint      7
#define MTLPixelFormatRG32Uint     8
#define MTLPixelFormatRGBA32Uint   9
#define MTLPixelFormatDepth32Float 999   /* not in the switch: bpe must be 0 */
'''

checks = r'''
int main(void)
{
    /* Exact fit: pitch == width * bpe -- must pass through unchanged. */
    {
        uint32_t width = 512;
        bool ok = r200_view_fits(MTLPixelFormatBGRA8Unorm, 512 * 4, &width, false);
        assert(ok && width == 512);
    }
    {
        uint32_t width = 512;
        bool ok = r200_view_fits(MTLPixelFormatBGRA8Unorm, 512 * 4, &width, true);
        assert(ok && width == 512);
    }

    /* Comfortably bigger pitch than the width needs -- also unchanged. */
    {
        uint32_t width = 200;
        bool ok = r200_view_fits(MTLPixelFormatBGRA8Unorm, 4096, &width, false);
        assert(ok && width == 200);
    }

    /* The exact upstream crash shape: guest width 2188 bytes/row's worth
     * (547 BGRA8 texels) but pitch only 1024 -- a sampled texture must
     * shrink instead of handing Metal an under-sized bytesPerRow. */
    {
        uint32_t width = 547;
        bool ok = r200_view_fits(MTLPixelFormatBGRA8Unorm, 1024, &width, false);
        assert(ok && "shrinkable sampled texture must not be refused outright");
        assert(width == 1024 / 4 && "must shrink to exactly what the pitch supports");
        assert(width * 4 <= 1024 && "shrunk width must never need more than the pitch provides");
    }

    /* Same geometry, but as a render target: qemu#22's actual crash site
     * (metal_draw_r200's `rk` and the R300 colour-buffer `ck[k]`, neither
     * validated this before calling r200_view()). Must refuse, not shrink
     * -- a render target can't drop rows the guest expects to be there. */
    {
        uint32_t width = 547;
        bool ok = r200_view_fits(MTLPixelFormatBGRA8Unorm, 1024, &width, true);
        assert(!ok && "render target with too-small pitch must be refused, not shrunk");
        assert(width == 547 && "refused render target must leave *width untouched");
    }

    /* Pitch too small to fit even one texel: shrinking would produce
     * width 0, which must also refuse (not hand Metal a 0-width texture). */
    {
        uint32_t width = 10;
        bool ok = r200_view_fits(MTLPixelFormatR32Uint, 3, &width, false);
        assert(!ok && "pitch narrower than one texel must refuse, not shrink to 0");
    }

    /* A pixel format mtl_bytes_per_element doesn't recognise: bpe is 0, so
     * the check can't be verified -- must fall through unchanged rather
     * than falsely refuse (this is the pre-fix behaviour for every format,
     * so an unrecognised one must not regress by being newly refused). */
    {
        uint32_t width = 9999;
        bool ok = r200_view_fits(MTLPixelFormatDepth32Float, 4, &width, true);
        assert(ok && width == 9999 && "unrecognised format must not be validated (and not crash)");
    }

    /* mtl_bytes_per_element itself: spot-check the sizes that r200_view()
     * is actually called with across R200 texture units, R200/R300 render
     * targets, and R200/R300 depth buffers. */
    assert(mtl_bytes_per_element(MTLPixelFormatR8Unorm) == 1);
    assert(mtl_bytes_per_element(MTLPixelFormatR8Uint) == 1);
    assert(mtl_bytes_per_element(MTLPixelFormatRG8Unorm) == 2);
    assert(mtl_bytes_per_element(MTLPixelFormatR16Uint) == 2);
    assert(mtl_bytes_per_element(MTLPixelFormatBGRA8Unorm) == 4);
    assert(mtl_bytes_per_element(MTLPixelFormatRGBA8Unorm) == 4);
    assert(mtl_bytes_per_element(MTLPixelFormatR32Uint) == 4);
    assert(mtl_bytes_per_element(MTLPixelFormatRG32Uint) == 8);
    assert(mtl_bytes_per_element(MTLPixelFormatRGBA32Uint) == 16);
    assert(mtl_bytes_per_element(MTLPixelFormatDepth32Float) == 0);

    /* r200_view_in_bounds: exact fit -- the last byte of the last row lands
     * exactly on the buffer end. Must pass through with height unchanged. */
    {
        uint32_t height = 10;
        bool ok = r200_view_in_bounds(0, 100, 4, 10, &height, 1000, false);
        assert(ok && height == 10);
    }

    /* Comfortably within a much bigger buffer -- also unchanged. */
    {
        uint32_t height = 10;
        bool ok = r200_view_in_bounds(0, 100, 4, 10, &height, 100000, true);
        assert(ok && height == 10);
    }

    /* Sampled texture whose rows run past the buffer end: must shrink
     * height to what actually fits, not refuse outright. */
    {
        uint32_t height = 20;
        bool ok = r200_view_in_bounds(0, 100, 4, 10, &height, 1000, false);
        assert(ok && "shrinkable sampled view must not be refused outright");
        assert(height == 10 && "must shrink to exactly what the buffer supports");
        assert((uint64_t)9 * 100 + 40 <= 1000 &&
               "shrunk height must never need more bytes than the buffer has");
    }

    /* Same geometry, but as a render target: qemu#22's actual gap --
     * metal_draw_r200()'s colour render target validated alignment but
     * never this. Must refuse, not shrink -- a render target can't drop
     * rows the guest expects to be there. */
    {
        uint32_t height = 20;
        bool ok = r200_view_in_bounds(0, 100, 4, 10, &height, 1000, true);
        assert(!ok && "render target that overruns VRAM must be refused, not shrunk");
        assert(height == 20 && "refused render target must leave *height untouched");
    }

    /* Offset already at or past the end of VRAM: refuse outright regardless
     * of render_target -- there is nothing to shrink toward. */
    {
        uint32_t height = 1;
        bool ok = r200_view_in_bounds(1000, 100, 4, 10, &height, 1000, false);
        assert(!ok && "offset >= vram length must refuse even for a sampled texture");
        assert(height == 1 && "*height must be untouched when refused");
    }

    /* Doesn't fit even at height 1 (one row alone runs past the end): no
     * amount of shrinking helps, must refuse even though it's sampled. */
    {
        uint32_t height = 1;
        bool ok = r200_view_in_bounds(970, 100, 4, 10, &height, 1000, false);
        assert(!ok && "a single row past the buffer end must refuse, not shrink to 0");
    }

    /* An unrecognised format (bpe 0, as mtl_bytes_per_element returns for
     * one) can't be verified -- must fall through unchanged, matching
     * r200_view_fits' same "can't verify" convention above. */
    {
        uint32_t height = 999999;
        bool ok = r200_view_in_bounds(999999, 1, 0, 1, &height, 10, true);
        assert(ok && height == 999999 && "unrecognised bpe must not be validated");
    }

    /* height == 0 in: nothing to check (an empty view, handled elsewhere by
     * r200_view's own !w/!h "empty" reasoning) -- must not divide by zero
     * or otherwise misbehave. */
    {
        uint32_t height = 0;
        bool ok = r200_view_in_bounds(0, 100, 4, 10, &height, 1000, false);
        assert(ok && height == 0);
    }

    puts("r200_view_fits: sampled shrinks, render target refuses: PASS");
    puts("r200_view_in_bounds: sampled shrinks, render target refuses: PASS");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='texview-bounds-', dir=Path(__file__).parent) as tmp:
    c = Path(tmp) / 'test.c'
    binary = Path(tmp) / 'test'
    c.write_text(stub + bpe_code + fits_code + checks)
    subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
        '-std=gnu11', '-O1', '-Wall', '-Wextra', '-Werror',
        '-Wno-unused-parameter', '-Wno-unused-function',
        str(c), '-o', str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True)

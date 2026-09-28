#!/usr/bin/env python3
"""qemu#20 regression: CNTL_BITBLT_MULTI / CNTL_PAINT_MULTI must never read
past their own declared body_dw, however the GMC flags are set.

Extracts the real ppc_mac_gpu_bbm_header / ppc_mac_gpu_pm_header from
production code -- the same technique test_pm4_ring.py and test_cp_reads.py
use -- so the bounds decision under test is the actual shipped control flow,
not a second implementation of it. Both functions are pure (no I/O, no
device-state mutation), which is what makes this extraction possible without
a device-state stub.

Each `d` buffer is malloc'd to EXACTLY body_dw dwords, and the whole test is
built with AddressSanitizer. Before qemu#20's fix, the "all GMC flags set,
body_dw at the guard minimum" cases below read 1-2 dwords past that
allocation -- ASan turns that into an immediate abort instead of a silent
pass, so this test cannot pass by accident just because the assertions
happen to tolerate a wrong-but-in-bounds answer.

Run: python3 tests/r300/test_2d_packet_headers.py
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'hw/display/ppc_mac_gpu.c').read_text()
start = source.index('static bool ppc_mac_gpu_bbm_header(const uint32_t *d, uint32_t body_dw,')
end = source.index('/*\n * Total dwords (header + body) a PM4 packet occupies', start)
code = source[start:end]
assert 'static bool ppc_mac_gpu_pm_header(' in code, \
    'ppc_mac_gpu_pm_header moved or was removed -- update this slice'
assert code.count('return false;') >= 5, \
    'expected bounds-check bailouts look different -- update this slice ' \
    'and its test cases together'

stub = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define R200_GMC_SRC_PITCH_OFFSET_CNTL (1 << 0)
#define R200_GMC_DST_PITCH_OFFSET_CNTL (1 << 1)
#define R200_GMC_SRC_CLIPPING          (1 << 2)
#define R200_GMC_DST_CLIPPING          (1 << 3)
'''

checks = r'''
/* Heap-exact buffer: any read of d[body_dw] or beyond is an ASan abort,
 * not a silently-tolerated wrong answer. */
static uint32_t *mkbuf(uint32_t body_dw, ...)
{
    uint32_t *d = malloc(body_dw * sizeof(uint32_t));
    assert(d);
    __builtin_va_list ap;
    __builtin_va_start(ap, body_dw);
    for (uint32_t i = 0; i < body_dw; i++) {
        d[i] = __builtin_va_arg(ap, uint32_t);
    }
    __builtin_va_end(ap);
    return d;
}

static void check_bbm_minimal(void)
{
    /* No GMC bits set: 1-dword header, both PO from the default register. */
    uint32_t *d = mkbuf(4, 0x00000000u, 11, 22, 33);
    uint32_t hdr_dw, src_po, dst_po, clip_tl, clip_br;
    bool ok = ppc_mac_gpu_bbm_header(d, 4, 0xAAAAAAAAu, &hdr_dw, &src_po,
                                      &dst_po, &clip_tl, &clip_br);
    assert(ok && hdr_dw == 1);
    assert(src_po == 0xAAAAAAAAu && dst_po == 0xAAAAAAAAu);
    free(d);
    puts("bbm minimal header: PASS");
}

static void check_bbm_full_header_fits(void)
{
    /* All four header-growing GMC bits set, body_dw exactly 6: the 6-dword
     * header (GMC, src_po, dst_po, [src clip skipped], clip_tl, clip_br)
     * fits exactly -- must succeed and read the real values. */
    uint32_t gmc = R200_GMC_SRC_PITCH_OFFSET_CNTL | R200_GMC_DST_PITCH_OFFSET_CNTL
                 | R200_GMC_SRC_CLIPPING | R200_GMC_DST_CLIPPING;
    uint32_t *d = mkbuf(6, gmc, 0x11111111u, 0x22222222u, 0xdeadbeefu,
                         0x33333333u, 0x44444444u);
    uint32_t hdr_dw, src_po, dst_po, clip_tl, clip_br;
    bool ok = ppc_mac_gpu_bbm_header(d, 6, 0, &hdr_dw, &src_po, &dst_po,
                                      &clip_tl, &clip_br);
    assert(ok && hdr_dw == 6);
    assert(src_po == 0x11111111u && dst_po == 0x22222222u);
    assert(clip_tl == 0x33333333u && clip_br == 0x44444444u);
    free(d);
    puts("bbm full header, body_dw exactly enough: PASS");
}

/* qemu#20's own overrun shape: all four GMC bits set (implying a 6-dword
 * header) but body_dw only 4 -- the guard that dispatches BITBLT_MULTI at
 * all is body_dw >= 4, so this is the smallest packet the real code path
 * can reach here with. Before the fix this read d[4]/d[5], 2 dwords past
 * a 4-dword allocation. */
static void check_bbm_overrun_rejected(void)
{
    uint32_t gmc = R200_GMC_SRC_PITCH_OFFSET_CNTL | R200_GMC_DST_PITCH_OFFSET_CNTL
                 | R200_GMC_SRC_CLIPPING | R200_GMC_DST_CLIPPING;
    uint32_t *d = mkbuf(4, gmc, 1, 2, 3);
    uint32_t hdr_dw, src_po, dst_po, clip_tl, clip_br;
    bool ok = ppc_mac_gpu_bbm_header(d, 4, 0, &hdr_dw, &src_po, &dst_po,
                                      &clip_tl, &clip_br);
    assert(!ok);
    free(d);

    /* body_dw = 5: still one short (needs 6). */
    d = mkbuf(5, gmc, 1, 2, 3, 4);
    ok = ppc_mac_gpu_bbm_header(d, 5, 0, &hdr_dw, &src_po, &dst_po,
                                 &clip_tl, &clip_br);
    assert(!ok);
    free(d);
    puts("bbm overrun (body_dw 4 and 5, all GMC bits set): PASS (rejected, no OOB read)");
}

static void check_pm_minimal(void)
{
    /* No PO, no clipping, no solid brush: 1-dword header. */
    uint32_t *d = mkbuf(3, 0x00000000u, 100, 200);
    uint32_t idx, offset, pitch, po, x0, y0, x1, y1, color;
    bool ok = ppc_mac_gpu_pm_header(d, 3, 0x55555555u, 0x66666666u, &idx,
                                     &offset, &pitch, &po, &x0, &y0, &x1, &y1,
                                     &color);
    assert(ok && idx == 1 && color == 0x66666666u);
    free(d);
    puts("pm minimal header: PASS");
}

/* qemu#20's own overrun shape: has_po + SRC_CLIPPING push idx to 3 before
 * the brush-colour read, but body_dw (the dispatch guard's minimum) is
 * only 3 -- before the fix this read d[3], 1 dword past a 3-dword
 * allocation, and idx (4) then exceeded body_dw (3), which underflows
 * (body_dw - idx) in the caller's fill-count arithmetic. */
static void check_pm_overrun_rejected(void)
{
    uint32_t gmc = R200_GMC_DST_PITCH_OFFSET_CNTL | R200_GMC_SRC_CLIPPING
                 | (0xD << 4);   /* has_po=1, brush=solid */
    uint32_t *d = mkbuf(3, gmc, 0xAAAAAAAAu, 0xBBBBBBBBu);
    uint32_t idx, offset, pitch, po, x0, y0, x1, y1, color;
    bool ok = ppc_mac_gpu_pm_header(d, 3, 0, 0, &idx, &offset, &pitch, &po,
                                     &x0, &y0, &x1, &y1, &color);
    assert(!ok);
    free(d);

    /* body_dw = 4: exactly enough (GMC, po, [src clip skip], color). d[2]
     * is the skipped SRC_SC_BOTTOM_RIGHT dword -- consumed positionally
     * but never read, so the colour is d[3], not d[2]. */
    d = mkbuf(4, gmc, 0xAAAAAAAAu, 0xCCCCCCCCu, 0xdeadbeefu);
    ok = ppc_mac_gpu_pm_header(d, 4, 0, 0, &idx, &offset, &pitch, &po,
                                &x0, &y0, &x1, &y1, &color);
    assert(ok && idx == 4 && color == 0xdeadbeefu);
    free(d);
    puts("pm overrun (has_po + src_clip + solid brush, body_dw 3 vs 4): PASS");
}

/* Codex --high review (qemu#20): DST_CLIPPING used to silently skip the
 * clip read and still succeed when body_dw was too short for it, instead
 * of rejecting the packet like every other short-header case here. Never
 * an OOB read (the read itself was already gated), just an inconsistent
 * "malformed packet accepted with defaults" outcome. */
static void check_pm_dst_clipping_too_short_rejected(void)
{
    uint32_t gmc = R200_GMC_DST_CLIPPING | (0xD << 4);   /* no PO, solid brush */
    /* idx=1 after header; DST_CLIPPING needs d[1],d[2] (idx+2<=body_dw), so
     * body_dw=2 is one short. */
    uint32_t *d = mkbuf(2, gmc, 0xdeadbeefu);
    uint32_t idx, offset, pitch, po, x0, y0, x1, y1, color;
    bool ok = ppc_mac_gpu_pm_header(d, 2, 0, 0, &idx, &offset, &pitch, &po,
                                     &x0, &y0, &x1, &y1, &color);
    assert(!ok);
    free(d);

    /* body_dw=4: exactly enough (GMC, clip_tl, clip_br, color). */
    d = mkbuf(4, gmc, 0x00010002u, 0x00030004u, 0x77777777u);
    ok = ppc_mac_gpu_pm_header(d, 4, 0, 0, &idx, &offset, &pitch, &po,
                                &x0, &y0, &x1, &y1, &color);
    assert(ok && idx == 4 && color == 0x77777777u);
    assert(x0 == 2 && y0 == 1 && x1 == 4 && y1 == 3);
    free(d);
    puts("pm DST_CLIPPING too-short header rejected (not silently skipped): PASS");
}

int main(void)
{
    check_bbm_minimal();
    check_bbm_full_header_fits();
    check_bbm_overrun_rejected();
    check_pm_minimal();
    check_pm_overrun_rejected();
    check_pm_dst_clipping_too_short_rejected();
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='2d-hdr-', dir=Path(__file__).parent) as tmp:
    c = Path(tmp) / 'test.c'
    binary = Path(tmp) / 'test'
    c.write_text(stub + code + checks)
    subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
        '-std=gnu11', '-O1', '-g', '-fsanitize=address,undefined',
        '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
        str(c), '-o', str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True)

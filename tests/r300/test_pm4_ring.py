#!/usr/bin/env python3
"""qemu#1 regression: an incomplete PM4 packet must not be acknowledged.

Extracts the real ppc_mac_gpu_process_pm4 (the packet-framing walker) and
ppc_mac_gpu_pm4_packet_dw (its length function) from production code, the
same technique test_cp_reads.py uses for the CP readers -- so the framing
decision under test (whether an incomplete packet is left unconsumed) is
the actual shipped control flow, not a second implementation of it.

ppc_mac_gpu_process_pm4 calls out to ppc_mac_gpu_pm4_dispatch once a packet
is confirmed to fully fit; that function (the per-opcode 2D/3D command
handling, hundreds of lines, entangled with the rest of the device model)
is deliberately NOT extracted. This file supplies a recording test double
for it instead -- exactly the split qemu#1's own review recommended, and
the reason ppc_mac_gpu_process_pm4 was refactored to call out to a named
function in the first place. The test double's contract (advance by
exactly ppc_mac_gpu_pm4_packet_dw(hdr) - 1 body dwords, matching what
every real dispatch branch is verified to do) is independent of the
framing bug: this test cannot pass by accident just because the stub is
lenient, since the walker's own break/bounds check is real production
code, unedited.

Confirmed to actually catch the regression it targets: with the walker's
`if (packet_dw > size_dw - i) { break; }` check commented out (reverting to
the pre-fix "run past the end" behavior), check_split_type0_not_reparsed
below fails instead of passing.

Run: python3 tests/r300/test_pm4_ring.py
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'hw/display/ppc_mac_gpu.c').read_text()
start = source.index('static uint32_t ppc_mac_gpu_pm4_packet_dw(uint32_t hdr)')
end = source.index('/*\n * Dispatches one already-validated', start)
code = source[start:end]
assert 'return ((hdr >> 16) & 0x3FFF) + 2;' in code, \
    'ppc_mac_gpu_pm4_packet_dw moved or changed shape -- update the slice'
assert 'if (packet_dw > size_dw - i) {' in code and 'break;' in code, \
    'ppc_mac_gpu_process_pm4 no longer breaks on an incomplete packet -- ' \
    'update the slice (or this IS the qemu#1 regression -- do not "fix" ' \
    'this assertion to make it pass)'
assert 'i = ppc_mac_gpu_pm4_dispatch(' in code, \
    'ppc_mac_gpu_process_pm4 no longer calls out to a separate dispatch ' \
    'function -- update this test to match the new structure'

stub = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include <assert.h>
#include <inttypes.h>

typedef struct PPCMacGPUState PPCMacGPUState;

static int g_in_pm4;
static const char *cp_submission = "test";

static FILE *r300_ringdump(void) { return NULL; }

static void qemu_log(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
}
'''

checks = r'''
/* The test double for ppc_mac_gpu_pm4_dispatch: the walker only ever calls
 * this once ppc_mac_gpu_pm4_packet_dw(hdr) dwords are confirmed to fit, so
 * it can unconditionally advance by that packet's own body length --
 * exactly the contract every real dispatch branch is verified (by the
 * qemu#1 review) to uphold. Records what it was asked to execute, so tests
 * can assert on WHAT ran and how often, not just on the final position. */
#define MAX_EXECUTED 64
static uint32_t executed_hdr[MAX_EXECUTED];
static uint32_t executed_n;

static uint32_t ppc_mac_gpu_pm4_dispatch(PPCMacGPUState *s,
                                          uint32_t *pm4_data,
                                          uint32_t size_dw,
                                          uint32_t hdr, uint32_t type,
                                          uint32_t i)
{
    (void)s; (void)pm4_data; (void)size_dw; (void)type;
    assert(executed_n < MAX_EXECUTED);
    executed_hdr[executed_n++] = hdr;
    return i + ppc_mac_gpu_pm4_packet_dw(hdr) - 1;
}

static uint32_t run_walker(uint32_t *ring, uint32_t size_dw)
{
    executed_n = 0;
    return ppc_mac_gpu_process_pm4(NULL, ring, size_dw);
}

static void check_packet_dw(void)
{
    /* Type 0, count field 0 -> 1 body dword (a single register write). */
    assert(ppc_mac_gpu_pm4_packet_dw(0x00000000u) == 2);
    /* Type 0, count field 3 -> 4 body dwords (qemu#1's own repro shape:
     * one packet writing four consecutive scratch registers). */
    assert(ppc_mac_gpu_pm4_packet_dw(0x00030578u) == 5);
    /* Type 1 (legacy): always 3 total, regardless of bits [29:16] -- this
     * is qemu#1's second bug: the old code read those bits as a count. */
    assert(ppc_mac_gpu_pm4_packet_dw(0x4FFF0000u) == 3);
    assert(ppc_mac_gpu_pm4_packet_dw(0x40000000u) == 3);
    /* Type 2 (NOP): always 1 (header only), any payload bits ignored. */
    assert(ppc_mac_gpu_pm4_packet_dw(0xBFFFFFFFu) == 1);
    /* Type 3, count field 0 -> 2 total (header + 1 body dword). */
    assert(ppc_mac_gpu_pm4_packet_dw(0xC0000000u) == 2);
    assert(ppc_mac_gpu_pm4_packet_dw(0xC0050000u) == 7);
    puts("pm4_packet_dw: PASS");
}

/*
 * qemu#1's own repro shape (from the ticket's captured log and Codex's
 * in-memory reconstruction): a type-0 packet writing 4 scratch registers,
 * header 0x00030578, split so only its header + first body dword (SCRATCH0
 * = 0x11111111) are visible in the first "chunk". The remaining 3 body
 * dwords -- 0x0000aaaa, 0x000105bb, and a third value -- must stay
 * unconsumed rather than be re-decoded as a new header (the corruption:
 * 0x00010052 read as a type-0 write of 2 registers at 0x148, producing
 * MC_FB_LOCATION=0x0000aaaa, MC_AGP_LOCATION=0x000105bb).
 */
static void check_split_type0_not_reparsed(void)
{
    uint32_t ring[8] = {0};
    ring[0] = 0x00030578u; ring[1] = 0x11111111u;
    ring[2] = 0x0000aaaau; ring[3] = 0x000105bbu; ring[4] = 0xdeadbeefu;
    /* A trailing marker packet: SCRATCH_REG, single write, unrelated hdr. */
    ring[5] = 0x00000600u; ring[6] = 0x600d0000u;

    /* First chunk exposes only the header + 1 of 4 body dwords. */
    uint32_t consumed = run_walker(ring, 2);
    assert(consumed == 0 && executed_n == 0);   /* nothing acked: incomplete */

    /* Second chunk exposes the rest of the split packet plus the marker.
     * Must execute the ORIGINAL packet exactly once, whole -- not reparse
     * 0x0000aaaa as a header, and not skip/duplicate the marker. */
    consumed = run_walker(ring, 7);
    assert(consumed == 7 && executed_n == 2);
    assert(executed_hdr[0] == 0x00030578u);   /* whole packet, once */
    assert(executed_hdr[1] == 0x00000600u);   /* marker, once, not shifted */
    puts("split type-0 packet: PASS (executed once, whole, no reparse)");
}

/* A split type-3 packet must not be dispatched short, and header-only
 * (zero body dwords visible) must behave the same as any other split. */
static void check_split_type3_and_header_only(void)
{
    uint32_t ring[4] = {0};
    ring[0] = 0xC0020000u;                  /* type 3, 3 body dwords -> 4 total */
    ring[1] = 1; ring[2] = 2; ring[3] = 3;

    assert(run_walker(ring, 1) == 0 && executed_n == 0);  /* header only */
    assert(run_walker(ring, 3) == 0 && executed_n == 0);  /* partial body */
    uint32_t consumed = run_walker(ring, 4);              /* fully visible */
    assert(consumed == 4 && executed_n == 1);
    assert(executed_hdr[0] == 0xC0020000u);
    puts("split type-3 packet / header-only: PASS");
}

/* A truncated type-1 header followed by a real marker: the walker must not
 * misjudge type-1's (fixed, 2-dword) body length -- the ticket's second,
 * independent alignment bug (the old code read bits [29:16] as a count). */
static void check_split_type1_then_marker(void)
{
    uint32_t ring[6] = {0};
    ring[0] = 0x7FFF0000u; ring[1] = 0xaaaaaaaau;   /* type 1: 2 body dwords */
    ring[2] = 0xbbbbbbbbu;
    ring[3] = 0x00000700u; ring[4] = 0x700d0000u;   /* marker, type 0 */

    assert(run_walker(ring, 2) == 0 && executed_n == 0);  /* body incomplete */
    uint32_t consumed = run_walker(ring, 5);
    assert(consumed == 5 && executed_n == 2);
    assert(executed_hdr[0] == 0x7FFF0000u);
    assert(executed_hdr[1] == 0x00000700u);
    puts("split type-1 packet + marker: PASS");
}

/* Re-running with no new complete packets must not re-execute anything. */
static void check_no_replay(void)
{
    uint32_t ring[2] = {0x00000000u, 0xaaaaaaaau};  /* type 0, 1 body dword */
    assert(run_walker(ring, 2) == 2 && executed_n == 1);
    assert(run_walker(ring, 0) == 0 && executed_n == 0);
    puts("no-replay on empty window: PASS");
}

/* A single call whose visible window holds one complete packet followed by
 * one incomplete packet must ack ONLY the complete prefix -- found by
 * Codex's qemu#1 review pass 2 via mutation testing: a mutant that acked
 * the whole window (complete prefix + incomplete tail) as consumed passed
 * every other check above. The earlier checks only ever grow the window
 * across separate calls, so none of them exercise a complete-then-
 * incomplete boundary inside one call. */
static void check_complete_prefix_incomplete_tail(void)
{
    uint32_t ring[] = {0x00000600u, 0x600d0000u,          /* marker: complete */
                        0x00030578u, 1, 2, 3, 4};          /* tail packet, 5 total */

    /* Window covers the whole marker plus only 2 of the tail's 5 dwords. */
    assert(run_walker(ring, 4) == 2 && executed_n == 1);
    assert(executed_hdr[0] == 0x00000600u);

    /* Tail alone, still short: nothing acked. */
    assert(run_walker(ring + 2, 2) == 0 && executed_n == 0);

    /* Tail's full window arrives: now it executes, whole, once. */
    assert(run_walker(ring + 2, 5) == 5 && executed_n == 1);
    assert(executed_hdr[0] == 0x00030578u);
    puts("complete prefix + incomplete tail in one call: PASS");
}

int main(void)
{
    check_packet_dw();
    check_split_type0_not_reparsed();
    check_split_type3_and_header_only();
    check_split_type1_then_marker();
    check_no_replay();
    check_complete_prefix_incomplete_tail();
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='pm4-ring-', dir=Path(__file__).parent) as tmp:
    c = Path(tmp) / 'test.c'
    binary = Path(tmp) / 'test'
    c.write_text(stub + code + checks)
    subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
        '-std=gnu11', '-O1', '-Wall', '-Wextra', '-Werror',
        '-Wno-unused-parameter',
        str(c), '-o', str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True)

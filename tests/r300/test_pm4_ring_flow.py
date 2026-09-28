#!/usr/bin/env python3
"""qemu#1 regression: ring-buffer flow control must never strand RPTR.

Extracts the real ppc_mac_gpu_process_ring_buffer (plus the packet walker
it calls, ppc_mac_gpu_process_pm4/ppc_mac_gpu_pm4_packet_dw) from production
code, the same technique test_pm4_ring.py and test_pm4_ib.py use -- so the
flow-control decisions under test are the actual shipped control flow.

Covers three bugs a Codex --high review found in the qemu#1 fix
(40ea2c2c49) once it started leaving genuinely incomplete data unconsumed
instead of unconditionally acknowledging it as done:

1. The `count > 65536` "oversized batch" guard used to return outright,
   leaving RPTR untouched -- so the next identical WPTR write hit the exact
   same rejection forever. Fixed by draining in bounded chunks instead.
2. Deferring a packet whose declared size exceeds the ring's own usable
   capacity (ring_size_dw - 1 dwords, the one-slot-reserved convention real
   CP ring producers use) deadlocks the guest: it cannot publish the
   remainder without WPTR overtaking RPTR, and RPTR cannot move until the
   packet completes. Fixed by staging the already-copied-out dwords in the
   device and freeing that ring space, rather than holding RPTR on data
   that can never arrive.
3. Commands queued while CP_ME_CNTL's ME_HALT bit was set never ran when it
   cleared -- the WPTR handler only calls ppc_mac_gpu_process_ring_buffer()
   while running, and nothing previously resumed the backlog on the
   halt->run transition. That resume call lives in the CP_ME_CNTL mmio
   write case, which (unlike process_ring_buffer itself) is entangled with
   the rest of the giant mmio_write() switch and not practical to extract
   whole; this file instead (a) asserts the resume call's source pattern is
   present as a tripwire against silent reversion, matching this repo's
   existing practice for the process_pm4 break check, and (b) functionally
   exercises process_ring_buffer with the exact [RPTR, WPTR) span the
   resume call makes -- multiple WPTR writes while halted only ever update
   cp_rb_wptr, so by the time resume fires only the final WPTR value and
   the original RPTR matter, which collapse to one span either way.

A follow-up Codex --high review of the first version of fix 2 (the ring
staging) found three further bugs, all fixed here:

  a. The stage was dropped on any call where THAT call's own `count` didn't
     independently look like a deadlock, discarding an in-progress stage
     the moment the guest published the rest across more than one WPTR
     write -- reproduced below by completing a staged packet across two
     separate one-dword updates instead of one two-dword update.
  b. The chunk loop broke on any `consumed < chunk`, not just `consumed ==
     0`, stranding already-published commands at a chunk boundary that
     split a packet -- reproduced below with a packet straddling exactly
     RING_BATCH_MAX_DW.
  c. The stage lived in file-scope statics, shared across every device
     instance, untouched by reset, and absent from VMState. Fixed by
     moving it into PPCMacGPUState (ring_stage_dw/ring_stage_len, a fixed
     16384-dword buffer -- a PM4 header caps a single packet at 16385
     dwords, so that capacity can never be exceeded), clearing it in
     ppc_mac_gpu_reset(), and adding it to vmstate_ppc_mac_gpu. Device
     construction, reset and migration are outside what this offline
     harness extracts (they need a real QEMU object model), so this file
     only asserts the source pattern for the reset clear as a tripwire;
     the struct/VMState placement is checked by the normal build.

A second follow-up review of that fix found three more lifecycle issues,
also outside what this offline harness can extract and so also covered
only by source-pattern tripwires:

  d. Nothing cleared the stage when the guest reinitializes the ring
     (CP_RB_BASE/CP_RB_CNTL/CP_RB_RPTR writes) -- a stale staged partial
     packet from before the reinit would be silently prepended to the new
     ring's first real traffic. Fixed by clearing ring_stage_len in all
     three write handlers.
  e. The new fields were appended to vmstate_ppc_mac_gpu's unconditional
     field list without bumping its version, so every snapshot taken
     before this fix -- with no ring-stage bytes in the stream at all --
     would have its next bytes misread as ring-stage state on load. Fixed
     by moving them into a subsection (vmstate_ppc_mac_gpu_ring_stage)
     gated on ring_stage_len != 0: a pre-fix snapshot has no subsection
     and loads exactly as before (ring_stage_len stays 0), and a post-fix
     snapshot only carries the subsection when there was genuinely
     something staged.
  f. ppc_mac_gpu_post_load() trusted a restored ring_stage_len completely;
     a corrupted or hand-edited snapshot could set it past
     ring_stage_dw's capacity (feeding a bad size into the next
     malloc/memcpy in ppc_mac_gpu_process_ring_buffer) or to a value not
     actually shorter than its own staged header's declared packet
     length. Fixed with two post_load checks, each rejecting the load
     (return -1) rather than silently clamping or trusting the data.

A third follow-up review, of fix d specifically, found two more issues and
one bug in this file's own tripwire:

  g. Clearing on every CP_RB_CNTL write was too broad: that register also
     carries writeback/fetch policy (RB_NO_UPDATE, RB_BLKSZ, MAX_FETCH)
     that a driver can legitimately change mid-stream without abandoning
     a packet already staged, only ppc_mac_gpu_process_ring_buffer reads
     bits[5:0] (rb_bufsz, the ring size). Narrowed to clear only when
     those bits actually change.
  h. R200_RBBM_SOFT_RESET's SOFT_RESET_CP bit resets the command
     processor itself -- a separate abandonment event from a
     BASE/CNTL/RPTR reinit, and nothing cleared the stage for it. Fixed
     by clearing ring_stage_len when that bit is set.
  i. This file's own tripwire for finding d had a bug: a fixed 700-
     character window after each case label was wide enough to also
     contain the NEXT case's clear line, so removing only the CNTL
     clear (leaving RPTR's) still passed. Fixed by bounding each case's
     extracted body to its own `break;`.

Run: python3 tests/r300/test_pm4_ring_flow.py
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = ROOT / 'hw/display/ppc_mac_gpu.c'
source = SRC_PATH.read_text()

start = source.index('static uint32_t ppc_mac_gpu_pm4_packet_dw(uint32_t hdr)')
end = source.index('/*\n * Dispatches one already-validated', start)
walker = source[start:end]
assert 'if (packet_dw > size_dw - i) {' in walker and 'break;' in walker, \
    'ppc_mac_gpu_process_pm4 no longer breaks on an incomplete packet -- ' \
    'update the slice (or this IS the qemu#1 regression)'

start = source.index('/* Bound each process_pm4() pass over ring data')
end = source.index('/* qemu#25 ----', start)  # ring code ends here; CP thread follows
ring = source[start:end]
assert 's->ring_stage_dw' in ring and 's->ring_stage_len' in ring, \
    'ppc_mac_gpu_process_ring_buffer no longer stages an oversized ' \
    'deferred packet -- update the slice'
assert 'RING_BATCH_MAX_DW' in ring, \
    'ppc_mac_gpu_process_ring_buffer no longer chunks large batches -- ' \
    'update the slice'

# Tripwires (device object model, not extractable into this offline
# harness) that the ring stage is device-owned, cleared on reset and ring
# reinitialization, and migrated safely -- see the module docstring, items
# 3c and the follow-up review's three lifecycle findings.
reset_start = source.index('static void ppc_mac_gpu_reset(DeviceState *dev)')
reset_end = source.index('\n}\n', reset_start)
assert 's->ring_stage_len = 0;' in source[reset_start:reset_end], \
    'ppc_mac_gpu_reset no longer clears ring_stage_len -- a guest reboot ' \
    'would resume a stale partial packet from before the reset'

mmio_write_def = source.index(
    'static void ppc_mac_gpu_mmio_write(void *opaque, hwaddr addr,\n'
    '                                    uint64_t val, unsigned int size)\n{')
for reg in ('R200_CP_RB_BASE', 'R200_CP_RB_CNTL', 'R200_CP_RB_RPTR',
            'R200_RBBM_SOFT_RESET'):
    idx = source.index('case %s:\n' % reg, mmio_write_def)
    # Bound strictly to this case's own body (up to ITS break;), not the
    # next case's -- a wider window let the CNTL-clear mutation escape
    # undetected in an earlier version of this tripwire (Codex review).
    case_end = source.index('\n        break;', idx)
    case_body = source[idx:case_end]
    assert 's->ring_stage_len = 0;' in case_body, \
        '%s no longer clears ring_stage_len on ring (re)initialization ' \
        '-- a stale staged packet from before the reinit could be ' \
        'prepended to the new ring\'s traffic' % reg
    if reg == 'R200_CP_RB_CNTL':
        assert '& 0x3F) != (s->regs.cp_rb_cntl & 0x3F)' in case_body, \
            'CP_RB_CNTL no longer narrows the clear to an actual ring-' \
            'size change -- a harmless writeback/fetch-policy write ' \
            'would now drop a legitimately in-progress staged packet'
    if reg == 'R200_RBBM_SOFT_RESET':
        assert 'val & (1 << 0)' in case_body, \
            'RBBM_SOFT_RESET no longer gates the clear on SOFT_RESET_CP ' \
            '-- update the slice (or this IS the regression)'

subsection_start = source.index(
    'static const VMStateDescription vmstate_ppc_mac_gpu_ring_stage')
subsection_end = source.index('VMSTATE_END_OF_LIST()', subsection_start)
subsection = source[subsection_start:subsection_end]
assert '.needed = ppc_mac_gpu_ring_stage_needed' in subsection, \
    'vmstate_ppc_mac_gpu_ring_stage lost its .needed gate -- every ' \
    'snapshot would carry this field unconditionally, same as a version ' \
    'bump that invalidates snapshots taken before this fix'
assert 'VMSTATE_UINT32(ring_stage_len, PPCMacGPUState)' in subsection and \
    'VMSTATE_BUFFER_UNSAFE(ring_stage_dw, PPCMacGPUState' in subsection, \
    'vmstate_ppc_mac_gpu_ring_stage no longer migrates the ring stage -- ' \
    'a snapshot taken mid-deferral would silently drop staged commands'

vmstate_start = source.index('static const VMStateDescription vmstate_ppc_mac_gpu = {')
vmstate_end = source.index('};', vmstate_start)
assert '&vmstate_ppc_mac_gpu_ring_stage' in source[vmstate_start:vmstate_end], \
    'vmstate_ppc_mac_gpu no longer lists the ring-stage subsection -- it ' \
    'would never be saved or loaded'

post_load_start = source.index('static int ppc_mac_gpu_post_load')
post_load_end = source.index('\n}\n', post_load_start)
post_load = source[post_load_start:post_load_end]
assert 'ring_stage_len > ARRAY_SIZE(s->ring_stage_dw)' in post_load, \
    'ppc_mac_gpu_post_load no longer bounds-checks a restored ' \
    'ring_stage_len -- a malformed snapshot could drive an OOB read/copy ' \
    'the next time the ring is processed'
assert 'ppc_mac_gpu_pm4_packet_dw(s->ring_stage_dw[0])' in post_load, \
    'ppc_mac_gpu_post_load no longer validates a restored stage is ' \
    'shorter than its own header\'s declared packet length'

# Tripwire (not a substitute for the functional check below) that the
# CP_ME_CNTL handler still resumes a backlog queued while ME_HALT was set.
me_cntl_start = source.index('case R200_CP_ME_CNTL: {')
me_cntl_end = source.index('\n    }\n', me_cntl_start)
me_cntl = source[me_cntl_start:me_cntl_end]
assert 'if (was_halted && !now_halted &&\n' in me_cntl, \
    'CP_ME_CNTL handler no longer unconditionally resumes on the ' \
    'halt->run transition -- update the slice (or this IS the qemu#1 P2 ' \
    'regression)'
# qemu#25: the drain is now a CP kick (a doorbell);
# cp_run is what calls process_ring_buffer for [RPTR, WPTR).
assert 'ppc_mac_gpu_cp_kick(s);' in me_cntl, \
    'CP_ME_CNTL resume no longer kicks the CP -- update the slice'
cp_run_start = source.index('static void ppc_mac_gpu_cp_run(PPCMacGPUState *s)')
cp_run = source[cp_run_start:source.index('\n}\n', cp_run_start)]
assert 'ppc_mac_gpu_process_ring_buffer(s, old_rptr, wptr)' in cp_run, \
    'cp_run no longer drains [RPTR, WPTR) -- update the slice'

stub = r'''
#include <assert.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define g_malloc malloc
#define g_free free
#define gpu_debug_log(...) ((void)0)

typedef struct PPCMacGPUCPReadTrace { int unused; } PPCMacGPUCPReadTrace;

typedef struct PPCMacGPUState {
    struct {
        uint32_t cp_rb_base, cp_rb_cntl, cp_rb_rptr, cp_rb_wptr;
        uint64_t stall_ring_dwords;
    } regs;
    uint32_t ring_stage_dw[16384];
    uint32_t ring_stage_len;
} PPCMacGPUState;

static int g_in_pm4;
static const char *cp_submission = "test";
static struct {
    uint64_t mmio_writes, mmio_reads, ring_dwords, type0_regs, draws;
    int64_t t0;
} r200_traffic;

static FILE *r300_ringdump(void) { return NULL; }

static void qemu_log(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
}

/* Fake guest ring memory: a flat dword array, cp_rb_base always 0 in these
 * tests, sized to the largest ring under test. process_ring_buffer never
 * asks for a run crossing the ring's own wrap point (it splits reads at
 * the wrap itself), so a single flat array is enough. */
static uint32_t *ring_mem;
static uint32_t ring_mem_len;

static bool ppc_mac_gpu_read_dwords(PPCMacGPUState *s, uint32_t gpu_addr,
                                     uint32_t *dst, uint32_t n,
                                     PPCMacGPUCPReadTrace *t)
{
    (void)s; (void)t;
    assert(gpu_addr % 4 == 0);
    uint32_t idx = gpu_addr / 4;
    assert(idx + n <= ring_mem_len);
    memcpy(dst, ring_mem + idx, n * 4);
    return true;
}

static void ppc_mac_gpu_cp_read_good(PPCMacGPUState *s, unsigned kind,
                                      PPCMacGPUCPReadTrace *t)
{ (void)s; (void)kind; (void)t; }

static void ppc_mac_gpu_dump_ib_lost_diag(PPCMacGPUState *s,
                                           PPCMacGPUCPReadTrace *t,
                                           bool ring)
{ (void)s; (void)t; (void)ring; }
'''

dispatch = r'''
/* Records how many packets ran and the last header seen -- the bulk NOP
 * check runs tens of thousands of packets, so a bounded log is enough. */
static uint32_t executed_n;
static uint32_t executed_last_hdr;

static uint32_t ppc_mac_gpu_pm4_dispatch(PPCMacGPUState *s,
                                          uint32_t *pm4_data,
                                          uint32_t size_dw,
                                          uint32_t hdr, uint32_t type,
                                          uint32_t i)
{
    (void)s; (void)pm4_data; (void)size_dw; (void)type;
    executed_n++;
    executed_last_hdr = hdr;
    return i + ppc_mac_gpu_pm4_packet_dw(hdr) - 1;
}
'''

checks = r'''
static void set_ring(uint32_t ring_size_dw)
{
    free(ring_mem);
    ring_mem_len = ring_size_dw;
    ring_mem = calloc(ring_size_dw, 4);
    for (uint32_t i = 0; i < ring_size_dw; i++) {
        ring_mem[i] = 0x80000000u; /* type-2 NOP padding */
    }
}

static uint32_t bufsz_for(uint32_t ring_size_dw)
{
    uint32_t rb_bufsz = 0;
    while ((2U << rb_bufsz) != ring_size_dw) {
        rb_bufsz++;
        assert(rb_bufsz <= 25);
    }
    return rb_bufsz;
}

/*
 * P1a: a single WPTR update publishing more than RING_BATCH_MAX_DW dwords
 * used to be rejected outright with RPTR untouched -- a permanent stall,
 * since the next identical WPTR write hits the same rejection. 65,537
 * complete NOPs (one more than the old cap) must all be consumed in one
 * call, not stranded.
 */
static void check_oversized_batch_drains(void)
{
    uint32_t ring_size_dw = 262144; /* well past RING_BATCH_MAX_DW */
    set_ring(ring_size_dw);
    PPCMacGPUState s = {0};
    s.regs.cp_rb_cntl = bufsz_for(ring_size_dw);
    executed_n = 0;

    uint32_t count = 65537;
    ppc_mac_gpu_process_ring_buffer(&s, 0, count);

    assert(s.regs.cp_rb_rptr == count);
    assert(s.regs.stall_ring_dwords == count);
    assert(executed_n == count); /* every NOP dispatched, none dropped */
    puts("oversized batch (65537 NOPs) fully drains, RPTR not stranded: PASS");
}

/*
 * P1b: a packet whose declared size exceeds the ring's own usable capacity
 * (ring_size_dw - 1) can never complete by waiting -- reproduces the
 * concrete deadlock shape from the Codex review: a near-full ring (RPTR
 * only 1 slot ahead of WPTR) carrying a type-3 packet that needs 2 more
 * dwords than are currently published. The fix must free that ring space
 * (advance RPTR, stage the packet in the device) rather than holding RPTR
 * on data the guest structurally cannot complete publishing.
 */
static void check_oversized_packet_stages_and_completes(void)
{
    uint32_t ring_size_dw = 16384;
    uint32_t ring_mask = ring_size_dw - 1;
    set_ring(ring_size_dw);
    PPCMacGPUState s = {0};
    s.regs.cp_rb_cntl = bufsz_for(ring_size_dw);
    executed_n = 0;

    uint32_t old_rptr = 16380, new_wptr = 16379; /* count == 16383 */
    uint32_t count = (new_wptr - old_rptr) & ring_mask;
    assert(count == ring_size_dw - 1);

    /* Type-3 packet declaring 16385 total dwords (header + 16384 body) --
     * one more than this ring can ever hold at once. */
    uint32_t hdr = 0xC0000000u | (0x3FFFu << 16);
    assert(((hdr >> 16) & 0x3FFF) + 2 == 16385);
    ring_mem[old_rptr] = hdr;
    for (uint32_t k = 1; k < count; k++) {
        ring_mem[(old_rptr + k) & ring_mask] = 0x11111111u; /* body filler */
    }

    ppc_mac_gpu_process_ring_buffer(&s, old_rptr, new_wptr);

    /* Deadlock detected: ring space freed (RPTR advanced past all of it),
     * nothing dispatched yet, packet staged rather than lost. */
    assert(s.regs.cp_rb_rptr == ((old_rptr + count) & ring_mask));
    assert(s.regs.stall_ring_dwords == count);
    assert(executed_n == 0);
    puts("oversized packet stages instead of deadlocking: PASS (round 1)");

    /* Guest publishes the final 2 dwords the packet needed -- in two
     * SEPARATE one-dword WPTR updates, not one two-dword update. Each of
     * these, taken alone, publishes far less than the ring's usable
     * capacity, so a version of `keep_staging` that re-derives "deadlock"
     * from this call's own `count` (rather than from staged_len > 0
     * entering the call) drops the stage after the first of the two and
     * misparses body data as a header on the second -- the exact bug
     * Codex's review caught in the first version of this fix. */
    old_rptr = s.regs.cp_rb_rptr;
    new_wptr = (old_rptr + 1) & ring_mask;
    ring_mem[old_rptr] = 0x22222222u;
    ppc_mac_gpu_process_ring_buffer(&s, old_rptr, new_wptr);
    assert(s.regs.cp_rb_rptr == new_wptr);
    assert(executed_n == 0); /* still incomplete: one dword short */
    puts("oversized packet stays staged across a too-small update: PASS (round 2)");

    old_rptr = s.regs.cp_rb_rptr;
    new_wptr = (old_rptr + 1) & ring_mask;
    ring_mem[old_rptr] = 0x33333333u;
    ppc_mac_gpu_process_ring_buffer(&s, old_rptr, new_wptr);

    assert(s.regs.cp_rb_rptr == new_wptr);
    assert(s.regs.stall_ring_dwords == count + 2);
    assert(executed_n == 1); /* the staged packet, reassembled, dispatched once */
    assert(executed_last_hdr == hdr);
    puts("oversized packet completes across two separate small updates: PASS (round 3)");
}

/*
 * P1a (chunk-boundary continuation): a packet split by RING_BATCH_MAX_DW's
 * artificial chunk cut -- not a real end-of-submission -- must complete
 * within the SAME call once more of `total` remains beyond the chunk that
 * first deferred it. Reproduces Codex's review finding directly: a fixed
 * `if (consumed < chunk) break;` stops here even though 65,535 NOPs before
 * the split packet, and its own remaining dwords after, were all published
 * in this one WPTR update -- the guest never gets to see RPTR reach the
 * final packet's end, and has no reason to write WPTR again since it
 * already told the device about all of this data once.
 */
static void check_chunk_boundary_defer_continues_same_call(void)
{
    uint32_t ring_size_dw = 262144;
    set_ring(ring_size_dw);
    PPCMacGPUState s = {0};
    s.regs.cp_rb_cntl = bufsz_for(ring_size_dw);
    executed_n = 0;

    /* 65,534 NOPs exactly fill the first RING_BATCH_MAX_DW (65,536) chunk
     * to within 2 dwords: a type-0 header (2 body dwords -> 4 total) lands
     * at index 65,534, so only its header + 1 of 2 body dwords are visible
     * in that first chunk. Its remaining word, plus a trailing marker
     * packet, follow immediately after -- already published, just beyond
     * the chunk cut. */
    uint32_t split_at = 65534;
    for (uint32_t i = 0; i < split_at; i++) {
        ring_mem[i] = 0x80000000u;
    }
    ring_mem[split_at] = 0x00010578u; /* type 0, count field 2 -> 4 total */
    ring_mem[split_at + 1] = 0xaaaaaaaau;
    ring_mem[split_at + 2] = 0xbbbbbbbbu;
    ring_mem[split_at + 3] = 0x00000600u; /* trailing marker, complete */
    ring_mem[split_at + 4] = 0x600d0000u;
    uint32_t count = split_at + 5;

    ppc_mac_gpu_process_ring_buffer(&s, 0, count);

    assert(s.regs.cp_rb_rptr == count); /* everything published, in one call */
    assert(s.regs.stall_ring_dwords == count);
    assert(executed_n == split_at + 2); /* 65,534 NOPs + split packet + marker */
    assert(executed_last_hdr == 0x00000600u);
    puts("packet split by a chunk boundary completes in the same call: PASS");
}

/*
 * P2 (functional half -- see the module docstring for the source-pattern
 * tripwire covering the CP_ME_CNTL case itself): the exact call the resume
 * path makes -- process_ring_buffer(s, rptr_at_halt, final_wptr_written)
 * -- must drain everything queued across however many WPTR writes happened
 * while halted, exactly as an ordinary (non-halted) WPTR write would.
 */
static void check_halt_then_resume_drains_backlog(void)
{
    uint32_t ring_size_dw = 4096;
    set_ring(ring_size_dw);
    PPCMacGPUState s = {0};
    s.regs.cp_rb_cntl = bufsz_for(ring_size_dw);
    executed_n = 0;

    /* Three "queued while halted" register writes, as if three separate
     * WPTR bumps had each been skipped by the halted WPTR handler. */
    ring_mem[0] = 0x00000600u; ring_mem[1] = 0x11111111u;
    ring_mem[2] = 0x00000601u; ring_mem[3] = 0x22222222u;
    ring_mem[4] = 0x00000602u; ring_mem[5] = 0x33333333u;

    uint32_t rptr_at_halt = 0;
    uint32_t final_wptr_while_halted = 6;
    ppc_mac_gpu_process_ring_buffer(&s, rptr_at_halt, final_wptr_while_halted);

    assert(s.regs.cp_rb_rptr == final_wptr_while_halted);
    assert(executed_n == 3);
    assert(executed_last_hdr == 0x00000602u);
    puts("backlog queued while halted drains on resume: PASS");
}

int main(void)
{
    check_oversized_batch_drains();
    check_oversized_packet_stages_and_completes();
    check_chunk_boundary_defer_continues_same_call();
    check_halt_then_resume_drains_backlog();
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='pm4-ring-flow-',
                                  dir=Path(__file__).parent) as tmp:
    c = Path(tmp) / 'test.c'
    binary = Path(tmp) / 'test'
    c.write_text(stub + walker + ring + dispatch + checks)
    subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
        '-std=gnu11', '-O1', '-Wall', '-Wextra', '-Werror',
        '-Wno-unused-parameter',
        str(c), '-o', str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True)

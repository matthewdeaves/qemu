#!/usr/bin/env python3
"""Exercise IB bounds and completion accounting with the captured R300 tail."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / "hw/display/ppc_mac_gpu.c").read_text()
start = source.index("static uint32_t ppc_mac_gpu_pm4_packet_dw(uint32_t hdr)")
walker = source[start:source.index("/*\n * Dispatches one already-validated", start)]
start = source.index("static struct {\n    bool valid, deferred;")
ib = source[start:source.index("/*\n * Process commands from the CP ring buffer.", start)]

stub = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef struct {
    struct { uint64_t stall_ib_done, stall_ib_lost, stall_ib_dwords; } regs;
    uint64_t vram_size;
    int vram;
} PPCMacGPUState;
typedef struct { int unused; } PPCMacGPUCPReadTrace;
static int g_in_pm4;
static const char *cp_submission = "test";
static FILE *r300_ringdump(void) { return NULL; }
static void qemu_log(const char *fmt, ...) { (void)fmt; }
#define gpu_debug_log(...) ((void)0)
#define g_malloc malloc
#define g_free free
static uint32_t memory[752], small[8], expected_size;
static uint32_t reg[16], value[16], writes;
static uint32_t *ppc_mac_gpu_read_ib_via_gart(
    PPCMacGPUState *s, uint32_t base, uint32_t n, PPCMacGPUCPReadTrace *t)
{
    (void)s; (void)t;
    assert(n == expected_size); /* No implicit fetch-length expansion. */
    uint32_t *src = base == 0x10034b20 ? memory :
                    base == 0x10034d80 ? memory + 152 :
                    base == 0x10000020 ? small : NULL;
    assert(src);
    uint32_t *copy = malloc(n * 4);
    memcpy(copy, src, n * 4);
    return copy;
}
static void ppc_mac_gpu_cp_read_good(PPCMacGPUState *s, int i,
                                   PPCMacGPUCPReadTrace *t)
{ (void)s; (void)i; (void)t; }
static void ppc_mac_gpu_dump_ib_lost_diag(PPCMacGPUState *s,
                                        PPCMacGPUCPReadTrace *t, bool ring)
{ (void)s; (void)t; (void)ring; }
static void *memory_region_get_ram_ptr(int *r) { (void)r; abort(); }
static uint32_t ldl_be_p(void *p) { (void)p; abort(); }
static bool ppc_mac_gpu_read_dwords(PPCMacGPUState *s, uint32_t addr,
    uint32_t *dst, uint32_t n, PPCMacGPUCPReadTrace *t)
{
    (void)s; (void)addr; (void)dst; (void)n; (void)t;
    return false; /* Optional diagnostic read, never execution input. */
}
'''
dispatch = r'''
static uint32_t ppc_mac_gpu_pm4_dispatch(PPCMacGPUState *s,
    uint32_t *data, uint32_t size, uint32_t hdr, uint32_t type, uint32_t i)
{
    (void)s;
    uint32_t n = ppc_mac_gpu_pm4_packet_dw(hdr) - 1;
    assert(n <= size - i);
    if (type == 0) {
        for (uint32_t k = 0; k < n; k++) {
            assert(writes < 16);
            reg[writes] = ((hdr & 0x7fff) + ((hdr & 0x8000) ? 0 : k)) * 4;
            value[writes++] = data[i + k];
        }
    }
    return i + n;
}
'''
checks = r'''
int main(void)
{
    PPCMacGPUState s = {0};
    const uint32_t tail[8] = {
        0x00000000, 0x000005c8, 0x00020000, 0x80000000,
        0x80000000, 0x80000000, 0x80000000, 0x80000000
    };
    for (unsigned i = 0; i < 752; i++) memory[i] = 0x80000000;
    /* Synthetic complete 151-dword prefix; captured header at dword 151. */
    memory[0] = 0xc0951000; /* PACKET3_NOP, 150 body dwords. */
    memory[151] = 0x00001393;
    memcpy(memory + 152, tail, sizeof(tail));
    expected_size = 152;
    ppc_mac_gpu_execute_ib(&s, 0x10034b20, expected_size);
    assert(writes == 0);
    assert(s.regs.stall_ib_done == 0 && s.regs.stall_ib_lost == 1);
    assert(s.regs.stall_ib_dwords == 151);

    /* Synthetic independent ring packet and intervening eight-dword IB. */
    uint32_t ring[] = {0x00000600, 0xfeedbeef};
    assert(ppc_mac_gpu_process_pm4(&s, ring, 2) == 2);
    for (unsigned i = 0; i < 8; i++) small[i] = 0x80000000;
    small[0] = 0x00000601; small[1] = 0xabcdef01;
    expected_size = 8;
    ppc_mac_gpu_execute_ib(&s, 0x10000020, expected_size);
    assert(writes == 2 && reg[0] == 0x1800 && reg[1] == 0x1804);
    assert(value[0] == 0xfeedbeef && value[1] == 0xabcdef01);

    /* A fresh IB does not inherit the old header. Record the bad framing. */
    expected_size = 600;
    ppc_mac_gpu_execute_ib(&s, 0x10034d80, expected_size);
    assert(writes == 6 && reg[2] == 0 && value[2] == 0x000005c8);
    for (unsigned i = 3; i < 6; i++) {
        assert(reg[i] == (i - 3) * 4 && value[i] == 0x80000000);
    }
    assert(s.regs.stall_ib_done == 2 && s.regs.stall_ib_lost == 1);
    assert(s.regs.stall_ib_dwords == 151 + 8 + 600);

    /* Control: explicitly submitting 160 dwords makes both writes valid. */
    s = (PPCMacGPUState){0}; writes = 0; expected_size = 160;
    ppc_mac_gpu_execute_ib(&s, 0x10034b20, expected_size);
    assert(writes == 2 && reg[0] == 0x4e4c && value[0] == 0);
    assert(reg[1] == 0x1720 && value[1] == 0x00020000);
    assert(s.regs.stall_ib_done == 1 && s.regs.stall_ib_lost == 0);
    assert(s.regs.stall_ib_dwords == 160);
    puts("IB boundary and accounting: PASS");
}
'''
with tempfile.TemporaryDirectory(prefix="pm4-ib-") as tmp:
    c, binary = Path(tmp) / "test.c", Path(tmp) / "test"
    c.write_text(stub + walker + dispatch + ib + checks)
    subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
        "-std=gnu11", "-O1", "-Wall", "-Wextra", "-Werror",
        str(c), "-o", str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True)

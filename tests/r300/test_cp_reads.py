#!/usr/bin/env python3
"""Exercise the actual CP readers with fake RAM and an independent PCI DMA gate.

No QEMU/Metal runtime required. Extract the source so this tests the production
translation/read logic, rather than a second implementation of that logic.
Run: python3 tests/r300/test_cp_reads.py
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'hw/display/ppc_mac_gpu.c').read_text()
start = source.index('typedef struct PPCMacGPUXlatTrace')
end = source.index('static uint32_t *ppc_mac_gpu_read_ib_via_gart', start)
code = source[start:end]

stub = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include <endian.h>
'''
# Darwin has no endian.h; keep byte conversion independent of host endianness.
stub = stub.replace('#include <endian.h>', r'''
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
#define le32_to_cpu(x) (x)
#define be32_to_cpu(x) __builtin_bswap32(x)
#else
#define le32_to_cpu(x) __builtin_bswap32(x)
#define be32_to_cpu(x) (x)
#endif
#define MIN(a,b) ((a) < (b) ? (a) : (b))
#define MEMTX_OK 0
#define MEMTX_ERROR 1
#define MEMTX_DECODE_ERROR 2
#define MEMTXATTRS_UNSPECIFIED 0
#define gpu_debug_log(...) ((void)0)
typedef uint64_t hwaddr;
typedef unsigned MemTxResult;
typedef int AddressSpace;
static AddressSpace address_space_memory, dma;
typedef struct PPCMacGPUState {
    struct {
        uint32_t aic_ctrl, aic_lo_addr, aic_hi_addr, aic_pt_base;
        uint32_t mc_agp_location;
    } regs;
    bool r300;
    uint32_t r300_aic_pt_base;
    int pci;
} PPCMacGPUState;
static AddressSpace *pci_get_address_space(int *pci) { return &dma; }
static hwaddr bridge;
static hwaddr uninorth_get_agp_gart_base(void) { return bridge; }
static unsigned pte_reads, data_reads;
static bool dma_disabled;
static struct { hwaddr addr; uint32_t val; MemTxResult tx; } ptes[4];
static MemTxResult address_space_read(AddressSpace *as, hwaddr addr,
                                      int attrs, void *dst, unsigned len)
{
    if (as == &address_space_memory) {
        pte_reads++;
        assert(len == 4);
        for (unsigned i = 0; i < 4; i++) {
            if (ptes[i].addr == addr) {
                uint32_t wire = le32_to_cpu(ptes[i].val);
                memcpy(dst, &wire, 4);
                return ptes[i].tx;
            }
        }
        return MEMTX_DECODE_ERROR;
    }
    data_reads++;
    if (dma_disabled) {
        return MEMTX_DECODE_ERROR;
    }
    /* Encode the physical address as BE words, to verify translation and
     * payload endian conversion across noncontiguous physical pages. */
    for (unsigned i = 0; i < len / 4; i++) {
        uint32_t word = be32_to_cpu((uint32_t)addr + i * 4);
        memcpy((uint8_t *)dst + i * 4, &word, 4);
    }
    return MEMTX_OK;
}
''')

checks = r'''
static PPCMacGPUState setup(void)
{
    PPCMacGPUState s = { .regs = {
        .aic_ctrl = 1, .aic_lo_addr = 0x10000000,
        .aic_hi_addr = 0x1001ffff, .aic_pt_base = 0x2000,
        .mc_agp_location = 0x10011000,
    }};
    bridge = 0x3000;
    pte_reads = data_reads = 0;
    dma_disabled = false;
    memset(ptes, 0, sizeof(ptes));
    ptes[0].addr = 0x2000; ptes[0].val = 0x8001;
    ptes[1].addr = 0x2004; ptes[1].val = 0xc001;
    ptes[2].addr = 0x3000; ptes[2].val = 0xa001;
    return s;
}
int main(void)
{
    PPCMacGPUCPReadTrace t;
    PPCMacGPUXlatTrace x;
    uint32_t dst[16];
    hwaddr phys;
    PPCMacGPUState s = setup();
    assert(ppc_mac_gpu_read_dwords(&s, 0x10000ff0, dst, 8, &t));
    assert(dst[0] == 0x8ff0 && dst[4] == 0xc000);
    assert(t.addr == 0x10001000 && t.run == 4);
    assert(!strcmp(t.path, "aic") && !strcmp(t.agp.reason, "not-tried"));
    assert(pte_reads == 2 && data_reads == 2);

    s = setup();
    s.regs.aic_ctrl = 0;
    assert(ppc_mac_gpu_read_dwords(&s, 0x10000020, dst, 8, &t));
    assert(!strcmp(t.aic.reason, "disabled") && !strcmp(t.path, "agp"));
    assert(t.agp.table == bridge && t.agp.pte == 0xa001 && dst[0] == 0xa020);
    assert(pte_reads == 1 && data_reads == 1);
    bridge = 0;
    assert(ppc_mac_gpu_read_dwords(&s, 0x10000020, dst, 8, &t));
    assert(t.agp.table == 0x2000 && dst[0] == 0x8020);
    s.regs.aic_pt_base = 0;
    assert(!ppc_mac_gpu_read_dwords(&s, 0x10000020, dst, 8, &t));
    assert(!strcmp(t.agp.reason, "no-table") && !t.data_read);

    s = setup();
    s.regs.mc_agp_location = 0;
    ptes[1].val = 0;
    assert(!ppc_mac_gpu_read_dwords(&s, 0x10000ff0, dst, 8, &t));
    assert(t.base == 0x10000ff0 && t.addr == 0x10001000 && t.size_dw == 8);
    assert(!strcmp(t.aic.reason, "zero-page") && t.aic.pte_addr == 0x2004);
    assert(!t.data_read && t.phys == 0 && !strcmp(t.path, "none"));
    assert(pte_reads == 2 && data_reads == 1); /* No diagnostic reread. */

    s = setup();
    ptes[0].tx = MEMTX_ERROR;
    ptes[2].tx = MEMTX_DECODE_ERROR;
    assert(!ppc_mac_gpu_read_dwords(&s, 0x10000020, dst, 8, &t));
    assert(!strcmp(t.aic.reason, "pte-read") && t.aic.tx == MEMTX_ERROR);
    assert(!strcmp(t.agp.reason, "pte-read") && t.agp.tx == MEMTX_DECODE_ERROR);
    assert(pte_reads == 2 && data_reads == 0);

    s = setup();
    dma_disabled = true;
    assert(!ppc_mac_gpu_read_dwords(&s, 0x10000020, dst, 8, &t));
    assert(!strcmp(t.aic.reason, "ok") && t.data_read);
    assert(t.tx == MEMTX_DECODE_ERROR && t.phys == 0x8020);
    assert(pte_reads == 1 && data_reads == 1); /* Preserve no AGP retry. */

    s = setup();
    s.r300 = true; s.r300_aic_pt_base = 0x4000;
    ptes[0].addr = 0x4000 + 30 * 4;
    assert(ppc_mac_gpu_gart_translate_trace(&s, 0x1001e020, &phys, &x));
    assert(x.page_idx == 30 && x.pte_addr == 0x4078 && phys == 0x8020);
    assert(ppc_mac_gpu_gart_translate(&s, 0x1001e020, &phys));
    assert(ppc_mac_gpu_agp_translate(&s, 0x10000020, &phys));

    /* Different aperture origins select different PTE indices. */
    s = setup();
    s.regs.aic_lo_addr = 0x10001000;
    ptes[2].addr = bridge + 4;
    assert(ppc_mac_gpu_gart_translate_trace(&s, 0x10001020, &phys, &x));
    assert(x.page_idx == 0 && phys == 0x8020);
    assert(ppc_mac_gpu_agp_translate_trace(&s, 0x10001020, &phys, &x));
    assert(x.page_idx == 1 && phys == 0xa020);

    /* Even the largest 32-bit GPU offset uses a 64-bit PTE address. */
    s = setup();
    s.regs.aic_lo_addr = 0; s.regs.aic_hi_addr = UINT32_MAX;
    s.regs.aic_pt_base = 0xfffff000;
    ptes[0].addr = UINT64_C(0xfffff000) + 0xfffff * 4;
    assert(ppc_mac_gpu_gart_translate_trace(&s, 0xfffff020, &phys, &x));
    assert(x.page_idx == 0xfffff && x.pte_addr > UINT32_MAX);
    puts("CP read diagnostics: PASS (fallback, page boundaries, PTE/data failures, no extra reads)");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='cp-reads-', dir=Path(__file__).parent) as tmp:
    c = Path(tmp) / 'test.c'
    binary = Path(tmp) / 'test'
    c.write_text(stub + code + checks)
    subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
        '-std=gnu11', '-O1', '-Wall', '-Wextra', '-Werror',
        '-Wno-unused-parameter', str(c), '-o', str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True)

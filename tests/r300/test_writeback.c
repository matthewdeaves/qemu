/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Panther's native-endian ring-pointer polling must survive a full ring. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include "../../hw/display/radeon_writeback.h"

static uint32_t ppc_load_le_wire(uint32_t word)
{
    /* A little-endian GPU store followed by a big-endian CPU load. */
    return ((word & 0xffu) << 24) | ((word & 0xff00u) << 8) |
           ((word & 0xff0000u) >> 8) | ((word >> 24) & 0xffu);
}

int main(void)
{
    assert(radeon_rptr_writeback_word(0x12345678, 0) == 0x12345678);
    assert(radeon_rptr_writeback_word(0x12345678, 1) == 0x34127856);
    assert(radeon_rptr_writeback_word(0x12345678, 2) == 0x78563412);
    assert(radeon_rptr_writeback_word(0x12345678, 3) == 0x56781234);

    /*
     * State captured from Panther 10.3.0 after its first ring traversal:
     * RPTR_ADDR=0x08000002, consumed RPTR=WPTR=0x7f8. The previous always-LE
     * writeback becomes 0xf8070000 to the driver and reports no free space.
     */
    uint32_t wrong = ppc_load_le_wire(0x7f8);
    uint32_t correct = ppc_load_le_wire(radeon_rptr_writeback_word(0x7f8, 2));
    assert(wrong == 0xf8070000);
    assert(correct == 0x7f8);
    assert((((wrong - 0x7f8 - 1) & 0x7ff) - 7) == 0);
    assert((((correct - 0x7f8 - 1) & 0x7ff) - 7) == 2040);
    puts("Radeon ring writeback byte-order tests passed");
    return 0;
}

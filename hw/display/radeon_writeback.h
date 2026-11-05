/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef RADEON_WRITEBACK_H
#define RADEON_WRITEBACK_H

#include <stdint.h>

/* Apply RB_RPTR_SWAP before storing the result in little-endian order. */
static inline uint32_t radeon_rptr_writeback_word(uint32_t value, unsigned swap)
{
    switch (swap & 3) {
    case 1: /* exchange bytes within each 16-bit word */
        return ((value & 0x00ff00ffu) << 8) |
               ((value & 0xff00ff00u) >> 8);
    case 2: /* exchange bytes within the 32-bit word */
        return (value >> 24) | ((value >> 8) & 0x0000ff00u) |
               ((value << 8) & 0x00ff0000u) | (value << 24);
    case 3: /* exchange the two 16-bit words */
        return (value >> 16) | (value << 16);
    default:
        return value;
    }
}

#endif

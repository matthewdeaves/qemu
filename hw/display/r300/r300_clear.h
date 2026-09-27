/* SPDX-License-Identifier: GPL-2.0-or-later */
/* R300 clear scissor coordinates are inclusive and biased by 1440. */
#ifndef R300_CLEAR_H
#define R300_CLEAR_H
#include <stdint.h>

static inline uint64_t r300_clear_bytes(uint32_t scissor_br, uint64_t pitch,
                                        uint32_t samples, uint64_t offset,
                                        uint64_t vram_size)
{
    uint32_t bottom = (scissor_br >> 13) & 0x1fff;
    if (bottom < 1440 || !pitch || !samples || offset >= vram_size) {
        return 0;
    }
    uint64_t rows = (uint64_t)(bottom - 1440 + 1) * samples;
    uint64_t available = (vram_size - offset) / pitch;
    if (rows > available) rows = available;
    return rows * pitch;
}
#endif

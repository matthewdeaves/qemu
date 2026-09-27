/* SPDX-License-Identifier: GPL-2.0-or-later */
/* System-memory uploads into the CPU-aperture representation of R300 VRAM. */
#ifndef R300_UPLOAD_H
#define R300_UPLOAD_H
#include <stdint.h>

static inline uint32_t r300_upload_word(uint32_t v, unsigned bpp, unsigned swap)
{
    if (bpp == 4) {
        /* Source swap followed by the aperture's dword swap. */
        switch (swap & 3) {
        case 0:
            return (v >> 24) | ((v >> 8) & 0xff00) |
                   ((v << 8) & 0xff0000) | (v << 24);
        case 1:
            return (v >> 16) | (v << 16);
        case 2:
            return v;
        case 3:
            return ((v & 0xff00ff) << 8) | ((v >> 8) & 0xff00ff);
        }
    }
    if (bpp == 2 && swap == 0) {
        return ((v & 0xff00ff) << 8) | ((v >> 8) & 0xff00ff);
    }
    return v;
}
#endif

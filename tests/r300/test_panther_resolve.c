/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Panther Chess resolves MSAA with an XY-only immediate point. */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "../../hw/display/r300/r300_draw.h"

static bool no_read(void *opaque, uint32_t addr, void *data, uint32_t len)
{
    return false;
}

int main(void)
{
    static R300State st;
    const uint32_t regs[][2] = {
        { 0x4e38, 0x00d10300 }, { 0x46a4, 0x1b01 },
        { 0x43e0, 0x00b405a0 }, { 0x43e4, 0x03ffffff },
        { 0x2140, 0x102 }, { 0x2090, 3 }, { 0x20b4, 2 },
        { 0x2150, 0x2001 }, { 0x21e0, 0x3008 },
        { 0x421c, 0x11580ff0 }, { 0x4e88, 3 },
    };
    float xy[2] = { 370, 340 };
    uint32_t data[3] = { 0x10031 };
    R300Arrays arrays = { 0 };
    R300DrawPacket pkt;
    const char *err;
    float lo[2] = { 1e9, 1e9 }, hi[2] = { -1e9, -1e9 };

    r300_state_reset(&st);
    for (unsigned i = 0; i < sizeof(regs) / sizeof(regs[0]); i++) {
        r300_state_write(&st, regs[i][0], regs[i][1]);
    }
    memcpy(data + 1, xy, sizeof(xy));
    assert(r300_draw_build(&st, &arrays, 0x35, data, 3, no_read, NULL,
                           &pkt, &err));
    for (unsigned i = 0; i < pkt.num_verts; i++) {
        float *v = pkt.verts[i].pos;
        float x = (v[0] / v[3] + 1) * pkt.rt_width / 2;
        float y = (1 - v[1] / v[3]) * pkt.rt_height / 2;

        lo[0] = fminf(lo[0], x);
        lo[1] = fminf(lo[1], y);
        hi[0] = fmaxf(hi[0], x);
        hi[1] = fmaxf(hi[1], y);
    }
    printf("Panther resolve bounds: %g,%g - %g,%g\n",
           lo[0], lo[1], hi[0], hi[1]);
    assert(fabsf(lo[0]) < .001f && fabsf(lo[1]) < .001f);
    assert(fabsf(hi[0] - 740) < .001f && fabsf(hi[1] - 680) < .001f);
    r300_draw_free(&pkt);
    return 0;
}

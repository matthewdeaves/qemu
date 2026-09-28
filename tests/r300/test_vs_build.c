/* GPU packet selection, translation cache and CPU safety switch. No GPU needed. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../../hw/display/r300/r300_draw.h"
#include "../../hw/display/r300/r300_pvs.h"

static bool no_read(void *o, uint32_t a, void *d, uint32_t n) { return false; }
static R300State base;
static uint32_t packet[49] = { 0x0004003D };

static R300DrawPacket build(R300State *st, unsigned prim)
{
    R300Arrays arr = { 0 };
    R300DrawPacket p;
    const char *err = NULL;
    packet[0] = (packet[0] & ~15u) | prim;
    bool ok = r300_draw_build(st, &arr, 0x35, packet, 49, no_read, NULL, &p, &err);
    if (!ok) { fprintf(stderr, "build: %s\n", err); }
    assert(ok);
    return p;
}

static void fallback(const char *name, unsigned reg, uint32_t value, unsigned prim)
{
    R300State st = base;
    r300_state_write(&st, reg, value);
    R300DrawPacket p = build(&st, prim);
    assert(p.verts && !p.vs_msl && !p.vs_in && !p.vs_idx && !p.vs_u);
    r300_draw_free(&p);
    printf("CPU fallback: %s PASS\n", name);
}

int main(void)
{
    FILE *f = fopen("qe_draw1_full.txt", "r");
    assert(f);
    char line[256];
    int mode = 0;
    r300_state_reset(&base);
    while (fgets(line, sizeof(line), f)) {
        unsigned a, v, i, d[4];
        float c[4];
        if (strstr(line, "PVS code")) { mode = 1; continue; }
        if (strstr(line, "PVS constants")) { mode = 2; continue; }
        if (mode == 0 && sscanf(line, " %x %x", &a, &v) == 2) r300_state_write(&base, a, v);
        if (mode == 1 && sscanf(line, " %u: %x %x %x %x", &i, &d[0], &d[1], &d[2], &d[3]) == 5)
            memcpy(&base.pvs_mem[i * 4], d, 16);
        if (mode == 2 && sscanf(line, " c%u %f %f %f %f", &i, &c[0], &c[1], &c[2], &c[3]) == 5)
            memcpy(&base.pvs_mem[(512 + i) * 4], c, 16);
    }
    fclose(f);
    float verts[4][12] = {
        { 10, 5, 0, 1, 1, .5, .25, 1, 0, 0, 0, 1 },
        { 240, 20, 0, 1, 0, 1, 1, .5, 256, 0, 0, 1 },
        { 230, 250, 0, 1, 1, 1, 0, 1, 256, 256, 0, 1 },
        { 15, 200, 0, 1, .2, .4, .6, .8, 0, 256, 0, 1 },
    };
    memcpy(packet + 1, verts, sizeof(verts));
    R300DrawPacket cpu = build(&base, 13);
    assert(cpu.verts && !cpu.vs_msl); /* non-Metal capability defaults off */
    base.gpu_vs = true;
    R300DrawPacket gpu = build(&base, 13);
    if (getenv("R300_CPU_VS")) {
        assert(gpu.verts && !gpu.vs_msl && gpu.num_verts == cpu.num_verts);
        for (unsigned i = 0; i < cpu.num_verts; i++) {
            assert(!memcmp(cpu.verts[i].pos, gpu.verts[i].pos, sizeof(cpu.verts[i].pos)));
            assert(!memcmp(cpu.verts[i].v, gpu.verts[i].v, sizeof(cpu.verts[i].v)));
            assert(cpu.verts[i].aux[0] == gpu.verts[i].aux[0]);
        }
        puts("R300_CPU_VS=1: Metal-capable draw uses interpreter; all vertex positions, varyings and fog match PASS");
    } else {
        assert(gpu.vs_msl && !gpu.verts && gpu.vs_idx && gpu.vs_u);
        assert(gpu.num_verts == 6 && gpu.vs_in_vecs == 12);
        R300DrawPacket again = build(&base, 13);
        assert(again.vs_msl == gpu.vs_msl && again.vs_id == gpu.vs_id);
        assert(again.msl == cpu.msl && again.msl_id == cpu.msl_id);
        r300_draw_free(&again);
        /* Runtime constants/viewport change uniforms, not generated text. */
        R300State changed = base;
        changed.pvs_mem[512 * 4] ^= 0x10000;
        r300_state_write(&changed, 0x1D98, 0x43800000);
        again = build(&changed, 13);
        assert(again.vs_id == gpu.vs_id && again.vs_msl == gpu.vs_msl);
        assert(memcmp(again.vs_u, gpu.vs_u, sizeof(*gpu.vs_u)));
        r300_draw_free(&again);
        /* Program and routing state must distinguish cache entries. */
        changed = base;
        changed.pvs_mem[1] ^= 1u << 25;
        again = build(&changed, 13);
        assert(again.vs_id != gpu.vs_id);
        r300_draw_free(&again);
        changed = base;
        r300_state_write(&changed, 0x4310, r300_reg(&changed, 0x4310) ^ (1u << 13));
        again = build(&changed, 13);
        assert(again.vs_id != gpu.vs_id);
        r300_draw_free(&again);
        puts("GPU packet: 4 inputs / 6 indices; program+state cache and uniform-only reuse PASS");
        fallback("points", 0x4288, 0, 1);
        fallback("lines", 0x4288, 0, 2);
        fallback("rectangle lists", 0x4288, 0, 8);
        fallback("polygon mode", 0x4288, 1, 13);
        fallback("flat colours", 0x4278, 0, 13);
        fallback("two-sided colours", 0x2090, r300_reg(&base, 0x2090) | 8, 13);
        fallback("flow control", 0x22DC, 1, 13);
        fallback("AA resolve", 0x4E88, 1, 13);
    }
    r300_draw_free(&cpu);
    r300_draw_free(&gpu);
    /* A dual MAD has no third vector operand, even after stack reuse. */
    uint32_t code[4] = { 4 | (2u << 8) | (15u << 20) | (1u << 28),
                        1 | (1u << 16) | (2u << 19) | (3u << 22),
                        1 | (1u << 16) | (2u << 19) | (3u << 22), 0 };
    float in[32][4] = { { 2, 3, 4, 5 } }, out[32][4] = { 0 }, c[256][4] = { 0 };
    R300PVSProgram prog = { .code = code, .consts = c };
    assert(r300_pvs_run(&prog, in, out) == 0);
    for (unsigned k = 0; k < 4; k++) { assert(out[0][k] == in[0][k] * in[0][k]); }
    puts("dual-issue vector third operand is zero PASS");
    return 0;
}

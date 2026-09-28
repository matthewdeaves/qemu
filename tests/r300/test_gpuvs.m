/*
 * Vertex shading on the GPU (R300State.gpu_vs) against the CPU path:
 *
 * 1. Random PVS programs (every vector and math op, swizzles, abs/negate,
 *    saturation, dual and macro instructions, A0-relative constants) run
 *    through r300_pvs_run and through r300_pvs_to_msl on Metal.
 * Upstream reports one denormal flushed by Metal at 5000 iterations.
 * The default 400-program seed is unchanged; pass 5000 to investigate.
 * 2. The captured Quartz Extreme draw built both ways: the interpolated
 *    varyings and the covered pixels must match.
 */
#import <Metal/Metal.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../../hw/display/r300/r300_draw.h"
#include "../../hw/display/r300/r300_pvs.h"
#include "../../hw/display/r300/r300_sb.h"
#include "../../hw/display/r300/r300_metal_cache.h"
#include "../../hw/display/r300/r300_us.h"

static id<MTLDevice> dev;
static id<MTLCommandQueue> q;
static int fails;
static const char *emit_dir;

#define CHECK(c, ...) do { if (!(c)) { printf("FAIL %s:%d: ", __FILE__, __LINE__); \
    printf(__VA_ARGS__); printf("\n"); fails++; } } while (0)

static id<MTLFunction> fn(const char *msl, const char *entry)
{
    NSError *error = nil;
    MTLCompileOptions *opt = !strcmp(entry, "r300_vs") ?
                            r300_metal_vertex_options() : nil;
    id<MTLLibrary> lib = [dev newLibraryWithSource:@(msl) options:opt error:&error];
    if (!lib) {
        printf("compile: %s\n%s\n", error.localizedDescription.UTF8String, msl);
        fails++;
        return nil;
    }
    return [lib newFunctionWithName:@(entry)];
}

/* Optional offline compiler input; this does not claim GPU execution. */
static void emit_source(const char *name, const char *msl)
{
    char path[1024];
    snprintf(path, sizeof(path), "%s/%s.metal", emit_dir, name);
    FILE *f = fopen(path, "w");
    CHECK(f != NULL, "cannot write %s", path);
    if (f) { fputs(msl, f); fclose(f); }
}

static bool close_enough(float a, float b)
{
    if (isnan(a) || isnan(b)) {
        return isnan(a) && isnan(b);
    }
    if (isinf(a) || isinf(b)) {
        return a == b;
    }
    return fabsf(a - b) <= 1e-3f + 1e-3f * fmaxf(fabsf(a), fabsf(b));
}

/* ---- 1. random programs ---- */

#define NV 64           /* vertices (distinct inputs) per program */
#define NOUT 8

static uint32_t rnd_state = 12345;
static uint32_t rnd(uint32_t n)
{
    rnd_state = rnd_state * 1103515245u + 12345u;
    return (rnd_state >> 8) % n;
}

static uint32_t rsrc(bool rel_ok)
{
    static const unsigned lim[4] = { 8, 4, 16, 4 };
    uint32_t type = rnd(4), s = type | (rnd(lim[type]) << 5);
    for (int i = 0; i < 4; i++) {
        s |= rnd(8) << (13 + 3 * i);
        s |= rnd(4) == 0 ? 1u << (25 + i) : 0;
    }
    s |= rnd(5) == 0 ? 1u << 3 : 0;
    if (type == 2 && rel_ok && rnd(4) == 0) {
        s |= 1u << 4 | rnd(4) << 29;            /* c[idx + a0.sel] */
    }
    return s;
}

static void random_program(uint32_t *code, unsigned *ninst)
{
    static const unsigned vops[] = { 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
                                     23, 24, 25, 26, 27, 28 };
    static const unsigned mops[] = { 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
                                     16, 17, 18, 19, 20 };
    unsigned n = 0, body = 4 + rnd(12);
    bool a0 = false;

    for (unsigned k = 0; k < body; k++, n++) {
        uint32_t *d = &code[n * 4];
        uint32_t kind = rnd(10);
        uint32_t dt = rnd(10) < 7 ? 0 : rnd(3) == 0 ? 1 : 4;   /* temp, a0, alt */
        uint32_t idx = dt == 4 ? rnd(4) : rnd(8);
        uint32_t d0 = (dt << 8) | (idx << 13) | ((1 + rnd(15)) << 20);
        d0 |= rnd(4) == 0 ? 1u << 24 : 0;
        d0 |= rnd(4) == 0 ? 1u << 25 : 0;
        if (kind < 3) {
            d0 |= mops[rnd(sizeof(mops) / 4)] | 1u << 6;
        } else if (kind == 3) {
            d0 |= rnd(2) | 1u << 7;                              /* MAD / M2X_ADD */
        } else {
            d0 |= vops[rnd(sizeof(vops) / 4)];
        }
        bool dual = kind == 4;
        if (dual) {
            d0 |= 1u << 28;
        }
        d[0] = d0;
        d[1] = rsrc(a0);
        d[2] = rsrc(a0);
        if (dual) {
            uint32_t mop = mops[rnd(sizeof(mops) / 4)];
            uint32_t s = rsrc(a0) & ~(0xFu << 21) & ~(1u << 2) & ~(3u << 19) & ~(3u << 27);
            s |= (mop & 0xF) << 21 | ((mop >> 4) & 1) << 2 | rnd(4) << 19 | rnd(4) << 27;
            d[3] = s;
        } else {
            d[3] = rsrc(a0);
        }
        if (dt == 1) {
            a0 = true;
        }
    }
    /* o_k = t_k + 0 */
    for (unsigned k = 0; k < NOUT; k++, n++) {
        uint32_t *d = &code[n * 4];
        d[0] = 3 | (2u << 8) | (k << 13) | (15u << 20);
        d[1] = 0 | (k << 5) | (0u << 13) | (1u << 16) | (2u << 19) | (3u << 22);
        d[2] = 0 | (4u << 13) | (4u << 16) | (4u << 19) | (4u << 22);
        d[3] = d[2];
    }
    *ninst = n;
}

static void random_programs(unsigned count)
{
    static float consts[256][4], in[NV][R300_PVS_NUM_INPUTS][4];
    uint32_t code[64 * 4];
    unsigned mism = 0, skipped = 0;

    for (int i = 0; i < 16; i++) {
        for (int c = 0; c < 4; c++) {
            consts[i][c] = ((int)rnd(2001) - 1000) / 250.0f;
        }
    }
    consts[3][0] = 0.0f;                        /* exact zeros, too */
    for (int v = 0; v < NV; v++) {
        for (int i = 0; i < 4; i++) {
            for (int c = 0; c < 4; c++) {
                in[v][i][c] = ((int)rnd(2001) - 1000) / 250.0f;
            }
        }
    }
    id<MTLBuffer> vin = [dev newBufferWithLength:NV * 4 * 16 options:MTLResourceStorageModeShared];
    for (int v = 0; dev && v < NV; v++) {
        memcpy((float *)vin.contents + v * 16, in[v], 4 * 16);
    }
    R300VSUniforms *u = calloc(1, sizeof(*u));
    memcpy(u->c, consts, sizeof(u->c));
    id<MTLBuffer> ub = [dev newBufferWithBytes:u length:sizeof(*u) options:MTLResourceStorageModeShared];
    free(u);
    id<MTLBuffer> res = [dev newBufferWithLength:NV * NOUT * 16 options:MTLResourceStorageModeShared];

    MTLTextureDescriptor *td = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:1 height:1 mipmapped:NO];
    td.usage = MTLTextureUsageRenderTarget;
    id<MTLTexture> rt = [dev newTextureWithDescriptor:td];
    id<MTLFunction> fs = dev ? fn("#include <metal_stdlib>\nusing namespace metal;\n"
                            "fragment float4 r300_fs() { return float4(0.0f); }\n", "r300_fs") : nil;

    for (unsigned t = 0; t < count; t++) @autoreleasepool {
        unsigned ninst;
        random_program(code, &ninst);
        R300PVSProgram prog = { code, 0, ninst - 1, (const float (*)[4])consts, 15 };
        R300Sb body, sb;
        uint32_t used;
        r300_sb_init(&body);
        if (!r300_pvs_to_msl(&prog, &body, &used)) {
            skipped++;
            r300_sb_free(&body);
            continue;
        }
        r300_sb_init(&sb);
        r300_sb_printf(&sb, "#include <metal_stdlib>\nusing namespace metal;\n%s"
            "struct R300VSU { float4 c[256]; float4 ucp[6]; float4 vp0; float4 vp1; float4 fogp; uint4 info; };\n"
            "float4 pvs_c(constant float4 *c, int x) { return x >= 0 && x <= 15 ? c[x] : float4(0.0f); }\n"
            "vertex float4 r300_vs(uint vid [[vertex_id]], const device float4 *vin [[buffer(0)]],\n"
            "    device float4 *res [[buffer(1)]], constant R300VSU &vs [[buffer(2)]])\n{\n"
            "    uint vb = vid * 4u;\n"
            "    float4 i0 = vin[vb], i1 = vin[vb + 1u], i2 = vin[vb + 2u], i3 = vin[vb + 3u];\n"
            "%s", r300_pvs_msl_helpers, body.buf);
        for (int k = 0; k < NOUT; k++) {
            r300_sb_printf(&sb, "    res[vid * %d + %d] = o%d;\n", NOUT, k, k);
        }
        r300_sb_printf(&sb, "    return float4(4.0f, 4.0f, 0.0f, 1.0f);\n}\n");
        r300_sb_free(&body);
        if (emit_dir) {
            char name[32];
            snprintf(name, sizeof(name), "random-%u", t);
            emit_source(name, sb.buf);
            r300_sb_free(&sb);
            continue;
        }
        id<MTLFunction> vsf = fn(sb.buf, "r300_vs");
        if (!vsf) {
            r300_sb_free(&sb);
            return;
        }
        MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
        pd.vertexFunction = vsf;
        pd.fragmentFunction = fs;
        pd.colorAttachments[0].pixelFormat = MTLPixelFormatRGBA8Unorm;
        NSError *e = nil;
        id<MTLRenderPipelineState> p = [dev newRenderPipelineStateWithDescriptor:pd error:&e];
        if (!p) {
            printf("pipeline: %s\n", e.localizedDescription.UTF8String);
            fails++;
            r300_sb_free(&sb);
            return;
        }
        memset(res.contents, 0xFF, res.length);
        id<MTLCommandBuffer> cb = [q commandBuffer];
        MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
        rp.colorAttachments[0].texture = rt;
        id<MTLRenderCommandEncoder> enc = [cb renderCommandEncoderWithDescriptor:rp];
        [enc setRenderPipelineState:p];
        [enc setVertexBuffer:vin offset:0 atIndex:0];
        [enc setVertexBuffer:res offset:0 atIndex:1];
        [enc setVertexBuffer:ub offset:0 atIndex:2];
        [enc drawPrimitives:MTLPrimitiveTypePoint vertexStart:0 vertexCount:NV];
        [enc endEncoding];
        [cb commit];
        [cb waitUntilCompleted];
        CHECK(cb.status == MTLCommandBufferStatusCompleted, "GPU execution: %s", cb.error.description.UTF8String);

        const float (*gpu)[4] = res.contents;
        bool bad = false;
        for (int v = 0; v < NV && !bad; v++) {
            float out[R300_PVS_NUM_OUTPUTS][4];
            memset(out, 0, sizeof(out));
            r300_pvs_run(&prog, (const float (*)[4])in[v], out);
            for (int k = 0; k < NOUT && !bad; k++) {
                for (int c = 0; c < 4; c++) {
                    float a = out[k][c], b = gpu[v * NOUT + k][c];
                    if (!close_enough(a, b)) {
                        if (mism < 5) {
                            char dis[160];
                            printf("program %u vertex %d o%d.%c: cpu %g gpu %g\n",
                                   t, v, k, "xyzw"[c], a, b);
                            printf("  i1 %g %g %g %g  c2 %g %g %g %g\n", in[v][1][0], in[v][1][1],
                                   in[v][1][2], in[v][1][3], consts[2][0], consts[2][1],
                                   consts[2][2], consts[2][3]);
                            for (unsigned i = 0; i < ninst; i++) {
                                r300_pvs_disasm_inst(&code[i * 4], dis, sizeof(dis));
                                printf("  %2u %s\n", i, dis);
                            }
                        }
                        bad = true;
                    }
                }
            }
        }
        mism += bad;
        r300_sb_free(&sb);
    }
    printf(emit_dir ? "random PVS source emitted: %u programs, %u errors, %u skipped\n" :
           "random PVS programs: %u run, %u differ, %u left to the interpreter\n",
           count - skipped, mism, skipped);
    CHECK(mism == 0, "%u programs differ", mism);
    CHECK(skipped < count / 4, "too many skipped (%u)", skipped);
}

/* ---- 2. a whole draw, both ways ---- */

#define W 256
#define H 256

static const char capture_fs[] =
"#include <metal_stdlib>\nusing namespace metal;\n"
"struct In { float4 pos [[position]]; float4 v0 [[user(v0)]], v1 [[user(v1)]], aux [[user(aux)]]; };\n"
"struct Out { float4 o0 [[color(0)]], o1 [[color(1)]], o2 [[color(2)]]; };\n"
"fragment Out r300_fs(In in [[stage_in]]) { return { in.v0, in.v1, float4(in.aux.x, in.pos.z, 1.0f, 1.0f) }; }\n";

static void render(const R300DrawPacket *p, id<MTLFunction> vsf, id<MTLFunction> fsf,
                   __strong id<MTLTexture> out[3])
{
    MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
    pd.vertexFunction = vsf;
    pd.fragmentFunction = fsf;
    for (int k = 0; k < 3; k++) {
        pd.colorAttachments[k].pixelFormat = MTLPixelFormatRGBA32Float;
    }
    NSError *e = nil;
    id<MTLRenderPipelineState> ps = [dev newRenderPipelineStateWithDescriptor:pd error:&e];
    if (!ps) {
        printf("pipeline: %s\n", e.localizedDescription.UTF8String);
        fails++;
        return;
    }
    id<MTLCommandBuffer> cb = [q commandBuffer];
    MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
    for (int k = 0; k < 3; k++) {
        MTLTextureDescriptor *td = [MTLTextureDescriptor
            texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA32Float width:W height:H mipmapped:NO];
        td.usage = MTLTextureUsageRenderTarget;
        td.storageMode = MTLStorageModeShared;
        out[k] = [dev newTextureWithDescriptor:td];
        rp.colorAttachments[k].texture = out[k];
        rp.colorAttachments[k].loadAction = MTLLoadActionClear;
        rp.colorAttachments[k].clearColor = MTLClearColorMake(-7, -7, -7, -7);
        rp.colorAttachments[k].storeAction = MTLStoreActionStore;
    }
    id<MTLRenderCommandEncoder> enc = [cb renderCommandEncoderWithDescriptor:rp];
    [enc setRenderPipelineState:ps];
    [enc setViewport:(MTLViewport){ 0, 0, p->rt_width, p->rt_height, 0, 1 }];
    [enc setVertexBytes:(float[4]){ 0 } length:16 atIndex:1];
    if (p->vs_msl) {
        [enc setVertexBytes:p->vs_in length:p->vs_in_vecs * 16 atIndex:0];
        id<MTLBuffer> ub = [dev newBufferWithBytes:p->vs_u length:sizeof(*p->vs_u)
                                           options:MTLResourceStorageModeShared];
        id<MTLBuffer> ib = [dev newBufferWithBytes:p->vs_idx length:p->num_verts * 4
                                           options:MTLResourceStorageModeShared];
        [enc setVertexBuffer:ub offset:0 atIndex:2];
        [enc drawIndexedPrimitives:MTLPrimitiveTypeTriangle indexCount:p->num_verts
                         indexType:MTLIndexTypeUInt32 indexBuffer:ib indexBufferOffset:0];
    } else {
        [enc setVertexBytes:p->verts length:p->num_verts * sizeof(R300Vertex) atIndex:0];
        [enc drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:p->num_verts];
    }
    [enc endEncoding];
    [cb commit];
    [cb waitUntilCompleted];
    CHECK(cb.status == MTLCommandBufferStatusCompleted, "draw execution: %s", cb.error.description.UTF8String);
}

static bool no_read(void *o, uint32_t a, void *d, uint32_t l) { return false; }

static void load_qe(R300State *st)
{
    FILE *f = fopen("qe_draw1_full.txt", "r");
    CHECK(f != NULL, "missing QE fixture");
    if (!f) { return; }
    char line[256];
    int mode = 0;

    r300_state_reset(st);
    while (fgets(line, sizeof(line), f)) {
        unsigned a, v, i, d[4];
        float c[4];
        if (strstr(line, "PVS code")) { mode = 1; continue; }
        if (strstr(line, "PVS constants")) { mode = 2; continue; }
        if (mode == 0 && sscanf(line, " %x %x", &a, &v) == 2) r300_state_write(st, a, v);
        if (mode == 1 && sscanf(line, " %u: %x %x %x %x", &i, &d[0], &d[1], &d[2], &d[3]) == 5)
            memcpy(&st->pvs_mem[i * 4], d, 16);
        if (mode == 2 && sscanf(line, " c%u %f %f %f %f", &i, &c[0], &c[1], &c[2], &c[3]) == 5)
            memcpy(&st->pvs_mem[(512 + i) * 4], c, 16);
    }
    fclose(f);
}

static void whole_draw(const char *what, R300State *st, const uint32_t *pkt3, uint32_t ndw)
{
    R300Arrays arr = { 0 };
    R300DrawPacket cpu, gpu;
    const char *err;

    st->gpu_vs = false;
    CHECK(r300_draw_build(st, &arr, 0x35, pkt3, ndw, no_read, NULL, &cpu, &err),
          "%s cpu build: %s", what, err);
    st->gpu_vs = true;
    CHECK(r300_draw_build(st, &arr, 0x35, pkt3, ndw, no_read, NULL, &gpu, &err),
          "%s gpu build: %s", what, err);
    bool forced = getenv("R300_CPU_VS") != NULL;
    CHECK(forced ? gpu.vs_msl == NULL && gpu.verts != NULL : gpu.vs_msl != NULL,
          "%s: wrong vertex path (forced CPU=%d)", what, forced);
    if (forced) {
        CHECK(cpu.num_verts == gpu.num_verts, "CPU vertex count changed");
        for (unsigned i = 0; i < cpu.num_verts; i++) {
            CHECK(!memcmp(cpu.verts[i].pos, gpu.verts[i].pos, sizeof(cpu.verts[i].pos)) &&
                  !memcmp(cpu.verts[i].v, gpu.verts[i].v, sizeof(cpu.verts[i].v)) &&
                  cpu.verts[i].aux[0] == gpu.verts[i].aux[0], "CPU vertex %u changed", i);
        }
        printf("%s: R300_CPU_VS=1 selected interpreter, %u CPU vertices match\n", what, gpu.num_verts);
    }
    if (fails) {
        return;
    }
    if (emit_dir) {
        emit_source(what, gpu.vs_msl ? gpu.vs_msl : gpu.msl);
        r300_draw_free(&cpu);
        r300_draw_free(&gpu);
        return;
    }
    id<MTLFunction> fsf = fn(capture_fs, "r300_fs");
    id<MTLFunction> cvs = fn(cpu.msl, "r300_vs");
    id<MTLFunction> gvs = fn(gpu.vs_msl ? gpu.vs_msl : gpu.msl, "r300_vs");
    if (!fsf || !cvs || !gvs) {
        return;
    }
    id<MTLTexture> a[3], b[3];
    render(&cpu, cvs, fsf, a);
    render(&gpu, gvs, fsf, b);
    static float pa[W * H * 4], pb[W * H * 4];
    unsigned covered = 0, bad = 0;
    for (int k = 0; k < 3; k++) {
        [a[k] getBytes:pa bytesPerRow:W * 16 fromRegion:MTLRegionMake2D(0, 0, W, H) mipmapLevel:0];
        [b[k] getBytes:pb bytesPerRow:W * 16 fromRegion:MTLRegionMake2D(0, 0, W, H) mipmapLevel:0];
        for (int i = 0; i < W * H * 4; i++) {
            covered += k == 0 && i % 4 == 0 && pa[i] != -7.0f;
            if (!close_enough(pa[i], pb[i])) {
                if (bad++ < 5) {
                    printf("%s: target %d pixel (%d,%d).%c cpu %g gpu %g\n", what, k,
                           i / 4 % W, i / 4 / W, "xyzw"[i % 4], pa[i], pb[i]);
                }
            }
        }
    }
    printf("%s: %u pixels covered, %u values differ (%u CPU vertices, %u GPU indices)\n",
           what, covered, bad, cpu.num_verts, gpu.num_verts);
    CHECK(covered > 1000, "%s: nothing drawn", what);
    CHECK(bad == 0, "%s: %u values differ", what, bad);
    r300_draw_free(&cpu);
    r300_draw_free(&gpu);
}

int main(int argc, char **argv)
{
    @autoreleasepool {
        if (argc == 3 && !strcmp(argv[1], "--emit-msl")) {
            emit_dir = argv[2];
        } else {
            dev = MTLCreateSystemDefaultDevice();
            CHECK(dev != nil, "Metal device unavailable");
            if (!dev) { return 1; }
        }
        q = [dev newCommandQueue];
        bool forced = getenv("R300_CPU_VS") != NULL;
        if (!forced) { random_programs(!emit_dir && argc > 1 ? atoi(argv[1]) : 400); }

        static R300State st;
        load_qe(&st);
        /* 4 vertices x 12 dwords (pos, colour, texcoord), as quads */
        uint32_t pkt3[49] = { 0x0004003D };
        float verts[4][12] = {
            { 10, 5, 0, 1, 1, 0.5f, 0.25f, 1, 0, 0, 0, 1 },
            { 240, 20, 0, 1, 0, 1, 1, 0.5f, 256, 0, 0, 1 },
            { 230, 250, 0, 1, 1, 1, 0, 1, 256, 256, 0, 1 },
            { 15, 200, 0, 1, 0.2f, 0.4f, 0.6f, 0.8f, 0, 256, 0, 1 },
        };
        memcpy(&pkt3[1], verts, sizeof(verts));
        whole_draw("QE quad", &st, pkt3, 49);

        /* The same with fog from the colour's alpha. */
        r300_state_write(&st, 0x4BC0, 1);                   /* FG_FOG_BLEND on */
        r300_state_write(&st, 0x401C, (r300_reg(&st, 0x401C) & ~7u) | 0);
        float scale = 0.5f, off = 0.25f;
        uint32_t sb, ob;
        memcpy(&sb, &scale, 4);
        memcpy(&ob, &off, 4);
        r300_state_write(&st, 0x4294, sb);
        r300_state_write(&st, 0x4298, ob);
        whole_draw("QE quad with fog", &st, pkt3, 49);
    }
    printf(fails ? "test_gpuvs: FAIL\n" : emit_dir ? "test_gpuvs: source generation PASS (no GPU execution)\n" : "test_gpuvs: PASS\n");
    return fails != 0;
}

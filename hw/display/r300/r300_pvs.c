/*
 * R300-family Programmable Vertex Shader (PVS) interpreter.
 *
 * Semantics: AMD "R5xx Acceleration" v1.5, 7.5.7 (vector ops) and 7.5.8
 * (math ops), and the PVS instruction tables that follow.  Math ops read
 * the W channel of their operands (after swizzle): A.w is the argument or
 * base, B.w a clamp, C.w an exponent.
 *
 * This work is licensed under the terms of the GNU GPL, version 2 or later.
 */

#include "r300_pvs.h"

#include <float.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

/* Vector engine opcodes */
enum {
    VE_NOP = 0, VE_DOT_PRODUCT, VE_MULTIPLY, VE_ADD, VE_MULTIPLY_ADD,
    VE_DISTANCE_VECTOR, VE_FRACTION, VE_MAXIMUM, VE_MINIMUM,
    VE_SET_GREATER_THAN_EQUAL, VE_SET_LESS_THAN, VE_MULTIPLYX2_ADD,
    VE_MULTIPLY_CLAMP, VE_FLT2FIX_DX, VE_FLT2FIX_DX_RND,
    /* R5xx, harmless to implement */
    VE_COND_MUX_EQ = 23, VE_COND_MUX_GT, VE_COND_MUX_GTE,
    VE_SET_GREATER_THAN, VE_SET_EQUAL, VE_SET_NOT_EQUAL,
};

/* Math engine opcodes */
enum {
    ME_NOP = 0, ME_EXP_BASE2_DX, ME_LOG_BASE2_DX, ME_EXP_BASEE_FF,
    ME_LIGHT_COEFF_DX, ME_POWER_FUNC_FF, ME_RECIP_DX, ME_RECIP_FF,
    ME_RECIP_SQRT_DX, ME_RECIP_SQRT_FF, ME_MULTIPLY, ME_EXP_BASE2_FULL_DX,
    ME_LOG_BASE2_FULL_DX, ME_POWER_FUNC_FF_CLAMP_B,
    ME_POWER_FUNC_FF_CLAMP_B1, ME_POWER_FUNC_FF_CLAMP_01, ME_SIN, ME_COS,
    ME_LOG_BASE2_IEEE, ME_RECIP_IEEE, ME_RECIP_SQRT_IEEE,
};

enum { SRC_TEMP = 0, SRC_INPUT, SRC_CONST, SRC_ALT, SRC_ZERO };
enum { DST_TEMP = 0, DST_A0, DST_OUT, DST_OUT_REPL_X, DST_ALT, DST_INPUT };

typedef struct PVSRegs {
    float temp[R300_PVS_NUM_TEMPS][4];
    float alt[R300_PVS_NUM_ALT_TEMPS][4];
    int a0[4];
} PVSRegs;

typedef struct PVSCtx {
    const R300PVSProgram *prog;
    const float (*in)[4];
    float (*out)[4];
    PVSRegs r;
    const float (*base[5])[4];
    int al;                 /* fixed-point loop index of the innermost loop */
    uint32_t unsup;
} PVSCtx;

static const float zero4[4];

static const float *pvs_mem(PVSCtx *c, unsigned type, int index)
{
    switch (type) {
    case SRC_TEMP:
        return index >= 0 && index < R300_PVS_NUM_TEMPS ? c->r.temp[index] : zero4;
    case SRC_INPUT:
        return index >= 0 && index < R300_PVS_NUM_INPUTS ? c->in[index] : zero4;
    case SRC_CONST:
        return index >= 0 && index <= c->prog->max_const ?
               c->prog->consts[index] : zero4;
    default:
        return index >= 0 && index < R300_PVS_NUM_ALT_TEMPS ? c->r.alt[index] : zero4;
    }
}

/* ADDR_MODE: 0 absolute, 1 relative to A0.ADDR_SEL, 2 relative to the
 * loop index (R5xx guide, PVS source and destination operands). */
static int pvs_rel(PVSCtx *c, unsigned mode, unsigned sel, unsigned offset)
{
    if (mode == 1) {
        return (int)offset + c->r.a0[sel & 3];
    }
    if (mode == 2) {
        return (int)offset + c->al;
    }
    return offset;
}

static float pvs_select(const float *v, unsigned sel)
{
    return sel < 4 ? v[sel] : sel == 5 ? 1.0f : 0.0f;
}

static const float *pvs_source_mem(PVSCtx *c, const R300PVSSource *s)
{
    int index = s->index;

    if (!s->mode) {
        return c->base[s->type][s->index];
    }
    index = pvs_rel(c, s->mode, s->sel, index);
    return pvs_mem(c, s->type, index);
}

/* Identity operands can be consumed directly before either result is written. */
static const float *pvs_src(PVSCtx *c, const R300PVSSource *s, float o[4])
{
    const float *v = pvs_source_mem(c, s);

    if (s->identity) {
        return v;
    }
    for (int i = 0; i < 4; i++) {
        float x = pvs_select(v, s->swizzle[i]);
        if (s->abs) {
            x = fabsf(x);
        }
        if (s->neg & (1u << i)) {
            x = -x;
        }
        o[i] = x;
    }
    return o;
}

static float pvs_pow_ff(float base, float e)
{
    /* Special cases, in the documented order of detection. */
    if (base == 0.0f) {
        return e < 0.0f ? INFINITY : 0.0f;
    }
    if (e == 0.0f) {
        return 1.0f;
    }
    if (base < 0.0f) {
        return -powf(-base, e);
    }
    return powf(base, e);
}

/* Math engine: scalar operands A, B, C (the .w channels). */
static void pvs_math(PVSCtx *c, unsigned op, float a, float b, float cc,
                     float r[4])
{
    float x;

    switch (op) {
    case ME_EXP_BASE2_DX:
        r[0] = exp2f(floorf(a));
        r[1] = a > 128.0f ? 0.0f : a - floorf(a);
        r[2] = exp2f(a);
        r[3] = 1.0f;
        return;
    case ME_LOG_BASE2_DX:
        if (a == 0.0f) {
            r[0] = -FLT_MAX; r[1] = 1.0f; r[2] = -FLT_MAX; r[3] = 1.0f;
        } else {
            int e;
            float m = frexpf(fabsf(a), &e);    /* m in [0.5, 1) */
            r[0] = (float)(e - 1);
            r[1] = m * 2.0f;
            r[2] = log2f(fabsf(a));
            r[3] = 1.0f;
        }
        return;
    case ME_LIGHT_COEFF_DX:
        r[0] = 1.0f;
        r[1] = fmaxf(b, 0.0f);
        r[2] = b > 0.0f ? pvs_pow_ff(fmaxf(a, 0.0f),
                                     fminf(fmaxf(cc, -128.0f), 128.0f)) : 0.0f;
        r[3] = 1.0f;
        return;
    case ME_EXP_BASEE_FF:       x = expf(a); break;
    case ME_POWER_FUNC_FF:      x = pvs_pow_ff(a, cc); break;
    case ME_RECIP_DX:           x = a == 0.0f ? FLT_MAX : 1.0f / a; break;
    case ME_RECIP_FF:           x = a == 0.0f ? 0.0f : 1.0f / a; break;
    case ME_RECIP_SQRT_DX:      x = a == 0.0f ? FLT_MAX : 1.0f / sqrtf(fabsf(a)); break;
    case ME_RECIP_SQRT_FF:      x = a == 0.0f ? 0.0f : 1.0f / sqrtf(fabsf(a)); break;
    case ME_MULTIPLY:           x = a * b; break;
    case ME_EXP_BASE2_FULL_DX:  x = exp2f(a); break;
    case ME_LOG_BASE2_FULL_DX:  x = a == 0.0f ? -FLT_MAX : log2f(fabsf(a)); break;
    case ME_POWER_FUNC_FF_CLAMP_B:
        x = a < b ? 0.0f : pvs_pow_ff(a, cc);
        break;
    case ME_POWER_FUNC_FF_CLAMP_B1:
        x = a < b ? 0.0f : a > 1.0f ? 1.0f : pvs_pow_ff(a, cc);
        break;
    case ME_POWER_FUNC_FF_CLAMP_01:
        x = a <= 0.0f ? 0.0f : a > 1.0f ? 1.0f : pvs_pow_ff(a, cc);
        break;
    case ME_SIN:
    case ME_COS: {
        /* Inputs outside [-pi, pi] clamp: sin 0, cos -1. */
        float t = fminf(fmaxf(a, (float)-M_PI), (float)M_PI);
        x = op == ME_SIN ? sinf(t) : cosf(t);
        break;
    }
    case ME_LOG_BASE2_IEEE:     x = log2f(fabsf(a)); break;
    case ME_RECIP_IEEE:         x = 1.0f / a; break;
    case ME_RECIP_SQRT_IEEE:    x = 1.0f / sqrtf(fabsf(a)); break;
    case ME_NOP:                x = 0.0f; break;
    default:
        c->unsup |= R300_PVS_UNSUP_OPCODE;
        x = 0.0f;
        break;
    }
    r[0] = r[1] = r[2] = r[3] = x;
}

static void pvs_vector(PVSCtx *c, unsigned op, const float *a, const float *b,
                       const float *cc, float r[4])
{
    switch (op) {
    case VE_DOT_PRODUCT: {
        float d = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
        r[0] = r[1] = r[2] = r[3] = d;
        return;
    }
    case VE_DISTANCE_VECTOR:
        r[0] = 1.0f; r[1] = a[1] * b[1]; r[2] = a[2]; r[3] = b[3];
        return;
    case VE_MULTIPLY_CLAMP: {
        float x;
        if (cc[3] < a[3] * b[3]) {
            x = cc[3];
        } else if (cc[0] >= a[0] * b[0]) {
            x = cc[0];
        } else {
            x = a[0] * b[0];
        }
        r[0] = r[1] = r[2] = r[3] = x;
        return;
    }
    }
    for (int i = 0; i < 4; i++) {
        float x;
        switch (op) {
        case VE_NOP:                    x = 0.0f; break;
        case VE_MULTIPLY:               x = a[i] * b[i]; break;
        case VE_ADD:                    x = a[i] + b[i]; break;
        case VE_MULTIPLY_ADD:           x = a[i] * b[i] + cc[i]; break;
        case VE_FRACTION:               x = a[i] - floorf(a[i]); break;
        case VE_MAXIMUM:                x = fmaxf(a[i], b[i]); break;
        case VE_MINIMUM:                x = fminf(a[i], b[i]); break;
        case VE_SET_GREATER_THAN_EQUAL: x = a[i] >= b[i]; break;
        case VE_SET_LESS_THAN:          x = a[i] < b[i]; break;
        case VE_MULTIPLYX2_ADD:         x = 2.0f * (a[i] * b[i]) + cc[i]; break;
        case VE_FLT2FIX_DX:             x = floorf(a[i]); break;
        case VE_FLT2FIX_DX_RND:         x = floorf(a[i] + 0.5f); break;
        case VE_COND_MUX_EQ:            x = a[i] == 0.0f ? b[i] : cc[i]; break;
        case VE_COND_MUX_GT:            x = a[i] > 0.0f ? b[i] : cc[i]; break;
        case VE_COND_MUX_GTE:           x = a[i] >= 0.0f ? b[i] : cc[i]; break;
        case VE_SET_GREATER_THAN:       x = a[i] > b[i]; break;
        case VE_SET_EQUAL:              x = a[i] == b[i]; break;
        case VE_SET_NOT_EQUAL:          x = a[i] != b[i]; break;
        default:
            c->unsup |= R300_PVS_UNSUP_OPCODE;
            x = 0.0f;
            break;
        }
        r[i] = x;
    }
}

static void pvs_sat(float r[4])
{
    for (int i = 0; i < 4; i++) {
        r[i] = fminf(fmaxf(r[i], 0.0f), 1.0f);
    }
}

static void pvs_write(PVSCtx *c, const R300PVSDest *d, const float r[4])
{
    unsigned type = d->type;
    unsigned mask = d->mask;
    int rel = pvs_rel(c, d->mode, d->sel, d->index);
    unsigned index = rel < 0 ? ~0u : (unsigned)rel;
    float *dst;

    switch (type) {
    case DST_TEMP:
        dst = index < R300_PVS_NUM_TEMPS ? c->r.temp[index] : NULL;
        break;
    case DST_A0:
        for (int i = 0; i < 4; i++) {
            if (mask & (1u << i)) {
                float f = floorf(r[i]);
                c->r.a0[i] = f < -256.0f ? -256 : f > 255.0f ? 255 : (int)f;
            }
        }
        return;
    case DST_OUT:
    case DST_OUT_REPL_X:
        dst = index < R300_PVS_NUM_OUTPUTS ? c->out[index] : NULL;
        break;
    case DST_ALT:
        dst = index < R300_PVS_NUM_ALT_TEMPS ? c->r.alt[index] : NULL;
        break;
    default:
        return;
    }
    if (!dst) {
        return;
    }
    for (int i = 0; i < 4; i++) {
        if (mask & (1u << i)) {
            dst[i] = type == DST_OUT_REPL_X ? r[0] : r[i];
        }
    }
}

/*
 * The instruction after pc, following the flow control instructions
 * (AMD R5xx guide 7.5.5; VAP_PVS_FLOW_CNTL_*), once pc has run.  A
 * loop's end and a subroutine's
 * return are live only while that loop or call is.
 *
 *   ADDRS: 7:0 activation instruction (the last before the redirect),
 *          15:8 JUMP/JSR target or LOOP count, 23:16 last instruction of
 *          the loop or subroutine, 31:24 loop start or return address.
 *   LOOP_INDEX: 7:0 initial loop index, 15:8 signed step.
 */
typedef struct PVSLoop {
    int fc;                 /* its FLOW_CNTL instruction */
    unsigned count;         /* iterations left */
    int saved_al;           /* the enclosing loop's index */
} PVSLoop;

static unsigned pvs_flow(const R300PVSProgram *prog, PVSCtx *c, unsigned pc,
                         PVSLoop *loop, unsigned *nloop, int *jsr, unsigned *njsr)
{
    if (*nloop) {
        int k = loop[*nloop - 1].fc;
        if (((prog->fc_addrs[k] >> 16) & 0xFF) == pc) {
            c->al += (int8_t)(prog->fc_loop[k] >> 8);
            if (--loop[*nloop - 1].count) {
                return (prog->fc_addrs[k] >> 24) & 0xFF;
            }
            c->al = loop[--*nloop].saved_al;
            return pc + 1;
        }
    }
    if (*njsr) {
        int k = jsr[*njsr - 1];
        if (((prog->fc_addrs[k] >> 16) & 0xFF) == pc) {
            --*njsr;
            return (prog->fc_addrs[k] >> 24) & 0xFF;
        }
    }
    for (int k = 0; k < 16; k++) {
        unsigned op = (prog->fc_opc >> (2 * k)) & 3;
        uint32_t a = prog->fc_addrs[k];

        if (!op || (a & 0xFF) != pc) {
            continue;
        }
        switch (op) {
        case 1:                                 /* JUMP */
            return (a >> 8) & 0xFF;
        case 2:                                 /* LOOP */
            if (!((a >> 8) & 0xFF) || *nloop == 16) {
                return pc + 1;
            }
            loop[*nloop].fc = k;
            loop[*nloop].count = (a >> 8) & 0xFF;
            loop[*nloop].saved_al = c->al;
            ++*nloop;
            c->al = prog->fc_loop[k] & 0xFF;
            return (a >> 24) & 0xFF;
        default:                                /* JSR */
            if (*njsr == 16) {
                return pc + 1;
            }
            jsr[(*njsr)++] = k;
            return (a >> 8) & 0xFF;
        }
    }
    return pc + 1;
}

/* One instruction. */
static void pvs_exec(PVSCtx *c, const R300PVSInst *d)
{
    float av[4], bv[4], cv[4], r[4];
    const float *a, *b, *cc = cv;

    if (d->pred) {
        c->unsup |= R300_PVS_UNSUP_PRED;
    }
    a = pvs_src(c, &d->src[0], av);
    b = pvs_src(c, &d->src[1], bv);
    if (!d->dual) {
        cc = pvs_src(c, &d->src[2], cv);
    }

    if (d->math) {
        pvs_math(c, d->op, a[3], b[3], cc[3], r);
        if (d->math_sat) {
            pvs_sat(r);
        }
        pvs_write(c, &d->dst, r);
        return;
    }

    pvs_vector(c, d->op, a, b, cc, r);
    if (d->sat) {
        pvs_sat(r);
    }

    if (d->dual) {
        /* The third source dword describes a math op writing ATRM 0-3. */
        const R300PVSSource *s = &d->src[2];
        const float *v = pvs_source_mem(c, s);
        float x = pvs_select(v, s->swizzle[0]);
        float y = pvs_select(v, s->swizzle[1]);
        float mr[4];

        if (s->abs) {
            x = fabsf(x);
            y = fabsf(y);
        }
        if (s->neg & 1) {
            x = -x;
        }
        if (s->neg & 2) {
            y = -y;
        }
        pvs_math(c, d->dual_op, x, y, y, mr);
        if (d->math_sat) {
            pvs_sat(mr);
        }
        /* Both engines read before the vector and then math results are written. */
        pvs_write(c, &d->dst, r);
        c->r.alt[d->dual_index][d->dual_comp] = mr[d->dual_comp];
        return;
    }
    pvs_write(c, &d->dst, r);
}

void r300_pvs_prepare(R300PVSPrepared *prepared, const R300PVSProgram *prog)
{
    prepared->prog = *prog;
    prepared->num_temps = 0;
    prepared->num_alt = 0;
    if (prog->first_inst > prog->last_inst ||
        prog->first_inst >= R300_PVS_MAX_INSTS) {
        return;
    }
    /* Flow targets may precede first_inst. */
    unsigned first = prog->fc_opc ? 0 : prog->first_inst;

    for (unsigned pc = first;
         pc <= prog->last_inst && pc < R300_PVS_MAX_INSTS; pc++) {
        const uint32_t *words = &prog->code[pc * 4];
        uint32_t w = words[0];
        R300PVSInst *d = &prepared->inst[pc];
        bool macro = (w >> 7) & 1;

        d->op = w & 0x3F;
        d->math = (w >> 6) & 1;
        d->dual = !d->math && !macro && ((w >> 28) & 1);
        if (macro && !d->math) {
            d->op = (d->op & 1) ? VE_MULTIPLYX2_ADD : VE_MULTIPLY_ADD;
        }
        d->sat = (w >> 24) & 1;
        d->math_sat = (w >> 25) & 1;
        d->pred = (w >> 26) & 1;
        d->dst.type = (w >> 8) & 15;
        d->dst.mode = (w >> 31) | (((w >> 12) & 1) << 1);
        d->dst.sel = (w >> 29) & 3;
        d->dst.index = (w >> 13) & 127;
        d->dst.mask = (w >> 20) & 15;
        d->dual_op = ((words[3] >> 21) & 15) |
                     (((words[3] >> 2) & 1) << 4);
        d->dual_index = (words[3] >> 19) & 3;
        d->dual_comp = (words[3] >> 27) & 3;
        for (int k = 0; k < 3; k++) {
            R300PVSSource *s = &d->src[k];
            uint32_t word = words[k + 1];

            s->type = word & 3;
            s->mode = ((word >> 4) & 1) | ((word >> 31) << 1);
            s->sel = (word >> 29) & 3;
            s->index = (word >> 5) & 255;
            s->abs = (word >> 3) & 1;
            s->neg = (word >> 25) & 15;
            s->identity = !s->abs && !s->neg;
            for (int i = 0; i < 4; i++) {
                s->swizzle[i] = (word >> (13 + 3 * i)) & 7;
                s->identity &= s->swizzle[i] == i;
            }
            if (s->type == SRC_TEMP || s->type == SRC_ALT) {
                unsigned limit = s->type == SRC_TEMP ? R300_PVS_NUM_TEMPS :
                                                      R300_PVS_NUM_ALT_TEMPS;
                unsigned *count = s->type == SRC_TEMP ? &prepared->num_temps :
                                                      &prepared->num_alt;
                /* Mode 3 is absolute, just as in pvs_rel. */
                unsigned used = s->mode == 1 || s->mode == 2 ? limit :
                                s->index < limit ? s->index + 1 : 0;
                if (used > *count) {
                    *count = used;
                }
            }
            if (s->mode == 3) {
                s->mode = 0;
            }
            if (!s->mode) {
                int last = s->type == SRC_TEMP ? R300_PVS_NUM_TEMPS - 1 :
                           s->type == SRC_ALT ? R300_PVS_NUM_ALT_TEMPS - 1 :
                           s->type == SRC_INPUT ? R300_PVS_NUM_INPUTS - 1 :
                           prog->max_const;
                /* Absolute bounds checks are independent of the vertex. */
                if (s->index > last) {
                    s->type = SRC_ZERO;
                    s->index = 0;
                }
            }
        }
    }
}

uint32_t r300_pvs_run(const R300PVSProgram *prog,
                      const float in[R300_PVS_NUM_INPUTS][4],
                      float out[R300_PVS_NUM_OUTPUTS][4])
{
    R300PVSPrepared prepared;

    r300_pvs_prepare(&prepared, prog);
    return r300_pvs_run_prepared(&prepared, in, out);
}

uint32_t r300_pvs_run_prepared(const R300PVSPrepared *prepared,
                             const float in[R300_PVS_NUM_INPUTS][4],
                             float out[R300_PVS_NUM_OUTPUTS][4])
{
    PVSCtx c;
    const R300PVSProgram *prog = &prepared->prog;

    memset(c.r.temp, 0, prepared->num_temps * sizeof(c.r.temp[0]));
    memset(c.r.alt, 0, prepared->num_alt * sizeof(c.r.alt[0]));
    memset(c.r.a0, 0, sizeof(c.r.a0));
    c.prog = prog;
    c.in = in;
    c.out = out;
    c.base[SRC_TEMP] = c.r.temp;
    c.base[SRC_INPUT] = in;
    c.base[SRC_CONST] = prog->consts;
    c.base[SRC_ALT] = c.r.alt;
    c.base[SRC_ZERO] = &zero4;
    c.unsup = 0;

    /* Flow control state: active loops (innermost last) and subroutine
     * returns, as indices of their FLOW_CNTL instruction. */
    PVSLoop loop[16];
    int jsr[16];
    unsigned nloop = 0, njsr = 0, steps = 0;
    c.al = 0;

    for (unsigned pc = prog->first_inst, next;
         pc <= prog->last_inst && pc < R300_PVS_MAX_INSTS; pc = next) {
        if (++steps > 65536) {
            c.unsup |= R300_PVS_UNSUP_FLOW;     /* runaway loop */
            break;
        }
        pvs_exec(&c, &prepared->inst[pc]);
        next = prog->fc_opc ? pvs_flow(prog, &c, pc, loop, &nloop, jsr, &njsr)
                            : pc + 1;
    }
    return c.unsup;
}

void r300_pvs_disasm_inst(const uint32_t d[4], char *buf, unsigned len)
{
    static const char *dst_type[] = { "t", "a0", "o", "ox", "at", "i", "?", "?" };
    static const char *src_type[] = { "t", "i", "c", "at" };
    static const char swz[] = "xyzw01??";
    uint32_t d0 = d[0];
    int n;

    n = snprintf(buf, len, "%s%s op%-2u %s%u.%s%s%s%s <-",
                 (d0 >> 6) & 1 ? "ME" : (d0 >> 7) & 1 ? "MACRO" : "VE",
                 !((d0 >> 6) & 1) && ((d0 >> 28) & 1) ? "+dual" : "",
                 d0 & 0x3F, dst_type[(d0 >> 8) & 7], (d0 >> 13) & 0x7F,
                 (d0 >> 20) & 1 ? "x" : "", (d0 >> 21) & 1 ? "y" : "",
                 (d0 >> 22) & 1 ? "z" : "", (d0 >> 23) & 1 ? "w" : "");
    for (int k = 1; k < 4 && n > 0 && (unsigned)n < len; k++) {
        uint32_t s = d[k];
        char sw[5];
        for (int i = 0; i < 4; i++) {
            sw[i] = swz[(s >> (13 + 3 * i)) & 7];
            if (s & (1u << (25 + i))) {
                sw[i] = sw[i] >= 'a' ? sw[i] - 32 : sw[i];   /* upper = negated */
            }
        }
        sw[4] = 0;
        n += snprintf(buf + n, len - n, " %s%s[%u%s].%s",
                      s & 8 ? "|" : "", src_type[s & 3], (s >> 5) & 0xFF,
                      s & 16 ? "+a0" : "", sw);
    }
}

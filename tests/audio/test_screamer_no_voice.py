#!/usr/bin/env python3
"""qemu#21 regression: a Screamer TX DMA transfer must complete (io->len
reaches 0, dma_end fires) even when the host audio voice never opens -- not
silently stall forever with the ring reporting "full" and nothing draining
it.

Extracts the real pmac_screamer_tx_transfer, screamer_pace_cb,
screamerspk_callback and screamer_update_settings from production code
(hw/audio/screamer.c), the same technique tests/r300/test_cp_reads.py and
test_pm4_ring.py use, so the behavior under test is the actual shipped
control flow, not a second implementation of it.

Ported from linuxkid473/poweremu-qemu (branch r300, commit 6ea715317514):
"screamer: don't crash when the host audio voice won't open". That fork
shares this file's origin with ours; the fix is two parts, both exercised
by run_settings_then_ticks():
  1. screamer_update_settings allocates the ring before trying to open the
     voice, so the ring exists even when the voice never does.
  2. screamer_pace_cb drains the ring (rpos = wpos) after every transfer
     when there is no voice, so it never reports "full" and stalls the
     guest's DMA -- audio_be_write is what would normally drain it, but
     that never runs without a voice to call it back through.

Codex review of that fix found two more gaps, each with its own test here:
  3. run_full_ring_then_voice_loss(): a ring already full when the voice is
     lost (not filled by the transfer under test, e.g. left over from a
     voice that worked and then closed) never reached the drain, because
     the loop `break`s on "ring full" before getting there.
  4. run_drain_clears_primed() / run_reprime_threshold_after_reopen(): the
     drain didn't clear `primed`, so a voice reopening later could resume
     mid-silence instead of refilling its SCREAMER_PRIME_MS jitter buffer
     first.

Confirmed to actually catch the regression: with any of the three drain
sites' fix reverted (against a mutated in-memory copy of the extracted
slice, not the tracked file), the corresponding assertion fails.

Run: python3 tests/audio/test_screamer_no_voice.py
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'hw/audio/screamer.c').read_text()

sdbg_start = source.index('static struct {\n    int64_t last;')
tx_start = source.index('static int pmac_screamer_tx_transfer(ScreamerState *s, int max)')
tx_end = source.index('static void pmac_screamer_tx(DBDMA_io *io)', tx_start)
# Broadened past the original sdbg-struct-only slice to include sdbg_tick's
# body too: screamerspk_callback (pulled in below) calls it.
sdbg_end = tx_start
ring_code = source[sdbg_start:sdbg_end] + source[tx_start:tx_end]
assert 'if (!s->voice) {' in ring_code and 's->rpos = s->wpos;' in ring_code, \
    'screamer_pace_cb no longer drains the ring with no voice -- update slice'
assert ring_code.count('s->primed = false;') >= 2, \
    'screamer_pace_cb no longer clears primed at both drain sites -- update slice'

settings_start = source.index('static void screamer_update_settings(ScreamerState *s)')
settings_end = source.index('static void screamer_update_volume(ScreamerState *s)', settings_start)
settings_code = source[settings_start:settings_end]
assert settings_code.index('s->mixbuf = g_malloc0') < settings_code.index('audio_be_open_out'), \
    'screamer_update_settings no longer allocates the ring before opening the voice -- update slice'

cb_start = source.index('static void screamerspk_callback(void *opaque, int free_b)')
callback_code = source[cb_start:settings_start]
assert 's->primed = false' in callback_code and 'SCREAMER_PRIME_MS' in callback_code, \
    'screamerspk_callback priming logic changed -- update slice'

stub = r'''
#include <assert.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define NANOSECONDS_PER_SECOND 1000000000LL
#define SCALE_MS 1000000
#define SCREAMER_RING_FRAMES 8192
#define SCREAMER_PRIME_MS 40
#define g_malloc0(sz) calloc(1, (sz))
#define SCREAMER_DPRINTF(fmt, ...)

static int address_space_memory;

typedef uint64_t hwaddr;
typedef int MemTxAttrs;
#define MEMTXATTRS_UNSPECIFIED 0
#define QEMU_CLOCK_VIRTUAL 0
#define QEMU_CLOCK_REALTIME 1

typedef struct DBDMA_io DBDMA_io;
typedef void (*DBDMA_end)(DBDMA_io *io);
struct DBDMA_io {
    void *opaque;
    void *channel;
    hwaddr addr;
    int len;
    int is_last;
    int is_dma_out;
    DBDMA_end dma_end;
    bool processing;
};

typedef struct SWVoiceOut SWVoiceOut;
typedef struct AudioBackend AudioBackend;
typedef void (*audio_callback_fn)(void *opaque, int free_b);
struct audsettings { int freq, nchannels, fmt, endianness; };
#define AUDIO_FORMAT_S16 0

typedef struct QEMUTimer QEMUTimer;

typedef struct ScreamerState {
    AudioBackend *audio_be;
    SWVoiceOut *voice;
    uint8_t *mixbuf;
    int samples;
    int shift;

    uint32_t wpos;
    uint32_t rpos;
    QEMUTimer *pace_timer;
    int64_t pace_last, pace_frac, pace_idle;
    bool primed;

    uint32_t bpos;
    uint32_t ppos;
    uint32_t rate;
    DBDMA_io io;

    uint32_t regs[6];
    uint32_t codec_ctrl_regs[8];
} ScreamerState;

#define FRAME_CNT_REG 5

static int64_t fake_now;
static int64_t qemu_clock_get_ns(int clk) { (void)clk; return fake_now; }
static int64_t qemu_clock_get_ms(int clk) { (void)clk; return fake_now / 1000000; }
static void qemu_log(const char *fmt, ...) { (void)fmt; }

static void dma_memory_read(void *as, hwaddr addr, void *dst, size_t len,
                             MemTxAttrs attrs)
{
    (void)as; (void)addr; (void)attrs;
    memset(dst, 0, len);   /* content doesn't matter for this test */
}

/* Voice never opens -- the exact condition under test. */
static bool open_attempted;
static SWVoiceOut *audio_be_open_out(AudioBackend *be, SWVoiceOut *sw,
                                      const char *name, void *opaque,
                                      audio_callback_fn cb,
                                      const struct audsettings *as)
{
    (void)be; (void)sw; (void)name; (void)opaque; (void)cb; (void)as;
    open_attempted = true;
    return NULL;
}
static void audio_be_set_active_out(AudioBackend *be, SWVoiceOut *sw, bool on)
{
    (void)be; (void)sw; (void)on;
}
static void warn_report(const char *fmt, ...) { (void)fmt; }
static void timer_mod(QEMUTimer *t, int64_t when) { (void)t; (void)when; }

static int audio_be_write_calls;
static int audio_be_write(AudioBackend *be, SWVoiceOut *sw, const void *buf, int size)
{
    (void)be; (void)sw; (void)buf;
    audio_be_write_calls++;
    return size;   /* consume everything offered */
}

static const char *s_spk = "screamer";
'''

checks = r'''
static void dma_ended(DBDMA_io *io) { io->is_last = 1; }

static void run_settings_then_ticks(uint32_t dma_len)
{
    ScreamerState s;
    memset(&s, 0, sizeof(s));
    s.rate = 44100;

    open_attempted = false;
    screamer_update_settings(&s);
    assert(open_attempted);            /* the voice open was actually tried */
    assert(s.voice == NULL);           /* ...and it "failed", as designed */
    assert(s.mixbuf != NULL && s.samples > 0);   /* ring exists anyway */

    /* A DMA TX request bigger than one ring's worth, so a real stall (ring
     * reports full, nothing ever drains it) would need more than one pace
     * tick to surface -- exactly the shape a single-shot test could miss. */
    s.io.len = (int)dma_len;
    s.io.addr = 0x1000;
    s.io.dma_end = dma_ended;
    s.io.is_last = 0;

    fake_now = 0;
    int64_t tick_ns = NANOSECONDS_PER_SECOND / 1000;   /* 1ms, like the real pacer */
    for (int i = 0; i < 20000 && s.io.len > 0; i++) {
        fake_now += tick_ns;
        screamer_pace_cb(&s);
    }
    assert(s.io.len == 0 && "DMA transfer stalled with no host voice");
    assert(s.io.is_last == 1 && "dma_end never fired");
}

/* Gap 1: a ring that was already full at the moment the voice was lost (not
 * filled by the transfer under test) must still be reached by the drain --
 * the loop must not `break` on "ring full" before getting there. */
static void run_full_ring_then_voice_loss(void)
{
    ScreamerState s;
    memset(&s, 0, sizeof(s));
    s.rate = 44100;

    screamer_update_settings(&s);
    assert(s.mixbuf != NULL && s.samples > 0);

    /* Simulate an open, live voice (real open is stubbed to always fail, so
     * poke it directly) so the ring fills without draining -- a backend
     * that's fallen behind, not the qemu#21 bug by itself. */
    s.voice = (SWVoiceOut *)(uintptr_t)1;

    uint32_t big = (uint32_t)(s.samples * 2) << s.shift;   /* two rings' worth */
    s.io.len = (int)big;
    s.io.addr = 0x2000;
    s.io.dma_end = dma_ended;
    s.io.is_last = 0;

    fake_now = 0;
    int64_t tick_ns = NANOSECONDS_PER_SECOND / 1000;
    for (int i = 0; i < 500; i++) {
        fake_now += tick_ns;
        screamer_pace_cb(&s);
    }
    assert((uint32_t)(s.wpos - s.rpos) == (uint32_t)s.samples &&
           "ring should be completely full while a live voice never drains it");
    assert(s.io.len > 0 && "transfer should still be pending -- this isn't the bug yet");

    /* The voice is gone now -- e.g. the host backend closed it -- leaving
     * exactly the full-ring-with-no-voice state gap 1 covers. Without the
     * fix this stalls forever, just like the cold-start case did. */
    s.voice = NULL;
    for (int i = 0; i < 20000 && s.io.len > 0; i++) {
        fake_now += tick_ns;
        screamer_pace_cb(&s);
    }
    assert(s.io.len == 0 && "already-full ring never drained after voice loss");
    assert(s.io.is_last == 1 && "dma_end never fired after voice loss");
}

/* Gap 2, drain side: primed must clear at the "moved samples, no voice"
 * drain site too (not just the "ring was full" site above), so a reopened
 * voice can't tell this looks like an uninterrupted, still-primed stream. */
static void run_drain_clears_primed(void)
{
    ScreamerState s;
    memset(&s, 0, sizeof(s));
    s.rate = 44100;
    screamer_update_settings(&s);

    /* A voice that was open, primed, and mid-stream, then dropped with data
     * still queued -- not a cold start. */
    s.primed = true;
    s.wpos = 4000;
    s.rpos = 0;
    s.voice = NULL;

    s.io.len = (int)((uint32_t)4 << s.shift);
    s.io.addr = 0x3000;
    s.io.dma_end = dma_ended;
    s.io.is_last = 0;

    fake_now = 0;
    int64_t tick_ns = NANOSECONDS_PER_SECOND / 1000;
    for (int i = 0; i < 20000 && s.io.len > 0; i++) {
        fake_now += tick_ns;
        screamer_pace_cb(&s);
    }
    assert(s.io.len == 0 && "small transfer with no voice should still complete");
    assert(!s.primed && "primed must clear once the ring drains with no voice");
}

/* Gap 2, resume side: with primed left cleared by a drain, a reopened voice
 * must wait for a full SCREAMER_PRIME_MS jitter buffer before screamerspk_
 * callback resumes writing -- not flip primed (and play) off a few stray
 * queued frames. */
static void run_reprime_threshold_after_reopen(void)
{
    ScreamerState s;
    memset(&s, 0, sizeof(s));
    s.rate = 44100;
    screamer_update_settings(&s);

    s.voice = (SWVoiceOut *)(uintptr_t)1;   /* "reopened" */
    s.primed = false;                        /* as left by a prior drain */

    /* Only a handful of frames queued -- far short of the 40ms jitter
     * buffer -- must not resume playback yet. */
    s.wpos = 5;
    s.rpos = 0;
    audio_be_write_calls = 0;
    screamerspk_callback(&s, 4096);
    assert(!s.primed && "must not prime before the jitter-buffer threshold is met");
    assert(audio_be_write_calls == 0 && "must not resume playback before re-priming");

    /* Enough queued to cross SCREAMER_PRIME_MS -- should prime and write. */
    s.wpos = (uint32_t)(s.rate * SCREAMER_PRIME_MS / 1000) + 10;
    screamerspk_callback(&s, 4096);
    assert(s.primed && "should prime once enough is queued");
    assert(audio_be_write_calls > 0 && "should resume playback once primed");
}

int main(void)
{
    /* Bigger than SCREAMER_RING_FRAMES (8192) worth of stereo 16-bit frames,
     * so the ring must actually be drained (not just big enough) to finish.
     * Frame-aligned (4 bytes/frame at shift=2) -- a real DMA transfer always
     * is, and an unaligned length can never fully drain regardless of this
     * fix (the trailing partial frame is never enough to move). */
    run_settings_then_ticks(8192 * 4 * 3 + 400);
    puts("screamer: no-voice DMA drains instead of stalling: PASS");

    run_full_ring_then_voice_loss();
    puts("screamer: already-full ring drains after voice loss: PASS");

    run_drain_clears_primed();
    puts("screamer: primed clears when draining with no voice: PASS");

    run_reprime_threshold_after_reopen();
    puts("screamer: reopened voice re-primes before resuming: PASS");

    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='screamer-novoice-', dir=Path(__file__).parent) as tmp:
    c = Path(tmp) / 'test.c'
    binary = Path(tmp) / 'test'
    c.write_text(stub + ring_code + callback_code + settings_code + checks)
    subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + [
        '-std=gnu11', '-O1', '-Wall', '-Wextra', '-Werror',
        '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-sign-compare',
        str(c), '-o', str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True)

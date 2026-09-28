#!/usr/bin/env python3
"""qemu#25: the CP ring thread's dispatch logic.

Extracts the real cp struct and the ppc_mac_gpu_cp_* functions from
production code (same technique as test_pm4_ring.py) and runs them against
a recording stand-in for ppc_mac_gpu_process_ring_buffer, with the BQL and
QemuEvent/QemuThread mapped onto pthreads. Checks the parts that are easy
to get wrong and expensive to debug in a guest:

  * work is pending only while a doorbell (WPTR write / unhalt) is unrun, so
    RPTR/BASE/CNTL reinit writes never replay ring contents, a deferred
    incomplete tail (qemu#1) and ME_HALT never spin, and a wrapped WPTR that
    equals an old deferred value is still dispatched;
  * a kick makes the CP thread run the ring under the BQL;
  * cp_sync on another thread catches an unprocessed ring up exactly once;
  * a second device (not the worker's) runs inline;
  * cp_stop joins even if the thread is waiting for the BQL.

Run: python3 tests/r300/test_cp_thread.py
"""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'hw/display/ppc_mac_gpu.c').read_text()

s0 = source.index('static struct {\n    QemuThread thread;')
s1 = source.index('} cp;', s0) + len('} cp;')
struct_cp = source[s0:s1]
c0 = source.index('/* qemu#25 ----')
c1 = source.index('qemu_event_destroy(&cp.kick);\n}\n', c0) + \
    len('qemu_event_destroy(&cp.kick);\n}\n')
code = source[c0:c1]
for needle in ('ppc_mac_gpu_cp_pending', 'ppc_mac_gpu_cp_run', 'ppc_mac_gpu_cp_vm_state',
               'ppc_mac_gpu_cp_thread', 'ppc_mac_gpu_cp_kick',
               'ppc_mac_gpu_cp_sync', 'ppc_mac_gpu_cp_start',
               'ppc_mac_gpu_cp_stop', 's->cp_done = bell'):
    assert needle in code, 'cp code moved or changed shape: ' + needle

harness = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <assert.h>
#include <pthread.h>
#include <unistd.h>

#define qatomic_read(p) __atomic_load_n((p), __ATOMIC_SEQ_CST)
#define qatomic_set(p, v) __atomic_store_n((p), (v), __ATOMIC_SEQ_CST)
#define qatomic_inc(p) ((void)__atomic_fetch_add((p), 1, __ATOMIC_SEQ_CST))
#define QEMU_THREAD_JOINABLE 0
typedef pthread_t QemuThread;
typedef struct { pthread_mutex_t m; pthread_cond_t c; bool set; } QemuEvent;
static void qemu_event_init(QemuEvent *e, bool s)
{ pthread_mutex_init(&e->m, 0); pthread_cond_init(&e->c, 0); e->set = s; }
static void qemu_event_set(QemuEvent *e)
{ pthread_mutex_lock(&e->m); e->set = true; pthread_cond_broadcast(&e->c);
  pthread_mutex_unlock(&e->m); }
static void qemu_event_reset(QemuEvent *e)
{ pthread_mutex_lock(&e->m); e->set = false; pthread_mutex_unlock(&e->m); }
static void qemu_event_wait(QemuEvent *e)
{ pthread_mutex_lock(&e->m); while (!e->set) pthread_cond_wait(&e->c, &e->m);
  pthread_mutex_unlock(&e->m); }
static void qemu_event_destroy(QemuEvent *e) { (void)e; }
static void qemu_thread_create(QemuThread *t, const char *n, void *(*f)(void *),
                               void *a, int m)
{ (void)n; (void)m; pthread_create(t, 0, f, a); }
static void qemu_thread_join(QemuThread *t) { pthread_join(*t, 0); }
static void rcu_register_thread(void) {}
static void rcu_unregister_thread(void) {}
static pthread_mutex_t bql = PTHREAD_MUTEX_INITIALIZER;
static void bql_lock(void) { pthread_mutex_lock(&bql); }
static void bql_unlock(void) { pthread_mutex_unlock(&bql); }

typedef struct PPCMacGPUState {
    struct { uint32_t cp_rb_wptr, cp_rb_rptr, cp_me_cntl, cp_csq_stat_counter; } regs;
    uint32_t cp_doorbells, cp_done;
} PPCMacGPUState;
typedef struct VMChangeStateEntry { int unused; } VMChangeStateEntry;
typedef int RunState;
static VMChangeStateEntry vmce;
static VMChangeStateEntry *qemu_add_vm_change_state_handler(void (*f)(void *, bool, RunState), void *o)
{ (void)f; (void)o; return &vmce; }
static void qemu_del_vm_change_state_handler(VMChangeStateEntry *e) { (void)e; }

''' + struct_cp + r'''

static int process_calls;
static uint32_t defer_tail;        /* dwords process leaves unconsumed */
static bool bql_held_in_process;
static void ppc_mac_gpu_rptr_writeback(PPCMacGPUState *s) { (void)s; }
static void ppc_mac_gpu_process_ring_buffer(PPCMacGPUState *s,
                                            uint32_t old_rptr, uint32_t new_wptr)
{
    process_calls++;
    bql_held_in_process = pthread_mutex_trylock(&bql) != 0;
    if (!bql_held_in_process) pthread_mutex_unlock(&bql);
    uint32_t n = new_wptr - old_rptr;
    s->regs.cp_rb_rptr = old_rptr + (n > defer_tail ? n - defer_tail : 0);
}
''' + code + r'''

#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); return 1; } } while (0)

static void reset(PPCMacGPUState *s)
{
    s->regs.cp_rb_wptr = s->regs.cp_rb_rptr = s->regs.cp_me_cntl = 0;
    s->regs.cp_csq_stat_counter = 0;
    s->cp_doorbells = s->cp_done = 0;
    process_calls = 0; defer_tail = 0;
    cp.runs = cp.catchups = cp.kicks = 0;
}

static void wptr_write(PPCMacGPUState *s, uint32_t v)
{
    qatomic_set(&s->regs.cp_rb_wptr, v);
    ppc_mac_gpu_cp_kick(s);
}

static int test_inline(PPCMacGPUState *s)
{
    /* thread not started: the PPCGPU_CP_SYNC=1 behaviour */
    reset(s);
    CHECK(!ppc_mac_gpu_cp_pending(s));
    wptr_write(s, 16);
    CHECK(process_calls == 1 && s->regs.cp_rb_rptr == 16);
    CHECK(s->regs.cp_csq_stat_counter == 1);
    CHECK(!ppc_mac_gpu_cp_pending(s));
    return 0;
}

static int test_deferred_tail_no_spin(PPCMacGPUState *s)
{
    reset(s);
    defer_tail = 3;                     /* qemu#1: incomplete packet tail */
    wptr_write(s, 10);
    CHECK(process_calls == 1 && s->regs.cp_rb_rptr == 7);
    CHECK(!ppc_mac_gpu_cp_pending(s));
    ppc_mac_gpu_cp_run(s);              /* must not spin or re-run */
    ppc_mac_gpu_cp_run(s);
    CHECK(process_calls == 1);
    defer_tail = 0;
    wptr_write(s, 14);                  /* more data arrives */
    CHECK(process_calls == 2 && s->regs.cp_rb_rptr == 14);
    /* the wrapped ring later publishes the very same WPTR value (10) */
    s->regs.cp_rb_rptr = 6;
    wptr_write(s, 10);
    CHECK(process_calls == 3 && s->regs.cp_rb_rptr == 10);
    return 0;
}

static int test_reinit_no_replay(PPCMacGPUState *s)
{
    reset(s);
    wptr_write(s, 16);
    CHECK(process_calls == 1);
    /* ring reinit without ME_HALT: RPTR then BASE writes, WPTR not yet reset */
    s->regs.cp_rb_rptr = 0;
    ppc_mac_gpu_cp_run(s);
    ppc_mac_gpu_cp_sync(s);
    CHECK(process_calls == 1 && s->regs.cp_rb_rptr == 0);
    return 0;
}

static int test_halt(PPCMacGPUState *s)
{
    reset(s);
    s->regs.cp_me_cntl = 1u << 28;
    wptr_write(s, 8);
    CHECK(process_calls == 0 && s->regs.cp_rb_rptr == 0);
    CHECK(!ppc_mac_gpu_cp_pending(s));  /* halted: consumed, no spin */
    /* the ME_CNTL unhalt in mmio_write kicks */
    s->regs.cp_me_cntl = 0;
    ppc_mac_gpu_cp_kick(s);
    CHECK(process_calls == 1 && s->regs.cp_rb_rptr == 8);
    return 0;
}

static int test_thread(PPCMacGPUState *s)
{
    reset(s);
    CHECK(ppc_mac_gpu_cp_start(s));
    CHECK(cp.started && cp.dev == s);
    /* doorbell without the BQL, as the lock-free region does */
    wptr_write(s, 32);
    for (int i = 0; i < 2000 && qatomic_read(&s->regs.cp_rb_rptr) != 32; i++)
        usleep(1000);
    CHECK(qatomic_read(&s->regs.cp_rb_rptr) == 32);
    CHECK(bql_held_in_process);         /* ran under the BQL */
    CHECK(process_calls == 1);

    /* a second device is not served by the worker: it runs inline */
    PPCMacGPUState other = {0};
    int calls = process_calls;
    bql_lock();
    wptr_write(&other, 5);
    CHECK(process_calls == calls + 1 && other.regs.cp_rb_rptr == 5);
    bql_unlock();

    /* guest access while a submission is unprocessed: catch up once.
     * Hold the BQL so the thread cannot get in first. */
    bql_lock();
    wptr_write(s, 48);
    ppc_mac_gpu_cp_sync(s);
    CHECK(s->regs.cp_rb_rptr == 48 && cp.catchups == 1);
    ppc_mac_gpu_cp_sync(s);             /* nothing pending: not a catch-up */
    CHECK(cp.catchups == 1);
    calls = process_calls;
    bql_unlock();
    usleep(50000);                      /* thread wakes, finds nothing */
    CHECK(process_calls == calls);

    /* teardown while the thread is blocked on the BQL */
    bql_lock();
    wptr_write(s, 64);
    usleep(20000);
    ppc_mac_gpu_cp_stop(s);             /* unlocks, joins, relocks */
    CHECK(!cp.started);
    bql_unlock();
    return 0;
}

int main(void)
{
    PPCMacGPUState s = {0};
    int rc = test_inline(&s) | test_deferred_tail_no_spin(&s) |
             test_reinit_no_replay(&s) | test_halt(&s) | test_thread(&s);
    if (!rc) puts("test_cp_thread: all passed");
    return rc;
}
'''

with tempfile.TemporaryDirectory() as d:
    src = os.path.join(d, 'cp.c')
    exe = os.path.join(d, 'cp')
    open(src, 'w').write(harness)
    subprocess.run(['cc', '-O1', '-Wall', '-Wno-unused-function', '-o', exe,
                    src, '-lpthread'], check=True)
    subprocess.run([exe], check=True, timeout=60)

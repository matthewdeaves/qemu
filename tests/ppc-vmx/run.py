#!/usr/bin/env python3
"""Build vmx_test.c as a raw mac99 firmware image, run it, report results.

usage: run.py [--diff] [path/to/qemu-system-ppc]

With --diff the image also runs twice, PPC_VMX_HOST=1 and =0, and the exact
output hashes of the float ops must agree (old helpers are the oracle).

Needs Homebrew llvm (clang with the PowerPC target) and lld.
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ARGS = [a for a in sys.argv[1:] if a != '--diff']
DIFF = '--diff' in sys.argv[1:]
QEMU = (ARGS[0] if ARGS else
        os.path.join(HERE, '..', '..', 'build', 'qemu-system-ppc'))
DIFF_OPS = ['vaddfp', 'vsubfp', 'vmaxfp', 'vminfp', 'vmaddfp', 'vnmsubfp',
            'vrefp', 'vrsqrtefp', 'vcfsx', 'vcfux', 'vctsxs', 'vctuxs']
HASHES = 0x11000
RESULTS = 0x10000
OPS = """vperm vperm(d=a) vperm(d=c) vperm(a=b) vsldoi vsldoi(d=b)
vmrghb vmrghh vmrghw vmrglb vmrglh vmrglw
vpkuhum vpkuwum vpkuhus vpkuwus vpkshus vpkswus vpkshss vpkswss
vupkhsb vupkhsh vupklsb vupklsh
vmuleub vmuloub vmulesb vmulosb vmuleuh vmulouh vmulesh vmulosh
vmsumubm vmsummbm vmsumuhm vmsumshm
vaddfp vsubfp vmaxfp vminfp vmaddfp vnmsubfp vrefp vcfsx vcfux vctsxs vctuxs
""".split()
BENCH = ['vperm x4', 'vmrghb/vmrglh/vsldoi/vpkuhum',
         'vmuleub/vmsumubm/vpkshus/vupkhsb', 'vaddubm x4 (always inline)',
         'vmaddfp x4 (float)']


def tool(name):
    for d in ('/opt/homebrew/opt/llvm/bin', '/usr/local/opt/llvm/bin'):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    p = shutil.which(name)
    if not p:
        sys.exit(f'{name} not found (brew install llvm lld)')
    return p


def build(out):
    cc = [tool('clang'), '--target=powerpc-unknown-none-elf', '-mcpu=7450',
          '-maltivec', '-O2', '-ffreestanding', '-fno-builtin', '-nostdlib',
          '-fno-pic', '-c']
    objs = []
    for src in ('start.S', 'vmx_test.c'):
        o = os.path.join(out, src + '.o')
        subprocess.check_call(cc + [os.path.join(HERE, src), '-o', o])
        objs.append(o)
    elf = os.path.join(out, 'vmx_test.elf')
    subprocess.check_call([tool('ld.lld'), '-T', os.path.join(HERE, 'link.ld'),
                           '-o', elf] + objs)
    img = os.path.join(out, 'vmx_test.bin')
    subprocess.check_call([tool('llvm-objcopy'), '-O', 'binary', elf, img])
    return img


class Monitor:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX)
        for _ in range(100):
            try:
                self.s.connect(path)
                break
            except OSError:
                time.sleep(0.1)
        self.read_prompt()

    def read_prompt(self):
        buf = b''
        while not buf.endswith(b'(qemu) '):
            chunk = self.s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors='replace')

    def cmd(self, c):
        self.s.sendall(c.encode() + b'\n')
        return self.read_prompt()

    def words(self, addr, n):
        out = self.cmd(f'xp /{n}wx {addr:#x}')
        return [int(x, 16) for x in re.findall(r'0x([0-9a-f]{8})(?!:)', out)][:n]


def run_image(img, out, host=None):
    """Run the image; print results; return (failures, hashes)."""
    sock = os.path.join(out, 'mon.sock')
    if os.path.exists(sock):
        os.unlink(sock)
    env = dict(os.environ)
    if host is not None:
        env['PPC_VMX_HOST'] = host
    q = subprocess.Popen([QEMU, '-M', 'mac99', '-cpu', '7450', '-m', '64',
                          '-bios', img, '-display', 'none', '-serial', 'none',
                          '-monitor', f'unix:{sock},server=on,wait=off'],
                         stdout=subprocess.DEVNULL, env=env)
    try:
        mon = Monitor(sock)
        for _ in range(600):
            if mon.words(RESULTS, 1) == [0x564d5854]:
                break
            time.sleep(0.25)
        else:
            sys.exit('test did not finish')
        head = mon.words(RESULTS, 3 + len(OPS))
        checks, fails = head[1], head[2]
        for name, n in zip(OPS, head[3:]):
            if n:
                print(f'FAIL {name}: {n} mismatches')
        for k in range(min(fails, 8)):
            p = mon.words(RESULTS + 0x100 + k * 96, 24)
            it, gs, es = p[1] & 0xffff, (p[1] >> 16) & 0xff, p[1] >> 24
            vec = lambda i: ' '.join(f'{w:08x}' for w in p[i:i + 4])
            print(f'  {OPS[p[0]]} #{it}: a={vec(4)} b={vec(8)} c={vec(12)}')
            print(f'    got={vec(16)} sat={gs}  expected={vec(20)} sat={es}')
        tbfreq = 25_000_000  # mac99 timebase (TBFREQ)
        for name, t in zip(BENCH, mon.words(RESULTS + 0x800, len(BENCH))):
            print(f'bench {name:36s} {t / tbfreq * 1e3:8.1f} ms')
        print(f'{checks} checks, {fails} failures')
        hashes = mon.words(HASHES, 2 * len(DIFF_OPS))
        return fails, hashes
    finally:
        q.kill()
        q.wait()


def main():
    out = tempfile.mkdtemp(prefix='ppc-vmx-')
    img = build(out)
    fails, hashes = run_image(img, out)
    if DIFF:
        print('-- again with PPC_VMX_HOST=0 (old helpers)')
        _, old = run_image(img, out, '0')
        n = len(DIFF_OPS)
        for i, (new_h, old_h) in enumerate(zip(hashes, old)):
            if new_h != old_h:
                fails += 1
                print(f'DIFF {DIFF_OPS[i % n]} NJ={i // n}: '
                      f'host paths {new_h:08x}, old helpers {old_h:08x}')
        print(f'{2 * n} old-vs-new hashes compared, '
              f'{sum(a != b for a, b in zip(hashes, old))} differ')
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()

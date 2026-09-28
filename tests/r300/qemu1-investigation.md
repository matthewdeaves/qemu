# qemu#1 investigation, 2026-09-28

No live reproduction was available for this pass. This is instrumentation, not
an asserted hang fix. The two reproductions are taken from the task description;
the external evidence directories were not read, respecting the repository-only
scope. No guest execution, retries, completion rules, or address arithmetic were
changed.

## Findings and leading hypothesis

The best-supported working hypothesis is a persistent change in the shared
mapping or DMA-access state, or in the pool's PTE contents. This remains
unconfirmed. The persistent failures across previously usable addresses favor
that over a one-time torn PTE read. The current evidence cannot yet choose a
specific register or PTE failure.

The existing message does **not** establish that both translators failed.
`ppc_mac_gpu_read_dwords()` also returns false when translation succeeds but the
payload read fails. PTE reads use `address_space_memory`; payload reads use
`pci_get_address_space()`, the device's bus-master address space. Disabling PCI
bus mastering can therefore leave valid PTEs readable while making every CP
payload read fail. The test exercises this distinction. There is no evidence yet
that the guest actually disabled mastering in either reproduction.

Continued IB dispatches also do not establish successful ring reads. IBs can be
submitted through `CP_IB_BUFSZ` and PIO packets, including nested PM4 indirect
buffers. In addition, the WPTR handler unconditionally advances RPTR and writes
it back even if `ppc_mac_gpu_process_ring_buffer()` returned after a failed
read. That silent batch loss is confirmed in the source, but its involvement
in either hang is unconfirmed. It is deliberately left unchanged: introducing
stall/retry semantics needs a separate live-verified fix.

## Translation, register, and thread audit

- AIC derives both index and page offset from `gpu_addr - aic_lo_addr`. R300
  selects nonzero `r300_aic_pt_base` before `regs.aic_pt_base`. AGP derives its
  index from `MC_AGP_LOCATION` and prefers the UniNorth table, falling back to
  `regs.aic_pt_base`, not the R300 base. Those origins and tables can differ.
- No 32-bit index overflow exists at the reported addresses. The range check
  precedes subtraction; the maximum index is `0xfffff`, its four-byte offset
  fits in 32 bits, and adding the table base is in `hwaddr`. The 8-dword buffers
  at `0x10000020..0x1001e020` cannot wrap a 32-bit GPU address. The test covers
  that pool's upper page and a table-plus-offset calculation above 4 GiB.
- AGP's start is inherently 64 KiB aligned. AIC's base is stored without an
  alignment mask. There is a concrete latent mismatch for an unaligned AIC
  base: the translator uses relative page boundaries, but `read_dwords()`
  batches to absolute GPU page boundaries. For example, with AIC LO
  `0x10000004`, a read at `0x10001000` starts at physical page offset `0xffc`,
  yet may read several dwords without translating the next relative page.
  There is no captured unaligned AIC base and this does not by itself explain
  the persistent failure of the reported small buffers. No alignment fix was
  applied. The diagnostic records LO, index, PTE, address, and run length.
- Both translators accept any nonzero physical page; neither enforces PTE bit
  0. Their validity behavior is unchanged. A nonzero stale or corrupted PTE can
  therefore produce a payload-read error instead of a translation error.
- The seven requested register fields have explicit writers in
  `ppc_mac_gpu_mmio_write()`, including indexed-MMIO and PM4 type-0 routes.
  `0x0AB0` masks the R300 table base to a page and flushes the vertex translation
  cache. `ppc_mac_gpu_reset()` zeroes `regs` and initializes `mc_fb_location`;
  VMState restores the entire `regs` block. `r300_aic_pt_base` is outside that
  block, is not cleared by that memset, and has no VMState entry. There is no
  reset/migration evidence tying that separate lifecycle gap to this hang.
- UniNorth config writes covering GART base update its masked table pointer;
  base/aperture/control writes increment its generation. Those are observed
  through the existing accessors; no bridge files were changed.
- CP reads do not use `r200_agp_tc`, the 1024-entry vertex-read cache. Each CP
  page is translated afresh. Metal sampler eviction operates on sampler
  objects, with no route that invalidates CP mappings. No host pool eviction
  explaining this hang was found. The anisotropy first-use message only updates
  a feature-log bitset; it does not change mapping registers or PTEs.
- CP reads execute synchronously from MMIO/PIO/ring dispatch. The MMIO region
  is not lockless, and the TCG MMIO store path holds the BQL. The `core99`
  machine permits only one guest CPU. It cannot execute a guest PTE write
  concurrently while that same CPU is handling the submission.
- The Metal completion callback only atomically updates the completed sequence
  and schedules a main-loop bottom half; the bottom half performs scratch
  writeback. The optional R200 draw worker can read guest vertex data outside
  the BQL, but it does not execute IB/ring reads. R300 packet processing is
  synchronous. Shader compilation releases and reacquires the BQL, allowing
  other host activity, but does not resume the blocked single guest CPU.
  BQL is not a general guest-RAM lock on multi-vCPU machines; the single-CPU
  restriction matters to this conclusion. No Metal-thread CP/PTE race was
  established.

## Diagnostic output

Always-on diagnostics retain independent budgets of five IB failures and five
ring-read failures per process. Existing unbounded IB-lost messages are
unchanged. Each report includes:

- The original failing GPU page/run and original AIC/AGP PTE transaction
  results, with decoded little-endian PTE, table address, and index. There are
  no extra guest-memory reads, rechecks, or retries.
- Whether a payload read was attempted, its selected path, physical address,
  and `MemTxResult`. `data_tx=0` is meaningful only with `data_read=1`.
- Current AIC, R300 table, AGP, framebuffer, bridge base/generation, PCI command,
  DMA-enable state, ring pointers, reset generation, draw count, BQL state,
  and submission source. `source=ring/pio/mmio` describes the submitting path;
  a ring read triggered by a WPTR MMIO write can itself show `source=mmio`.
- The last successful system-memory IB read and ring span, with their original
  final-page PTEs and mapping state. Ring `count` counts successful spans, not
  executed batches; a wrapping batch can have two spans. Snapshots can predate
  a reset, so compare `reset` too. An absent snapshot says `unavailable`.
- The latest 16 effective mapping-register/PCI-command changes, oldest first,
  with old/new values and draw/reset/source context. Reset/migration are bulk
  writes and are not represented as MMIO-write entries. Register history is
  bounded and can overwrite earlier changes. Entries are filtered by device.

For the next capture, start with the first `IB lost diag` or `RING lost diag`:

1. `path=aic/agp`, `data_read=1`, nonzero `data_tx`: translation succeeded.
   Check `dma_enabled` and PCI command bit 2, then the PTE's physical page.
2. `path=none`: inspect each path's reason: `disabled`, `range`, `no-table`,
   `pte-read`, or `zero-page`. `not-tried` means AIC succeeded first. Compare
   current mapping registers and bridge base/generation with last-good state.
3. Stable configuration and changing/zero pool PTEs, while last-ring counts
   advance: investigate the guest mapping/fence lifecycle for those pages.
   This establishes which read failed, not who wrote the PTE or why.
4. Ring failures too, or no advancing successful ring spans: investigate shared
   mapping/access loss and silent RPTR advancement. Continued PIO/MMIO IBs
   cannot be used as evidence of a healthy ring.

The budgets may be consumed by earlier failures. Capture from process startup.
This patch does not trace every guest PTE store and cannot prove a transient
race solely from a zero PTE; a later targeted watch would need the physical PTE
address identified here.

## Validation

- `python3 tests/r300/test_cp_reads.py`: passed. Compiles the actual translation
  and CP-read functions with fake RAM and a separate PCI DMA gate. Covers
  noncontiguous page reads, AIC/AGP fallback, R300 table selection, differing
  aperture origins, zero PTEs, PTE transaction errors, payload errors, precise
  failing-page reporting, and absence of diagnostic rereads.
- Device source checked with the existing build's compiler flags and
  `-fsyntax-only`: passed.
- `sh tests/r300/run.sh tests-qemu1-build`: CPU stages passed through shader
  generation; stopped at `mslcheck` with `FAIL: (null)`. The independent Metal
  fence test reports `no Metal device available`. Full suite success and live
  VM validation are therefore **not established** here.
- The remaining CPU `test_features` was run separately from `tests/r300` with
  `MallocPreScribble=1`: passed.
- `git diff --check`: passed.

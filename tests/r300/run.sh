#!/bin/sh
# Build and run the offline R300 tests: ./run.sh [builddir]
set -e
cd "$(dirname "$0")"
# qemu#16: draw_core()'s per-draw scratch arrays are malloc'd, not calloc'd
# (72bfdb7199) -- a genuine but easy-to-miss uninitialized read regresses
# silently on a freshly-mapped page, which is usually already zero. macOS's
# libSystem malloc will actually fill every new allocation with 0xAA before
# handing it back when asked, so any output component draw_core() reads but
# never writes turns into a byte pattern no float comparison mistakes for a
# real value -- the same check the fix in r300_draw.c was verified against.
export MallocPreScribble=1
B=${1:-../../build/r300-tests}
mkdir -p "$B/tmp"
TMPDIR=$(cd "$B/tmp" && pwd)
export TMPDIR
python3 test_cp_reads.py
python3 test_pm4_ring.py
python3 test_pm4_ring_flow.py
python3 test_pm4_ib.py
python3 test_2d_packet_headers.py
python3 test_texview_bounds.py
R=../../hw/display/r300
SRC="$R/r300_state.c $R/r300_pvs.c $R/r300_us.c $R/r300_draw.c"
for t in test_pvs test_us test_draw test_features test_upload test_clear test_lightlog test_vs_build; do
    cc -O1 -Wall -Wno-unused-function -o "$B/$t" $t.c $SRC -lm
done
clang -fobjc-arc -framework Metal -framework Foundation mslcheck.m -o "$B/mslcheck"
clang -O1 -fobjc-arc -framework Metal -framework Foundation test_zs.m $SRC -o "$B/test_zs"
clang -O1 -fobjc-arc -framework Metal -framework Foundation test_fmt.m $SRC -o "$B/test_fmt"
clang -O1 -fobjc-arc -framework Metal -framework Foundation test_raster.m $SRC -o "$B/test_raster"
clang -O1 -fobjc-arc -framework Metal -framework Foundation test_gpuvs.m $SRC -o "$B/test_gpuvs"
clang -O1 -framework Metal -framework Foundation test_archive.m "$R/r300_metal_cache.m" -o "$B/test_archive"
clang -O1 -framework Metal -framework Foundation test_fence.m -o "$B/test_fence"
"$B/test_pvs"
env -u R300_CPU_VS "$B/test_vs_build"
R300_CPU_VS=1 "$B/test_vs_build"
"$B/test_draw"
"$B/test_upload"
"$B/test_clear"
"$B/test_lightlog"
"$B/test_lightlog" "$B/lightlog.txt"
"$B/test_us" > "$B/qe.metal"
"$B/mslcheck" "$B/qe.metal"
"$B/test_features"
"$B/test_zs"
"$B/test_fmt"
"$B/test_raster"
env -u R300_CPU_VS "$B/test_gpuvs"
R300_CPU_VS=1 "$B/test_gpuvs"
CACHE=$(mktemp -d "$B/archive.XXXXXX")
trap 'rm -rf "$CACHE"' 0
"$B/test_archive" "$CACHE"
"$B/test_archive" "$CACHE" --warm
"$B/test_fence"
echo "all R300 tests passed"

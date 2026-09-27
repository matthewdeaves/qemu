/* Regression: Q3's guard-row draw must not grow a later depth clear. */
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "../../hw/display/r300/r300_clear.h"

int main(void)
{
    const size_t pitch = 4096, depth = pitch * 768, lightmap = 65536;
    unsigned char *vram = malloc(depth + lightmap);
    assert(vram);
    memset(vram, 0x5a, depth + lightmap);
    /* Captured SC_SCISSORS_BR from Tiger Q3's 3D_CLEAR_ZMASK packet.
     * Previous drawing used 769 rows. The old historical-height maximum
     * cleared 4096 bytes of the neighbouring 128x128 RGBA lightmap. */
    uint64_t bytes = r300_clear_bytes(0x113e99f, pitch, 1, 0, depth + lightmap);
    assert(bytes == depth);
    memset(vram, 0xff, bytes);
    assert(vram[depth - 1] == 0xff);
    for (size_t i = depth; i < depth + lightmap; i++) assert(vram[i] == 0x5a);
    assert(r300_clear_bytes(0x113e99f, pitch, 4, 0, depth * 4) == depth * 4);
    assert(r300_clear_bytes(1440u << 13, pitch, 1, 0, pitch) == pitch);
    assert(r300_clear_bytes(1439u << 13, pitch, 1, 0, pitch) == 0);
    assert(r300_clear_bytes(0x113e99f, pitch, 1, 100, 99) == 0);
    assert(r300_clear_bytes(0x113e99f, 0, 1, 0, depth) == 0);
    assert(r300_clear_bytes(0x113e99f, pitch, 1, 0, pitch * 2 + 1) == pitch * 2);
    free(vram);
    puts("R300 clear-boundary regression passed");
    return 0;
}

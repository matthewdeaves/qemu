/* Diagnostic correctness: atlas changes away from the origin, padding,
 * unchanged binds, source changes, and the disabled/unsupported paths. */
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include "../../hw/display/r300/r300_draw.h"

int main(int argc, char **argv)
{
    if (argc == 1) {
        unsetenv("R300_LIGHTLOG");
        assert(!r300_lightlog());
        puts("lightlog disabled: ok");
        return 0;
    }
    assert(setenv("R300_LIGHTLOG", argv[1], 1) == 0);
    static R300State st;
    static R300DrawPacket pkt;
    uint8_t bytes[32] = { 0 };
    R300TexDesc *td = &pkt.tex[1];
    r300_state_reset(&st);
    pkt.diag_draw = 42;
    td->bound = true;
    td->gpu_addr = 0x2000;
    td->width = td->height = 2;
    td->pitch_bytes = 16;
    td->size_bytes = sizeof(bytes);
    td->kind = R300_TEXK_RGBA8;
    td->format = 0xc;
    td->swap = 1;
    td->levels = 1;
    r300_state_write(&st, 0x4104, 2);
    r300_state_write(&st, 0x4544, 0x2001);
    r300_lightlog_draw(&st, &pkt);
    r300_lightlog_texels(&pkt, 1, bytes);
    pkt.diag_draw++;
    bytes[8] = 99; /* Row padding must not count as a changed texel. */
    r300_lightlog_texels(&pkt, 1, bytes);
    pkt.diag_draw++;
    bytes[20] = 255;
    bytes[21] = 32;
    bytes[22] = 128;
    bytes[23] = 64;
    r300_lightlog_texels(&pkt, 1, bytes);
    pkt.diag_draw++;
    td->host_data = bytes; /* Same address, different memory source. */
    r300_lightlog_texels(&pkt, 1, bytes);
    pkt.diag_draw++;
    td->size_bytes = 1; /* Invalid descriptor must not read out of range. */
    r300_lightlog_texels(&pkt, 1, bytes);
    fflush(r300_lightlog());

    FILE *f = fopen(argv[1], "r");
    assert(f);
    char output[65536];
    size_t n = fread(output, 1, sizeof(output) - 1, f);
    assert(feof(f));
    output[n] = 0;
    fclose(f);
    assert(strstr(output, "source_dlight_rgb=unavailable"));
    assert(strstr(output, "CONST D42 c0 raw=000000,000000,000000,000000 rgba=0,0,0,0"));
    assert(strstr(output, "TEX D42 t1 bound=1 tx_offset=00002001 endian=1"));
    assert(strstr(output, "PIX D44 t1 xy=1,1 old=00000000 new=ff208040"));
    assert(strstr(output, "changed=1 bbox=1,1-2,2"));
    assert(strstr(output, "changed=0 bbox=0,0-0,0"));
    assert(!strstr(output, "PIX D43"));
    assert(strstr(output, "TEXELS D45 t1 addr=00002000 baseline=1"));
    assert(strstr(output, "TEXELS D46 t1 skipped=unsupported-layout-or-size"));
    puts("lightlog atlas diagnostics: ok");
    return 0;
}

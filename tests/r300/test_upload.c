/* A GL RGB lightmap's unused byte must not become its red component. */
#include <assert.h>
#include <stdio.h>
#include "../../hw/display/r300/r300_upload.h"

int main(void)
{
    /* Little-endian host word for source bytes R,G,B,0. GPU component X
     * is R; CPU-aperture VRAM stores 0,B,G,R. Distinct channels detect
     * every pair/halfword/dword permutation, not just an all-white case. */
    const uint32_t source = 0x00563412;
    assert(r300_upload_word(source, 4, 0) == 0x12345600);
    assert(r300_upload_word(source, 4, 1) == 0x34120056);
    assert(r300_upload_word(source, 4, 2) == source);
    assert(r300_upload_word(source, 4, 3) == 0x56001234);
    assert(r300_upload_word(0x00ffffff, 4, 0) == 0xffffff00);
    /* Quartz's source words already match the swapped CPU aperture. */
    assert(r300_upload_word(0x12345678, 4, 2) == 0x12345678);
    assert(r300_upload_word(0x12345678, 2, 0) == 0x34127856);
    assert(r300_upload_word(0x12345678, 2, 2) == 0x12345678);
    puts("R300 upload byte-order tests passed");
    return 0;
}

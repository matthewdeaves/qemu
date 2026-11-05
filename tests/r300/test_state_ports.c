/* SPDX-License-Identifier: GPL-2.0-or-later */
/* The stock Panther ATI driver uploads PVS code/constants through 0x2204. */
#include <assert.h>
#include <stdio.h>
#include "../../hw/display/r300/r300_state.h"

int main(void)
{
    const uint32_t instruction[4] = {
        0x00100201, 0x00d10002, 0x00d10001, 0x00d10009
    };
    const uint32_t constant[4] = { 0x3f800000, 0, 0, 0 };
    const uint32_t ports[2] = {
        R300_VAP_PVS_UPLOAD_DATA_LEGACY, R300_VAP_PVS_UPLOAD_DATA
    };
    for (unsigned port = 0; port < 2; port++) {
        R300State st;
        r300_state_reset(&st);
        r300_state_write(&st, R300_VAP_PVS_VECTOR_INDX_REG, 0);
        for (unsigned i = 0; i < 4; i++) {
            r300_state_write(&st, ports[port], instruction[i]);
            assert(st.pvs_mem[i] == instruction[i]);
        }
        r300_state_write(&st, R300_VAP_PVS_VECTOR_INDX_REG, 512);
        for (unsigned i = 0; i < 4; i++) {
            r300_state_write(&st, ports[port], constant[i]);
            assert(st.pvs_mem[512 * 4 + i] == constant[i]);
        }
        assert(st.pvs_upload_dw == 513 * 4);
        assert(st.pvs_gen == 8);
        /* Both addresses feed the same cursor, rather than two banks. */
        r300_state_write(&st, ports[1 - port], 0x12345678);
        assert(st.pvs_mem[513 * 4] == 0x12345678);
        assert(st.pvs_mem[0] == instruction[0]);
    }
    puts("R300 Panther and modern PVS upload ports passed");
    return 0;
}

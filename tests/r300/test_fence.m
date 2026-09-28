/*
 * qemu#17/QemuMac#22: does a single MTLFence updateFence/waitForFence pair,
 * called only when a batch-conflict split happens, correctly order an
 * EARLIER encoder's writes too -- one that closed without ever touching the
 * fence itself (the pre-b3e5942d2b-fix shape) -- or does Metal need every
 * encoder boundary in the chain to touch the fence (the fix Codex proposed
 * in review, landed here)?
 *
 * Mirrors the real driver's technique: two distinct MTLTexture objects
 * ("views") over the SAME MTLBuffer memory -- the exact case QEMU's own
 * comments say Metal's automatic hazard tracking misses. One writes via a
 * render pass, the other reads via a different render pass, same shape as
 * r200_view()/r200_end_encoder()/r200_begin_encoder().
 *
 * Three real render-command-encoders per iteration, all in ONE still-open
 * command buffer (matching g_r200_cb's lifetime across many draws/splits):
 *   E1 writes value=i into the shared buffer via texA, closes.
 *   E2 is unrelated work (a different target entirely) that closes with
 *      updateFence -- standing in for "the encoder open when a split
 *      actually happens", which may not be the same encoder that did the
 *      write under test.
 *   E3 opens, waitForFence, reads the shared buffer via texB (aliasing
 *      texA), writes what it saw into a fresh per-iteration readback
 *      texture/buffer.
 * After the command buffer completes, every readback slot must equal the
 * value E1 wrote for that iteration -- anything else is a lost write.
 *
 * unfenced_ok(): E1 never touches the fence itself (only E2 does) --
 *   exercises whether ordinary encoder-order execution alone already
 *   covers an unfenced producer several boundaries back.
 * chained_ok():  E1 ALSO updates the fence at its own close, matching
 *   r200_end_encoder()'s current (fixed) behaviour -- must always pass;
 *   if this ever fails the harness itself, not the hypothesis, is wrong.
 */
#include <stdio.h>
#include <stdint.h>
#import <Metal/Metal.h>

#define N 20000
#define CHUNK 2000
#define DIM 256

static const char *kSource =
    "#include <metal_stdlib>\n"
    "using namespace metal;\n"
    "struct VOut { float4 position [[position]]; };\n"
    "vertex VOut vs_fullscreen(uint vid [[vertex_id]]) {\n"
    "    float2 pos[3] = { float2(-1,-1), float2(3,-1), float2(-1,3) };\n"
    "    VOut o; o.position = float4(pos[vid], 0, 1); return o;\n"
    "}\n"
    "fragment uint4 fs_write(constant uint &val [[buffer(0)]]) {\n"
    "    return uint4(val, 0, 0, 0);\n"
    "}\n"
    "fragment uint4 fs_dummy() { return uint4(0xdeadbeef, 0, 0, 0); }\n"
    "fragment uint4 fs_read(texture2d<uint, access::read> tex [[texture(0)]]) {\n"
    "    return uint4(tex.read(uint2(0,0)).r, 0, 0, 0);\n"
    "}\n";

static id<MTLRenderPipelineState> make_pipeline(id<MTLDevice> dev, id<MTLLibrary> lib,
                                                 NSString *fsName, NSError **err)
{
    MTLRenderPipelineDescriptor *pd = [[MTLRenderPipelineDescriptor alloc] init];
    pd.vertexFunction = [[lib newFunctionWithName:@"vs_fullscreen"] autorelease];
    pd.fragmentFunction = [[lib newFunctionWithName:fsName] autorelease];
    pd.colorAttachments[0].pixelFormat = MTLPixelFormatR32Uint;
    id<MTLRenderPipelineState> p = [dev newRenderPipelineStateWithDescriptor:pd
                                                                         error:err];
    [pd release];
    return p;
}

static id<MTLTexture> buffer_view(id<MTLBuffer> buf, unsigned dim, bool renderTarget)
{
    MTLTextureDescriptor *d =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatR32Uint
                                                            width:dim height:dim mipmapped:NO];
    d.usage = MTLTextureUsageShaderRead | (renderTarget ? MTLTextureUsageRenderTarget : 0);
    d.storageMode = MTLStorageModeShared;
    return [buf newTextureWithDescriptor:d offset:0 bytesPerRow:dim * 4];
}

/* fence_every_boundary: false reproduces the pre-fix shape (only the
 * encoder open at "split time", E2, ever touches the fence); true adds
 * r200_end_encoder()'s unconditional update, matching the landed fix. */
static int run_case(id<MTLDevice> dev, id<MTLCommandQueue> queue,
                     id<MTLRenderPipelineState> writeP, id<MTLRenderPipelineState> dummyP,
                     id<MTLRenderPipelineState> readP, bool fence_every_boundary)
{
    id<MTLBuffer> vram = [dev newBufferWithLength:DIM * DIM * 4
                                           options:MTLResourceStorageModeShared];
    id<MTLTexture> texA = buffer_view(vram, DIM, true);
    id<MTLTexture> texB = buffer_view(vram, DIM, false);
    id<MTLBuffer> scratch = [dev newBufferWithLength:DIM * DIM * 4
                                              options:MTLResourceStorageModeShared];
    id<MTLTexture> texC = buffer_view(scratch, DIM, true);
    id<MTLFence> fence = [dev newFence];

    MTLRenderPassDescriptor *rpA = [MTLRenderPassDescriptor renderPassDescriptor];
    rpA.colorAttachments[0].texture = texA;
    rpA.colorAttachments[0].loadAction = MTLLoadActionDontCare;
    rpA.colorAttachments[0].storeAction = MTLStoreActionStore;

    MTLRenderPassDescriptor *rpC = [MTLRenderPassDescriptor renderPassDescriptor];
    rpC.colorAttachments[0].texture = texC;
    rpC.colorAttachments[0].loadAction = MTLLoadActionDontCare;
    rpC.colorAttachments[0].storeAction = MTLStoreActionStore;

    /* Committed in CHUNK-sized command buffers -- one giant buffer holding
     * every iteration's encoders/resources runs the real GPU out of memory
     * long before it says anything about fence ordering. The fence and the
     * aliased vram/scratch buffers are still reused across every chunk, so
     * the long-lived-resource shape the real driver exercises is intact. */
    int mismatches = 0;
    for (uint32_t base = 0; base < N; base += CHUNK) {
        uint32_t n = (N - base < CHUNK) ? (N - base) : CHUNK;
        /* A 1x1 R32Uint linear texture still needs a 16-byte-aligned
         * bytesPerRow on this hardware; only the first 4 bytes are real. */
        id<MTLBuffer> rdBufs[CHUNK];
        id<MTLTexture> rdTex[CHUNK];
        for (uint32_t k = 0; k < n; k++) {
            rdBufs[k] = [dev newBufferWithLength:16 options:MTLResourceStorageModeShared];
            MTLTextureDescriptor *rd = [MTLTextureDescriptor
                texture2DDescriptorWithPixelFormat:MTLPixelFormatR32Uint
                                              width:1 height:1 mipmapped:NO];
            rd.usage = MTLTextureUsageRenderTarget;
            rd.storageMode = MTLStorageModeShared;
            rdTex[k] = [rdBufs[k] newTextureWithDescriptor:rd offset:0 bytesPerRow:16];
        }

        id<MTLCommandBuffer> cb = [[queue commandBuffer] retain];
        for (uint32_t k = 0; k < n; k++) {
            uint32_t i = base + k;
            id<MTLRenderCommandEncoder> e1 = [cb renderCommandEncoderWithDescriptor:rpA];
            [e1 setRenderPipelineState:writeP];
            [e1 setFragmentBytes:&i length:sizeof(i) atIndex:0];
            [e1 drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
            if (fence_every_boundary) {
                [e1 updateFence:fence afterStages:MTLRenderStageFragment];
            }
            [e1 endEncoding];

            id<MTLRenderCommandEncoder> e2 = [cb renderCommandEncoderWithDescriptor:rpC];
            [e2 setRenderPipelineState:dummyP];
            [e2 drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
            [e2 updateFence:fence afterStages:MTLRenderStageFragment];
            [e2 endEncoding];

            MTLRenderPassDescriptor *rpD = [MTLRenderPassDescriptor renderPassDescriptor];
            rpD.colorAttachments[0].texture = rdTex[k];
            rpD.colorAttachments[0].loadAction = MTLLoadActionDontCare;
            rpD.colorAttachments[0].storeAction = MTLStoreActionStore;
            id<MTLRenderCommandEncoder> e3 = [cb renderCommandEncoderWithDescriptor:rpD];
            [e3 waitForFence:fence beforeStages:MTLRenderStageVertex | MTLRenderStageFragment];
            [e3 setRenderPipelineState:readP];
            [e3 setFragmentTexture:texB atIndex:0];
            [e3 drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
            [e3 endEncoding];
        }
        [cb commit];
        [cb waitUntilCompleted];
        if (cb.status == MTLCommandBufferStatusError) {
            fprintf(stderr, "command buffer error at base %u: %s\n", base,
                    cb.error.localizedDescription.UTF8String);
            return -1;
        }

        for (uint32_t k = 0; k < n; k++) {
            uint32_t i = base + k;
            uint32_t got = *(uint32_t *)rdBufs[k].contents;
            if (got != i) {
                if (mismatches < 5) {
                    fprintf(stderr, "  iter %u: wrote %u, read back %u\n", i, i, got);
                }
                mismatches++;
            }
        }
        [cb release];
        for (uint32_t k = 0; k < n; k++) { [rdTex[k] release]; [rdBufs[k] release]; }
    }

    [fence release];
    [texC release]; [scratch release];
    [texB release]; [texA release]; [vram release];
    return mismatches;
}

int main(void)
{
    @autoreleasepool {
        id<MTLDevice> dev = MTLCreateSystemDefaultDevice();
        if (!dev) {
            fprintf(stderr, "test_fence: no Metal device available\n");
            return 2;
        }
        id<MTLCommandQueue> queue = [dev newCommandQueue];
        NSError *err = nil;
        id<MTLLibrary> lib = [dev newLibraryWithSource:[NSString stringWithUTF8String:kSource]
                                                options:nil error:&err];
        if (!lib) {
            fprintf(stderr, "test_fence: shader compile failed: %s\n",
                    err.localizedDescription.UTF8String);
            return 1;
        }
        id<MTLRenderPipelineState> writeP = make_pipeline(dev, lib, @"fs_write", &err);
        id<MTLRenderPipelineState> dummyP = make_pipeline(dev, lib, @"fs_dummy", &err);
        id<MTLRenderPipelineState> readP = make_pipeline(dev, lib, @"fs_read", &err);
        if (!writeP || !dummyP || !readP) {
            fprintf(stderr, "test_fence: pipeline creation failed: %s\n",
                    err.localizedDescription.UTF8String);
            return 1;
        }

        int chained = run_case(dev, queue, writeP, dummyP, readP, true);
        if (chained != 0) {
            fprintf(stderr,
                    "test_fence: FAIL -- chained (fixed) case lost %d/%d writes; "
                    "the test harness itself is broken, not just the hypothesis\n",
                    chained, N);
            return 1;
        }
        printf("test_fence: chained (every boundary fenced): 0/%d mismatches\n", N);

        int unfenced = run_case(dev, queue, writeP, dummyP, readP, false);
        printf("test_fence: unfenced producer (pre-fix shape): %d/%d mismatches\n",
               unfenced, N);
        if (unfenced != 0) {
            fprintf(stderr,
                    "test_fence: CONFIRMED -- an encoder that closes without updating "
                    "the fence is not ordered before a later waitForFence on real "
                    "hardware; b3e5942d2b's original single-encoder fence was a real "
                    "correctness bug (QemuMac#22)\n");
            return 1;
        }
        fprintf(stderr,
                "test_fence: could not reproduce the unfenced-producer hazard on this "
                "hardware/driver after %d iterations -- inconclusive, not a clean bill "
                "of health for the pre-fix code; keeping the chained fix regardless\n",
                N);
        printf("test_fence: PASS\n");
    }
    return 0;
}

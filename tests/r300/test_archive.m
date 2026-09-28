/* Persistent pipeline cache: independent process cold/warm regression. */
#import "../../hw/display/r300/r300_metal_cache.h"
#include <stdio.h>
int main(int argc, char **argv)
{
    @autoreleasepool {
        if (argc < 2) { return 2; }
        bool warm = argc > 2;
        id<MTLDevice> dev = MTLCreateSystemDefaultDevice();
        NSString *dir = [NSString stringWithUTF8String:argv[1]];
        for (unsigned variant = 0; variant < 3; variant++) {
            for (unsigned n = 0; n < 20; n++) {
                NSError *error = nil;
                /* Shared entry names, distinct libraries, like r300_pipeline.
                 * Also reuse each source with two attachment formats. */
                NSString *source = [NSString stringWithFormat:
                    @"#include <metal_stdlib>\nusing namespace metal; "
                     "vertex float4 vs(uint i [[vertex_id]]) { return float4(0,0,0,1); } "
                     "fragment float4 fs() { return float4(%u.0/20,0,0,1); }", n / 2];
                NSString *vsource = variant ? [NSString stringWithFormat:
                    @"#include <metal_stdlib>\nusing namespace metal; "
                     "vertex float4 vs(uint i [[vertex_id]]) { return float4(%u.0/10,0,0,1); }",
                     variant] : nil;
                MTLPixelFormat fmt = n % 2 ? MTLPixelFormatRGBA8Unorm : MTLPixelFormatBGRA8Unorm;
                NSURL *url = nil;
                bool loaded;
                id<MTLBinaryArchive> archive = r300_metal_cache_open(dev, dir,
                    source.UTF8String, vsource.UTF8String, &fmt, 1, MTLPixelFormatInvalid, &url, &loaded, &error);
                if (!archive || loaded != warm) {
                    fprintf(stderr, "archive %u: expected %s: %s\n", n,
                            warm ? "hit" : "miss", error.description.UTF8String);
                    return 1;
                }
                id<MTLLibrary> lib = [dev newLibraryWithSource:source options:nil error:&error];
                MTLRenderPipelineDescriptor *pd = [[MTLRenderPipelineDescriptor alloc] init];
                id<MTLLibrary> vlib = [dev newLibraryWithSource:vsource ? vsource : source
                    options:r300_metal_vertex_options() error:&error];
                pd.vertexFunction = [[vlib newFunctionWithName:@"vs"] autorelease];
                pd.fragmentFunction = [[lib newFunctionWithName:@"fs"] autorelease];
                pd.colorAttachments[0].pixelFormat = fmt;
                pd.binaryArchives = @[archive];
                id<MTLRenderPipelineState> pipe = [dev newRenderPipelineStateWithDescriptor:pd
                    options:warm ? MTLPipelineOptionFailOnBinaryArchiveMiss : MTLPipelineOptionNone
                    reflection:NULL error:&error];
                if (!pipe || (!warm && !r300_metal_cache_store(archive, pd, url, &error))) {
                    fprintf(stderr, "pipeline %u: %s\n", n, error.description.UTF8String);
                    return 1;
                }
                [pipe release]; [pd release]; [lib release]; [vlib release]; [archive release];
            }
        }
        printf("test_archive: %s PASS\n", warm ? "warm (no archive misses)" : "cold (fragment + two vertex variants)");
    }
    return 0;
}

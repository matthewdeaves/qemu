/* R300 Metal pipeline archives. SPDX-License-Identifier: GPL-2.0-or-later */
#include "r300_metal_cache.h"
#include <CommonCrypto/CommonDigest.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

id<MTLBinaryArchive> r300_metal_cache_open(id<MTLDevice> dev, NSString *dir,
    const char *source, const char *vertex_source,
    const MTLPixelFormat *formats, unsigned count,
    MTLPixelFormat depth, NSURL **url, bool *loaded, NSError **error)
{
    *loaded = false;
    if (!dir || ![[NSFileManager defaultManager] createDirectoryAtPath:dir
            withIntermediateDirectories:YES attributes:nil error:error]) {
        return nil;
    }
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    /* Include stage boundaries and math policy: old fast-math archives
     * must never satisfy a safe vertex-program pipeline. */
    CC_SHA256_CTX ctx;
    CC_SHA256_Init(&ctx);
    const char version[] = "r300-vs-safe-v1";
    CC_SHA256_Update(&ctx, version, sizeof(version));
    CC_SHA256_Update(&ctx, source, (CC_LONG)strlen(source) + 1);
    if (vertex_source) {
        CC_SHA256_Update(&ctx, vertex_source, (CC_LONG)strlen(vertex_source) + 1);
    }
    CC_SHA256_Final(digest, &ctx);
    NSMutableString *key = [NSMutableString string];
    for (unsigned i = 0; i < sizeof(digest); i++) {
        [key appendFormat:@"%02x", digest[i]];
    }
    [key appendFormat:@"-%u", count];
    for (unsigned i = 0; i < count; i++) {
        [key appendFormat:@"-%lu", (unsigned long)formats[i]];
    }
    [key appendFormat:@"-%lu.bin", (unsigned long)depth];
    *url = [NSURL fileURLWithPath:[dir stringByAppendingPathComponent:key]];
    MTLBinaryArchiveDescriptor *ad = [[MTLBinaryArchiveDescriptor alloc] init];
    if ([[NSFileManager defaultManager] fileExistsAtPath:(*url).path]) {
        ad.url = *url;
    }
    id<MTLBinaryArchive> archive = [dev newBinaryArchiveWithDescriptor:ad error:error];
    *loaded = archive && ad.url;
    if (!archive && ad.url) {
        /* A stale/corrupt cache is a miss, never a rendering failure. */
        ad.url = nil;
        archive = [dev newBinaryArchiveWithDescriptor:ad error:error];
    }
    [ad release];
    return archive;
}

bool r300_metal_cache_store(id<MTLBinaryArchive> archive,
    MTLRenderPipelineDescriptor *desc, NSURL *url, NSError **error)
{
    /* Each archive contains one descriptor. Adding distinct libraries
     * with the same entry-point names to a shared archive can serialize
     * with missing vertex/fragment stages on Apple M5. */
    NSArray *saved = [desc.binaryArchives retain];
    desc.binaryArchives = nil;
    BOOL ok = [archive addRenderPipelineFunctionsWithDescriptor:desc error:error];
    desc.binaryArchives = saved;
    [saved release];
    NSURL *tmp = [NSURL fileURLWithPath:[url.path
        stringByAppendingFormat:@".tmp-%d", getpid()]];
    if (ok) {
        ok = [archive serializeToURL:tmp error:error];
    }
    if (ok) {
        ok = rename(tmp.path.fileSystemRepresentation,
                    url.path.fileSystemRepresentation) == 0;
    }
    [[NSFileManager defaultManager] removeItemAtURL:tmp error:NULL];
    return ok;
}

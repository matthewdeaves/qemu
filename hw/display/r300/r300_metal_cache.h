/* R300 Metal pipeline archives. SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef R300_METAL_CACHE_H
#define R300_METAL_CACHE_H
#import <Metal/Metal.h>
#import <Foundation/Foundation.h>

/* Returned archive is owned by the caller. URL is autoreleased. */
id<MTLBinaryArchive> r300_metal_cache_open(id<MTLDevice> dev, NSString *dir,
    const char *source, const MTLPixelFormat *formats, unsigned count,
    MTLPixelFormat depth, NSURL **url, bool *loaded, NSError **error);
bool r300_metal_cache_store(id<MTLBinaryArchive> archive,
    MTLRenderPipelineDescriptor *desc, NSURL *url, NSError **error);
#endif

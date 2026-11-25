/* Native URLSession transport for the ECHO request helper, extracted so the ObjC
 * qualification harness can drive it with an NSURLProtocol stub and no network.
 * Production custody, Keychain and signing live only in mac-request.m;  nothing here
 * touches them. */
#ifndef MAC_TRANSPORT_H
#define MAC_TRANSPORT_H

#import <Foundation/Foundation.h>
#include "mac-protocol.h"

#define MP_TRANSPORT_SESSION_SECONDS 180.0
#define MP_TRANSPORT_REQUEST_SECONDS 120.0
#define MP_TRANSPORT_CANCEL_BARRIER_SECONDS 10.0  /* reserved inside the session deadline */

/* One transport per helper session.  protocolClasses is nil in production;  the harness
 * passes its stub class so no request can physically reach the network. */
@interface MPTransport : NSObject
@property (readonly, nonatomic) NSURLSession *session;
@property (readonly, nonatomic) id delegateRef;
@property (nonatomic) struct timespec deadline;  /* CLOCK_MONOTONIC whole-session bound */
@property (nonatomic) int sessionDead;           /* uncertain timeout:  fail closed */
- (instancetype)initWithProtocolClasses:(NSArray *)protocolClasses;
- (void)shutdown;
- (int)post:(const unsigned char *)frame
     length:(size_t)length
    headers:(const mp_headers *)headers
         to:(mp_http_response *)out;
@end

/* Adapter matching the core's mp_transport_post signature;  ctx is the MPTransport. */
int mp_native_post(void *ctx, const unsigned char *frame, size_t frame_len,
                   const mp_headers *headers, mp_http_response *out);

#endif

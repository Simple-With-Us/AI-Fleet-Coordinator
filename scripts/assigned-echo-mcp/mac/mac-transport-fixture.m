/* ObjC qualification harness for mac-transport.m.  An NSURLProtocol stub intercepts
 * every request on the harness session, so nothing can physically reach the network.
 * NO Keychain, NO signing, NO production main, NO credentials:  the bearer header here
 * is a fixed synthetic marker.  Host executes on the actual Mac;  I own the source.
 *
 * Build:  clang -fobjc-arc -Wall -Wextra -Werror -framework Foundation \
 *           -DMP_TRANSPORT_TEST_HOOKS \
 *           mac-transport-fixture.m mac-transport.m -o mac-transport-fixture */
#import <Foundation/Foundation.h>
#include <stdio.h>
#include <string.h>
#include "mac-transport.h"

#ifdef MP_TRANSPORT_TEST_HOOKS
extern void (^mp_transport_test_on_redirect_refusal)(void);
#else
#error "the fixture requires -DMP_TRANSPORT_TEST_HOOKS"
#endif

#define TOKEN "sbt_echo_HARNESSSYNTHETIC000000000000000000000000000"  /* 52 chars, fake */

static int failures;
#define CHECK(name, cond) do { \
    if (!(cond)) { ++failures; printf("FAIL: %s\n", name); } \
    else printf("ok: %s\n", name); \
} while (0)

/* ------------------------------------------------------------- network stub */
typedef NS_ENUM(int, MPStubMode) {
    MPStubRespond,         /* status + headers + body, then finish */
    MPStubNeverComplete,   /* never answer;  honors stopLoading */
    MPStubFailAfterPartial,/* headers + partial body, then an NSError */
    MPStubRedirect,        /* 302 + Location;  the follow must never arrive */
};

static MPStubMode stub_mode;
static long stub_status;
static NSDictionary *stub_headers;
static NSData *stub_body;
static NSMutableArray<NSURLRequest *> *stub_requests;
static BOOL stub_redirect_followed;
static BOOL stub_saw_foreign_host;

@interface MPStubProtocol : NSURLProtocol
@end

@implementation MPStubProtocol
+ (BOOL)canInitWithRequest:(NSURLRequest *)request {
    if (![request.URL.host isEqualToString:@"agents.simplewithus.com"])
        stub_saw_foreign_host = YES;
    return YES;  /* intercept everything:  this session cannot reach the network */
}
+ (NSURLRequest *)canonicalRequestForRequest:(NSURLRequest *)request { return request; }
- (void)startLoading {
    [stub_requests addObject:self.request];
    if ([self.request.URL.path isEqualToString:@"/redirect-target"])
        stub_redirect_followed = YES;
    id<NSURLProtocolClient> client = [self client];
    switch (stub_mode) {
        case MPStubNeverComplete:
            return;  /* stopLoading ends it */
        case MPStubRespond: {
            NSHTTPURLResponse *response =
                [[NSHTTPURLResponse alloc] initWithURL:self.request.URL
                                            statusCode:stub_status
                                           HTTPVersion:@"HTTP/1.1"
                                          headerFields:stub_headers];
            [client URLProtocol:self didReceiveResponse:response
             cacheStoragePolicy:NSURLCacheStorageNotAllowed];
            if (stub_body) [client URLProtocol:self didLoadData:stub_body];
            [client URLProtocolDidFinishLoading:self];
            return;
        }
        case MPStubFailAfterPartial: {
            NSHTTPURLResponse *response =
                [[NSHTTPURLResponse alloc] initWithURL:self.request.URL
                                            statusCode:stub_status
                                           HTTPVersion:@"HTTP/1.1"
                                          headerFields:stub_headers];
            [client URLProtocol:self didReceiveResponse:response
             cacheStoragePolicy:NSURLCacheStorageNotAllowed];
            [client URLProtocol:self didLoadData:[stub_body subdataWithRange:
                NSMakeRange(0, stub_body.length / 2)]];
            [client URLProtocol:self didFailWithError:
                [NSError errorWithDomain:NSURLErrorDomain
                                    code:NSURLErrorNetworkConnectionLost userInfo:nil]];
            return;
        }
        case MPStubRedirect: {
            /* A bare 302 response does NOT exercise the redirect delegate:  the protocol
             * must announce the redirect, which routes through willPerformHTTPRedirection
             * and its nil refusal. */
            NSHTTPURLResponse *response =
                [[NSHTTPURLResponse alloc] initWithURL:self.request.URL
                                            statusCode:302
                                           HTTPVersion:@"HTTP/1.1"
                                          headerFields:@{@"Location":
                @"https://agents.simplewithus.com/redirect-target"}];
            NSURLRequest *follow =
                [NSURLRequest requestWithURL:
                    [NSURL URLWithString:@"https://agents.simplewithus.com/redirect-target"]];
            [client URLProtocol:self wasRedirectedToRequest:follow redirectResponse:response];
            [client URLProtocol:self didReceiveResponse:response
             cacheStoragePolicy:NSURLCacheStorageNotAllowed];
            [client URLProtocolDidFinishLoading:self];
            return;
        }
    }
}
- (void)stopLoading { /* cancel honored:  no further client callbacks */ }
@end

static void stub_reset(void) {
    stub_mode = MPStubRespond;
    stub_status = 200;
    stub_headers = nil;
    stub_body = nil;
    stub_requests = [NSMutableArray new];
    stub_redirect_followed = NO;
}

static MPTransport *transport;
static mp_headers headers;
static mp_http_response response;

static int post(const char *frame) {
    memset(&headers, 0, sizeof(headers));
    strcpy(headers.authorization, "Bearer " TOKEN);
    headers.session = NULL;
    headers.protocol = NULL;
    memset(&response, 0, sizeof(response));
    return mp_native_post((__bridge void *)transport,
                          (const unsigned char *)frame, strlen(frame),
                          &headers, &response);
}

static void fresh_transport(double seconds) {
    [transport shutdown];
    transport = [[MPTransport alloc] initWithProtocolClasses:@[[MPStubProtocol class]]];
    struct timespec deadline;
    clock_gettime(CLOCK_MONOTONIC, &deadline);
    deadline.tv_sec += (time_t)seconds;
    transport.deadline = deadline;
}

int main(void) {
    @autoreleasepool {
        static const char *frame =
            "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{}}";

        /* 1. lifecycle + headers:  request shape recorded, response fields populated */
        stub_reset();
        stub_headers = @{@"Content-Type": @"application/json; charset=utf-8",
                         @"Mcp-Session-Id": @"sess-xyz"};
        stub_body = [@"{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}" dataUsingEncoding:NSUTF8StringEncoding];
        fresh_transport(180.0);
        int rc = post(frame);
        CHECK("success returns 0", rc == 0);
        CHECK("status captured", response.status == 200);
        CHECK("session header captured",
              response.session_header && !strcmp(response.session_header, "sess-xyz"));
        CHECK("content type trimmed at charset",
              response.content_type && !strcmp(response.content_type, "application/json"));
        CHECK("body captured",
              response.body_len == stub_body.length &&
              !memcmp(response.body, stub_body.bytes, response.body_len));
        NSURLRequest *sent = stub_requests.lastObject;
        CHECK("exactly one request", stub_requests.count == 1);
        CHECK("POST to the fixed endpoint",
              [sent.HTTPMethod isEqualToString:@"POST"] &&
              [sent.URL.absoluteString isEqualToString:@MP_ENDPOINT]);
        CHECK("bearer header sent", [sent valueForHTTPHeaderField:@"Authorization"] &&
              [[sent valueForHTTPHeaderField:@"Authorization"] hasPrefix:@"Bearer sbt_echo_"]);
        CHECK("accept header", [[sent valueForHTTPHeaderField:@"Accept"]
              isEqualToString:@"application/json, text/event-stream"]);
        CHECK("body posted as request data",
              [[NSString alloc] initWithData:sent.HTTPBody encoding:NSUTF8StringEncoding] != nil);

        /* 2. session + protocol headers pass through when present */
        stub_reset();
        stub_headers = @{@"Content-Type": @"application/json"};
        stub_body = [@"{}" dataUsingEncoding:NSUTF8StringEncoding];
        memset(&headers, 0, sizeof(headers));
        strcpy(headers.authorization, "Bearer " TOKEN);
        headers.session = "sess-xyz";
        headers.protocol = "2025-03-26";
        memset(&response, 0, sizeof(response));
        rc = mp_native_post((__bridge void *)transport,
                            (const unsigned char *)frame, strlen(frame),
                            &headers, &response);
        NSURLRequest *second = stub_requests.lastObject;
        CHECK("session header forwarded",
              [[second valueForHTTPHeaderField:@"Mcp-Session-Id"] isEqualToString:@"sess-xyz"]);
        CHECK("protocol header forwarded",
              [[second valueForHTTPHeaderField:@"MCP-Protocol-Version"] isEqualToString:@"2025-03-26"]);

        /* 3. redirect:  the refusal callback runs and the follow is never issued */
        stub_reset();
        stub_mode = MPStubRedirect;
        __block int redirect_refusals = 0;
        mp_transport_test_on_redirect_refusal = ^{ ++redirect_refusals; };
        rc = post(frame);
        CHECK("redirect completes as failure-free 302", rc == 0 && response.status == 302);
        CHECK("redirect refusal callback observed", redirect_refusals == 1);
        CHECK("redirect never followed", !stub_redirect_followed);
        CHECK("only the original request recorded", stub_requests.count == 1);
        mp_transport_test_on_redirect_refusal = nil;

        /* 4. partial body then NSError:  failed, never a false success */
        stub_reset();
        stub_mode = MPStubFailAfterPartial;
        stub_headers = @{@"Content-Type": @"application/json"};
        stub_body = [@"{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"partial\":true}}"
                     dataUsingEncoding:NSUTF8StringEncoding];
        rc = post(frame);
        CHECK("partial-then-error is failure", rc != 0);

        /* 5. bounds:  body over MP_MAX_BYTES overflows into failure */
        stub_reset();
        {
            NSMutableData *huge = [NSMutableData dataWithLength:MP_MAX_BYTES + 2];
            stub_body = huge;
            stub_headers = @{@"Content-Type": @"application/json"};
            rc = post(frame);
            CHECK("over-cap body refused", rc != 0);
        }

        /* 6. overlong session header:  failure, not silent absence */
        stub_reset();
        {
            NSMutableString *longid = [NSMutableString string];
            for (size_t i = 0; i < MP_MAX_SESSION + 1; ++i) [longid appendString:@"x"];
            stub_headers = @{@"Content-Type": @"application/json",
                             @"Mcp-Session-Id": longid};
            stub_body = [@"{}" dataUsingEncoding:NSUTF8StringEncoding];
            rc = post(frame);
            CHECK("overlong session header refused", rc != 0);
        }

        /* 7. timeout:  never-completing stub, deadline 12s => 2s in-flight budget;
         *    session dies, cancel barrier observed, second post never reaches the stub */
        stub_reset();
        stub_mode = MPStubNeverComplete;
        fresh_transport(12.0);  /* 12s - 10s barrier reserve = 2s budget */
        NSDate *began = [NSDate date];
        rc = post(frame);
        double elapsed = -[began timeIntervalSinceNow];
        CHECK("timeout returns failure", rc != 0);
        CHECK("session marked dead", transport.sessionDead == 1);
        CHECK("bounded wait (budget + barrier, well under 12s)",
              elapsed > 1.5 && elapsed < 12.0);
        size_t count_before = stub_requests.count;
        rc = post(frame);
        CHECK("post-timeout request never issued", rc != 0 &&
              stub_requests.count == count_before);

        /* 8. deadline already expired:  no request at all */
        stub_reset();
        stub_mode = MPStubNeverComplete;
        fresh_transport(180.0);
        {
            struct timespec deadline;
            clock_gettime(CLOCK_MONOTONIC, &deadline);
            deadline.tv_sec -= 1;  /* already past */
            transport.deadline = deadline;
        }
        rc = post(frame);
        CHECK("expired deadline refuses without network", rc != 0 && stub_requests.count == 0);

        /* 9. sequential requests on one session:  state resets between them */
        stub_reset();
        fresh_transport(180.0);
        stub_headers = @{@"Content-Type": @"application/json",
                         @"Mcp-Session-Id": @"sess-a"};
        stub_body = [@"{\"n\":1}" dataUsingEncoding:NSUTF8StringEncoding];
        rc = post(frame);
        CHECK("first sequential ok", rc == 0 &&
              !strcmp(response.session_header, "sess-a"));
        stub_headers = @{@"Content-Type": @"application/json",
                         @"Mcp-Session-Id": @"sess-b"};
        stub_body = [@"{\"n\":2}" dataUsingEncoding:NSUTF8StringEncoding];
        rc = post(frame);
        CHECK("second sequential ok with fresh state", rc == 0 &&
              !strcmp(response.session_header, "sess-b") &&
              response.body_len == 7 && !memcmp(response.body, "{\"n\":2}", 7));
        CHECK("two requests recorded", stub_requests.count == 2);

        /* 10. stub isolation:  no request ever left the fixed host */
        CHECK("no foreign host contacted", !stub_saw_foreign_host);

        printf(failures ? "\n%d FAILURES\n" : "\nALL TRANSPORT HARNESS CHECKS PASS\n",
               failures);
        [transport shutdown];
        return failures ? 1 : 0;
    }
}

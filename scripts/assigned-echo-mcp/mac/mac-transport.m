#import "mac-transport.h"

/* Fixture-only instrumentation, compiled solely with -DMP_TRANSPORT_TEST_HOOKS;  never
 * present in the production helper build. */
#ifdef MP_TRANSPORT_TEST_HOOKS
void (^mp_transport_test_on_redirect_refusal)(void);
#endif

/* Per-request owned state.  The delegate routes callbacks to the current request only;
 * requests run strictly sequentially and a state is cleared only after its terminal
 * didCompleteWithError has been observed, so no late callback can touch reused memory. */
@interface MPRequestState : NSObject
@property NSMutableData *body;
@property BOOL overflow;
@property BOOL failed;
@property long status;
@property (copy) NSString *sessionId;
@property (copy) NSString *contentType;
@property dispatch_semaphore_t done;
@end

@implementation MPRequestState
@end

@interface MPDelegate : NSObject <NSURLSessionDataDelegate>
@property MPRequestState *current;
@end

@implementation MPDelegate
- (void)URLSession:(NSURLSession *)session
              task:(NSURLSessionTask *)task
willPerformHTTPRedirection:(NSHTTPURLResponse *)response
        newRequest:(NSURLRequest *)request
 completionHandler:(void (^)(NSURLRequest *))completionHandler {
    /* The fixed endpoint answers directly or the call fails:  a redirect is never followed
     * and the credential is never forwarded to one. */
    (void)session; (void)task; (void)response; (void)request;
    if (!mp_redirect_allowed()) {
        completionHandler(nil);
#ifdef MP_TRANSPORT_TEST_HOOKS
        if (mp_transport_test_on_redirect_refusal)
            mp_transport_test_on_redirect_refusal();
#endif
    }
}
- (void)URLSession:(NSURLSession *)session
          dataTask:(NSURLSessionDataTask *)dataTask
didReceiveResponse:(NSURLResponse *)response
 completionHandler:(void (^)(NSURLSessionResponseDisposition))completionHandler {
    (void)session; (void)dataTask;
    MPRequestState *state = self.current;
    if (state && [response isKindOfClass:[NSHTTPURLResponse class]]) {
        NSHTTPURLResponse *http = (NSHTTPURLResponse *)response;
        state.status = http.statusCode;
        state.sessionId = http.allHeaderFields[@"Mcp-Session-Id"];
        NSString *type = http.allHeaderFields[@"Content-Type"];
        if (type) {
            NSRange semi = [type rangeOfString:@";"];
            if (semi.location != NSNotFound) type = [type substringToIndex:semi.location];
            state.contentType = [type stringByTrimmingCharactersInSet:
                                 [NSCharacterSet whitespaceCharacterSet]];
        }
    }
    completionHandler(NSURLSessionResponseAllow);
}
- (void)URLSession:(NSURLSession *)session
          dataTask:(NSURLSessionDataTask *)dataTask
    didReceiveData:(NSData *)data {
    (void)session;
    MPRequestState *state = self.current;
    if (!state) return;
    if (state.body.length + data.length > MP_MAX_BYTES + 1) {
        state.overflow = YES;
        [dataTask cancel];
        return;
    }
    [state.body appendData:data];
}
- (void)URLSession:(NSURLSession *)session
              task:(NSURLSessionTask *)task
didCompleteWithError:(NSError *)error {
    (void)session; (void)task;
    MPRequestState *state = self.current;
    if (!state) return;
    /* A partial status/body followed by an error is a failed request, not a success. */
    if (error) state.failed = YES;
    dispatch_semaphore_signal(state.done);
}
@end

static char session_header[MP_MAX_SESSION + 1];
static char content_type[128];
static unsigned char response_body[MP_MAX_BYTES + 1];

@implementation MPTransport

- (instancetype)initWithProtocolClasses:(NSArray *)protocolClasses {
    self = [super init];
    if (!self) return nil;
    NSURLSessionConfiguration *config = [NSURLSessionConfiguration ephemeralSessionConfiguration];
    config.URLCache = nil;
    config.HTTPCookieStorage = nil;
    config.HTTPShouldSetCookies = NO;
    config.timeoutIntervalForRequest = MP_TRANSPORT_REQUEST_SECONDS;
    config.timeoutIntervalForResource = MP_TRANSPORT_REQUEST_SECONDS;
    if (protocolClasses) config.protocolClasses = protocolClasses;
    MPDelegate *delegate = [MPDelegate new];
    _session = [NSURLSession sessionWithConfiguration:config
                                             delegate:delegate
                                        delegateQueue:nil];
    _delegateRef = delegate;
    clock_gettime(CLOCK_MONOTONIC, &_deadline);
    _deadline.tv_sec += MP_TRANSPORT_SESSION_SECONDS;
    _sessionDead = 0;
    return self;
}

- (void)shutdown {
    [_session invalidateAndCancel];
}

- (double)secondsRemaining {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    double r = (double)(_deadline.tv_sec - now.tv_sec)
             + (double)(_deadline.tv_nsec - now.tv_nsec) / 1e9;
    return r < 0 ? 0 : r;
}

- (int)post:(const unsigned char *)frame
     length:(size_t)frame_len
    headers:(const mp_headers *)headers
         to:(mp_http_response *)out {
    /* After an uncertain timeout the server may have applied the request:  fail closed
     * without touching the network again. */
    if (_sessionDead) return 1;
    memset(out, 0, sizeof(*out));  /* every field deterministic even on failure */
    /* Reserve the cancel-barrier window:  request budget + barrier never exceed the
     * whole-session deadline. */
    double budget = [self secondsRemaining] - MP_TRANSPORT_CANCEL_BARRIER_SECONDS;
    if (budget > MP_TRANSPORT_REQUEST_SECONDS) budget = MP_TRANSPORT_REQUEST_SECONDS;
    if (budget <= 0) { _sessionDead = 1; return 1; }
    MPDelegate *delegate = (MPDelegate *)_delegateRef;
    @autoreleasepool {
        NSMutableURLRequest *request =
            [NSMutableURLRequest requestWithURL:[NSURL URLWithString:@MP_ENDPOINT]];
        request.HTTPMethod = @"POST";
        /* Owned copy:  the caller's scratch is wiped and reused after return. */
        request.HTTPBody = [NSData dataWithBytes:frame length:frame_len];
        [request setValue:[NSString stringWithUTF8String:headers->authorization]
            forHTTPHeaderField:@"Authorization"];
        [request setValue:@"application/json" forHTTPHeaderField:@"Content-Type"];
        [request setValue:@"application/json, text/event-stream" forHTTPHeaderField:@"Accept"];
        [request setValue:@"assigned-echo-mcp/2.0" forHTTPHeaderField:@"User-Agent"];
        if (headers->session)
            [request setValue:[NSString stringWithUTF8String:headers->session]
                forHTTPHeaderField:@"Mcp-Session-Id"];
        if (headers->protocol)
            [request setValue:[NSString stringWithUTF8String:headers->protocol]
                forHTTPHeaderField:@"MCP-Protocol-Version"];
        MPRequestState *state = [MPRequestState new];
        state.done = dispatch_semaphore_create(0);
        state.body = [NSMutableData new];
        delegate.current = state;
        NSURLSessionDataTask *task = [_session dataTaskWithRequest:request];
        [task resume];
        long rc = dispatch_semaphore_wait(
            state.done, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(budget * NSEC_PER_SEC)));
        if (rc) {
            /* Uncertain timeout:  cancel, wait (within the reserved window) for the
             * terminal callback before touching any state, and never issue another
             * request on this session. */
            [task cancel];
            _sessionDead = 1;
            long rc2 = dispatch_semaphore_wait(
                state.done,
                dispatch_time(DISPATCH_TIME_NOW,
                              (int64_t)(MP_TRANSPORT_CANCEL_BARRIER_SECONDS * NSEC_PER_SEC)));
            if (!rc2) {
                delegate.current = nil;
                mp_wipe(state.body.mutableBytes, state.body.length);
            }
            /* If the terminal callback never arrives, the state stays owned by the
             * delegate:  its callbacks may still append, so nothing is wiped or freed. */
            return 1;
        }
        delegate.current = nil;
        /* Malformed or overlong headers are a failed response, not silent absence. */
        int failed = state.failed || state.overflow || !state.status;
        if (!failed && state.sessionId &&
            [state.sessionId lengthOfBytesUsingEncoding:NSUTF8StringEncoding]
                > MP_MAX_SESSION) failed = 1;
        if (!failed && state.contentType &&
            [state.contentType lengthOfBytesUsingEncoding:NSUTF8StringEncoding]
                >= sizeof(content_type)) failed = 1;
        if (!failed) {
            out->status = state.status;
            if (state.sessionId) {
                strcpy(session_header, [state.sessionId UTF8String]);
                out->session_header = session_header;
            }
            if (state.contentType) {
                strcpy(content_type, [state.contentType UTF8String]);
                out->content_type = content_type;
            }
            size_t length = state.body.length;
            memcpy(response_body, state.body.bytes, length);
            out->body = response_body;
            out->body_len = length;
        }
        mp_wipe(state.body.mutableBytes, state.body.length);
        return failed;
    }
}

@end

int mp_native_post(void *ctx, const unsigned char *frame, size_t frame_len,
                   const mp_headers *headers, mp_http_response *out) {
    return [(__bridge MPTransport *)ctx post:frame length:frame_len headers:headers to:out];
}

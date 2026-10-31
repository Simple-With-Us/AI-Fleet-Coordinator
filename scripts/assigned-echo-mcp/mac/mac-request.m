/* ECHO-only request-owning native runtime.  The fixed signed helper reads its own fixed
 * Keychain item and performs the bounded HTTPS session itself:  the bearer exists only in
 * this process's native memory.  No subprocess, no token environment, no selectors. */
#import <Foundation/Foundation.h>
#import <Security/Security.h>
#include <mach-o/dyld.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <pwd.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <time.h>
#include <poll.h>
#include <signal.h>
#include <ctype.h>

#include "mac-transport.h"
#include "mac-input.c"

_Static_assert(MP_MAX_FILE_FRAME == MP_INPUT_MAX_FRAME,
               "input layer must carry the core's full frame cap");

#define FIXED "/usr/local/libexec/assigned-echo-mcp/mac-request"
#define SERVICE "assigned-hosted-mcp"
#define ACCOUNT "ECHO"
#define REQUIREMENT "anchor apple generic and identifier \"assigned-echo-mcp-request\" and certificate leaf[subject.OU] = \"CC8UTF7ATG\""

static int held_dirs[5] = {-1, -1, -1, -1, -1};
static int helper_fd = -1;

static int safe_fd(int fd, int directory) {
    struct stat info;
    if (fd < 0 || fstat(fd, &info) || info.st_uid != 0 || (info.st_mode & 0022)) return 0;
    return directory ? S_ISDIR(info.st_mode) : S_ISREG(info.st_mode);
}

static void close_code(void) {
    if (helper_fd >= 0) close(helper_fd);
    for (size_t i = 0; i < 5; ++i) if (held_dirs[i] >= 0) close(held_dirs[i]);
}

/* Hold every validated inode through use.  No writable or symlink ancestor. */
static int validate_self(void) {
    char executable[PATH_MAX], canonical[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (geteuid() != 501 || _NSGetExecutablePath(executable, &size) ||
        !realpath(executable, canonical) || strcmp(canonical, FIXED)) return 0;
    held_dirs[0] = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (!safe_fd(held_dirs[0], 1)) return 0;
    const char *parts[] = {"usr", "local", "libexec", "assigned-echo-mcp"};
    for (size_t i = 0; i < 4; ++i) {
        held_dirs[i + 1] = openat(held_dirs[i], parts[i], O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (!safe_fd(held_dirs[i + 1], 1)) return 0;
    }
    helper_fd = openat(held_dirs[4], "mac-request", O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (!safe_fd(helper_fd, 0)) return 0;
    SecCodeRef self = NULL;
    SecRequirementRef requirement = NULL;
    OSStatus status = SecCodeCopySelf(kSecCSDefaultFlags, &self);
    if (!status) status = SecRequirementCreateWithString(CFSTR(REQUIREMENT), kSecCSDefaultFlags, &requirement);
    if (!status) status = SecCodeCheckValidity(self, kSecCSStrictValidate, requirement);
    if (requirement) CFRelease(requirement);
    if (self) CFRelease(self);
    return status == errSecSuccess;
}

static int valid_token(const unsigned char *token, size_t size) {
    if (size != 52 || memcmp(token, "sbt_echo_", 9)) return 0;
    for (size_t i = 9; i < size; ++i)
        if (!((token[i] >= 'a' && token[i] <= 'z') || (token[i] >= 'A' && token[i] <= 'Z') ||
              (token[i] >= '0' && token[i] <= '9') || token[i] == '_' || token[i] == '-')) return 0;
    return 1;
}

/* Owner-mode ingress, preserved from the retired env-export helper's reviewed contract:
 * private stdin read, refusal of ANY existing item (even one whose ACL would deny us),
 * generic-password creation with a trusted-app ACL containing only this fixed helper
 * (SecTrustedApplicationCreateFromPath(NULL) is self), buffer cleared on every path.
 * SOURCE ONLY until owner storage/signing approval:  never run by the CLI. */
static OSStatus lookup(SecKeychainRef keychain, UInt32 *size, void **bytes,
                       SecKeychainItemRef *item) {
    return SecKeychainFindGenericPassword(keychain, (UInt32)strlen(SERVICE), SERVICE,
                                          (UInt32)strlen(ACCOUNT), ACCOUNT,
                                          size, bytes, item);
}

static int ingress(SecKeychainRef keychain) {
    SecKeychainItemRef existing = NULL;
    OSStatus status = lookup(keychain, NULL, NULL, &existing);
    if (existing) CFRelease(existing);
    if (status != errSecItemNotFound) return 3;
    unsigned char secret[257] = {0};
    size_t size = fread(secret, 1, sizeof(secret), stdin);
    if (ferror(stdin) || size > 256) { mp_wipe(secret, sizeof(secret)); return 3; }
    size_t start = 0;
    while (start < size && isspace((unsigned char)secret[start])) ++start;
    while (size > start && isspace((unsigned char)secret[size - 1])) --size;
    size -= start;
    if (start) memmove(secret, secret + start, size);
    if (!valid_token(secret, size)) { mp_wipe(secret, sizeof(secret)); return 3; }
    SecTrustedApplicationRef trusted = NULL;
    SecAccessRef access = NULL;
    status = SecTrustedApplicationCreateFromPath(NULL, &trusted);  /* self:  the fixed helper */
    if (!status) {
        const void *values[] = { trusted };
        CFArrayRef apps = CFArrayCreate(NULL, values, 1, &kCFTypeArrayCallBacks);
        status = SecAccessCreate(CFSTR("Assigned ECHO MCP bearer"), apps, &access);
        CFRelease(apps);
    }
    if (!status) {
        SecKeychainAttribute attrs[] = {
            { kSecServiceItemAttr, (UInt32)strlen(SERVICE), (void *)SERVICE },
            { kSecAccountItemAttr, (UInt32)strlen(ACCOUNT), (void *)ACCOUNT }
        };
        SecKeychainAttributeList list = { 2, attrs };
        status = SecKeychainItemCreateFromContent(kSecGenericPasswordItemClass, &list,
                                                  (UInt32)size, secret, keychain, access, NULL);
    }
    mp_wipe(secret, sizeof(secret));
    if (access) CFRelease(access);
    if (trusted) CFRelease(trusted);
    return status == errSecSuccess ? 0 : 3;
}


/* -------------------------------------------- fixed-endpoint URLSession transport */

int main(int argc, char **argv) {
    (void)argv;
    SecKeychainSetUserInteractionAllowed(false);
    /* Hard process cap:  every input read and in-flight request is bounded by the 180s
     * session deadline, but a stdout write to a stalled reader is not.  alarm() is the
     * documented whole-process bound (session + barrier reserve + shutdown slack).  An
     * inherited SIGALRM ignore disposition or signal mask would silently defeat it, so
     * both are reset first;  the isolated alarm fixture qualifies exactly this. */
    signal(SIGALRM, SIG_DFL);
    sigset_t alarms;
    sigemptyset(&alarms);
    sigaddset(&alarms, SIGALRM);
    sigprocmask(SIG_UNBLOCK, &alarms, NULL);
    alarm((unsigned)MP_TRANSPORT_SESSION_SECONDS + 15);
    if ((argc != 1 && !(argc == 2 && !strcmp(argv[1], "ingress"))) || !validate_self()) {
        fputs("mac-request: fixed signed helper validation failed\n", stderr);
        return 3;
    }
    struct passwd *owner = getpwuid(501);
    char keychain_path[PATH_MAX];
    SecKeychainRef keychain = NULL;
    if (!owner || !owner->pw_dir ||
        snprintf(keychain_path, sizeof(keychain_path),
                 "%s/Library/Keychains/login.keychain-db", owner->pw_dir) >= (int)sizeof(keychain_path) ||
        SecKeychainOpen(keychain_path, &keychain)) {
        fputs("mac-request: fixed login Keychain unavailable\n", stderr);
        close_code();
        return 3;
    }
    if (argc == 2) {  /* owner-mode ingress:  provision, then exit;  never serves requests */
        int provisioned = ingress(keychain);
        CFRelease(keychain);
        close_code();
        return provisioned;
    }
    UInt32 token_size = 0;
    void *token_bytes = NULL;
    OSStatus status = lookup(keychain, &token_size, &token_bytes, NULL);
    if (status || !valid_token(token_bytes, token_size)) {
        if (token_bytes) { mp_wipe(token_bytes, token_size); SecKeychainItemFreeContent(NULL, token_bytes); }
        CFRelease(keychain);
        close_code();
        fputs("mac-request: native custody refused or unavailable\n", stderr);
        return 3;
    }
    char token[53];
    memcpy(token, token_bytes, token_size);
    token[token_size] = 0;
    mp_wipe(token_bytes, token_size);
    SecKeychainItemFreeContent(NULL, token_bytes);
    CFRelease(keychain);

    int result = 0;
    @autoreleasepool {
        MPTransport *transport = [[MPTransport alloc] initWithProtocolClasses:nil];
        struct timespec deadline;
        mp_session state;
        memset(&state, 0, sizeof(state));
        unsigned char *frame = malloc(MP_MAX_FILE_FRAME + 2);
        unsigned char *scratch = malloc(MP_MAX_FILE_FRAME + 64);
        unsigned char *out = malloc(MP_MAX_BYTES + 4096);
        if (!transport || !frame || !scratch || !out) result = 3;
        if (!result) deadline = transport.deadline;  /* fixed at init; snapshot for framing */
        while (!result) {
            long len = mp_read_frame(STDIN_FILENO, frame, MP_MAX_FILE_FRAME,
                                     &deadline);
            if (len == -1 || len == -2) break;  /* EOF or session deadline */
            if (len == -3) {  /* oversized frame, drained:  refuse with the fixed shape */
                size_t refused = mp_line_error(out, MP_MAX_BYTES + 4096,
                                               -32600, "Input frame too large.");
                if (refused) {
                    fwrite(out, 1, refused, stdout);
                    fputc('\n', stdout);
                    fflush(stdout);
                }
                continue;
            }
            if (!len) continue;
            size_t written = mp_handle_frame(&state, token, token_size, frame, (size_t)len,
                                             mp_native_post, (__bridge void *)transport,
                                             scratch, MP_MAX_FILE_FRAME + 64,
                                             out, MP_MAX_BYTES + 4096);
            if (written) {
                fwrite(out, 1, written, stdout);
                fputc('\n', stdout);
                fflush(stdout);
            }
            mp_wipe(frame, MP_MAX_FILE_FRAME + 2);
            mp_wipe(scratch, MP_MAX_FILE_FRAME + 64);
            mp_wipe(out, MP_MAX_BYTES + 4096);
        }
        [transport shutdown];
        if (frame) { mp_wipe(frame, MP_MAX_FILE_FRAME + 2); free(frame); }
        if (scratch) { mp_wipe(scratch, MP_MAX_FILE_FRAME + 64); free(scratch); }
        if (out) { mp_wipe(out, MP_MAX_BYTES + 4096); free(out); }
    }
    mp_wipe(token, sizeof(token));
    close_code();
    return result;
}

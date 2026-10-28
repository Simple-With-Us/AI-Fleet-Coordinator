/* ECHO-only native Keychain ingress/launch. No selectors or password arguments. */
#include <Security/Security.h>
#include <CoreFoundation/CoreFoundation.h>
#include <mach-o/dyld.h>
#include <sys/stat.h>
#include <unistd.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>

#define FIXED "/usr/local/libexec/assigned-echo-mcp/assigned-echo-mcp"
#define ADAPTER "/usr/local/libexec/assigned-echo-mcp/assigned-hosted-stdio.py"
#define SERVICE "assigned-hosted-mcp"
#define ACCOUNT "ECHO"
#define REQUIREMENT "anchor apple generic and identifier \"assigned-echo-mcp\" and certificate leaf[subject.OU] = \"CC8UTF7ATG\""

static int safe_path(const char *path, int directory) {
    struct stat s;
    if (lstat(path, &s) || s.st_uid != 0 || (s.st_mode & 0022)) return 0;
    return directory ? S_ISDIR(s.st_mode) : S_ISREG(s.st_mode);
}

static int validate_self(void) {
    char executable[PATH_MAX], canonical[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (geteuid() != 501 || _NSGetExecutablePath(executable, &size) ||
        !realpath(executable, canonical) || strcmp(canonical, FIXED)) return 0;
    const char *dirs[] = {"/usr", "/usr/local", "/usr/local/libexec", "/usr/local/libexec/assigned-echo-mcp"};
    for (size_t i = 0; i < sizeof(dirs)/sizeof(dirs[0]); ++i)
        if (!safe_path(dirs[i], 1)) return 0;
    if (!safe_path(FIXED, 0) || !safe_path(ADAPTER, 0)) return 0;
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

static OSStatus open_login(SecKeychainRef *keychain) {
    struct passwd *owner = getpwuid(501);
    char path[PATH_MAX];
    if (!owner || !owner->pw_dir || snprintf(path, sizeof(path), "%s/Library/Keychains/login.keychain-db", owner->pw_dir) >= (int)sizeof(path))
        return errSecParam;
    return SecKeychainOpen(path, keychain);
}

static OSStatus lookup(SecKeychainRef keychain, UInt32 *size, void **bytes, SecKeychainItemRef *item) {
    return SecKeychainFindGenericPassword(keychain, (UInt32)strlen(SERVICE), SERVICE,
                                         (UInt32)strlen(ACCOUNT), ACCOUNT, size, bytes, item);
}

static int ingress(SecKeychainRef keychain) {
    /* Fail closed on ANY existing item, even one whose password ACL denies us. */
    SecKeychainItemRef existing = NULL;
    OSStatus status = lookup(keychain, NULL, NULL, &existing);
    if (existing) CFRelease(existing);
    if (status != errSecItemNotFound) return 3;
    unsigned char secret[256] = {0};
    size_t size = fread(secret, 1, sizeof(secret), stdin);
    if (size && secret[size - 1] == '\n') --size;
    if (!valid_token(secret, size)) { explicit_bzero(secret, sizeof(secret)); return 3; }
    SecTrustedApplicationRef trusted = NULL;
    SecAccessRef access = NULL;
    status = SecTrustedApplicationCreateFromPath(FIXED, &trusted);
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
        status = SecKeychainItemCreateFromContent(kSecGenericPasswordItemClass, &list, (UInt32)size,
                                                 secret, keychain, access, NULL);
    }
    explicit_bzero(secret, sizeof(secret));
    if (access) CFRelease(access);
    if (trusted) CFRelease(trusted);
    return status == errSecSuccess ? 0 : 3;
}

static int launch(SecKeychainRef keychain) {
    UInt32 size = 0;
    void *bytes = NULL;
    OSStatus status = lookup(keychain, &size, &bytes, NULL);
    if (status || !valid_token(bytes, size)) {
        if (bytes) SecKeychainItemFreeContent(NULL, bytes);
        return 3;
    }
    char environment[sizeof("ASSIGNED_MCP_BEARER=") + 52];
    memcpy(environment, "ASSIGNED_MCP_BEARER=", 20);
    memcpy(environment + 20, bytes, size);
    environment[20 + size] = 0;
    explicit_bzero(bytes, size);
    SecKeychainItemFreeContent(NULL, bytes);
    char *argv[] = {"/usr/bin/python3", "-I", ADAPTER, NULL};
    char *env[] = {"PATH=/usr/bin:/bin", environment, NULL};
    execve(argv[0], argv, env);
    explicit_bzero(environment, sizeof(environment));
    return 3;
}

int main(int argc, char **argv) {
    /* No Keychain dialog or new trust grant can be triggered by this helper. */
    SecKeychainSetUserInteractionAllowed(false);
    if ((argc != 1 && !(argc == 2 && !strcmp(argv[1], "ingress"))) || !validate_self()) {
        fputs("assigned-echo-mcp: fixed signed helper validation failed\n", stderr);
        return 3;
    }
    SecKeychainRef keychain = NULL;
    if (open_login(&keychain)) {
        fputs("assigned-echo-mcp: fixed login Keychain unavailable\n", stderr);
        return 3;
    }
    int result = argc == 2 ? ingress(keychain) : launch(keychain);
    CFRelease(keychain);
    if (result) fputs("assigned-echo-mcp: native custody refused or unavailable\n", stderr);
    return result;
}

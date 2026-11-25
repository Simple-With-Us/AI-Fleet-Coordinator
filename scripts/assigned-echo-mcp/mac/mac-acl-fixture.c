/* NONSECRET isolated-Keychain ACL fixture. Run only after owner permission/review.
 * Built twice as different binaries. Does not know the production service/item. */
#include <Security/Security.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define VALUE "NONSECRET-ASSIGNED-MCP-ACL-FIXTURE"
#define SERVICE "assigned-echo-mcp-nonsecret-fixture"
#define ACCOUNT "fixture-only"
int main(int argc, char **argv) {
    if (argc != 4 || (strcmp(argv[1], "create") && strcmp(argv[1], "read"))) return 2;
    SecKeychainSetUserInteractionAllowed(false);
    SecKeychainRef keychain = NULL;
    OSStatus status = SecKeychainOpen(argv[2], &keychain);
    if (status) return 3;
    if (!strcmp(argv[1], "create")) {
        SecTrustedApplicationRef app = NULL;
        SecAccessRef access = NULL;
        status = SecTrustedApplicationCreateFromPath(argv[3], &app);
        if (!status) {
            const void *values[] = { app };
            CFArrayRef apps = CFArrayCreate(NULL, values, 1, &kCFTypeArrayCallBacks);
            status = SecAccessCreate(CFSTR("NONSECRET assigned MCP fixture"), apps, &access);
            CFRelease(apps);
        }
        if (!status) {
            SecKeychainAttribute attrs[] = {
                { kSecServiceItemAttr, sizeof(SERVICE)-1, (void *)SERVICE },
                { kSecAccountItemAttr, sizeof(ACCOUNT)-1, (void *)ACCOUNT }
            };
            SecKeychainAttributeList list = { 2, attrs };
            status = SecKeychainItemCreateFromContent(kSecGenericPasswordItemClass, &list,
                       sizeof(VALUE)-1, VALUE, keychain, access, NULL);
        }
        if (access) CFRelease(access);
        if (app) CFRelease(app);
    } else {
        UInt32 size = 0; void *data = NULL;
        status = SecKeychainFindGenericPassword(keychain, sizeof(SERVICE)-1, SERVICE,
                   sizeof(ACCOUNT)-1, ACCOUNT, &size, &data, NULL);
        if (!status && (size != sizeof(VALUE)-1 || memcmp(data, VALUE, size))) status = errSecDecode;
        if (data) SecKeychainItemFreeContent(NULL, data);
    }
    CFRelease(keychain);
    /* Status only. No keychain data printed, including the nonsensitive fixture. */
    printf("fixture-status=%d\n", (int)status);
    return status ? 3 : 0;
}

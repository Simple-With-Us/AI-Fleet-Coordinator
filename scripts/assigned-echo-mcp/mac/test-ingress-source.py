#!/usr/bin/env python3
"""Nonsensitive ingress control-flow fixture. No Apple APIs, Keychain or credentials.

Compiles the exact production ingress body with fake Security functions. This proves
one create attempt, no lookup/update/delete/retry, duplicate refusal and cleanup.
It does not qualify Apple's duplicate enforcement or live Keychain custody.
"""

import pathlib
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
source = (HERE / "mac-request.m").read_text()
start = source.index("static int ingress(")
end = source.index("\n\n\n/*", start)
body = source[start:end]
for forbidden in ("lookup(", "SecKeychainFind", "SecKeychainItemModify", "SecKeychainItemDelete"):
    assert forbidden not in body, forbidden
assert body.count("SecKeychainItemCreateFromContent(") == 1
preamble = r"""
#define _POSIX_C_SOURCE 200809L
#include <unistd.h>
#include "mac-input.c"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
typedef int OSStatus;
typedef unsigned int UInt32;
typedef void *SecKeychainRef;
typedef void *SecTrustedApplicationRef;
typedef void *SecAccessRef;
typedef void *CFArrayRef;
typedef struct { int tag; UInt32 length; void *data; } SecKeychainAttribute;
typedef struct { UInt32 count; SecKeychainAttribute *attr; } SecKeychainAttributeList;
#define SERVICE "fixture-service"
#define ACCOUNT "fixture-account"
#define CFSTR(x) (x)
#define errSecSuccess 0
#define errSecDuplicateItem -25299
#define kSecServiceItemAttr 1
#define kSecAccountItemAttr 2
#define kSecGenericPasswordItemClass 3
static int kCFTypeArrayCallBacks;
static int attempts, wipes, releases;
static OSStatus outcome;
static int valid_token(const unsigned char *s, size_t n) { return n == 1 && s[0] == 'x'; }
static void mp_wipe(void *s, size_t n) { memset(s, 0, n); ++wipes; }
static OSStatus SecTrustedApplicationCreateFromPath(const char *p, void **v) { (void)p; *v=(void *)1; return 0; }
static CFArrayRef CFArrayCreate(void *a, const void **v, int n, const void *c) { (void)a;(void)v;(void)n;(void)c;return (void *)2; }
static OSStatus SecAccessCreate(const char *s, void *a, void **v) { (void)s;(void)a;*v=(void *)3;return 0; }
static void CFRelease(void *v) { (void)v;++releases; }
static OSStatus SecKeychainItemCreateFromContent(int c, void *l, UInt32 n, const void *s, void *k, void *a, void *v) {
    (void)c;(void)l;(void)n;(void)s;(void)k;(void)a;(void)v;++attempts;return outcome;
}
"""
main = r"""
int main(int argc, char **argv) {
    if (argc != 2) return 10;
    outcome = atoi(argv[1]);
    int actual = ingress(NULL);
    if (actual != (outcome == errSecSuccess ? 0 : 3)) return 11;
    if (attempts != 1 || wipes != 1 || releases != 3) return 12;
    return 0;
}
"""
def run_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        c = pathlib.Path(tmp) / "ingress.c"
        exe = pathlib.Path(tmp) / "ingress"
        c.write_text(preamble + body + main)
        subprocess.run(
            ["cc", "-std=c99", "-Wall", "-Wextra", "-Werror", "-I", str(HERE), str(c), "-o", str(exe)], check=True, timeout=30
        )
        for outcome in (0, -25299, -25293, -1):
            subprocess.run([str(exe), str(outcome)], input=b"x", check=True, timeout=10)

try:
    run_fixture()
except subprocess.TimeoutExpired:
    raise SystemExit(124)
except FileNotFoundError:
    raise SystemExit(127)

# Stalled writers must refuse within the local deadline, not wait for SIGALRM.
with tempfile.TemporaryDirectory() as tmp:
    c = pathlib.Path(tmp)/"stall.c"
    exe = pathlib.Path(tmp)/"stall"
    stall = "int main(void) { int rc=ingress(NULL); return rc==3 && attempts==0 && wipes==1 ? 0 : 1; }"
    c.write_text(preamble + body + stall)
    try:
        subprocess.run(["cc","-std=c99","-Wall","-Wextra","-Werror","-I",str(HERE),str(c),"-o",str(exe)],check=True,timeout=30)
        child=subprocess.Popen([str(exe)],stdin=subprocess.PIPE)
        try:
            child.stdin.write(b"x")
            child.stdin.flush()
            assert child.wait(timeout=7)==0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            child.stdin.close()
    except subprocess.TimeoutExpired:
        raise SystemExit(124)
    except FileNotFoundError:
        raise SystemExit(127)

print("PASS: exact ingress body, one create, duplicate/error refusal, cleanup; stubs only")

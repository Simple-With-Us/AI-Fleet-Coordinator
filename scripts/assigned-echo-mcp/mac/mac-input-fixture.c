/* Synthetic-input regressions for mac-input.c framing, including the three exact host
 * v3 probe scenarios:  over-cap mutation, drain tail loss, oversize-EOF repetition.
 * No network, no credentials;  pipes only. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include "mac-input.h"

#define MID_CAP (4u * 1024u * 1024u)  /* ordinary-frame cap, for the mid-scale drain */

static int failures;
#define CHECK(name, cond) do { \
    if (!(cond)) { ++failures; printf("FAIL: %s\n", name); } \
    else printf("ok: %s\n", name); \
} while (0)

static struct timespec future;
static void reset_deadline(void) {
    mp_input_reset();
    clock_gettime(CLOCK_MONOTONIC, &future);
    future.tv_sec += 30;
}

/* Feed input through a pipe so reads behave like the helper's stdin. */
static int feed(const unsigned char *bytes, size_t n) {
    int fds[2];
    assert(pipe(fds) == 0);
    size_t off = 0;
    while (off < n) {
        ssize_t w = write(fds[1], bytes + off, n - off);
        assert(w > 0);
        off += (size_t)w;
    }
    close(fds[1]);
    return fds[0];
}

int main(void) {
    unsigned char buf[64];

    /* Host probe scenario 1:  "{}X\n" with cap 2 must refuse, never truncate to "{}". */
    reset_deadline();
    int fd = feed((const unsigned char *)"{}X\n", 4);
    long r = mp_read_frame(fd, buf, 2, &future);
    CHECK("over-cap line refused (-3), not truncated", r == -3);
    r = mp_read_frame(fd, buf, 2, &future);
    CHECK("over-cap line then EOF (-1)", r == -1);
    close(fd);

    /* Exact cap boundary:  32-byte line with cap 32 passes whole. */
    reset_deadline();
    {
        unsigned char line[35];
        memset(line, 'a', 32);
        memcpy(line, "{\"jsonrpc\":\"2.0\",\"id\":1,\"m\":\"x\"}", 32);  /* exactly 32 */
        line[32] = '\n';
        fd = feed(line, 33);
        r = mp_read_frame(fd, buf, 32, &future);
        CHECK("exact-cap line accepted whole", r == 32 && !memcmp(buf, line, 32));
        r = mp_read_frame(fd, buf, 32, &future);
        CHECK("exact-cap then EOF", r == -1);
        close(fd);
    }
    /* Cap + 1 line refused, not truncated. */
    reset_deadline();
    {
        unsigned char line[35];
        memset(line, 'a', 33);
        line[33] = '\n';
        fd = feed(line, 34);
        r = mp_read_frame(fd, buf, 32, &future);
        CHECK("cap+1 line refused", r == -3);
        close(fd);
    }

    /* Host probe scenario 2:  drain must preserve bytes past the LF (next request). */
    reset_deadline();
    fd = feed((const unsigned char *)"discard\n{}\n", 11);
    r = mp_read_frame(fd, buf, 4, &future);  /* "discard" is over cap 4 */
    CHECK("oversize refused (-3)", r == -3);
    r = mp_read_frame(fd, buf, 4, &future);
    CHECK("next frame preserved after drain", r == 2 && !memcmp(buf, "{}", 2));
    r = mp_read_frame(fd, buf, 4, &future);
    CHECK("then EOF", r == -1);
    close(fd);

    /* Host probe scenario 3:  oversize at EOF returns -3 once, then -1, never repeats. */
    reset_deadline();
    {
        unsigned char big[40];
        memset(big, 'X', 40);
        fd = feed(big, 40);  /* no LF at all */
        r = mp_read_frame(fd, buf, 32, &future);
        CHECK("oversize EOF first -3", r == -3);
        r = mp_read_frame(fd, buf, 32, &future);
        CHECK("oversize EOF second -1 (terminal)", r == -1);
        close(fd);
    }

    /* Ordinary EOF without LF returns the final line once. */
    reset_deadline();
    fd = feed((const unsigned char *)"{\"ok\":1}", 8);
    r = mp_read_frame(fd, buf, 32, &future);
    CHECK("final line without LF returned", r == 8 && !memcmp(buf, "{\"ok\":1}", 8));
    r = mp_read_frame(fd, buf, 32, &future);
    CHECK("then EOF", r == -1);
    close(fd);

    /* CRLF framing trims the CR. */
    reset_deadline();
    fd = feed((const unsigned char *)"{}\r\n{}\n", 7);
    r = mp_read_frame(fd, buf, 32, &future);
    CHECK("CRLF trimmed", r == 2 && !memcmp(buf, "{}", 2));
    r = mp_read_frame(fd, buf, 32, &future);
    CHECK("next after CRLF", r == 2);
    close(fd);

    /* Expired deadline reports -2 even with input pending. */
    mp_input_reset();
    {
        struct timespec past;
        clock_gettime(CLOCK_MONOTONIC, &past);
        past.tv_sec -= 1;
        fd = feed((const unsigned char *)"{}\n", 3);
        r = mp_read_frame(fd, buf, 32, &past);
        CHECK("expired deadline -2", r == -2);
        close(fd);
    }

    /* Full 4MiB no-LF overflow through the real drain:  tail after the LF survives. */
    reset_deadline();
    {
        int fds[2];
        assert(pipe(fds) == 0);
        pid_t pid = fork();
        assert(pid >= 0);
        if (!pid) {
            close(fds[0]);
            static unsigned char chunk[65536];
            memset(chunk, 'X', sizeof(chunk));
            size_t total = MID_CAP + 100;  /* no LF:  forces the drain */
            for (size_t off = 0; off < total;) {
                size_t want = total - off < sizeof(chunk) ? total - off : sizeof(chunk);
                ssize_t w = write(fds[1], chunk, want);
                if (w <= 0) _exit(1);
                off += (size_t)w;
            }
            if (write(fds[1], "\n{}\n", 4) != 4) _exit(1);
            close(fds[1]);
            _exit(0);
        }
        close(fds[1]);
        unsigned char *big = malloc(MP_INPUT_MAX_FRAME + 2);
        assert(big);
        r = mp_read_frame(fds[0], big, MID_CAP, &future);
        CHECK("4MiB overflow refused (-3)", r == -3);
        r = mp_read_frame(fds[0], big, MID_CAP, &future);
        CHECK("post-drain tail preserved", r == 2 && !memcmp(big, "{}", 2));
        r = mp_read_frame(fds[0], big, MID_CAP, &future);
        CHECK("post-drain EOF", r == -1);
        free(big);
        close(fds[0]);
        int status;
        waitpid(pid, &status, 0);
    }

    /* Production cap:  a maximal valid file frame (MP_INPUT_MAX_FRAME bytes + LF) is
     * accepted whole, never drained;  cap+1 refuses. */
    mp_input_reset();
    {
        int fds[2];
        assert(pipe(fds) == 0);
        pid_t pid = fork();
        assert(pid >= 0);
        if (!pid) {
            close(fds[0]);
            static unsigned char chunk[65536];
            memset(chunk, 'a', sizeof(chunk));
            memcpy(chunk, "{\"jsonrpc\":\"2.0\",\"id\":1,", 22);
            size_t total = MP_INPUT_MAX_FRAME;
            for (size_t off = 0; off < total;) {
                size_t want = total - off < sizeof(chunk) ? total - off : sizeof(chunk);
                ssize_t w = write(fds[1], chunk, want);
                if (w <= 0) _exit(1);
                off += (size_t)w;
            }
            if (write(fds[1], "\n", 1) != 1) _exit(1);
            close(fds[1]);
            _exit(0);
        }
        close(fds[1]);
        unsigned char *big = malloc(MP_INPUT_MAX_FRAME + 2);
        assert(big);
        r = mp_read_frame(fds[0], big, MP_INPUT_MAX_FRAME, &future);
        CHECK("production-cap frame accepted whole", r == (long)MP_INPUT_MAX_FRAME);
        r = mp_read_frame(fds[0], big, MP_INPUT_MAX_FRAME, &future);
        CHECK("production-cap then EOF", r == -1);
        free(big);
        close(fds[0]);
        int status;
        waitpid(pid, &status, 0);
    }
    mp_input_reset();
    {
        int fds[2];
        assert(pipe(fds) == 0);
        pid_t pid = fork();
        assert(pid >= 0);
        if (!pid) {
            close(fds[0]);
            static unsigned char chunk[65536];
            memset(chunk, 'a', sizeof(chunk));
            size_t total = MP_INPUT_MAX_FRAME + 1;  /* one byte over */
            for (size_t off = 0; off < total;) {
                size_t want = total - off < sizeof(chunk) ? total - off : sizeof(chunk);
                ssize_t w = write(fds[1], chunk, want);
                if (w <= 0) _exit(1);
                off += (size_t)w;
            }
            if (write(fds[1], "\n", 1) != 1) _exit(1);
            close(fds[1]);
            _exit(0);
        }
        close(fds[1]);
        unsigned char *big = malloc(MP_INPUT_MAX_FRAME + 2);
        assert(big);
        r = mp_read_frame(fds[0], big, MP_INPUT_MAX_FRAME, &future);
        CHECK("production cap+1 refused", r == -3);
        r = mp_read_frame(fds[0], big, MP_INPUT_MAX_FRAME, &future);
        CHECK("production cap+1 then EOF", r == -1);
        free(big);
        close(fds[0]);
        int status;
        waitpid(pid, &status, 0);
    }

    printf(failures ? "\n%d FAILURES\n" : "\nALL INPUT FIXTURE CHECKS PASS\n", failures);
    return failures ? 1 : 0;
}

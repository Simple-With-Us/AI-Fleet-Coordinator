#define _POSIX_C_SOURCE 200809L
#include "mac-input.h"

#include <poll.h>
#include <string.h>
#include <unistd.h>

#define MP_INPUT_CAP (MP_INPUT_MAX_FRAME + 2u)  /* production frame cap + LF + CR */

static unsigned char mp_carry[MP_INPUT_CAP];
static size_t mp_carry_len;
static int mp_input_eof;  /* set once a drain or read observed EOF:  terminal */

static double mp_deadline_remaining(const struct timespec *deadline) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    double r = (double)(deadline->tv_sec - now.tv_sec)
             + (double)(deadline->tv_nsec - now.tv_nsec) / 1e9;
    return r < 0 ? 0 : r;
}

static int mp_fd_readable(int fd, const struct timespec *deadline) {
    double seconds = mp_deadline_remaining(deadline);
    if (seconds <= 0) return 0;
    struct pollfd p;
    p.fd = fd;
    p.events = POLLIN;
    p.revents = 0;
    if (poll(&p, 1, (int)(seconds * 1000.0)) != 1) return 0;
    return (p.revents & (POLLIN | POLLHUP)) != 0;
}

/* Discard bytes through the next LF within the deadline;  keep any bytes past that LF
 * for the following frame.  EOF is terminal.  Returns 1 when the oversized line was
 * fully drained (its LF seen, or EOF made it terminal), 0 when the deadline or a read
 * error cut the drain short with the stream still mid-line. */
static int mp_drain_oversize(int fd, const struct timespec *deadline) {
    mp_carry_len = 0;
    while (!mp_input_eof) {
        if (!mp_fd_readable(fd, deadline)) return 0;
        unsigned char chunk[4096];
        ssize_t got = read(fd, chunk, sizeof(chunk));
        if (got < 0) return 0;
        if (got == 0) { mp_input_eof = 1; return 1; }
        for (ssize_t i = 0; i < got; ++i) {
            if (chunk[i] == '\n') {
                /* Preserve the tail:  it is the start of the next frame. */
                size_t tail = (size_t)(got - i - 1);
                if (tail > sizeof(mp_carry)) tail = sizeof(mp_carry);
                memcpy(mp_carry, chunk + i + 1, tail);
                mp_carry_len = tail;
                return 1;
            }
        }
    }
    return 1;  /* EOF already observed:  nothing left mid-line */
}

void mp_input_reset(void) {
    mp_carry_len = 0;
    mp_input_eof = 0;
}

long mp_read_frame(int fd, unsigned char *buf, size_t cap,
                   const struct timespec *deadline) {
    if (mp_input_eof && !mp_carry_len) return -1;
    for (;;) {
        for (size_t i = 0; i < mp_carry_len; ++i) {
            if (mp_carry[i] == '\n') {
                size_t len = i;
                if (len && mp_carry[len - 1] == '\r') --len;  /* CRLF:  the CR is framing */
                if (len > cap) {  /* line over the limit:  refuse whole, never truncate */
                    size_t rest = mp_carry_len - (i + 1);
                    memmove(mp_carry, mp_carry + i + 1, rest);
                    mp_carry_len = rest;
                    return -3;
                }
                memcpy(buf, mp_carry, len);
                size_t rest = mp_carry_len - (i + 1);
                memmove(mp_carry, mp_carry + i + 1, rest);
                mp_carry_len = rest;
                return (long)len;
            }
        }
        if (mp_input_eof) {
            /* Final line without LF:  within the limit it is a frame, over it a refusal. */
            if (!mp_carry_len) return -1;
            if (mp_carry_len > cap) { mp_carry_len = 0; return -3; }
            memcpy(buf, mp_carry, mp_carry_len);
            size_t len = mp_carry_len;
            mp_carry_len = 0;
            return (long)len;
        }
        if (mp_carry_len >= sizeof(mp_carry)) {  /* full, no LF:  oversized */
            /* A drain cut short leaves the stream mid-line:  report the deadline (-2),
             * never "fully drained" (-3), so the remainder is not reframed. */
            if (!mp_drain_oversize(fd, deadline)) return -2;
            return -3;
        }
        if (!mp_fd_readable(fd, deadline)) return -2;
        ssize_t got = read(fd, mp_carry + mp_carry_len, sizeof(mp_carry) - mp_carry_len);
        if (got < 0) return -2;
        if (got == 0) mp_input_eof = 1;
        else mp_carry_len += (size_t)got;
    }
}

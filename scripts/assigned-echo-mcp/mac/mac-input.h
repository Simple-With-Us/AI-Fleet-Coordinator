/* Deadline-aware bounded stdin framing for the ECHO request helper.  Pure C99, no
 * Objective-C, so the exact-limit behavior is fixture-testable on any POSIX toolchain. */
#ifndef MAC_INPUT_H
#define MAC_INPUT_H

#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif

#include <stddef.h>
#include <time.h>

/* Maximum frame this layer can carry:  must equal the protocol core's
 * MP_MAX_FILE_FRAME (file-verb exception included).  The wrapper asserts equality at
 * compile time where both headers are visible. */
#define MP_INPUT_MAX_FRAME (6u * 1024u * 1024u + 64u * 1024u)

/* Read one LF-terminated frame from fd into buf (at most cap bytes;  a trailing CR is
 * stripped).  Returns the frame length, -1 at EOF, -2 when the CLOCK_MONOTONIC deadline
 * expired first, -3 for an oversized frame (line fully drained, any bytes past its LF
 * preserved for the next call).  A final line without LF at EOF is returned as-is;  an
 * oversized final line at EOF is -3 once, then EOF. */
long mp_read_frame(int fd, unsigned char *buf, size_t cap,
                   const struct timespec *deadline);

/* Clear all framing state (test isolation;  the helper runs one session per process). */
void mp_input_reset(void);

#endif

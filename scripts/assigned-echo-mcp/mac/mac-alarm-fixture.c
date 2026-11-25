/* Isolated nonsensitive qualification for the wrapper's hard process cap:  this binary
 * performs the exact reset/unblock/alarm sequence mac-request.m uses, then parks.
 * The driver execs it with SIGALRM ignored and blocked, the states that would silently
 * defeat alarm().  Expected:  the child still dies with SIGALRM.  No Keychain, no
 * network, no credentials, no production code paths. */
#include <signal.h>
#include <unistd.h>

int main(void) {
    signal(SIGALRM, SIG_DFL);
    sigset_t alarms;
    sigemptyset(&alarms);
    sigaddset(&alarms, SIGALRM);
    sigprocmask(SIG_UNBLOCK, &alarms, NULL);
    alarm(1);
    for (;;) pause();
}

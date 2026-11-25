/* Driver for the alarm fixture:  runs the fixture with SIGALRM first IGNORED, then
 * BLOCKED, and requires the child to die with SIGALRM both times within 5s. */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>

static int run_case(int ignore, int block, const char *fixture_path) {
    pid_t pid = fork();
    if (pid < 0) return 0;
    if (!pid) {
        if (ignore) signal(SIGALRM, SIG_IGN);
        if (block) {
            sigset_t set;
            sigemptyset(&set);
            sigaddset(&set, SIGALRM);
            sigprocmask(SIG_BLOCK, &set, NULL);
        }
        execl(fixture_path, fixture_path, (char *)NULL);
        _exit(127);
    }
    for (int i = 0; i < 50; ++i) {  /* 5s guard:  a defeated alarm must fail, not hang */
        int status;
        pid_t got = waitpid(pid, &status, WNOHANG);
        if (got == pid) {
            return WIFSIGNALED(status) && WTERMSIG(status) == SIGALRM;
        }
        usleep(100000);
    }
    kill(pid, SIGKILL);
    waitpid(pid, NULL, 0);
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s /path/to/mac-alarm-fixture\n", argv[0]);
        return 2;
    }
    int failures = 0;
    if (run_case(1, 0, argv[1])) printf("ok: inherited SIG_IGN defeated, SIGALRM kills\n");
    else { ++failures; printf("FAIL: inherited ignore survived\n"); }
    if (run_case(0, 1, argv[1])) printf("ok: inherited blocked mask defeated, SIGALRM kills\n");
    else { ++failures; printf("FAIL: inherited block survived\n"); }
    if (run_case(1, 1, argv[1])) printf("ok: ignore+block both defeated, SIGALRM kills\n");
    else { ++failures; printf("FAIL: ignore+block survived\n"); }
    printf(failures ? "\n%d FAILURES\n" : "\nALL ALARM FIXTURE CHECKS PASS\n", failures);
    return failures ? 1 : 0;
}

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>
#include "framefx.h"

static volatile sig_atomic_t interrupted;

static void interrupt_wait(int signal_number)
{
    interrupted = signal_number;
}

static void check(int condition, const char *operation)
{
    if (!condition) {
        fprintf(stderr, "FRAMEFX_LIFECYCLE_FAIL %s: %s\n", operation, strerror(errno));
        exit(1);
    }
}

int main(void)
{
    int descriptor = open("/dev/framefx0", O_RDWR | O_CLOEXEC);
    check(descriptor >= 0, "open");
    struct framefx_buffers buffers = {.source_bytes = 640 * 480, .destination_bytes = 640 * 480};
    check(!ioctl(descriptor, FRAMEFX_ALLOC_BUFS, &buffers), "allocate");
    uint8_t *source = mmap(NULL, buffers.source_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, buffers.source_offset);
    uint8_t *destination = mmap(NULL, buffers.destination_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, buffers.destination_offset);
    check(source != MAP_FAILED && destination != MAP_FAILED, "mmap");
    check(mprotect(source, buffers.source_bytes, PROT_READ | PROT_EXEC) == -1 && errno == EACCES, "reject executable remap");
    check(syscall(SYS_delete_module, "framefx", O_NONBLOCK) == -1 && errno == EWOULDBLOCK, "reject unload while mapped");
    pid_t child = fork();
    check(child >= 0, "fork");
    if (!child) {
        unsigned char residency;
        _exit(mincore(source, 4096, &residency) == -1 && errno == ENOMEM ? 0 : 1);
    }
    int child_status;
    check(waitpid(child, &child_status, 0) == child && WIFEXITED(child_status) && !WEXITSTATUS(child_status), "fork must not inherit DMA mapping");
    memset(source, 0x91, buffers.source_bytes);
    memset(destination, 0x5a, buffers.destination_bytes);
    struct framefx_submit job = {.width = 640, .height = 480, .source_stride = 640, .destination_stride = 640, .mode = 3};
    check(!ioctl(descriptor, FRAMEFX_SUBMIT, &job), "submit");
    check(ioctl(descriptor, FRAMEFX_RESET, 0) == -1 && errno == EBUSY, "reject active reset");
    struct framefx_wait waiting = {.job_id = job.job_id, .timeout_ms = 1};
    check(ioctl(descriptor, FRAMEFX_WAIT, &waiting) == -1 && errno == ETIMEDOUT, "bounded wait timeout");
    struct sigaction action = {.sa_handler = interrupt_wait};
    sigemptyset(&action.sa_mask);
    check(!sigaction(SIGALRM, &action, NULL), "signal handler");
    struct itimerval timer = {.it_value = {.tv_usec = 5000}};
    check(!setitimer(ITIMER_REAL, &timer, NULL), "interrupt timer");
    waiting.timeout_ms = FRAMEFX_MAX_WAIT_MS;
    check(ioctl(descriptor, FRAMEFX_WAIT, &waiting) == -1 && errno == EINTR && interrupted == SIGALRM, "interruptible wait");
    check(!close(descriptor), "close active mapped fd");
    check(open("/dev/framefx0", O_RDWR) == -1 && errno == EBUSY, "active mapping retains owner");
    check(!munmap(source, buffers.source_bytes), "unmap active source");
    check(!munmap(destination, buffers.destination_bytes), "last unmap waits for DMA idle");
    descriptor = open("/dev/framefx0", O_RDWR | O_CLOEXEC);
    check(descriptor >= 0, "reopen after active close");
    waiting = (struct framefx_wait){.job_id = job.job_id, .timeout_ms = 1};
    check(ioctl(descriptor, FRAMEFX_WAIT, &waiting) == -1 && errno == ESTALE, "new owner must not inherit old result");
    buffers = (struct framefx_buffers){.source_bytes = 640 * 480, .destination_bytes = 640 * 480};
    check(!ioctl(descriptor, FRAMEFX_ALLOC_BUFS, &buffers), "reallocate after active close");
    check(!close(descriptor), "release replacement buffers");
    puts("FRAMEFX_ACTIVE_LIFETIME_PASS");
    return 0;
}

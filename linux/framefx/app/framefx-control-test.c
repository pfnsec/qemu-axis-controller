#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>
#include "framefx.h"

static void check(int condition, const char *operation)
{
    if (!condition) {
        fprintf(stderr, "FRAMEFX_CONTROL_FAIL %s: %s\n", operation, strerror(errno));
        exit(1);
    }
}

int main(int argc, char **argv)
{
    check(argc == 2, "usage: framefx-control-test caps|submit|reset|wait");
    int descriptor = open("/dev/framefx0", O_RDWR | O_CLOEXEC);
    check(descriptor >= 0, "open");
    struct framefx_buffers buffers = {.source_bytes = 8192, .destination_bytes = 8192};
    check(!ioctl(descriptor, FRAMEFX_ALLOC_BUFS, &buffers), "allocate");
    struct framefx_submit job = {.width = 65, .height = 9, .source_stride = 128, .destination_stride = 192};
    struct framefx_caps caps;
    struct timespec before, after;
    check(!clock_gettime(CLOCK_MONOTONIC, &before), "start time");
    int result;
    if (!strcmp(argv[1], "caps")) result = ioctl(descriptor, FRAMEFX_QUERY_CAPS, &caps);
    else if (!strcmp(argv[1], "submit")) result = ioctl(descriptor, FRAMEFX_SUBMIT, &job);
    else if (!strcmp(argv[1], "reset")) result = ioctl(descriptor, FRAMEFX_RESET, 0);
    else {
        check(!strcmp(argv[1], "wait"), "unknown control scenario");
        result = ioctl(descriptor, FRAMEFX_SUBMIT, &job);
        struct framefx_wait waiting = {.job_id = job.job_id, .timeout_ms = 2000};
        if (!result) result = ioctl(descriptor, FRAMEFX_WAIT, &waiting);
    }
    check(result == -1 && (errno == EIO || (!strcmp(argv[1], "wait") && errno == ENODEV)),
          "fault must return EIO or already-offline ENODEV, never success");
    check(!clock_gettime(CLOCK_MONOTONIC, &after), "end time");
    check(after.tv_sec - before.tv_sec < 5, "prompt control failure");
    check(ioctl(descriptor, FRAMEFX_QUERY_CAPS, &caps) == -1 && errno == ENODEV, "offline state must latch");
    check(!close(descriptor), "close failed owner");
    check(open("/dev/framefx0", O_RDWR) == -1 && errno == ENODEV, "offline reopen rejection");
    printf("FRAMEFX_CONTROL_PASS operation=%s\n", argv[1]);
    return 0;
}

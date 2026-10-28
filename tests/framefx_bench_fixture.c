#define main benchmark_main
#include "../linux/framefx/app/framefx-bench.c"
#undef main
#include <stdarg.h>

static uint8_t *fixture_source, *fixture_destination;
static struct framefx_wait fixture_result;
static unsigned fixture_jobs;

int __real_open(const char *path, int flags, ...);
int __real_close(int descriptor);
void *__real_mmap(void *address, size_t length, int protection, int flags, int descriptor, off_t offset);
int __real_munmap(void *address, size_t length);
ssize_t __real_write(int descriptor, const void *buffer, size_t length);

ssize_t __wrap_write(int descriptor, const void *buffer, size_t length)
{
    require(buffer != fixture_source && buffer != fixture_destination, "DMA exports must use normal RAM staging");
    return __real_write(descriptor, buffer, length > 17 ? 17 : length);
}

int __wrap_open(const char *path, int flags, ...)
{
    if (!strcmp(path, "/dev/framefx0")) return 10000;
    va_list arguments;
    va_start(arguments, flags);
    mode_t mode = flags & O_CREAT ? va_arg(arguments, int) : 0;
    va_end(arguments);
    return __real_open(path, flags, mode);
}

int __wrap_close(int descriptor)
{
    return descriptor == 10000 ? 0 : __real_close(descriptor);
}

void *__wrap_mmap(void *address, size_t length, int protection, int flags, int descriptor, off_t offset)
{
    if (descriptor != 10000) return __real_mmap(address, length, protection, flags, descriptor, offset);
    uint8_t *buffer = calloc(1, length);
    if (offset) fixture_destination = buffer;
    else fixture_source = buffer;
    return buffer ? buffer : MAP_FAILED;
}

int __wrap_munmap(void *address, size_t length)
{
    if (address == fixture_source || address == fixture_destination) {
        free(address);
        return 0;
    }
    return __real_munmap(address, length);
}

int __wrap_ioctl(int descriptor, unsigned long command, ...)
{
    require(descriptor == 10000, "fixture descriptor");
    va_list arguments;
    va_start(arguments, command);
    void *argument = va_arg(arguments, void *);
    va_end(arguments);
    if (command == FRAMEFX_QUERY_CAPS) {
        memcpy(argument, &expected_caps, sizeof(expected_caps));
    } else if (command == FRAMEFX_ALLOC_BUFS) {
        struct framefx_buffers *buffers = argument;
        buffers->destination_offset = FRAMEFX_DESTINATION_OFFSET;
    } else if (command == FRAMEFX_SUBMIT) {
        struct framefx_submit *job = argument;
        fixture_jobs++;
        job->job_id = fixture_jobs;
        for (unsigned row = 0; row < job->height; row++)
            for (unsigned column = 0; column < job->width; column++)
                fixture_destination[row * job->destination_stride + column] =
                    golden(fixture_source, job->width, job->height, job->source_stride,
                           row, column, job->mode, job->threshold);
        fixture_result = (struct framefx_wait){.job_id = job->job_id, .status = 2, .cycles = 1000,
            .bytes_read = ((job->width + 7) & ~7U) * job->height,
            .bytes_written = job->width * job->height, .irq_count = fixture_jobs};
        const char *fault = getenv("FRAMEFX_BENCH_FIXTURE_FAULT");
        if (fault && !strcmp(fault, "guard")) fixture_destination[job->width] ^= 1;
        if (fault && !strcmp(fault, "counter")) fixture_result.bytes_written++;
    } else if (command == FRAMEFX_WAIT) {
        if (getenv("FRAMEFX_BENCH_FIXTURE_FAULT") && !strcmp(getenv("FRAMEFX_BENCH_FIXTURE_FAULT"), "wait")) {
            errno = ETIMEDOUT;
            return -1;
        }
        memcpy(argument, &fixture_result, sizeof(fixture_result));
    } else {
        require(0, "fixture unexpected ioctl (no reset/recovery)");
    }
    return 0;
}

int main(int argc, char **argv)
{
    return benchmark_main(argc, argv);
}

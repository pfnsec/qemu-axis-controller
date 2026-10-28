#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include "framefx.h"
#include "framefx-reference.h"

_Static_assert(sizeof(struct framefx_caps) == 32, "caps ABI");
_Static_assert(sizeof(struct framefx_buffers) == 32, "buffers ABI");
_Static_assert(sizeof(struct framefx_submit) == 40, "submit ABI");
_Static_assert(sizeof(struct framefx_wait) == 48, "wait ABI");
_Static_assert(offsetof(struct framefx_wait, cycles) == 24, "cycles ABI");
_Static_assert(offsetof(struct framefx_wait, irq_count) == 40, "irq ABI");

static void check(int condition, const char *operation)
{
    if (!condition) {
        fprintf(stderr, "FRAMEFX_FAIL %s: %s\n", operation, strerror(errno));
        exit(1);
    }
}

static unsigned number(const char *value, unsigned maximum)
{
    char *end;
    unsigned long parsed;
    errno = 0;
    parsed = strtoul(value, &end, 0);
    check(!errno && *value && !*end && parsed <= maximum, "numeric argument");
    return parsed;
}

static FILE *output_file(const char *directory, const char *kind, unsigned index, const char *extension)
{
    char filename[1024];
    int length = snprintf(filename, sizeof(filename), "%s/%s-%u.%s", directory, kind, index, extension);
    check(length > 0 && (size_t)length < sizeof(filename), "output path length");
    FILE *stream = fopen(filename, "wb");
    check(stream != NULL, "open output");
    return stream;
}

static void save_image(const char *directory, const char *kind, unsigned index, const uint8_t *pixels,
                       unsigned width, unsigned height, unsigned stride)
{
    FILE *stream = output_file(directory, kind, index, "pgm");
    check(fprintf(stream, "P5\n%u %u\n255\n", width, height) > 0, "PGM header");
    for (unsigned row = 0; row < height; row++)
        check(fwrite(pixels + row * stride, 1, width, stream) == width, "PGM pixels");
    check(!fclose(stream), "close PGM");
}

static void save_buffer(const char *directory, const char *kind, unsigned index, const uint8_t *bytes, unsigned size)
{
    FILE *stream = output_file(directory, kind, index, "bin");
    uint8_t staging[4096];
    for (unsigned offset = 0; offset < size;) {
        unsigned count = size - offset < sizeof(staging) ? size - offset : sizeof(staging);
        memcpy(staging, bytes + offset, count);
        check(fwrite(staging, 1, count, stream) == count, "buffer output");
        offset += count;
    }
    check(!fclose(stream), "close buffer");
}

static void expected_error(int result, int expected, const char *operation)
{
    check(result == -1 && errno == expected, operation);
}

static void negative_tests(int descriptor, struct framefx_buffers buffers, struct framefx_submit job)
{
    struct framefx_wait waiting = {.job_id = UINT64_MAX, .timeout_ms = 1};
    expected_error(open("/dev/framefx0", O_RDWR), EBUSY, "exclusive open");
    expected_error(ioctl(descriptor, _IO('F', 63), 0), ENOTTY, "unknown ioctl");
    expected_error(ioctl(descriptor, FRAMEFX_QUERY_CAPS, NULL), EFAULT, "bad user pointer");
    expected_error(ioctl(descriptor, FRAMEFX_ALLOC_BUFS, &buffers), EBUSY, "allocate twice");
    expected_error(ioctl(descriptor, FRAMEFX_WAIT, &waiting), ESTALE, "stale job");
    waiting.timeout_ms = 0;
    expected_error(ioctl(descriptor, FRAMEFX_WAIT, &waiting), EINVAL, "zero timeout");
    waiting.timeout_ms = FRAMEFX_MAX_WAIT_MS + 1;
    expected_error(ioctl(descriptor, FRAMEFX_WAIT, &waiting), EINVAL, "unbounded timeout");
    check(mmap(NULL, buffers.source_bytes, PROT_READ, MAP_SHARED, descriptor, 4096) == MAP_FAILED && errno == EINVAL,
          "invalid mmap offset");
    check(mmap(NULL, buffers.source_bytes, PROT_READ, MAP_PRIVATE, descriptor, 0) == MAP_FAILED && errno == EINVAL,
          "private mmap");
    check(mmap(NULL, buffers.source_bytes + 4096, PROT_READ, MAP_SHARED, descriptor, 0) == MAP_FAILED && errno == EINVAL,
          "oversized mmap");
    check(mmap(NULL, buffers.source_bytes, PROT_READ | PROT_EXEC, MAP_SHARED, descriptor, 0) == MAP_FAILED && errno == EINVAL,
          "executable mmap");
    struct framefx_submit bad = job;
    bad.width = 0;
    expected_error(ioctl(descriptor, FRAMEFX_SUBMIT, &bad), EINVAL, "zero width");
    bad = job; bad.source_stride++;
    expected_error(ioctl(descriptor, FRAMEFX_SUBMIT, &bad), EINVAL, "unaligned stride");
    bad = job; bad.destination_stride = UINT32_MAX & ~63U;
    expected_error(ioctl(descriptor, FRAMEFX_SUBMIT, &bad), EINVAL, "extent overflow");
    bad = job; bad.reserved[1] = 1;
    expected_error(ioctl(descriptor, FRAMEFX_SUBMIT, &bad), EINVAL, "reserved submit");
    bad = job; bad.mode = 4;
    expected_error(ioctl(descriptor, FRAMEFX_SUBMIT, &bad), EINVAL, "invalid mode");
    expected_error(ioctl(descriptor, FRAMEFX_RESET, 1), EINVAL, "reset argument");
    check(!ioctl(descriptor, FRAMEFX_RESET, 0), "idle reset");
    puts("FRAMEFX_NEGATIVE_PASS");
}

static void allocation_tests(int descriptor)
{
    struct framefx_buffers invalid = {0};
    expected_error(ioctl(descriptor, FRAMEFX_ALLOC_BUFS, &invalid), EINVAL, "empty allocation");
    invalid.source_bytes = FRAMEFX_MAX_BUFFER + 1;
    invalid.destination_bytes = 4096;
    expected_error(ioctl(descriptor, FRAMEFX_ALLOC_BUFS, &invalid), EINVAL, "oversized allocation");
    invalid.source_bytes = 4096;
    invalid.reserved[0] = 1;
    expected_error(ioctl(descriptor, FRAMEFX_ALLOC_BUFS, &invalid), EINVAL, "reserved allocation");
    struct framefx_buffers *readonly = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    check(readonly != MAP_FAILED, "allocate readonly request page");
    *readonly = (struct framefx_buffers){.source_bytes = 4096, .destination_bytes = 4096};
    check(!mprotect(readonly, 4096, PROT_READ), "protect allocation request");
    expected_error(ioctl(descriptor, FRAMEFX_ALLOC_BUFS, readonly), EFAULT, "allocation copyout rollback");
    check(!munmap(readonly, 4096), "unmap allocation request");
}

static void mapping_lifetime(int descriptor, uint8_t *source, uint8_t *destination, struct framefx_buffers buffers)
{
    check(!close(descriptor), "close mapped descriptor");
    expected_error(open("/dev/framefx0", O_RDWR), EBUSY, "mapping retains ownership");
    source[0] ^= 0xff;
    destination[buffers.destination_bytes - 1] ^= 0xff;
    check(!munmap(source, buffers.source_bytes), "unmap source");
    expected_error(open("/dev/framefx0", O_RDWR), EBUSY, "second mapping retains ownership");
    check(!munmap(destination, buffers.destination_bytes), "unmap destination");
    descriptor = open("/dev/framefx0", O_RDWR);
    check(descriptor >= 0, "reopen after final unmap");
    check(!close(descriptor), "close reopened descriptor");
    puts("FRAMEFX_LIFETIME_PASS");
}

int main(int argc, char **argv)
{
    unsigned width = 320, height = 240, seed = 1, repeat = 1, mode = 4, threshold = 127;
    int selftest = 0;
    unsigned expected_error_code = 0;
    unsigned timeout_ms = FRAMEFX_MAX_WAIT_MS;
    const char *directory = ".";
    const struct option options[] = {
        {"width", required_argument, NULL, 'w'}, {"height", required_argument, NULL, 'h'},
        {"seed", required_argument, NULL, 's'}, {"repeat", required_argument, NULL, 'r'},
        {"mode", required_argument, NULL, 'm'}, {"threshold", required_argument, NULL, 't'},
        {"output", required_argument, NULL, 'o'}, {"headless", no_argument, NULL, 'q'},
        {"selftest", no_argument, NULL, 'v'}, {"expect-error", required_argument, NULL, 'e'},
        {"timeout-ms", required_argument, NULL, 'b'}, {NULL, 0, NULL, 0}
    };
    int option;
    while ((option = getopt_long(argc, argv, "", options, NULL)) != -1) {
        switch (option) {
        case 'w': width = number(optarg, 1024); break;
        case 'h': height = number(optarg, 1024); break;
        case 's': seed = number(optarg, UINT32_MAX); break;
        case 'r': repeat = number(optarg, 1000); break;
        case 't': threshold = number(optarg, 255); break;
        case 'o': directory = optarg; break;
        case 'q': break;
        case 'v': selftest = 1; break;
        case 'e': expected_error_code = number(optarg, 3); check(expected_error_code >= 2, "expected DMA error"); break;
        case 'b': timeout_ms = number(optarg, FRAMEFX_MAX_WAIT_MS); check(timeout_ms > 0, "wait timeout"); break;
        case 'm':
            if (!strcmp(optarg, "copy")) mode = 0;
            else if (!strcmp(optarg, "invert")) mode = 1;
            else if (!strcmp(optarg, "threshold")) mode = 2;
            else if (!strcmp(optarg, "sobel")) mode = 3;
            else if (!strcmp(optarg, "all")) mode = 4;
            else check(0, "mode must be copy/invert/threshold/sobel/all");
            break;
        default: return 2;
        }
    }
    check(optind == argc && width && height && repeat && (mode < 3 || (width >= 3 && height >= 3)), "image geometry");
    int descriptor = open("/dev/framefx0", O_RDWR | O_CLOEXEC);
    check(descriptor >= 0, "open framefx");
    struct framefx_caps caps;
    check(!ioctl(descriptor, FRAMEFX_QUERY_CAPS, &caps), "query caps");
    check(caps.abi_version == FRAMEFX_ABI_VERSION && caps.hardware_version == 0x10000 &&
          (caps.capabilities & 15) == 15 && caps.stride_alignment == 64, "ABI capabilities");
    struct framefx_submit job = {.width = width, .height = height, .threshold = threshold,
        .source_stride = ((width + 63) & ~63U) + 64, .destination_stride = ((width + 63) & ~63U) + 128};
    struct framefx_buffers buffers = {.source_bytes = job.source_stride * height + 4096,
                                     .destination_bytes = job.destination_stride * height + 4096};
    if (selftest) allocation_tests(descriptor);
    check(!ioctl(descriptor, FRAMEFX_ALLOC_BUFS, &buffers), "allocate buffers");
    uint8_t *source = mmap(NULL, buffers.source_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, buffers.source_offset);
    uint8_t *destination = mmap(NULL, buffers.destination_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, buffers.destination_offset);
    check(source != MAP_FAILED && destination != MAP_FAILED, "map coherent buffers");
    if (selftest) negative_tests(descriptor, buffers, job);
    uint64_t previous_irq = 0;
    unsigned jobs = repeat * (mode == 4 ? 4 : 1);
    for (unsigned index = 0; index < jobs; index++) {
        unsigned image_seed = seed + index;
        job.mode = mode == 4 ? index % 4 : mode;
        job.job_id = 0;
        memset(source, 0xa5, buffers.source_bytes);
        memset(destination, 0x5a, buffers.destination_bytes);
        for (unsigned row = 0; row < height; row++)
            for (unsigned column = 0; column < width; column++)
                source[row * job.source_stride + column] = input_pixel(row, column, image_seed);
        struct timespec start, end;
        check(!clock_gettime(CLOCK_MONOTONIC, &start), "start clock");
        check(!ioctl(descriptor, FRAMEFX_SUBMIT, &job), "submit");
        if (selftest) {
            struct framefx_submit duplicate = job;
            duplicate.job_id = 0;
            expected_error(ioctl(descriptor, FRAMEFX_SUBMIT, &duplicate), EBUSY, "unconsumed job");
        }
        struct framefx_wait result = {.job_id = job.job_id, .timeout_ms = timeout_ms};
        if (!index && expected_error_code) {
            expected_error(ioctl(descriptor, FRAMEFX_WAIT, &result), EIO, "expected DMA fault");
            check(result.error_code == expected_error_code && result.status == 6 && result.bytes_written &&
                  result.bytes_written < width * height && result.irq_count, "partial DMA failure counters");
            printf("FRAMEFX_DMA_ERROR_PASS error=%u read=%u written=%u\n", result.error_code, result.bytes_read, result.bytes_written);
            previous_irq = result.irq_count;
            job.job_id = 0;
            check(!ioctl(descriptor, FRAMEFX_SUBMIT, &job), "resubmit after DMA fault without reset");
            result = (struct framefx_wait){.job_id = job.job_id, .timeout_ms = timeout_ms};
        }
        check(!ioctl(descriptor, FRAMEFX_WAIT, &result), "wait for interrupt completion");
        check(!clock_gettime(CLOCK_MONOTONIC, &end), "end clock");
        check(result.status == 2 && !result.error_code && result.cycles &&
              result.bytes_read == ((width + 7) & ~7U) * height && result.bytes_written == width * height &&
              result.irq_count && (!previous_irq || result.irq_count == previous_irq + 1), "completion counters");
        previous_irq = result.irq_count;
        for (unsigned offset = 0; offset < buffers.source_bytes; offset++) {
            unsigned row = offset / job.source_stride, column = offset % job.source_stride;
            uint8_t expected = row < height && column < width ? input_pixel(row, column, image_seed) : 0xa5;
            check(source[offset] == expected, "source or source padding modified");
        }
        for (unsigned offset = 0; offset < buffers.destination_bytes; offset++) {
            unsigned row = offset / job.destination_stride, column = offset % job.destination_stride;
            uint8_t expected = row < height && column < width ?
                golden(source, width, height, job.source_stride, row, column, job.mode, threshold) : 0x5a;
            if (destination[offset] != expected) {
                fprintf(stderr, "mismatch offset=%u row=%u column=%u expected=%u actual=%u\n", offset, row, column, expected, destination[offset]);
                check(0, "destination or padding mismatch");
            }
        }
        save_image(directory, "input", index, source, width, height, job.source_stride);
        save_image(directory, "output", index, destination, width, height, job.destination_stride);
        save_buffer(directory, "source", index, source, buffers.source_bytes);
        save_buffer(directory, "destination", index, destination, buffers.destination_bytes);
        FILE *stream = output_file(directory, "job", index, "json");
        double elapsed = (end.tv_sec - start.tv_sec) * 1000.0 + (end.tv_nsec - start.tv_nsec) / 1000000.0;
        check(fprintf(stream, "{\"status\":\"PASS\",\"width\":%u,\"height\":%u,\"seed\":%u,\"mode\":%u,\"threshold\":%u,"
            "\"source_stride\":%u,\"destination_stride\":%u,\"source_bytes\":%u,\"destination_bytes\":%u,"
            "\"job_id\":%" PRIu64 ",\"cycles\":%" PRIu64 ",\"bytes_read\":%u,\"bytes_written\":%u,"
            "\"irq_count\":%" PRIu64 ",\"elapsed_ms\":%.3f}\n", width, height, image_seed, job.mode, threshold,
            job.source_stride, job.destination_stride, buffers.source_bytes, buffers.destination_bytes,
            (uint64_t)result.job_id, (uint64_t)result.cycles, result.bytes_read, result.bytes_written,
            (uint64_t)result.irq_count, elapsed) > 0, "JSON output");
        check(!fclose(stream), "close JSON");
        printf("FRAMEFX_JOB index=%u mode=%u width=%u height=%u cycles=%" PRIu64 " irq=%" PRIu64 " PASS\n",
               index, job.mode, width, height, (uint64_t)result.cycles, (uint64_t)result.irq_count);
    }
    check(!ioctl(descriptor, FRAMEFX_RESET, 0), "final idle reset");
    mapping_lifetime(descriptor, source, destination, buffers);
    printf("FRAMEFX_RESULT jobs=%u PASS\n", jobs);
    return 0;
}

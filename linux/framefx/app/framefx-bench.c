#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
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

static void require(int condition, const char *operation)
{
    if (!condition) {
        fprintf(stderr, "FRAMEFX_BENCH_FAIL %s (errno=%d); stop, do not retry hardware\n", operation, errno);
        exit(1);
    }
}

static unsigned number(const char *text, unsigned minimum, unsigned maximum)
{
    char *end;
    errno = 0;
    unsigned long value = strtoul(text, &end, 10);
    require(isdigit((unsigned char)*text) && !errno && !*end && value >= minimum && value <= maximum,
            "numeric argument");
    return value;
}

static uint64_t now_ns(void)
{
    struct timespec stamp;
    require(!clock_gettime(CLOCK_MONOTONIC_RAW, &stamp), "monotonic clock");
    return (uint64_t)stamp.tv_sec * UINT64_C(1000000000) + stamp.tv_nsec;
}

static uint32_t crc_table[256];

static void initialize_crc(void)
{
    for (unsigned index = 0; index < 256; index++) {
        uint32_t value = index;
        for (unsigned bit = 0; bit < 8; bit++)
            value = (value >> 1) ^ ((value & 1) ? UINT32_C(0xedb88320) : 0);
        crc_table[index] = value;
    }
}

static uint32_t checksum(const uint8_t *bytes, unsigned size)
{
    uint32_t value = UINT32_MAX;
    for (unsigned index = 0; index < size; index++)
        value = crc_table[(value ^ bytes[index]) & 255] ^ (value >> 8);
    return value ^ UINT32_MAX;
}

static const struct framefx_caps expected_caps = {
    FRAMEFX_ABI_VERSION, 0x10000, 0x10f, 1024, 1024, 64, FRAMEFX_MAX_BUFFER, 0
};

static void query_caps(int descriptor)
{
    struct framefx_caps caps = {0};
    require(!ioctl(descriptor, FRAMEFX_QUERY_CAPS, &caps), "QUERY_CAPS");
    require(!memcmp(&caps, &expected_caps, sizeof(caps)), "stable capabilities");
}

struct control_worker {
    int descriptor;
    atomic_bool stop;
    uint64_t count, total_ns, maximum_ns;
};

static void *control_traffic(void *argument)
{
    struct control_worker *worker = argument;
    while (!atomic_load(&worker->stop)) {
        uint64_t start = now_ns();
        query_caps(worker->descriptor);
        uint64_t elapsed = now_ns() - start;
        worker->count++;
        worker->total_ns += elapsed;
        if (elapsed > worker->maximum_ns) worker->maximum_ns = elapsed;
        struct timespec pause = {.tv_nsec = 100000};
        require(!nanosleep(&pause, NULL), "control pause");
    }
    return NULL;
}

static void save_buffer(const char *directory, const char *name, const uint8_t *buffer, unsigned size)
{
    char filename[1024];
    int length = snprintf(filename, sizeof(filename), "%s/%s.bin", directory, name);
    require(length > 0 && (size_t)length < sizeof(filename), "export path");
    int descriptor = open(filename, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    require(descriptor >= 0, "create snapshot (no overwrite)");
    uint8_t staging[4096];
    for (unsigned offset = 0; offset < size;) {
        unsigned count = size - offset < sizeof(staging) ? size - offset : sizeof(staging);
        memcpy(staging, buffer + offset, count);
        for (unsigned sent = 0; sent < count;) {
            ssize_t written = write(descriptor, staging + sent, count - sent);
            require(written > 0, "write snapshot");
            sent += written;
        }
        offset += count;
    }
    require(!close(descriptor), "close snapshot");
}

int main(int argc, char **argv)
{
    require(argc == 8, "usage: framefx-bench WIDTH HEIGHT MODE SAMPLES WARMUP CONTEND OUTPUT_DIRECTORY");
    unsigned width = number(argv[1], 3, 1024), height = number(argv[2], 3, 1024);
    unsigned mode = number(argv[3], 0, 3), samples = number(argv[4], 1, 256);
    unsigned warmup = number(argv[5], 0, 16), contend = number(argv[6], 0, 1);
    unsigned source_stride = ((width + 63) & ~63U) + 64;
    unsigned destination_stride = source_stride + 64;
    struct framefx_buffers buffers = {
        .source_bytes = (source_stride * height + 8191) & ~4095U,
        .destination_bytes = (destination_stride * height + 8191) & ~4095U
    };
    require(sysconf(_SC_PAGESIZE) == 4096, "4-KiB page ABI");
    initialize_crc();
    require(!setvbuf(stdout, NULL, _IOLBF, 0), "line-buffered evidence");
    int descriptor = open("/dev/framefx0", O_RDWR | O_CLOEXEC);
    require(descriptor >= 0, "exclusive device open");
    query_caps(descriptor);
    uint64_t caps_ns[128];
    for (unsigned index = 0; index < 144; index++) {
        uint64_t start = now_ns();
        query_caps(descriptor);
        uint64_t elapsed = now_ns() - start;
        if (index >= 16) caps_ns[index - 16] = elapsed;
    }
    uint64_t allocation_start = now_ns();
    unsigned source_bytes = buffers.source_bytes, destination_bytes = buffers.destination_bytes;
    require(!ioctl(descriptor, FRAMEFX_ALLOC_BUFS, &buffers), "ALLOC_BUFS");
    require(buffers.source_bytes == source_bytes && buffers.destination_bytes == destination_bytes &&
            !buffers.reserved[0] && !buffers.reserved[1] && !buffers.source_offset &&
            buffers.destination_offset == FRAMEFX_DESTINATION_OFFSET, "allocation ABI");
    uint8_t *source = mmap(NULL, source_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, buffers.source_offset);
    uint8_t *destination = mmap(NULL, destination_bytes, PROT_READ | PROT_WRITE, MAP_SHARED,
                                descriptor, buffers.destination_offset);
    require(source != MAP_FAILED && destination != MAP_FAILED, "mmap buffers");
    uint64_t allocation_ns = now_ns() - allocation_start;
    uint8_t *expected_source = malloc(source_bytes), *expected_destination = malloc(destination_bytes);
    require(expected_source && expected_destination, "reference allocation");
    printf("{\"type\":\"plan\",\"schema\":1,\"width\":%u,\"height\":%u,\"mode\":%u,"
           "\"samples\":%u,\"warmup\":%u,\"contend\":%u,\"threshold\":127,\"seed_base\":12000,"
           "\"source_stride\":%u,\"destination_stride\":%u,\"source_bytes\":%u,\"destination_bytes\":%u,"
           "\"clock\":\"CLOCK_MONOTONIC_RAW\",\"allocation_ns\":%" PRIu64 ",\"caps_ns\":[",
           width, height, mode, samples, warmup, contend, source_stride, destination_stride,
           source_bytes, destination_bytes, allocation_ns);
    for (unsigned index = 0; index < 128; index++)
        printf("%s%" PRIu64, index ? "," : "", caps_ns[index]);
    puts("]}");
    struct control_worker worker = {.descriptor = descriptor, .stop = ATOMIC_VAR_INIT(0)};
    pthread_t thread;
    if (contend) require(!pthread_create(&thread, NULL, control_traffic, &worker), "control thread");
    uint64_t previous_job = 0, previous_irq = 0;
    for (unsigned index = 0; index < samples + warmup; index++) {
        unsigned seed = 12000 + index % 4;
        memset(expected_source, 0xa5, source_bytes);
        memset(expected_destination, 0x5a, destination_bytes);
        for (unsigned row = 0; row < height; row++)
            for (unsigned column = 0; column < width; column++)
                expected_source[row * source_stride + column] = input_pixel(row, column, seed);
        for (unsigned row = 0; row < height; row++)
            for (unsigned column = 0; column < width; column++)
                expected_destination[row * destination_stride + column] =
                    golden(expected_source, width, height, source_stride, row, column, mode, 127);
        uint64_t prepare_start = now_ns();
        memcpy(source, expected_source, source_bytes);
        memset(destination, 0x5a, destination_bytes);
        uint64_t prepare_ns = now_ns() - prepare_start;
        struct framefx_submit job = {.width = width, .height = height, .source_stride = source_stride,
                                    .destination_stride = destination_stride, .mode = mode, .threshold = 127};
        uint64_t start = now_ns();
        require(!ioctl(descriptor, FRAMEFX_SUBMIT, &job), "SUBMIT (uncertain START on failure)");
        uint64_t submitted = now_ns();
        struct framefx_wait result = {.job_id = job.job_id, .timeout_ms = 1000};
        require(!ioctl(descriptor, FRAMEFX_WAIT, &result), "WAIT (no retry/reset on failure)");
        uint64_t completed = now_ns();
        require(job.job_id && result.job_id == job.job_id && result.status == 2 && !result.error_code &&
                result.cycles && result.bytes_read == ((width + 7) & ~7U) * height &&
                result.bytes_written == width * height && result.irq_count &&
                (!index || (job.job_id == previous_job + 1 && result.irq_count == previous_irq + 1)),
                "completion telemetry");
        require(!memcmp(source, expected_source, source_bytes), "full source allocation");
        require(!memcmp(destination, expected_destination, destination_bytes), "full destination allocation");
        uint32_t source_crc = checksum(source, source_bytes), destination_crc = checksum(destination, destination_bytes);
        uint64_t verify_ns = now_ns() - completed;
        printf("{\"type\":\"sample\",\"index\":%u,\"seed\":%u,\"job_id\":%" PRIu64 ","
               "\"irq_count\":%" PRIu64 ",\"cycles\":%" PRIu64 ",\"bytes_read\":%u,\"bytes_written\":%u,"
               "\"prepare_ns\":%" PRIu64 ",\"submit_ns\":%" PRIu64 ",\"wait_ns\":%" PRIu64 ","
               "\"elapsed_ns\":%" PRIu64 ",\"verify_ns\":%" PRIu64 ",\"source_crc32\":%" PRIu32 ","
               "\"destination_crc32\":%" PRIu32 ",\"status\":\"PASS\"}\n",
               index, seed, (uint64_t)job.job_id, (uint64_t)result.irq_count, (uint64_t)result.cycles,
               result.bytes_read, result.bytes_written, prepare_ns, submitted - start, completed - submitted,
               completed - start, verify_ns, source_crc, destination_crc);
        previous_job = job.job_id;
        previous_irq = result.irq_count;
    }
    if (contend) {
        atomic_store(&worker.stop, 1);
        require(!pthread_join(thread, NULL), "join control thread");
        require(worker.count > 0, "control traffic executed");
    }
    save_buffer(argv[7], "source", source, source_bytes);
    save_buffer(argv[7], "destination", destination, destination_bytes);
    free(expected_source);
    free(expected_destination);
    require(!munmap(source, source_bytes) && !munmap(destination, destination_bytes), "release mappings");
    require(!close(descriptor), "close owner");
    descriptor = open("/dev/framefx0", O_RDWR | O_CLOEXEC);
    require(descriptor >= 0, "reopen after release");
    query_caps(descriptor);
    require(!close(descriptor), "close reopened owner");
    printf("{\"type\":\"result\",\"status\":\"PASS\",\"jobs\":%u,\"control_queries\":%" PRIu64 ","
           "\"control_total_ns\":%" PRIu64 ",\"control_max_ns\":%" PRIu64 ",\"ownership_released\":true}\n",
           samples + warmup, worker.count, worker.total_ns, worker.maximum_ns);
    require(!fflush(stdout) && !ferror(stdout), "flush evidence");
    return 0;
}

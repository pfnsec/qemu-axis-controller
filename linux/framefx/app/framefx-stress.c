#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#include "framefx.h"
#include "framefx-reference.h"

#define BUFFER_BYTES (128U * 1024)
#define WORKERS 4

struct context {
    int descriptor;
    unsigned jobs, cpus;
    struct framefx_submit job;
    struct framefx_wait results[2];
    pthread_barrier_t start, done;
};

struct worker {
    struct context *context;
    unsigned index, operations;
};

static void check(int condition, const char *operation)
{
    if (!condition) {
        fprintf(stderr, "FRAMEFX_STRESS_FAIL %s: %s\n", operation, strerror(errno));
        exit(1);
    }
}

static void barrier(pthread_barrier_t *value)
{
    int result = pthread_barrier_wait(value);
    check(!result || result == PTHREAD_BARRIER_SERIAL_THREAD, "barrier");
}

static void pin(unsigned cpu)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    check(!sched_setaffinity(0, sizeof(set), &set), "CPU affinity");
}

static uint32_t random_value(uint32_t *state)
{
    *state ^= *state << 13;
    *state ^= *state >> 17;
    *state ^= *state << 5;
    return *state;
}

static unsigned number(const char *value, unsigned maximum)
{
    char *end;
    errno = 0;
    unsigned long result = strtoul(value, &end, 0);
    check(!errno && *value && !*end && result && result <= maximum, "positive numeric argument");
    return result;
}

static void *contend(void *opaque)
{
    struct worker *worker = opaque;
    struct context *context = worker->context;
    pin(worker->index % context->cpus);
    for (unsigned index = 0; index < context->jobs; index++) {
        barrier(&context->start);
        if (worker->index < 2) {
            struct framefx_wait result = {.job_id = context->job.job_id, .timeout_ms = FRAMEFX_MAX_WAIT_MS};
            check(!ioctl(context->descriptor, FRAMEFX_WAIT, &result), "concurrent wait");
            context->results[worker->index] = result;
            worker->operations++;
        } else if (worker->index == 2) {
            for (unsigned repeat = 0; repeat < 3; repeat++) {
                struct framefx_caps caps;
                check(!ioctl(context->descriptor, FRAMEFX_QUERY_CAPS, &caps) && caps.abi_version == 1 &&
                      caps.hardware_version == 0x10000 && caps.capabilities == 0x10f, "concurrent caps");
                struct framefx_submit invalid = context->job;
                invalid.width = 0; invalid.job_id = 0;
                check(ioctl(context->descriptor, FRAMEFX_SUBMIT, &invalid) == -1 &&
                      (errno == EBUSY || errno == EINVAL), "concurrent invalid submit");
                worker->operations += 2;
            }
        } else {
            check(open("/dev/framefx0", O_RDWR) == -1 && errno == EBUSY, "concurrent exclusive open");
            int duplicate = dup(context->descriptor);
            check(duplicate >= 0, "concurrent dup");
            void *alias = mmap(NULL, BUFFER_BYTES, PROT_READ, MAP_SHARED, duplicate, FRAMEFX_DESTINATION_OFFSET);
            check(alias != MAP_FAILED, "concurrent alias mapping");
            check(!close(duplicate), "concurrent close duplicate");
            check(!munmap(alias, BUFFER_BYTES), "concurrent unmap alias");
            check(ioctl(context->descriptor, _IO('F', 63), 0) == -1 && errno == ENOTTY, "concurrent unknown ioctl");
            worker->operations += 6;
        }
        barrier(&context->done);
    }
    return NULL;
}

static int allocate(uint8_t **source, uint8_t **destination)
{
    int descriptor = open("/dev/framefx0", O_RDWR | O_CLOEXEC);
    check(descriptor >= 0, "open owner");
    struct framefx_buffers buffers = {.source_bytes = BUFFER_BYTES, .destination_bytes = BUFFER_BYTES};
    check(!ioctl(descriptor, FRAMEFX_ALLOC_BUFS, &buffers), "allocate");
    check(buffers.source_bytes == BUFFER_BYTES && buffers.destination_bytes == BUFFER_BYTES, "allocation size");
    *source = mmap(NULL, BUFFER_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, buffers.source_offset);
    *destination = mmap(NULL, BUFFER_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, buffers.destination_offset);
    check(*source != MAP_FAILED && *destination != MAP_FAILED, "map buffers");
    return descriptor;
}

static void release(int descriptor, uint8_t *source, uint8_t *destination)
{
    check(!close(descriptor), "close mapped owner");
    check(!munmap(source + BUFFER_BYTES / 2, BUFFER_BYTES / 2), "split source VMA");
    check(open("/dev/framefx0", O_RDWR) == -1 && errno == EBUSY, "split mapping retains owner");
    check(!munmap(destination, BUFFER_BYTES), "release destination");
    check(open("/dev/framefx0", O_RDWR) == -1 && errno == EBUSY, "last split mapping retains owner");
    check(!munmap(source, BUFFER_BYTES / 2), "release source fragment");
}

static void killed_owner(void)
{
    int notification[2];
    check(!pipe(notification), "child pipe");
    pid_t child = fork();
    check(child >= 0, "fork killed owner");
    if (!child) {
        close(notification[0]);
        int descriptor = open("/dev/framefx0", O_RDWR | O_CLOEXEC);
        check(descriptor >= 0, "child open");
        struct framefx_buffers buffers = {.source_bytes = 1024 * 1024, .destination_bytes = 1024 * 1024};
        check(!ioctl(descriptor, FRAMEFX_ALLOC_BUFS, &buffers), "child allocate");
        void *source = mmap(NULL, buffers.source_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, 0);
        check(source != MAP_FAILED, "child map");
        memset(source, 0x91, buffers.source_bytes);
        struct framefx_submit job = {.width = 1024, .height = 1024, .source_stride = 1024, .destination_stride = 1024, .mode = 3};
        check(!ioctl(descriptor, FRAMEFX_SUBMIT, &job), "child submit");
        check(ioctl(descriptor, FRAMEFX_RESET, 0) == -1 && errno == EBUSY, "child is active");
        check(write(notification[1], "!", 1) == 1, "child ready");
        for (;;) pause();
    }
    close(notification[1]);
    char ready;
    check(read(notification[0], &ready, 1) == 1 && ready == '!', "child active notification");
    close(notification[0]);
    check(!kill(child, SIGKILL), "kill active owner");
    int child_status;
    check(waitpid(child, &child_status, 0) == child && WIFSIGNALED(child_status) && WTERMSIG(child_status) == SIGKILL,
          "killed owner reaped");
    int descriptor = open("/dev/framefx0", O_RDWR | O_CLOEXEC);
    check(descriptor >= 0, "reopen after SIGKILL");
    check(!ioctl(descriptor, FRAMEFX_RESET, 0), "idle after SIGKILL");
    check(!close(descriptor), "close after SIGKILL");
    puts("FRAMEFX_KILLED_OWNER_PASS");
}

int main(int argc, char **argv)
{
    check(argc == 3, "usage: framefx-stress JOBS SEED (writes in current directory)");
    unsigned jobs = number(argv[1], 100000), seed = number(argv[2], UINT32_MAX);
    uint32_t state = seed;
    struct context context = {.jobs = jobs, .cpus = (unsigned)sysconf(_SC_NPROCESSORS_ONLN)};
    check(context.cpus && context.cpus <= CPU_SETSIZE, "CPU count");
    pin(0);
    killed_owner();
    uint8_t *source, *destination;
    uint8_t *original = malloc(BUFFER_BYTES), *expected = malloc(BUFFER_BYTES), *snapshot = malloc(BUFFER_BYTES);
    check(original && expected && snapshot, "scoreboard buffers");
    void *request = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    check(request != MAP_FAILED, "readonly request allocation");
    context.descriptor = allocate(&source, &destination);
    check(!pthread_barrier_init(&context.start, NULL, WORKERS + 1) &&
          !pthread_barrier_init(&context.done, NULL, WORKERS + 1), "barrier init");
    pthread_t threads[WORKERS];
    struct worker workers[WORKERS];
    for (unsigned index = 0; index < WORKERS; index++) {
        workers[index] = (struct worker){.context = &context, .index = index};
        check(!pthread_create(&threads[index], NULL, contend, &workers[index]), "create contender");
    }
    FILE *records = fopen("stress.jsonl", "w"), *outputs = fopen("destination.bin", "wb");
    check(records && outputs, "open evidence files");
    uint64_t previous_job = 0, previous_irq = 0;
    unsigned owners = 1;
    const unsigned edges[] = {1, 2, 3, 7, 8, 9, 63, 64, 65, 127, 128, 129, 255, 256, 257, 511, 512, 513, 1023, 1024};
    for (unsigned index = 0; index < jobs; index++) {
        unsigned width = index % 2 ? 1 + random_value(&state) % 1024 : edges[random_value(&state) % 20];
        unsigned height = 1 + random_value(&state) % 65, mode = index % 4;
        if (mode == 3) { if (width < 3) width = 3; if (height < 3) height = 3; }
        unsigned source_stride = ((width + 63) & ~63U) + 64 * (random_value(&state) % 9);
        unsigned destination_stride = ((width + 63) & ~63U) + 64 * (random_value(&state) % 9);
        unsigned threshold = random_value(&state) & 255, image_seed = random_value(&state);
        context.job = (struct framefx_submit){.width = width, .height = height, .source_stride = source_stride,
            .destination_stride = destination_stride, .mode = mode, .threshold = threshold};
        memset(original, 0xa5, BUFFER_BYTES); memset(expected, 0x5a, BUFFER_BYTES);
        for (unsigned row = 0; row < height; row++)
            for (unsigned column = 0; column < width; column++)
                original[row * source_stride + column] = input_pixel(row, column, image_seed);
        for (unsigned row = 0; row < height; row++)
            for (unsigned column = 0; column < width; column++)
                expected[row * destination_stride + column] = golden(original, width, height, source_stride, row, column, mode, threshold);
        memcpy(source, original, BUFFER_BYTES); memset(destination, 0x5a, BUFFER_BYTES);
        if (index % 16 == 0) {
            memcpy(request, &context.job, sizeof(context.job));
            check(!mprotect(request, 4096, PROT_READ), "protect submit request");
            check(ioctl(context.descriptor, FRAMEFX_SUBMIT, request) == -1 && errno == EFAULT, "submit copyout rollback");
            check(!mprotect(request, 4096, PROT_READ | PROT_WRITE), "unprotect submit request");
        }
        check(!ioctl(context.descriptor, FRAMEFX_SUBMIT, &context.job), "submit");
        check(!previous_job || context.job.job_id == previous_job + 1, "job sequence / rollback");
        if (index % 16 == 0) {
            struct framefx_wait waiting = {.job_id = context.job.job_id, .timeout_ms = FRAMEFX_MAX_WAIT_MS};
            memcpy(request, &waiting, sizeof(waiting));
            check(!mprotect(request, 4096, PROT_READ), "protect wait request");
            check(ioctl(context.descriptor, FRAMEFX_WAIT, request) == -1 && errno == EFAULT, "wait copyout preserves result");
            struct framefx_submit blocked = context.job;
            blocked.job_id = 0;
            check(ioctl(context.descriptor, FRAMEFX_SUBMIT, &blocked) == -1 && errno == EBUSY, "unconsumed result blocks submit");
            check(!mprotect(request, 4096, PROT_READ | PROT_WRITE), "unprotect wait request");
        }
        barrier(&context.start); barrier(&context.done);
        struct framefx_wait result = context.results[0];
        check(!memcmp(&result, &context.results[1], sizeof(result)), "concurrent waiters agree");
        check(result.job_id == context.job.job_id && result.status == 2 && !result.error_code && result.cycles &&
              result.bytes_read == ((width + 7) & ~7U) * height && result.bytes_written == width * height &&
              (!previous_irq || result.irq_count == previous_irq + 1), "completion accounting");
        previous_job = result.job_id; previous_irq = result.irq_count;
        check(!memcmp(source, original, BUFFER_BYTES), "full source scoreboard");
        memcpy(snapshot, destination, BUFFER_BYTES);
        check(!memcmp(snapshot, expected, BUFFER_BYTES), "full destination scoreboard");
        check(fwrite(snapshot, 1, BUFFER_BYTES, outputs) == BUFFER_BYTES, "write destination snapshot");
        check(fprintf(records, "{\"index\":%u,\"seed\":%u,\"width\":%u,\"height\":%u,\"source_stride\":%u,"
              "\"destination_stride\":%u,\"mode\":%u,\"threshold\":%u,\"job_id\":%" PRIu64 ",\"cycles\":%" PRIu64 ","
              "\"bytes_read\":%u,\"bytes_written\":%u,\"irq_count\":%" PRIu64 "}\n",
              index, image_seed, width, height, source_stride, destination_stride, mode, threshold,
              (uint64_t)result.job_id, (uint64_t)result.cycles, result.bytes_read, result.bytes_written,
              (uint64_t)result.irq_count) > 0, "write job record");
        if ((index + 1) % 32 == 0 && index + 1 < jobs) {
            release(context.descriptor, source, destination);
            context.descriptor = allocate(&source, &destination);
            struct framefx_wait stale = {.job_id = previous_job, .timeout_ms = 1};
            check(ioctl(context.descriptor, FRAMEFX_WAIT, &stale) == -1 && errno == ESTALE, "new owner has no old result");
            owners++;
        }
        if ((index + 1) % 32 == 0) printf("FRAMEFX_STRESS_PROGRESS jobs=%u\n", index + 1);
    }
    unsigned operations = 0;
    for (unsigned index = 0; index < WORKERS; index++) {
        check(!pthread_join(threads[index], NULL), "join contender");
        operations += workers[index].operations;
    }
    check(!fclose(records) && !fclose(outputs), "close evidence files");
    release(context.descriptor, source, destination);
    check(!munmap(request, 4096), "release readonly request page");
    check(!pthread_barrier_destroy(&context.start) && !pthread_barrier_destroy(&context.done), "barrier destroy");
    free(original); free(expected); free(snapshot);
    printf("FRAMEFX_STRESS_PASS jobs=%u seed=%u cpus=%u workers=%u operations=%u owners=%u\n",
           jobs, seed, context.cpus, WORKERS, operations, owners);
    return 0;
}

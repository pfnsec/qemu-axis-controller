#include "framefx_uapi.c"

_Static_assert(sizeof(void *) == 4, "runtime fixture must be 32-bit");

enum { SYS_WRITE = 4, SYS_OPEN = 5, SYS_CLOSE = 6, SYS_IOCTL = 54, SYS_MUNMAP = 91,
       SYS_MPROTECT = 125, SYS_MMAP2 = 192, SYS_EXIT_GROUP = 248 };
enum { PROT_READ = 1, PROT_WRITE = 2, PROT_EXEC = 4, MAP_SHARED = 1, OPEN_RDWR = 2 };
enum { BUFFER_BYTES = 8192 };

static long syscall6(long number, long first, long second, long third, long fourth, long fifth, long sixth)
{
    register long result __asm__("r0") = first;
    register long argument2 __asm__("r1") = second;
    register long argument3 __asm__("r2") = third;
    register long argument4 __asm__("r3") = fourth;
    register long argument5 __asm__("r4") = fifth;
    register long argument6 __asm__("r5") = sixth;
    register long operation __asm__("r7") = number;
    __asm__ volatile ("svc 0" : "+r"(result) : "r"(argument2), "r"(argument3), "r"(argument4),
                      "r"(argument5), "r"(argument6), "r"(operation) : "memory", "cc");
    return result;
}

static void print(const char *message)
{
    unsigned length = 0;
    while (message[length]) ++length;
    syscall6(SYS_WRITE, 1, (long)message, length, 0, 0, 0);
}

static void check(int condition, const char *operation)
{
    if (!condition) {
        print("FRAMEFX_COMPAT32_FAIL ");
        print(operation);
        print("\n");
        syscall6(SYS_EXIT_GROUP, 1, 0, 0, 0, 0, 0);
        __builtin_trap();
    }
}

static long control(long descriptor, unsigned command, void *argument)
{
    return syscall6(SYS_IOCTL, descriptor, command, (long)argument, 0, 0, 0);
}

static struct framefx_caps caps;
static struct framefx_buffers buffers = {.source_bytes = BUFFER_BYTES, .destination_bytes = BUFFER_BYTES};
static struct framefx_submit job = {.width = 65, .height = 9, .source_stride = 128, .destination_stride = 192,
                                    .threshold = 127};
static struct framefx_wait waiting;

void _start(void)
{
    long descriptor = syscall6(SYS_OPEN, (long)"/dev/framefx0", OPEN_RDWR, 0, 0, 0, 0);
    check(descriptor >= 0, "open");
    check(!control(descriptor, FRAMEFX_QUERY_CAPS, &caps), "query");
    check(caps.abi_version == 1 && caps.hardware_version == 0x10000 && caps.capabilities == 0x10f, "caps layout");
    check(control(descriptor, FRAMEFX_QUERY_CAPS, (void *)1) == -14, "bad compat pointer");
    check(!control(descriptor, FRAMEFX_ALLOC_BUFS, &buffers), "allocate");
    check(buffers.source_bytes == BUFFER_BYTES && buffers.destination_bytes == BUFFER_BYTES &&
          buffers.source_offset == 0 && buffers.destination_offset == FRAMEFX_DESTINATION_OFFSET, "allocation layout");
    long source_address = syscall6(SYS_MMAP2, 0, BUFFER_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, 0);
    long destination_address = syscall6(SYS_MMAP2, 0, BUFFER_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor,
                                        FRAMEFX_DESTINATION_OFFSET / 4096);
    check((unsigned long)source_address < (unsigned long)-4095 &&
          (unsigned long)destination_address < (unsigned long)-4095, "compat mmap2 offsets");
    unsigned char *source = (void *)source_address;
    unsigned char *destination = (void *)destination_address;
    check(syscall6(SYS_MPROTECT, destination_address, BUFFER_BYTES, PROT_READ | PROT_EXEC, 0, 0, 0) == -13, "non-executable DMA");
    for (unsigned iteration = 0; iteration < 8; ++iteration) {
        job.mode = iteration % 4;
        for (unsigned offset = 0; offset < BUFFER_BYTES; ++offset) {
            source[offset] = 0xa5;
            destination[offset] = 0x5a;
        }
        for (unsigned row = 0; row < 9; ++row)
            for (unsigned column = 0; column < 65; ++column)
                source[row * 128 + column] = job.mode == 3 ? 57 : (row * 65 + column + iteration);
        job.job_id = 0;
        check(!control(descriptor, FRAMEFX_SUBMIT, &job), "submit");
        check(job.job_id == iteration + 1, "64-bit job ID output");
        waiting.job_id = job.job_id | (1ULL << 32);
        waiting.timeout_ms = FRAMEFX_MAX_WAIT_MS;
        check(control(descriptor, FRAMEFX_WAIT, &waiting) == -116, "64-bit job ID input cannot truncate");
        waiting.job_id = job.job_id;
        check(!control(descriptor, FRAMEFX_WAIT, &waiting), "wait");
        check(waiting.job_id == job.job_id && waiting.status == 2 && !waiting.error_code && waiting.cycles &&
              waiting.bytes_read == 72 * 9 && waiting.bytes_written == 65 * 9 && waiting.irq_count == iteration + 1,
              "completion layout and counters");
        for (unsigned offset = 0; offset < BUFFER_BYTES; ++offset) {
            unsigned row = offset / 192, column = offset % 192;
            unsigned char expected = 0x5a;
            if (row < 9 && column < 65) {
                unsigned char pixel = source[row * 128 + column];
                expected = job.mode == 0 ? pixel : job.mode == 1 ? 255 - pixel : job.mode == 2 ?
                           (pixel >= 127 ? 255 : 0) : 0;
            }
            check(destination[offset] == expected, "full output allocation");
            row = offset / 128;
            column = offset % 128;
            expected = row < 9 && column < 65 ? (job.mode == 3 ? 57 : (row * 65 + column + iteration)) : 0xa5;
            check(source[offset] == expected, "source immutability");
        }
    }
    check(!control(descriptor, FRAMEFX_RESET, 0), "reset");
    waiting.timeout_ms = FRAMEFX_MAX_WAIT_MS;
    check(control(descriptor, FRAMEFX_WAIT, &waiting) == -116, "reset invalidates result");
    check(!syscall6(SYS_CLOSE, descriptor, 0, 0, 0, 0, 0), "close mapped owner");
    check(syscall6(SYS_OPEN, (long)"/dev/framefx0", OPEN_RDWR, 0, 0, 0, 0) == -16, "VMA retains ownership");
    check(!syscall6(SYS_MUNMAP, source_address, BUFFER_BYTES, 0, 0, 0, 0), "unmap source");
    check(!syscall6(SYS_MUNMAP, destination_address, BUFFER_BYTES, 0, 0, 0, 0), "unmap destination");
    descriptor = syscall6(SYS_OPEN, (long)"/dev/framefx0", OPEN_RDWR, 0, 0, 0, 0);
    check(descriptor >= 0, "reopen after last VMA");
    check(!syscall6(SYS_CLOSE, descriptor, 0, 0, 0, 0, 0), "close new owner");
    print("FRAMEFX_COMPAT32_PASS jobs=8 modes=4 pointer_bits=32\n");
    syscall6(SYS_EXIT_GROUP, 0, 0, 0, 0, 0, 0);
    __builtin_trap();
}

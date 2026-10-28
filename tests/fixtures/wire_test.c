#include "wire.h"
#include <signal.h>
#include <sys/time.h>

int test_encode(uint8_t *output, uint16_t kind, uint32_t status,
                uint64_t id, uint64_t address, uint32_t length)
{
    struct ax_frame frame = {.kind = kind, .status = status, .id = id,
                             .address = address, .length = length};
    return ax_encode(output, &frame);
}

int test_decode(const uint8_t *input)
{
    struct ax_frame frame;
    return ax_decode(&frame, input);
}

int test_receive(int descriptor, int timeout, uint8_t *output)
{
    struct ax_link link = {.fd = descriptor, .pump_fd = -1};
    struct ax_frame frame;
    int result = ax_receive(&link, &frame, ax_now() + timeout);
    if (!result) {
        result = ax_encode(output, &frame);
        memcpy(output + AX_HEADER, frame.payload, frame.length);
    }
    return result;
}

int test_send(int descriptor, int timeout, unsigned fragment, const uint8_t *input)
{
    struct ax_link link = {.fd = descriptor, .pump_fd = -1, .fragment = fragment};
    struct ax_frame frame;
    int result = ax_decode(&frame, input);
    if (!result) {
        memcpy(frame.payload, input + AX_HEADER, frame.length);
        result = ax_send(&link, &frame, ax_now() + timeout);
    }
    return result;
}

static int pump_echo(void *opaque, int64_t deadline)
{
    struct ax_link *link = opaque;
    struct ax_frame frame;
    int result = ax_receive(link, &frame, deadline);
    if (!result) {
        frame.kind |= AX_RESPONSE;
        result = ax_send(link, &frame, deadline);
    }
    return result;
}

int test_pump(int descriptor, int reverse_descriptor, int timeout)
{
    struct ax_link reverse = {.fd = reverse_descriptor, .pump_fd = -1};
    struct ax_link command = {.fd = descriptor, .pump_fd = reverse_descriptor,
                              .pump = pump_echo, .opaque = &reverse};
    struct ax_frame frame;
    return ax_receive(&command, &frame, ax_now() + timeout);
}

int test_match(const uint8_t *request_bytes, const uint8_t *reply_bytes)
{
    struct ax_frame request, reply;
    if (ax_decode(&request, request_bytes) || ax_decode(&reply, reply_bytes)) {
        return 0;
    }
    return ax_reply_matches(&request, &reply);
}

static volatile sig_atomic_t interruptions;

static void interrupt_wait(int signal_number)
{
    (void)signal_number;
    interruptions++;
}

int test_interrupted_receive(int descriptor)
{
    struct sigaction action = {.sa_handler = interrupt_wait}, previous;
    struct itimerval timer = {.it_interval = {.tv_usec = 5000}, .it_value = {.tv_usec = 5000}};
    struct itimerval stopped = {0}, original;
    uint8_t output[AX_HEADER + AX_MAX_PAYLOAD];
    interruptions = 0;
    sigemptyset(&action.sa_mask);
    sigaction(SIGALRM, &action, &previous);
    setitimer(ITIMER_REAL, &timer, &original);
    int result = test_receive(descriptor, 60, output);
    setitimer(ITIMER_REAL, &stopped, NULL);
    sigaction(SIGALRM, &previous, NULL);
    setitimer(ITIMER_REAL, &original, NULL);
    return result == -ETIMEDOUT && interruptions > 0;
}

#ifndef AXI_LAB_WIRE_H
#define AXI_LAB_WIRE_H

#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>

#define AX_MAGIC UINT32_C(0x41584943)
#define AX_HEADER 40
#define AX_MAX_PAYLOAD 1024
#define AX_MAX_DMA 128
#define AX_RESPONSE 0x8000

enum ax_kind {
    AX_HELLO = 1, AX_MMIO_READ, AX_MMIO_WRITE, AX_DMA_READ, AX_DMA_WRITE,
    AX_IRQ, AX_RESET, AX_ERROR, AX_GOODBYE
};

enum ax_status {
    AX_OK, AX_ALIGNMENT, AX_RANGE, AX_UNSUPPORTED, AX_TIMEOUT,
    AX_PROTOCOL, AX_STOPPED, AX_BUS_ERROR
};

struct ax_frame {
    uint16_t kind;
    uint32_t status;
    uint64_t id;
    uint64_t address;
    uint32_t length;
    uint8_t payload[AX_MAX_PAYLOAD];
};

struct ax_link {
    int fd;
    int pump_fd;
    int (*pump)(void *opaque, int64_t deadline);
    void *opaque;
    unsigned fragment;
};

static inline uint64_t ax_get(const uint8_t *bytes, unsigned count)
{
    uint64_t value = 0;
    for (unsigned index = 0; index < count; index++) {
        value |= (uint64_t)bytes[index] << (8 * index);
    }
    return value;
}

static inline void ax_put(uint8_t *bytes, uint64_t value, unsigned count)
{
    for (unsigned index = 0; index < count; index++) {
        bytes[index] = (uint8_t)(value >> (8 * index));
    }
}

static inline int64_t ax_now(void)
{
    struct timespec current;
    clock_gettime(CLOCK_MONOTONIC, &current);
    return (int64_t)current.tv_sec * 1000 + current.tv_nsec / 1000000;
}

static inline int ax_encode(uint8_t *header, const struct ax_frame *frame)
{
    unsigned kind = frame->kind & ~AX_RESPONSE;
    if (!frame->id || kind < AX_HELLO || kind > AX_GOODBYE ||
        frame->length > AX_MAX_PAYLOAD || frame->status > AX_BUS_ERROR ||
        (!(frame->kind & AX_RESPONSE) && frame->status)) {
        return -EPROTO;
    }
    memset(header, 0, AX_HEADER);
    ax_put(header, AX_MAGIC, 4);
    ax_put(header + 4, 1, 2);
    ax_put(header + 8, frame->kind, 2);
    ax_put(header + 12, frame->status, 4);
    ax_put(header + 16, frame->id, 8);
    ax_put(header + 24, frame->address, 8);
    ax_put(header + 32, frame->length, 4);
    return 0;
}

static inline int ax_decode(struct ax_frame *frame, const uint8_t *header)
{
    uint8_t canonical[AX_HEADER];
    frame->kind = (uint16_t)ax_get(header + 8, 2);
    frame->status = (uint32_t)ax_get(header + 12, 4);
    frame->id = ax_get(header + 16, 8);
    frame->address = ax_get(header + 24, 8);
    frame->length = (uint32_t)ax_get(header + 32, 4);
    if (ax_encode(canonical, frame) || memcmp(header, canonical, AX_HEADER)) {
        return -EPROTO;
    }
    return 0;
}

static inline int ax_ready(struct ax_link *link, short events, int64_t deadline)
{
    for (;;) {
        int64_t remaining = deadline - ax_now();
        struct pollfd descriptors[2] = {
            {link->fd, events, 0}, {link->pump_fd, POLLIN, 0}
        };
        if (remaining <= 0) {
            return -ETIMEDOUT;
        }
        int result = poll(descriptors, 2, remaining > INT_MAX ? INT_MAX : (int)remaining);
        if (result < 0 && errno == EINTR) {
            continue;
        }
        if (result < 0) {
            return -errno;
        }
        if (!result) {
            return -ETIMEDOUT;
        }
        if (descriptors[1].revents) {
            result = link->pump(link->opaque, deadline);
            if (result) {
                return result;
            }
        }
        if (descriptors[0].revents & POLLNVAL) {
            return -EBADF;
        }
        if (descriptors[0].revents & (events | POLLHUP | POLLERR)) {
            return 0;
        }
    }
}

static inline int ax_transfer(struct ax_link *link, uint8_t *bytes, unsigned count,
                              int sending, int64_t deadline)
{
    while (count) {
        int result = ax_ready(link, sending ? POLLOUT : POLLIN, deadline);
        if (result) {
            return result;
        }
        unsigned chunk = count;
        if (sending && link->fragment && chunk > link->fragment) {
            chunk = link->fragment;
        }
        ssize_t transferred = sending ? send(link->fd, bytes, chunk, MSG_NOSIGNAL | MSG_DONTWAIT)
                                     : recv(link->fd, bytes, chunk, MSG_DONTWAIT);
        if (transferred < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) {
            continue;
        }
        if (transferred <= 0) {
            return transferred ? -errno : -ECONNRESET;
        }
        bytes += transferred;
        count -= (unsigned)transferred;
    }
    return 0;
}

static inline int ax_send(struct ax_link *link, const struct ax_frame *frame, int64_t deadline)
{
    uint8_t bytes[AX_HEADER + AX_MAX_PAYLOAD];
    int result = ax_encode(bytes, frame);
    if (result) {
        return result;
    }
    memcpy(bytes + AX_HEADER, frame->payload, frame->length);
    return ax_transfer(link, bytes, AX_HEADER + frame->length, 1, deadline);
}

static inline int ax_receive(struct ax_link *link, struct ax_frame *frame, int64_t deadline)
{
    uint8_t header[AX_HEADER];
    int result = ax_transfer(link, header, AX_HEADER, 0, deadline);
    if (!result) {
        result = ax_decode(frame, header);
    }
    if (!result) {
        result = ax_transfer(link, frame->payload, frame->length, 0, deadline);
    }
    return result;
}

static inline int ax_reply_matches(const struct ax_frame *request, const struct ax_frame *reply)
{
    return reply->kind == (request->kind | AX_RESPONSE) &&
           reply->id == request->id && reply->address == request->address;
}

static inline void ax_capabilities(uint8_t *payload, uint64_t session)
{
    ax_put(payload, 32, 4);
    ax_put(payload + 4, 64, 4);
    ax_put(payload + 8, 16, 4);
    ax_put(payload + 12, 1, 4);
    ax_put(payload + 16, 100000000, 4);
    ax_put(payload + 20, AX_MAX_DMA, 4);
    ax_put(payload + 24, session, 8);
}

#endif

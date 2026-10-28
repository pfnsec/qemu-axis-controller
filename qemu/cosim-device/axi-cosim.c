#include "qemu/osdep.h"
#include <sys/un.h>
#include "hw/sysbus.h"
#include "hw/irq.h"
#include "hw/qdev-properties.h"
#include "migration/vmstate.h"
#include "qapi/error.h"
#include "qemu/error-report.h"
#include "qemu/main-loop.h"
#include "qemu/module.h"
#include "qemu/rcu.h"
#include "system/address-spaces.h"
#include "system/runstate.h"
#include "wire.h"

#define TYPE_AXI_COSIM "axi-cosim"
OBJECT_DECLARE_SIMPLE_TYPE(AxiCosim, AXI_COSIM)

struct AxiCosim {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    char *command_path;
    char *reverse_path;
    uint32_t timeout_ms;
    bool spike;
    uint64_t session;
    struct ax_link command;
    struct ax_link reverse;
    uint64_t next_id;
    uint64_t reverse_id;
    bool failed;
    bool pending;
    Notifier shutdown;
};

static void axi_fail(AxiCosim *device, int result)
{
    assert(bql_locked());
    if (!device->failed) {
        error_report("AXI_COSIM_FAULT error=%d (%s) pending=%d bql=1", result,
                     strerror(-result), device->pending);
    }
    device->failed = true;
    qemu_set_irq(device->irq, 0);
    if (device->reverse.fd >= 0) {
        qemu_set_fd_handler(device->reverse.fd, NULL, NULL, NULL);
        shutdown(device->reverse.fd, SHUT_RDWR);
    }
    if (device->command.fd >= 0) {
        shutdown(device->command.fd, SHUT_RDWR);
    }
}

static bool axi_ram_range(uint64_t address, uint32_t count)
{
    if (!count || count > AX_MAX_DMA || address > UINT64_MAX - count) {
        return false;
    }
    while (count) {
        MemoryRegionSection section = memory_region_find(get_system_memory(), address, count);
        bool valid = section.mr && section.offset_within_address_space == address &&
                     memory_region_is_ram(section.mr) && !memory_region_is_ram_device(section.mr) &&
                     !section.readonly && !memory_region_is_rom(section.mr);
        uint64_t length = int128_get64(section.size);
        if (section.mr) {
            memory_region_unref(section.mr);
        }
        if (!valid || !length || length > count) {
            return false;
        }
        address += length;
        count -= length;
    }
    return true;
}

static uint32_t axi_dma(struct ax_frame *request, struct ax_frame *reply)
{
    uint32_t count;
    bool writing = request->kind == AX_DMA_WRITE;
    MemTxResult result = MEMTX_OK;
    MemTxAttrs attributes = MEMTXATTRS_UNSPECIFIED;
    attributes.memory = true;
    if (request->length < 4) {
        return AX_PROTOCOL;
    }
    count = ax_get(request->payload, 4);
    if (!count || count > AX_MAX_DMA || request->length != (writing ? 4 + 2 * count : 4)) {
        return AX_RANGE;
    }
    if (request->address % 8 || count % 8) {
        return AX_ALIGNMENT;
    }
    if (writing) {
        for (unsigned index = 0; index < count; index++) {
            if (request->payload[4 + count + index] > 1) {
                return AX_PROTOCOL;
            }
        }
    }
    RCU_READ_LOCK_GUARD();
    if (!axi_ram_range(request->address, count) ||
        !address_space_access_valid(&address_space_memory, request->address, count, writing, attributes)) {
        return AX_RANGE;
    }
    if (!writing) {
        result = address_space_read(&address_space_memory, request->address, attributes, reply->payload, count);
        reply->length = result == MEMTX_OK ? count : 0;
    } else {
        unsigned index = 0;
        while (index < count && result == MEMTX_OK) {
            unsigned start;
            while (index < count && !request->payload[4 + count + index]) {
                index++;
            }
            start = index;
            while (index < count && request->payload[4 + count + index]) {
                index++;
            }
            if (index != start) {
                result = address_space_write(&address_space_memory, request->address + start,
                                             attributes, request->payload + 4 + start, index - start);
            }
        }
    }
    return result == MEMTX_OK ? AX_OK : AX_BUS_ERROR;
}

static int axi_reverse(void *opaque, int64_t deadline)
{
    AxiCosim *device = opaque;
    struct ax_frame request, reply = {0};
    int result;
    assert(bql_locked());
    if (device->failed) {
        return -ESHUTDOWN;
    }
    result = ax_receive(&device->reverse, &request, deadline);
    if (result) {
        return result;
    }
    if (request.id <= device->reverse_id || (request.kind & AX_RESPONSE)) {
        return -EPROTO;
    }
    device->reverse_id = request.id;
    if (request.kind == AX_IRQ) {
        if (request.address || request.length != 4 || ax_get(request.payload, 4) > 1) {
            return -EPROTO;
        }
        qemu_set_irq(device->irq, ax_get(request.payload, 4));
        fprintf(stderr, "AXI_IRQ level=%u pending=%d bql=1 thread=%d\n",
                (unsigned)ax_get(request.payload, 4), device->pending, qemu_get_thread_id());
        return 0;
    }
    if (request.kind != AX_DMA_READ && request.kind != AX_DMA_WRITE) {
        return -EPROTO;
    }
    reply.kind = request.kind | AX_RESPONSE;
    reply.id = request.id;
    reply.address = request.address;
    reply.status = axi_dma(&request, &reply);
    fprintf(stderr, "AXI_DMA kind=%u id=%"PRIu64" address=0x%"PRIx64" status=%u pending=%d bql=1 thread=%d\n",
            request.kind, request.id, request.address, reply.status, device->pending, qemu_get_thread_id());
    return ax_send(&device->reverse, &reply, deadline);
}

static void axi_readable(void *opaque)
{
    AxiCosim *device = opaque;
    struct pollfd descriptor = {.fd = device->reverse.fd, .events = POLLIN};
    if (device->failed || poll(&descriptor, 1, 0) <= 0) {
        return;
    }
    int result = axi_reverse(device, ax_now() + device->timeout_ms);
    if (result) {
        axi_fail(device, result);
    }
}

static int axi_rpc(AxiCosim *device, struct ax_frame *request, struct ax_frame *reply)
{
    int result;
    int64_t deadline = ax_now() + device->timeout_ms;
    assert(bql_locked());
    if (device->failed) {
        return -ESHUTDOWN;
    }
    request->id = ++device->next_id;
    device->pending = true;
    result = ax_send(&device->command, request, deadline);
    if (!result) {
        result = ax_receive(&device->command, reply, deadline);
    }
    if (!result && !ax_reply_matches(request, reply)) {
        result = -EPROTO;
    }
    fprintf(stderr, "AXI_REPLY kind=%u id=%"PRIu64" address=0x%"PRIx64" error=%d bql=1\n",
            request->kind, request->id, request->address, result);
    if (result) {
        axi_fail(device, result);
    }
    device->pending = false;
    return result;
}

static MemTxResult axi_read(void *opaque, hwaddr address, uint64_t *value,
                            unsigned size, MemTxAttrs attributes)
{
    AxiCosim *device = opaque;
    struct ax_frame request = {.kind = AX_MMIO_READ, .address = address}, reply;
    *value = UINT32_MAX;
    if (axi_rpc(device, &request, &reply)) {
        return MEMTX_OK;
    }
    if (reply.status == AX_BUS_ERROR && !reply.length) {
        return MEMTX_ERROR;
    }
    if (reply.status || reply.length != 4) {
        axi_fail(device, -EPROTO);
        return MEMTX_OK;
    }
    *value = ax_get(reply.payload, 4);
    return MEMTX_OK;
}

static MemTxResult axi_write(void *opaque, hwaddr address, uint64_t value,
                             unsigned size, MemTxAttrs attributes)
{
    AxiCosim *device = opaque;
    struct ax_frame request = {.kind = AX_MMIO_WRITE, .address = address, .length = 4}, reply;
    ax_put(request.payload, value, 4);
    if (axi_rpc(device, &request, &reply)) {
        return MEMTX_OK;
    }
    if (reply.status == AX_BUS_ERROR && !reply.length) {
        return MEMTX_ERROR;
    }
    if (reply.status || reply.length) {
        axi_fail(device, -EPROTO);
    }
    return MEMTX_OK;
}

static const MemoryRegionOps axi_ops = {
    .read_with_attrs = axi_read,
    .write_with_attrs = axi_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {.min_access_size = 4, .max_access_size = 4, .unaligned = false},
    .impl = {.min_access_size = 4, .max_access_size = 4, .unaligned = false},
};

static int axi_connect(const char *path)
{
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    int descriptor, result;
    if (!path || strlen(path) >= sizeof(address.sun_path)) {
        return -EINVAL;
    }
    memcpy(address.sun_path, path, strlen(path) + 1);
    descriptor = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (descriptor < 0) {
        return -errno;
    }
    if (connect(descriptor, (struct sockaddr *)&address, sizeof(address))) {
        result = -errno;
        close(descriptor);
        return result;
    }
    return descriptor;
}

static void axi_reset(DeviceState *state)
{
    AxiCosim *device = AXI_COSIM(state);
    struct ax_frame request = {.kind = AX_RESET}, reply;
    qemu_set_irq(device->irq, 0);
    if (!axi_rpc(device, &request, &reply) && (reply.status || reply.length)) {
        axi_fail(device, -EPROTO);
    }
}

static void axi_close(DeviceState *state)
{
    AxiCosim *device = AXI_COSIM(state);
    if (device->shutdown.notify) {
        notifier_remove(&device->shutdown);
        device->shutdown.notify = NULL;
    }
    if (device->reverse.fd >= 0) {
        qemu_set_fd_handler(device->reverse.fd, NULL, NULL, NULL);
        close(device->reverse.fd);
        device->reverse.fd = -1;
    }
    if (device->command.fd >= 0) {
        close(device->command.fd);
        device->command.fd = -1;
    }
}

static void axi_shutdown(Notifier *notifier, void *opaque)
{
    AxiCosim *device = container_of(notifier, AxiCosim, shutdown);
    struct ax_frame request = {.kind = AX_GOODBYE}, reply;
    if (!axi_rpc(device, &request, &reply) && (reply.status || reply.length)) {
        axi_fail(device, -EPROTO);
    }
    axi_close(DEVICE(device));
}

static void axi_realize(DeviceState *state, Error **errp)
{
    AxiCosim *device = AXI_COSIM(state);
    struct ax_link *links[] = {&device->command, &device->reverse};
    const char *paths[] = {device->command_path, device->reverse_path};
    if (!device->session || !device->timeout_ms || device->timeout_ms > 10000) {
        error_setg(errp, "axi-cosim needs a nonzero session and timeout-ms in 1..10000");
        return;
    }
    for (unsigned index = 0; index < 2; index++) {
        struct ax_frame request = {.kind = AX_HELLO, .id = 1, .length = 32}, reply;
        int result;
        int64_t deadline = ax_now() + device->timeout_ms;
        links[index]->fd = axi_connect(paths[index]);
        result = links[index]->fd < 0 ? links[index]->fd : 0;
        ax_capabilities(request.payload, device->session);
        if (!result) {
            result = ax_send(links[index], &request, deadline);
        }
        if (!result) {
            result = ax_receive(links[index], &reply, deadline);
        }
        if (!result && (!ax_reply_matches(&request, &reply) || reply.status || reply.length != 32 ||
                        memcmp(reply.payload, request.payload, 32))) {
            result = -EPROTO;
        }
        if (result) {
            error_setg(errp, "axi-cosim channel %u handshake: %s", index, strerror(-result));
            axi_close(state);
            return;
        }
    }
    device->next_id = 1;
    device->reverse_id = 1;
    device->command.pump_fd = device->reverse.fd;
    device->command.pump = axi_reverse;
    device->command.opaque = device;
    qemu_set_fd_handler(device->reverse.fd, axi_readable, NULL, device);
    device->shutdown.notify = axi_shutdown;
    qemu_register_shutdown_notifier(&device->shutdown);
    fprintf(stderr, "AXI_COSIM_READY session=%"PRIu64" bql=%d\n", device->session, bql_locked());
}

static void axi_init(Object *object)
{
    AxiCosim *device = AXI_COSIM(object);
    device->command.fd = device->reverse.fd = -1;
    device->command.pump_fd = device->reverse.pump_fd = -1;
    memory_region_init_io(&device->mmio, object, &axi_ops, device, TYPE_AXI_COSIM, 0x1000);
    sysbus_init_mmio(SYS_BUS_DEVICE(object), &device->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(object), &device->irq);
}

static const VMStateDescription axi_vmstate = {
    .name = TYPE_AXI_COSIM,
    .unmigratable = true,
};

static const Property axi_properties[] = {
    DEFINE_PROP_STRING("command", AxiCosim, command_path),
    DEFINE_PROP_STRING("reverse", AxiCosim, reverse_path),
    DEFINE_PROP_UINT32("timeout-ms", AxiCosim, timeout_ms, 2000),
    DEFINE_PROP_UINT64("session", AxiCosim, session, 0),
    DEFINE_PROP_BOOL("spike", AxiCosim, spike, true),
};

static void axi_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *device = DEVICE_CLASS(klass);
    device->realize = axi_realize;
    device->unrealize = axi_close;
    device->vmsd = &axi_vmstate;
    device->user_creatable = true;
    device->hotpluggable = false;
    device_class_set_legacy_reset(device, axi_reset);
    device_class_set_props(device, axi_properties);
}

static const TypeInfo axi_type = {
    .name = TYPE_AXI_COSIM,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(AxiCosim),
    .instance_init = axi_init,
    .class_init = axi_class_init,
};

static void axi_register(void)
{
    type_register_static(&axi_type);
}

type_init(axi_register)

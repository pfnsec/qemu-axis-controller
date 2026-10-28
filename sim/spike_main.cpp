#ifdef AXI_FRAMEFX
#include "Vframefx_core.h"
#include "axi/model.hpp"
using SocketModel = axi::ClockedModel<Vframefx_core>;
static constexpr unsigned job_timeout_ms = 30000;
#else
#include "axi/spike_model.hpp"
using SocketModel = axi::SpikeModel;
static constexpr unsigned job_timeout_ms = 5000;
#endif
#include "axi/socket_memory.hpp"
#include <csignal>
#include <cstdio>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

using axi::checked;
using axi::require;

static volatile sig_atomic_t stopping;

static void stop_signal(int)
{
    stopping = 1;
}

static int listener(const std::string &path)
{
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    require(path.size() < sizeof(address.sun_path), "socket path too long");
    memcpy(address.sun_path, path.c_str(), path.size() + 1);
    int descriptor = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    require(descriptor >= 0, "socket failed");
    require(!bind(descriptor, reinterpret_cast<sockaddr *>(&address), sizeof(address)), "bind failed");
    require(!chmod(path.c_str(), 0600) && !listen(descriptor, 1), "listen failed");
    return descriptor;
}

static ax_link accept_link(int descriptor, uint64_t session, unsigned fragment)
{
    ax_link waiting{descriptor, -1, nullptr, nullptr, 0};
    checked(ax_ready(&waiting, POLLIN, ax_now() + 30000), "accept wait");
    ax_link link{accept4(descriptor, nullptr, nullptr, SOCK_CLOEXEC | SOCK_NONBLOCK), -1,
                 nullptr, nullptr, fragment};
    close(descriptor);
    require(link.fd >= 0, "accept failed");
    ax_frame request{}, reply{};
    checked(ax_receive(&link, &request, ax_now() + 5000), "HELLO receive");
    ax_capabilities(reply.payload, session);
    require(request.kind == AX_HELLO && request.id == 1 && request.address == 0 &&
            request.length == 32 && !memcmp(request.payload, reply.payload, 32), "HELLO mismatch");
    reply = request;
    reply.kind |= AX_RESPONSE;
    checked(ax_send(&link, &reply, ax_now() + 5000), "HELLO reply");
    return link;
}

class Session {
    ax_link command, reverse;
    uint64_t command_id = 1, reverse_id = 1;
    axi::FaultPlan fault;
    unsigned matched_mmios = 0;
    bool inject_mmio = false;
    axi::SocketMemory backend;
    SocketModel model;
    bool previous_irq = false;
    ax_frame request{};
    int64_t command_deadline = 0, job_deadline = 0;
    uint64_t assertions = 0, mmios = 0, resets = 1;

    void irq()
    {
        if (static_cast<bool>(model.dut.irq) == previous_irq) return;
        previous_irq = model.dut.irq;
        ax_frame notification{};
        notification.kind = AX_IRQ;
        notification.id = ++reverse_id;
        notification.length = 4;
        ax_put(notification.payload, previous_irq, 4);
        checked(ax_send(&reverse, &notification, ax_now() + 5000), "IRQ send");
        if (previous_irq) assertions++;
        printf("RTL_IRQ level=%d cycle=%llu\n", previous_irq, static_cast<unsigned long long>(model.cycles));
    }

    bool receive()
    {
        int result = ax_receive(&command, &request, ax_now() + 5000);
        if (result == -ECONNRESET && !model.dut.debug_busy && !model.memory.busy()) return false;
        checked(result, "command receive");
        require(!(request.kind & AX_RESPONSE) && request.id > command_id, "command ordering");
        command_id = request.id;
        ax_frame reply = request;
        reply.kind |= AX_RESPONSE;
        reply.length = 0;
        if (request.kind == AX_RESET || request.kind == AX_GOODBYE) {
            require(request.length == 0 && request.address == 0, "control payload");
            model.reset(); resets++; irq(); job_deadline = 0;
            checked(ax_send(&command, &reply, ax_now() + 5000), "control reply");
            if (request.kind == AX_GOODBYE) {
                ax_frame terminal{};
                require(ax_receive(&command, &terminal, ax_now() + 5000) == -ECONNRESET,
                        "GOODBYE must be followed by command closure");
            }
            return request.kind != AX_GOODBYE;
        }
        if (request.kind != AX_MMIO_READ && request.kind != AX_MMIO_WRITE) reply.status = AX_UNSUPPORTED;
        else if (request.address >= 4096) reply.status = AX_RANGE;
        else if (request.address % 4) reply.status = AX_ALIGNMENT;
        else if (request.length != (request.kind == AX_MMIO_WRITE ? 4U : 0U)) reply.status = AX_PROTOCOL;
        if (reply.status) {
            checked(ax_send(&command, &reply, ax_now() + 5000), "rejection reply");
            return true;
        }
        model.lite.begin(request.kind == AX_MMIO_WRITE, request.address,
                         request.length ? ax_get(request.payload, 4) : 0, 15, model.cycles);
        inject_mmio = request.kind == fault.kind && request.address == fault.address && ++matched_mmios == fault.ordinal;
        command_deadline = ax_now() + 5000;
        return true;
    }

public:
    Session(ax_link command_link, ax_link reverse_link, unsigned seed, const axi::FaultPlan &plan,
            const std::string &waveform)
        : command(command_link), reverse(reverse_link), fault(plan), backend(reverse, reverse_id, plan),
          model(backend, seed, waveform, [](const std::string &event) { printf("RTL_BUS %s\n", event.c_str()); }) {}

    void run()
    {
        try {
            while (!stopping) {
                if (!model.lite.busy()) {
                    pollfd descriptor{command.fd, POLLIN, 0};
                    int result = poll(&descriptor, 1, model.dut.debug_busy || model.memory.busy() ? 0 : 20);
                    require(result >= 0 || errno == EINTR, "command poll");
                    if (result > 0 && !receive()) break;
                }
                bool complete = model.step();
                bool active = model.dut.debug_busy || model.memory.busy();
                if (!active) job_deadline = 0;
                else if (!job_deadline) job_deadline = ax_now() + job_timeout_ms;
                irq();
                if (complete) {
                    axi::LiteResult value = model.lite.value();
                    ax_frame reply{};
                    reply.kind = request.kind | AX_RESPONSE; reply.id = request.id; reply.address = request.address;
                    reply.status = value.response == axi::Okay ? AX_OK : AX_BUS_ERROR;
                    if (request.kind == AX_MMIO_READ && !reply.status) {
                        reply.length = 4;
                        ax_put(reply.payload, value.data, 4);
                    }
                    if (inject_mmio) {
                        printf("RTL_INJECTED_MMIO mode=%s kind=%u address=0x%llx ordinal=%u\n", fault.mode.c_str(),
                               request.kind, static_cast<unsigned long long>(request.address), fault.ordinal);
                        if (fault.mode == "mmio-disconnect") {
                            shutdown(command.fd, SHUT_RDWR);
                            throw std::runtime_error("injected MMIO disconnect after RTL acceptance");
                        }
                        require(reply.status == AX_OK && reply.length == 4, "MMIO corruption requires a successful read");
                        ax_put(reply.payload, fault.value, 4);
                    }
                    checked(ax_send(&command, &reply, command_deadline), "MMIO reply");
                    mmios++;
                }
                require(!model.lite.busy() || ax_now() < command_deadline, "AXI-Lite timeout");
                require(!active || ax_now() < job_deadline, "DMA job timeout");
            }
        } catch (...) {
            fprintf(stderr, "RECENT_AXI\n%s", model.history.tail().c_str());
            throw;
        }
        printf("RTL_SUMMARY cycles=%llu reads=%u writes=%u irqs=%llu mmios=%llu resets=%llu max_read=%u max_write=%u\n",
               static_cast<unsigned long long>(model.cycles), model.memory.read_calls, model.memory.write_calls,
               static_cast<unsigned long long>(assertions), static_cast<unsigned long long>(mmios),
               static_cast<unsigned long long>(resets), model.memory.longest_read, model.memory.longest_write);
    }
};

int main(int argc, char **argv)
{
    setvbuf(stdout, nullptr, _IOLBF, 0);
    signal(SIGTERM, stop_signal);
    signal(SIGINT, stop_signal);
    try {
        require(argc == 6 || argc == 7, "usage: SIMULATOR SOCKET_DIRECTORY SESSION SEED FRAGMENT FAULT [VCD]");
        Verilated::traceEverOn(argc == 7);
        std::string directory = argv[1];
        uint64_t session = axi::number(argv[2], UINT64_MAX, true);
        unsigned seed = axi::number(argv[3], UINT32_MAX), fragment = axi::number(argv[4], AX_HEADER + AX_MAX_PAYLOAD);
        axi::FaultPlan fault(argv[5]);
        int command_listener = listener(directory + "/command.sock");
        int reverse_listener = listener(directory + "/reverse.sock");
        printf("RTL_LISTENING seed=%u fragment=%u fault=%s\n", seed, fragment, argv[5]);
        ax_link command = accept_link(command_listener, session, fragment);
        ax_link reverse = accept_link(reverse_listener, session, fragment);
        Session simulator(command, reverse, seed, fault, argc == 7 ? argv[6] : "");
        simulator.run();
        close(command.fd); close(reverse.fd);
        return 0;
    } catch (const std::exception &error) {
        fprintf(stderr, "RTL_FAILURE %s\n", error.what());
        return 1;
    }
}

#ifndef AXI_LAB_SOCKET_MEMORY_HPP
#define AXI_LAB_SOCKET_MEMORY_HPP

#include "fabric.hpp"
#include "wire.h"
#include "options.hpp"
#include <cstdio>
#include <unistd.h>

namespace axi {

inline void checked(int result, const char *operation)
{
    if (result) throw std::runtime_error(std::string(operation) + ": " + strerror(-result));
}

class SocketMemory : public Memory {
    ax_link &link;
    uint64_t &sequence;
    std::string fault;
    unsigned transfers = 0, fault_at = 1;
    unsigned error_transfers = 0;

    ax_frame transfer(unsigned kind, uint64_t address, unsigned count,
                      const std::vector<uint8_t> &bytes = {}, const std::vector<uint8_t> &enables = {})
    {
        require(count && count <= AX_MAX_DMA, "socket DMA size outside protocol limit");
        bool injecting = ++transfers == fault_at;
        if (((fault == "read-error" && kind == AX_DMA_READ) || (fault == "write-error" && kind == AX_DMA_WRITE)) &&
            ++error_transfers == fault_at) {
            ax_frame failed{};
            failed.status = AX_BUS_ERROR;
            printf("RTL_INJECTED_AXI kind=%u ordinal=%u address=0x%llx\n", kind, error_transfers,
                   static_cast<unsigned long long>(address));
            return failed;
        }
        if (injecting && fault == "disconnect") {
            shutdown(link.fd, SHUT_RDWR);
            throw std::runtime_error("injected disconnect");
        }
        if (injecting && fault == "stall") {
            poll(nullptr, 0, 6000);
            throw std::runtime_error("injected stall");
        }
        if (injecting && (fault == "malformed" || fault == "partial")) {
            uint8_t header[AX_HEADER];
            ax_frame malformed{};
            malformed.kind = AX_DMA_READ; malformed.id = ++sequence;
            checked(ax_encode(header, &malformed), "encode malformed fixture");
            ax_put(header + 32, AX_MAX_PAYLOAD + 1, 4);
            checked(ax_transfer(&link, header, fault == "partial" ? 10 : AX_HEADER, 1, ax_now() + 2000), "malformed send");
            if (fault == "partial") poll(nullptr, 0, 6000);
            throw std::runtime_error("injected oversized/truncated frame");
        }
        ax_frame request{}, reply{};
        request.kind = kind; request.id = ++sequence; request.address = address;
        request.length = kind == AX_DMA_READ ? 4 : 4 + 2 * count;
        ax_put(request.payload, count, 4);
        if (kind == AX_DMA_WRITE) {
            require(bytes.size() == count && enables.size() == count, "socket DMA write size");
            memcpy(request.payload + 4, bytes.data(), count);
            memcpy(request.payload + 4 + count, enables.data(), count);
        }
        int64_t deadline = ax_now() + 5000;
        checked(ax_send(&link, &request, deadline), "DMA send");
        checked(ax_receive(&link, &reply, deadline), "DMA receive");
        require(ax_reply_matches(&request, &reply), "DMA response ID mismatch");
        require(reply.length == (reply.status || kind == AX_DMA_WRITE ? 0 : count), "DMA response length");
        printf("RTL_DMA kind=%u bytes=%u id=%llu status=%u\n", kind, count,
               static_cast<unsigned long long>(request.id), reply.status);
        return reply;
    }

    unsigned response(unsigned status)
    {
        return status == AX_OK ? Okay : status == AX_RANGE || status == AX_ALIGNMENT ? DecodeError : SlaveError;
    }

public:
    SocketMemory(ax_link &channel, uint64_t &next_id, const FaultPlan &plan)
        : link(channel), sequence(next_id), fault(plan.mode), fault_at(plan.ordinal) {}

    Transfer read(uint64_t address, unsigned count) override
    {
        ax_frame reply = transfer(AX_DMA_READ, address, count);
        return {response(reply.status), std::vector<uint8_t>(reply.payload, reply.payload + reply.length)};
    }

    unsigned write(uint64_t address, const std::vector<uint8_t> &bytes,
                   const std::vector<uint8_t> &enables) override
    {
        return response(transfer(AX_DMA_WRITE, address, bytes.size(), bytes, enables).status);
    }
};

}
#endif

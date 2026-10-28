#ifndef AXI_LAB_OPTIONS_HPP
#define AXI_LAB_OPTIONS_HPP

#include "fabric.hpp"
#include "wire.h"
#include <charconv>

namespace axi {

inline uint64_t number(const std::string &text, uint64_t maximum, bool nonzero = false)
{
    uint64_t value = 0;
    bool hexadecimal = text.size() > 2 && text.substr(0, 2) == "0x";
    auto parsed = std::from_chars(text.data() + (hexadecimal ? 2 : 0), text.data() + text.size(), value, hexadecimal ? 16 : 10);
    require(parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size() && value <= maximum && (!nonzero || value),
            "invalid unsigned argument: " + text);
    return value;
}

struct FaultPlan {
    std::string mode;
    unsigned ordinal = 1, kind = 0, address = 0, value = 0;

    explicit FaultPlan(const std::string &text)
    {
        std::vector<std::string> fields;
        size_t start = 0, separator;
        do {
            separator = text.find(':', start);
            fields.push_back(text.substr(start, separator == std::string::npos ? separator : separator - start));
            start = separator + 1;
        } while (separator != std::string::npos);
        mode = fields[0];
        if (mode == "mmio-disconnect" || mode == "mmio-corrupt") {
            require(fields.size() == 4, "MMIO fault requires three arguments");
            if (mode == "mmio-disconnect") {
                kind = number(fields[1], AX_MMIO_WRITE);
                require(kind == AX_MMIO_READ || kind == AX_MMIO_WRITE, "MMIO fault kind");
                address = number(fields[2], 4092);
                ordinal = number(fields[3], UINT32_MAX, true);
            } else {
                kind = AX_MMIO_READ;
                address = number(fields[1], 4092);
                ordinal = number(fields[2], UINT32_MAX, true);
                value = number(fields[3], UINT32_MAX);
            }
            require(address % 4 == 0, "MMIO fault alignment");
        } else {
            require(mode == "none" || mode == "disconnect" || mode == "stall" || mode == "malformed" ||
                    mode == "partial" || mode == "read-error" || mode == "write-error", "unknown fault mode");
            require(fields.size() == 1 || (fields.size() == 2 && mode != "none"), "invalid fault arguments");
            if (fields.size() == 2) ordinal = number(fields[1], UINT32_MAX, true);
        }
    }
};

}
#endif

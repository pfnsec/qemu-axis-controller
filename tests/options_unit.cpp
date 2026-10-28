#include "axi/options.hpp"
#include <iostream>

int main()
{
    try {
        axi::require(axi::number("0xffffffffffffffff", UINT64_MAX) == UINT64_MAX, "u64 maximum");
        axi::require(axi::number("4294967295", UINT32_MAX) == UINT32_MAX, "u32 maximum");
        axi::require(axi::number("0", 0) == 0, "zero");
        for (const auto &value : {"", "-1", "+1", " 1", "1 ", "1garbage", "0x", "0xgg", "0x10000000000000000",
                                  "18446744073709551616", "1844674407370955161600000000000"}) {
            bool rejected = false;
            try { axi::number(value, UINT64_MAX); } catch (const std::runtime_error &) { rejected = true; }
            axi::require(rejected, std::string("accepted invalid number: ") + value);
        }
        for (const auto &value : {"none", "disconnect", "stall:5", "malformed:0xffffffff", "partial:1", "read-error:7", "write-error:3",
                                  "mmio-disconnect:2:4:2", "mmio-disconnect:3:0xffc:1", "mmio-corrupt:4:2:0xffffffff"})
            axi::FaultPlan{value};
        for (const auto &value : {"", "other", "none:1", "read-error:0", "stall:-1", "partial:1garbage", "disconnect:",
                                  "malformed:4294967296", "stall:1:2", "mmio-disconnect:1:4:2", "mmio-disconnect:4:4:2",
                                  "mmio-disconnect:2:3:2", "mmio-disconnect:2:4096:2", "mmio-disconnect:2:4:0",
                                  "mmio-corrupt:4:2:4294967296", "mmio-corrupt:4:2", "mmio-corrupt:4:2:0:",
                                  "mmio-corrupt:4:-1:0", "mmio-corrupt:4:2:-1"}) {
            bool rejected = false;
            try { axi::FaultPlan{value}; } catch (const std::runtime_error &) { rejected = true; }
            axi::require(rejected, std::string("accepted invalid fault: ") + value);
        }
        for (const auto &value : {"0", "4294967296"}) {
            bool rejected = false;
            try { axi::number(value, UINT32_MAX, true); } catch (const std::runtime_error &) { rejected = true; }
            axi::require(rejected, "ordinal bound");
        }
        axi::FaultPlan fault("mmio-corrupt:0x54:12:0x12345678");
        axi::require(fault.kind == AX_MMIO_READ && fault.address == 0x54 && fault.ordinal == 12 && fault.value == 0x12345678,
                     "MMIO fault parameters");
        std::cout << "OPTIONS_UNIT_PASS rejected=32\n";
    } catch (const std::exception &error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}

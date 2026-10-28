#include "axi/fabric.hpp"
#include <cstdio>

struct Pins {
    uint64_t m_araddr = 0, m_awaddr = 0, m_wdata = 0, m_rdata = 0, m_wstrb = 0;
    unsigned m_arlen = 0, m_arsize = 3, m_arburst = 1, m_arvalid = 0, m_arready = 0;
    unsigned m_awlen = 0, m_awsize = 3, m_awburst = 1, m_awvalid = 0, m_awready = 0;
    unsigned m_wvalid = 0, m_wready = 0, m_wlast = 0;
    unsigned m_rvalid = 0, m_rready = 0, m_rresp = 0, m_rlast = 0;
    unsigned m_bvalid = 0, m_bready = 0, m_bresp = 0;
};

template<class Action> void rejects(Action action, const std::string &expected)
{
    try { action(); }
    catch (const std::runtime_error &error) {
        axi::require(std::string(error.what()).find(expected) != std::string::npos,
                     "Wrong rejection: " + std::string(error.what()));
        return;
    }
    throw std::runtime_error("Missing rejection: " + expected);
}

struct LitePins {
    uint32_t s_awaddr = 0, s_araddr = 0, s_wdata = 0, s_rdata = 0;
    unsigned s_awvalid = 0, s_awready = 0, s_wvalid = 0, s_wready = 0, s_wstrb = 0;
    unsigned s_arvalid = 0, s_arready = 0, s_rvalid = 0, s_rready = 0, s_rresp = 0;
    unsigned s_bvalid = 0, s_bready = 0, s_bresp = 0;
};

template<unsigned AddressBits> void lite_parameter()
{
    axi::LiteMaster<AddressBits> master(42, 20, {0, 0, false}, {0, 0, false}, {0, 0, false});
    LitePins pins;
    master.reset(pins);
    uint32_t last = (UINT64_C(1) << AddressBits) - 4;
    master.begin(true, last, 0x12345678, 5, 0);
    master.drive(pins, 0);
    axi::require(pins.s_awaddr == last && pins.s_wdata == 0x12345678 && pins.s_wstrb == 5, "Lite parameter payload");
    pins.s_wready = 1;
    master.sample(pins); master.commit(pins);
    master.drive(pins, 1);
    axi::require(pins.s_awvalid && !pins.s_wvalid, "Lite independent W acceptance");
    pins.s_awready = 1;
    master.sample(pins); master.commit(pins);
    master.drive(pins, 2);
    pins.s_bvalid = 1; pins.s_bresp = axi::SlaveError;
    master.sample(pins);
    axi::require(master.commit(pins) && master.value().response == axi::SlaveError, "Lite error propagation");
    pins.s_bvalid = 0;
    master.begin(false, last, 0, 15, 3);
    master.drive(pins, 3); pins.s_arready = 1;
    master.sample(pins); master.commit(pins);
    master.drive(pins, 4); pins.s_rvalid = 1; pins.s_rdata = 0x87654321;
    master.sample(pins);
    axi::require(master.commit(pins) && master.value().data == 0x87654321, "Lite read payload");
    rejects([&] { master.begin(false, 1, 0, 15, 5); }, "alignment");
    rejects([&] { master.begin(true, 0, 0, 16, 5); }, "strobes");
    if constexpr (AddressBits < 32)
        rejects([&] { master.begin(false, uint32_t(UINT64_C(1) << AddressBits), 0, 15, 5); }, "aperture");
    master.reset(pins);
    master.begin(true, 0, 0, 15, 5);
    rejects([&] { master.drive(pins, 25); }, "timeout");
    master.reset(pins);
    axi::require(!master.busy(), "Lite reset");
}

template<unsigned Width, unsigned Maximum> void concurrent(unsigned seed, bool data_first, unsigned beats)
{
    axi::Ram ram(0x1000, 0x4000);
    for (unsigned index = 0; index < ram.bytes.size(); index++) ram.bytes[index] = index * 37 + seed;
    auto original = ram.bytes;
    axi::History history;
    axi::MemorySlave<Width, Maximum> slave(ram, history, seed);
    Pins pins;
    slave.reset(pins);
    bool read_address = false, write_address = false, response = false;
    unsigned written = 0, read = 0;
    for (unsigned cycle = 0; cycle < 5000 && (!response || read < beats); cycle++) {
        pins.m_araddr = 0x1000; pins.m_arlen = beats - 1; pins.m_arsize = Width == 8 ? 3 : 2;
        pins.m_arvalid = !read_address;
        pins.m_awaddr = 0x2000; pins.m_awlen = beats - 1; pins.m_awsize = Width == 8 ? 3 : 2;
        pins.m_awvalid = !write_address && (!data_first || cycle >= 100);
        pins.m_wvalid = written < beats && (data_first || cycle >= 100);
        pins.m_wdata = UINT64_C(0x8877665544332211) ^ (uint64_t(written) * UINT64_C(0x0101010101010101));
        pins.m_wstrb = Width == 8 ? 0xa5 : 0x5;
        pins.m_wlast = written + 1 == beats;
        pins.m_rready = cycle > 20 && cycle % 7 == 0;
        pins.m_bready = cycle % 13 == 0;
        slave.drive(pins, cycle);
        if (pins.m_arvalid && pins.m_arready) read_address = true;
        if (pins.m_awvalid && pins.m_awready) write_address = true;
        if (pins.m_wvalid && pins.m_wready) written++;
        if (pins.m_rvalid && pins.m_rready) {
            uint64_t expected = 0;
            for (unsigned lane = 0; lane < Width; lane++) expected |= uint64_t(original[read * Width + lane]) << (8 * lane);
            axi::require(pins.m_rdata == expected && pins.m_rresp == 0 && pins.m_rlast == unsigned(read + 1 == beats), "R beat scoreboard");
            read++;
        }
        if (pins.m_bvalid) axi::require(write_address && written == beats && ram.writes == 1, "premature BVALID");
        if (pins.m_bvalid && pins.m_bready) { axi::require(pins.m_bresp == 0, "write response"); response = true; }
        slave.sample(pins); slave.commit(cycle);
    }
    axi::require(response && read == beats && ram.reads == 1 && ram.writes == 1, "coalescing/concurrency completion");
    for (unsigned beat = 0; beat < beats; beat++) {
        uint64_t data = UINT64_C(0x8877665544332211) ^ (uint64_t(beat) * UINT64_C(0x0101010101010101));
        for (unsigned lane = 0; lane < Width; lane++) {
            if ((Width == 8 ? 0xa5 : 0x5) & (1U << lane)) original[0x1000 + beat * Width + lane] = data >> (8 * lane);
        }
    }
    axi::require(ram.bytes == original, "WSTRB/guard scoreboard");
}

void lite_monitor_cases()
{
    for (unsigned test = 0; test < 10; test++) {
        axi::LiteMaster<> master(42, 100, {0, 0, false}, {0, 0, false}, {0, 50, false});
        LitePins pins;
        master.reset(pins);
        bool writing = test % 2 == 0;
        if (test >= 2) master.begin(writing, 0, 0, 15, 0);
        master.drive(pins, 0);
        if (test >= 6) {
            pins.s_awready = pins.s_wready = pins.s_arready = 1;
            master.sample(pins); master.commit(pins);
            master.drive(pins, 1);
        }
        if (writing) pins.s_bvalid = 1;
        else pins.s_rvalid = 1;
        if (test == 4) { pins.s_bvalid = 0; pins.s_rvalid = 1; }
        if (test == 5) { pins.s_rvalid = 0; pins.s_bvalid = 1; }
        if (test == 6 || test == 7) pins.s_bresp = pins.s_rresp = 1;
        if (test >= 8) {
            master.sample(pins); master.commit(pins);
            if (writing) pins.s_bresp = 2;
            else pins.s_rdata++;
        }
        rejects([&] { master.sample(pins); }, test < 6 ? "unsolicited response" :
                test < 8 ? "reserved response" : "VALID/payload changed");
    }
    for (bool writing : {false, true}) {
        axi::Ram ram(0x1000, 4096);
        axi::History history;
        axi::MemorySlave<> slave(ram, history, 42);
        Pins pins;
        slave.reset(pins);
        slave.hold_ar = slave.hold_aw = true;
        pins.m_arvalid = !writing; pins.m_awvalid = writing;
        pins.m_araddr = pins.m_awaddr = 0x1001;
        slave.drive(pins, 0);
        rejects([&] { slave.sample(pins); }, "aligned");
        axi::require(!ram.reads && !ram.writes, "Stalled illegal address reached backend");
    }
    rejects([] { axi::Ram ram(UINT64_MAX - 7, 16); }, "overflowing range");
    rejects([] { axi::Ram ram(0, 0); }, "empty/overflowing range");
}

void negative_cases()
{
    for (unsigned test = 0; test < 7; test++) {
        axi::Ram ram(0x1000, 0x4000);
        axi::History history;
        axi::MemorySlave<> slave(ram, history, 1);
        Pins pins;
        slave.reset(pins);
        pins.m_arvalid = 1; pins.m_araddr = 0x1000; pins.m_arlen = 15;
        std::string expected;
        if (test == 0) { pins.m_arlen = 16; expected = "burst length"; }
        if (test == 1) { pins.m_araddr = 0x1ff8; expected = "4-KiB"; }
        if (test == 2) { pins.m_araddr = 0x1001; expected = "aligned"; }
        if (test == 3) { pins.m_arsize = 2; expected = "full-width"; }
        if (test == 4) { pins.m_arburst = 2; expected = "INCR"; }
        if (test == 5) { pins.m_araddr = UINT64_MAX - 7; expected = "overflow"; }
        if (test == 6) { pins.m_arvalid = 0; pins.m_wvalid = 1; pins.m_wstrb = 256; expected = "strobes"; }
        slave.drive(pins, 0);
        rejects([&] { slave.sample(pins); slave.commit(0); }, expected);
        axi::require(!ram.reads && !ram.writes, "illegal transaction reached RAM");
    }
    for (bool early : {false, true}) {
        axi::Ram ram(0x1000, 0x4000);
        axi::History history;
        axi::MemorySlave<> slave(ram, history, 1);
        Pins pins;
        slave.reset(pins);
        pins.m_awvalid = pins.m_wvalid = 1; pins.m_awaddr = 0x2000;
        pins.m_awlen = early ? 1 : 0; pins.m_wlast = early; pins.m_wstrb = 255;
        slave.drive(pins, 0);
        rejects([&] { slave.sample(pins); slave.commit(0); }, early ? "early WLAST" : "missing WLAST");
        axi::require(!ram.writes, "malformed LAST reached RAM");
    }
    {
        axi::Ram ram(0x1000, 0x4000);
        axi::History history;
        axi::MemorySlave<> slave(ram, history, 1, 10);
        Pins pins;
        slave.reset(pins); slave.hold_ar = true;
        pins.m_arvalid = 1; pins.m_araddr = 0x1000;
        slave.drive(pins, 0); slave.sample(pins); slave.commit(0);
        pins.m_araddr = 0x1008;
        slave.drive(pins, 1);
        rejects([&] { slave.sample(pins); }, "AR: VALID/payload");
        slave.reset(pins); slave.hold_ar = false; slave.hold_r = true;
        pins.m_araddr = 0x1000;
        slave.drive(pins, 0); slave.sample(pins); slave.commit(0);
        pins.m_arvalid = 0;
        rejects([&] { slave.drive(pins, 10); }, "read cycle timeout");
        slave.reset(pins);
        axi::require(!slave.busy(), "reset failed to flush read queue");
    }
    {
        axi::Ram ram(0x1000, 0x4000);
        axi::History history;
        axi::MemorySlave<> slave(ram, history, 1, 10);
        Pins pins;
        slave.reset(pins);
        pins.m_wvalid = 1; pins.m_wlast = 1; pins.m_wstrb = 255;
        slave.drive(pins, 0); slave.sample(pins); slave.commit(0);
        pins.m_wvalid = 0;
        rejects([&] { slave.drive(pins, 10); }, "write cycle timeout");
        slave.reset(pins);
        axi::require(!slave.busy() && !ram.writes, "reset committed orphaned W data");
    }
    {
        axi::Ram ram(UINT64_C(0x100000000), 16);
        auto original = ram.bytes;
        axi::require(ram.write(UINT64_C(0x100000008), std::vector<uint8_t>(16, 1), std::vector<uint8_t>(16, 1)) == axi::DecodeError,
                     "RAM whole-range validation");
        axi::require(ram.write(UINT64_C(0x100000000), std::vector<uint8_t>(8, 1), std::vector<uint8_t>(8, 2)) == axi::DecodeError,
                     "RAM enable validation");
        axi::require(ram.bytes == original, "RAM rejected request changed bytes");
    }
}

int main()
{
    try {
        unsigned count = 0;
        for (unsigned seed : {1U, 42U, 2026U, 0xdeadbeefU}) {
            for (bool data_first : {false, true}) {
                for (unsigned beats : {1U, 2U, 15U, 16U}) {
                    concurrent<8, 16>(seed, data_first, beats); count++;
                    concurrent<4, 16>(seed, data_first, beats); count++;
                }
                concurrent<8, 1>(seed, data_first, 1); count++;
                concurrent<4, 4>(seed, data_first, 4); count++;
            }
        }
        negative_cases();
        lite_parameter<2>(); lite_parameter<12>(); lite_parameter<32>();
        lite_monitor_cases();
        printf("FABRIC_UNIT_PASS concurrent_cases=%u negative_cases=12\n", count);
        printf("LITE_UNIT_PASS address_configurations=3\n");
        printf("MONITOR_UNIT_PASS negative_cases=14\n");
        return 0;
    } catch (const std::exception &error) {
        fprintf(stderr, "FABRIC_UNIT_FAIL %s\n", error.what());
        return 1;
    }
}

#include "axi/spike_model.hpp"
#include <cstdio>
#include <filesystem>
#include <fstream>

using axi::require;

static constexpr uint64_t RamBase = UINT64_C(0x100000000);
static constexpr unsigned RamSize = 65536;
static constexpr std::array<uint8_t, 8> XorBytes = {0xf4, 0xe5, 0x96, 0x87, 0xa0, 0xb1, 0xc2, 0xd3};

struct Case {
    std::string name;
    unsigned source = 64, destination = 0x4040, beats = 16;
    uint8_t mask = 255;
    unsigned error = 0, read_fault = 0, write_fault = 0;
    unsigned written = 16;
};

static std::vector<Case> cases()
{
    std::vector<Case> tests;
    for (unsigned beats : {1U, 2U, 15U, 16U, 17U, 31U, 32U, 33U, 257U}) {
        for (unsigned source : {64U, 0xf80U, 0xff8U}) {
            for (unsigned destination : {0x4040U, 0x4fc0U, 0x4ff8U}) {
                for (uint8_t mask : {0, 0x55, 0xaa, 0xff})
                    tests.push_back({"burst", source, destination, beats, mask, 0, 0, 0, beats});
            }
        }
    }
    tests.push_back({"read-error-first", 64, 0x4040, 33, 255, 2, 1, 0, 0});
    tests.push_back({"write-error-first", 64, 0x4040, 33, 255, 3, 0, 1, 0});
    tests.push_back({"read-error-second", 64, 0x4040, 33, 255, 2, 2, 0, 16});
    tests.push_back({"write-error-second", 64, 0x4040, 33, 255, 3, 0, 2, 16});
    tests.push_back({"read-unmapped", RamSize, 0x4040, 16, 255, 2, 0, 0, 0});
    tests.push_back({"write-unmapped", 64, RamSize, 16, 255, 3, 0, 0, 0});
    tests.push_back({"zero-length", 64, 0x4040, 0, 255, 1, 0, 0, 0});
    tests.push_back({"misaligned", 65, 0x4040, 16, 255, 1, 0, 0, 0});
    tests.push_back({"overlap", 64, 64, 16, 255, 1, 0, 0, 0});
    tests.push_back({"overflow", 64, 0x4040, 2, 255, 1, 0, 0, 0});
    tests.push_back({"bad-rlast", 64, 0x4040, 1, 255, 4, 0, 0, 0});
    for (const char *phase : {"reset-ar", "reset-r", "reset-aw", "reset-w", "reset-b",
                              "reset-lite-aw", "reset-lite-w", "reset-lite-b", "reset-lite-r", "reset-irq"})
        tests.push_back({phase});
    tests.push_back({"consecutive-100", 64, 0x4040, 1, 255, 0, 0, 0, 1});
    return tests;
}

static void fill(axi::Ram &ram, unsigned seed)
{
    for (unsigned offset = 0; offset < ram.bytes.size(); offset++)
        ram.bytes[offset] = (offset * 37U + seed * 17U) ^ (offset >> 8);
}

template<class Predicate> static void until(axi::SpikeModel &model, Predicate complete)
{
    uint64_t deadline = model.cycles + 100000;
    while (!complete()) {
        require(model.cycles < deadline, "test cycle deadline");
        model.step();
    }
}

static void job(axi::SpikeModel &model, axi::Ram &ram, const Case &test, unsigned index, bool acknowledge = true)
{
    uint64_t source = test.name == "overflow" ? UINT64_MAX - 7 : RamBase + test.source;
    model.configure(source, RamBase + test.destination, test.beats, test.mask);
    bool masked = index % 3 == 0;
    model.write(0x14, masked ? 0 : 1);
    unsigned reads_before = ram.reads;
    model.write(8, index & 1 ? 3 : 1);
    if (!(index & 1) && test.error != 1) require(ram.reads > reads_before, "deferred START replied before DMA");
    if ((index & 1) && test.error != 1) require(ram.reads == reads_before, "early START did not return before DMA");
    if ((index & 1) && test.error != 1) {
        require(model.read(0x0c) & 1, "async START did not set busy");
        require(model.access(true, 0x2c, 99).response == axi::SlaveError, "busy reconfiguration accepted");
        require(model.access(true, 8, 1).response == axi::SlaveError, "busy START accepted");
        require(model.read(0x2c) == test.beats, "busy reconfiguration changed count");
    }
    until(model, [&] { return !model.dut.debug_busy; });
    require(!model.memory.busy(), "completion left an outstanding transaction");
    require(model.read(0x0c) == (test.error ? 2U : 0U), "RTL status mismatch");
    require(model.read(0x30) == test.error, "RTL error detail mismatch");
    require(model.read(0x10) == 1, "completion latch missing");
    if (masked) {
        require(!model.dut.irq, "IRQ asserted while masked");
        for (unsigned count = 0; count < 20; count++) model.step();
        require(!model.dut.irq && model.read(0x10) == 1, "masked IRQ latch lost");
        model.write(0x14, 1);
    }
    for (unsigned count = 0; count < 20; count++) {
        require(model.dut.irq, "IRQ level dropped before W1C");
        model.step();
    }
    if (acknowledge) {
        model.write(0x10, 1);
        require(!model.dut.irq && model.read(0x10) == 0, "IRQ W1C failed");
    }
}

static void reset_case(axi::SpikeModel &model, axi::Ram &ram, const Case &test, unsigned seed)
{
    if (test.name == "reset-irq") {
        job(model, ram, test, 0, false);
    } else if (test.name.find("reset-lite") == 0) {
        axi::Stall address{}, data{}, response{};
        if (test.name == "reset-lite-aw") data.blocked = true;
        if (test.name == "reset-lite-w") address.blocked = true;
        if (test.name == "reset-lite-b" || test.name == "reset-lite-r") response.blocked = true;
        model.lite = axi::LiteMaster<>(seed, 100000, address, data, response);
        model.lite.begin(test.name != "reset-lite-r", test.name == "reset-lite-r" ? 0 : 0x2c, 77, 15, model.cycles);
        for (unsigned count = 0; count < 30; count++) model.step();
        require(model.lite.busy(), "reset fixture did not hold AXI-Lite request");
        if (test.name == "reset-lite-b") require(model.dut.s_bvalid, "missing held BVALID");
        if (test.name == "reset-lite-r") require(model.dut.s_rvalid, "missing held RVALID");
    } else {
        model.configure(RamBase + test.source, RamBase + test.destination, test.beats, test.mask);
        model.memory.hold_ar = test.name == "reset-ar";
        model.memory.hold_r = test.name == "reset-r";
        model.memory.hold_aw = test.name == "reset-aw";
        model.memory.hold_w = test.name == "reset-w";
        model.memory.hold_b = test.name == "reset-b";
        model.write(8, 3);
        until(model, [&] {
            if (test.name == "reset-ar") return bool(model.dut.m_arvalid);
            if (test.name == "reset-r") return ram.reads == 1;
            if (test.name == "reset-aw") return model.memory.write_handshakes == test.beats;
            if (test.name == "reset-w") return bool(model.dut.m_wvalid);
            return ram.writes == 1;
        });
        for (unsigned count = 0; count < 20; count++) model.step();
        require(model.dut.debug_busy, "reset fixture completed before reset");
        require(ram.writes == (test.name == "reset-b" ? 1U : 0U), "unexpected pre-reset write");
    }
    auto retained = ram.bytes;
    unsigned prior_writes = ram.writes;
    model.reset();
    model.memory.hold_ar = model.memory.hold_r = model.memory.hold_aw = model.memory.hold_w = model.memory.hold_b = false;
    model.lite = axi::LiteMaster<>(seed);
    for (unsigned count = 0; count < 40; count++) model.step();
    require(!model.dut.irq && !model.dut.debug_busy && !model.memory.busy(), "reset did not clear busy/IRQ/queues");
    require(ram.bytes == retained && ram.writes == prior_writes, "reset committed stale queued data");
    require(model.read(0x2c) == 1 && model.read(0x10) == 0, "reset register defaults");
    job(model, ram, test, 0);
}

static void execute(const Case &test, unsigned index, unsigned seed, const std::filesystem::path &directory,
                    bool tracing, std::ofstream &results, std::ofstream &events)
{
    printf("RTL_CASE_BEGIN id=%u name=%s seed=%u\n", index, test.name.c_str(), seed);
    axi::Ram ram(RamBase, RamSize);
    fill(ram, seed);
    ram.fail_read = test.read_fault; ram.fail_write = test.write_fault;
    axi::SpikeModel model(ram, seed ^ index, tracing ? (directory / "wave.vcd").string() : "",
                          [&](const std::string &event) { events << index << " " << event << "\n"; });
    unsigned fill_seed = seed;
    try {
        require(model.read(0) == 0x53504b31 && model.read(4) == 1, "identity registers");
        require(model.access(false, 0x100).response == axi::SlaveError, "reserved CSR read");
        require(model.access(true, 0x100, 1).response == axi::SlaveError, "reserved CSR write");
        require(model.access(true, 0x2c, 99, 1).response == axi::SlaveError && model.read(0x2c) == 1,
                "partial CSR write changed state");
        if (test.name.find("reset-") == 0) reset_case(model, ram, test, seed);
        else if (test.name == "consecutive-100") {
            for (unsigned repeat = 0; repeat < 100; repeat++) {
                fill_seed = seed + repeat; fill(ram, fill_seed);
                auto expected = ram.bytes;
                for (unsigned lane = 0; lane < XorBytes.size(); lane++)
                    expected[test.destination + lane] = expected[test.source + lane] ^ XorBytes[lane];
                job(model, ram, test, repeat);
                require(ram.bytes == expected, "consecutive job RAM scoreboard");
            }
            require(ram.reads == 100 && ram.writes == 100, "consecutive job accounting");
        } else {
            model.memory.corrupt_read_last = test.name == "bad-rlast";
            job(model, ram, test, index);
        }
        axi::Ram expected(RamBase, RamSize);
        fill(expected, fill_seed);
        for (unsigned offset = 0; offset < test.written * 8; offset++) {
            if (test.mask & (1U << (offset % 8)))
                expected.bytes[test.destination + offset] = expected.bytes[test.source + offset] ^ XorBytes[offset % 8];
        }
        require(ram.bytes == expected.bytes, "full RAM data/source/padding scoreboard");
        if (test.name == "burst") {
            require(model.memory.read_handshakes == test.beats && model.memory.write_handshakes == test.beats, "beat accounting");
            require(ram.reads == ram.writes, "burst coalescing accounting");
        }
        if (test.error == 1) require(ram.reads == 0 && ram.writes == 0, "invalid configuration reached RAM");
        std::string filename = "case-" + std::to_string(index) + ".bin";
        std::ofstream snapshot(directory / filename, std::ios::binary);
        snapshot.write(reinterpret_cast<const char *>(ram.bytes.data()), ram.bytes.size());
        require(snapshot.good(), "RAM snapshot write failed");
        results << "{\"id\":" << index << ",\"name\":\"" << test.name << "\",\"seed\":" << seed
                << ",\"fill_seed\":" << fill_seed << ",\"source\":" << test.source << ",\"destination\":" << test.destination
                << ",\"beats\":" << test.beats << ",\"written\":" << test.written << ",\"mask\":" << unsigned(test.mask)
                << ",\"error\":" << test.error << ",\"cycles\":" << model.cycles
                << ",\"reads\":" << ram.reads << ",\"writes\":" << ram.writes
                << ",\"read_beats\":" << model.memory.read_handshakes << ",\"write_beats\":" << model.memory.write_handshakes
                << ",\"max_read\":" << model.memory.longest_read << ",\"max_write\":" << model.memory.longest_write
                << ",\"snapshot\":\"" << filename << "\",\"status\":\"PASS\"}\n";
        results.flush();
    } catch (...) {
        fprintf(stderr, "RTL_CASE_FAIL id=%u seed=%u source=%u destination=%u beats=%u\nRECENT_AXI\n%s",
                index, seed, test.source, test.destination, test.beats, model.history.tail().c_str());
        throw;
    }
}

int main(int argc, char **argv)
{
    setvbuf(stdout, nullptr, _IOLBF, 0);
    try {
        require(argc == 3 || argc == 4, "usage: rtl-test SEED OUTPUT_DIRECTORY [TRACE_ONLY_CASE]");
        unsigned seed = std::stoul(argv[1]);
        std::filesystem::path directory = argv[2];
        std::filesystem::create_directories(directory);
        Verilated::traceEverOn(argc == 4);
        auto tests = cases();
        unsigned selected = argc == 4 ? std::stoul(argv[3]) : tests.size();
        require(argc != 4 || selected < tests.size(), "invalid trace case");
        std::ofstream results(directory / "cases.jsonl"), events(directory / "events.log");
        require(results.good() && events.good(), "cannot open evidence outputs");
        for (unsigned index = 0; index < tests.size(); index++) {
            if (argc == 4 && index != selected) continue;
            execute(tests[index], index, seed, directory, argc == 4, results, events);
        }
        require(results.good() && events.good(), "evidence write failed");
        printf("RTL_SUITE_PASS seed=%u cases=%zu\n", seed, argc == 4 ? size_t(1) : tests.size());
        return 0;
    } catch (const std::exception &error) {
        fprintf(stderr, "RTL_SUITE_FAIL %s\n", error.what());
        return 1;
    }
}

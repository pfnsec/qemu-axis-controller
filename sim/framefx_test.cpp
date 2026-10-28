#include "Vframefx_core.h"
#include "axi/model.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>

using axi::require;
using Model = axi::ClockedModel<Vframefx_core>;
static constexpr uint64_t RamBase = UINT64_C(0x100000000);

static std::vector<uint8_t> bytes(const std::filesystem::path &filename)
{
    std::ifstream input(filename, std::ios::binary);
    require(input.good(), "Cannot open " + filename.string());
    return std::vector<uint8_t>(std::istreambuf_iterator<char>(input), {});
}

static uint64_t counter(Model &model)
{
    uint64_t low = model.read(0x48);
    return low | (uint64_t(model.read(0x4c)) << 32);
}

static void compare_ram(const std::vector<uint8_t> &actual, const std::vector<uint8_t> &expected,
                        uint64_t destination, unsigned stride)
{
    if (actual == expected) return;
    require(actual.size() == expected.size(), "RAM snapshot size mismatch");
    for (unsigned offset = 0; offset < actual.size(); offset++) {
        if (actual[offset] == expected[offset]) continue;
        uint64_t address = RamBase + offset;
        std::string message = "RAM mismatch byte=" + std::to_string(offset) + " expected=" + std::to_string(expected[offset]) +
                              " actual=" + std::to_string(actual[offset]);
        if (stride && address >= destination)
            message += " destination_row=" + std::to_string((address - destination) / stride) +
                       " column_or_padding=" + std::to_string((address - destination) % stride);
        throw std::runtime_error(message);
    }
    throw std::runtime_error("RAM snapshot size mismatch");
}

int main(int argc, char **argv)
{
    setvbuf(stdout, nullptr, _IOLBF, 0);
    try {
        require(argc == 3 || argc == 4, "usage: framefx-test CASE_DIRECTORY SEED [trace]");
        std::filesystem::path directory = argv[1];
        unsigned seed = std::stoul(argv[2]);
        Verilated::traceEverOn(argc == 4);
        auto initial = bytes(directory / "input.bin");
        auto expected = bytes(directory / "expected.bin");
        require(initial.size() == expected.size(), "RAM evidence sizes differ");
        axi::Ram ram(RamBase, initial.size());
        ram.bytes = initial;
        std::ifstream configuration(directory / "config.txt");
        uint32_t width, height, source_stride, destination_stride, mode, threshold, scenario, expected_error;
        uint64_t source, destination;
        configuration >> width >> height >> source_stride >> destination_stride >> mode >> threshold
                      >> source >> destination >> scenario >> expected_error >> ram.fail_read >> ram.fail_write;
        require(configuration.good(), "Invalid case configuration");
        std::ofstream events(directory / "events.log");
        bool record_events = true;
        Model model(ram, seed, argc == 4 ? (directory / "wave.vcd").string() : "",
                    [&](const std::string &event) {
                        if (record_events && (event.find(" AR ") != std::string::npos || event.find(" AW ") != std::string::npos ||
                            event.find("WRITE_COMMIT") != std::string::npos)) events << event << "\n";
                    });
        try {
            require(model.read(0) == 0x46524658 && model.read(4) == 0x10000 && model.read(8) == 0x10f, "Identity/capabilities");
            require(model.read(0x10) == 0 && model.read(0x3c) == 0 && !model.dut.irq, "Reset status");
            model.write(0xc, 1);
            require(model.read(0x10) == 6 && model.read(0x44) == 1 && model.read(0x3c) == 2 && !model.dut.irq,
                    "Invalid initial START/error latch");
            for (unsigned address : {0xcU, 0x58U, 0xffcU})
                require(model.access(false, address).response == axi::SlaveError, "Illegal CSR read accepted");
            for (unsigned address : {0U, 4U, 8U, 0x10U, 0x44U, 0x48U, 0x4cU, 0x50U, 0x54U, 0x58U, 0xffcU})
                require(model.access(true, address, 1).response == axi::SlaveError, "Illegal CSR write accepted");
            require(model.access(true, 0x24, 7, 1).response == axi::SlaveError && model.read(0x24) == 0, "Partial CSR write mutated state");
            require(model.access(true, 0xc, 3).response == axi::SlaveError, "START plus RESET accepted");
            std::array<uint32_t, 10> values = {uint32_t(source), uint32_t(source >> 32), uint32_t(destination),
                uint32_t(destination >> 32), width, height, source_stride, destination_stride, mode, threshold};
            for (unsigned index = 0; index < values.size(); index++) {
                model.write(0x14 + index * 4, values[index]);
                require(model.read(0x14 + index * 4) == values[index], "Configuration readback");
            }
            model.memory.corrupt_read_last = scenario == 4;
            unsigned repetitions = scenario == 2 ? 100 : 1;
            uint64_t measured = 0;
            for (unsigned repeat = 0; repeat < repetitions; repeat++) {
                if (scenario == 2) model.write(0x34, repeat % 4);
                bool masked = (seed + repeat) % 2;
                model.write(0x40, masked ? 0 : 3);
                model.memory.hold_ar = scenario == 1 || scenario == 3;
                model.write(0xc, 1);
                if (scenario == 1 || scenario == 3) {
                    require(model.read(0x10) == 1, "START did not set BUSY/clear sticky flags");
                    for (unsigned index = 0; index < values.size(); index++) {
                        require(model.access(true, 0x14 + index * 4, 99).response == axi::SlaveError, "Busy configuration accepted");
                        require(model.read(0x14 + index * 4) == values[index], "Busy configuration changed state");
                    }
                    require(model.access(true, 0xc, 2).response == axi::SlaveError, "Busy soft reset accepted");
                    uint64_t before = counter(model);
                    for (unsigned count = 0; count < 30; count++) model.step();
                    require(counter(model) > before, "Busy counter not live");
                    if (scenario == 1) {
                        require(model.access(true, 0xc, 1).response == axi::SlaveError, "Busy START accepted");
                        require(model.read(0x44) == 4 && model.read(0x10) == 5, "Busy START did not latch error without abort");
                    } else {
                        model.reset();
                        require(!model.dut.irq && !model.memory.busy() && ram.bytes == initial, "Active reset leaked DMA");
                        for (unsigned index = 0; index < values.size(); index++) model.write(0x14 + index * 4, values[index]);
                        model.write(0x40, masked ? 0 : 3);
                        model.write(0xc, 1);
                    }
                    model.memory.hold_ar = false;
                }
                uint64_t deadline = model.cycles + 20000000;
                while (model.read(0x10) & 1) {
                    require(model.cycles < deadline, "Frame cycle timeout");
                    for (unsigned count = 0; count < 32; count++) {
                        model.step();
                        if (scenario != 1 && model.dut.irq) require(!model.memory.busy(), "IRQ before final DMA response");
                    }
                }
                require(!model.memory.busy(), "Completion left outstanding DMA");
                require(model.read(0x10) == (expected_error ? 6U : 2U), "Final status");
                require(model.read(0x44) == expected_error, "Error code");
                unsigned pending = scenario == 1 ? 3 : expected_error ? 2 : 1;
                require(model.read(0x3c) == pending && bool(model.dut.irq) == !masked, "IRQ pending/enable");
                auto wanted = scenario == 2 ? bytes(directory / ("expected-mode-" + std::to_string(repeat % 4) + ".bin")) : expected;
                compare_ram(ram.bytes, wanted, destination, destination_stride);
                if (expected_error == 0 || expected_error == 4)
                    require(model.read(0x50) == ((width + 7) & ~7U) * height && model.read(0x54) == width * height,
                            "Operation byte counters were not reset or are inaccurate");
                measured = counter(model);
                if (expected_error != 1) require(measured > 0, "Missing cycle count");
                for (unsigned count = 0; count < 20; count++) model.step();
                require(counter(model) == measured, "Completed cycle counter changed");
                model.write(0x40, 0);
                require(!model.dut.irq && model.read(0x3c) == pending, "IRQ masking lost pending state");
                model.write(0x40, 3);
                for (unsigned count = 0; count < 20; count++) { require(model.dut.irq, "IRQ not held"); model.step(); }
                model.write(0x3c, 1);
                require(model.read(0x3c) == (pending & 2) && bool(model.dut.irq) == bool(pending & 2), "Completion W1C damaged error pending");
                model.write(0x3c, 2);
                require(!model.dut.irq && model.read(0x3c) == 0, "IRQ W1C failed");
            }
            std::ofstream snapshot(directory / "actual.bin", std::ios::binary);
            snapshot.write(reinterpret_cast<const char *>(ram.bytes.data()), ram.bytes.size());
            std::ofstream result(directory / "rtl.json");
            result << "{\"status\":\"PASS\",\"cycles\":" << measured << ",\"read_bytes\":" << model.read(0x50)
                   << ",\"written_bytes\":" << model.read(0x54) << ",\"reads\":" << ram.reads << ",\"writes\":" << ram.writes
                   << ",\"read_beats\":" << model.memory.read_handshakes << ",\"write_beats\":" << model.memory.write_handshakes
                   << ",\"max_read\":" << model.memory.longest_read << ",\"max_write\":" << model.memory.longest_write
                   << ",\"error\":" << model.read(0x44) << ",\"repetitions\":" << repetitions << "}\n";
            if (std::filesystem::exists(directory / "recovery.bin")) {
                record_events = false;
                ram.bytes = initial;
                ram.fail_read = ram.fail_write = 0;
                model.write(0xc, 1);
                uint64_t deadline = model.cycles + 20000000;
                while (model.read(0x10) & 1) {
                    require(model.cycles < deadline, "Recovery cycle timeout");
                    for (unsigned count = 0; count < 32; count++) model.step();
                }
                require(model.read(0x10) == 2 && model.read(0x44) == 0 && model.read(0x3c) == 1 && model.dut.irq,
                        "Valid START after DMA failure did not clear old error");
                compare_ram(ram.bytes, bytes(directory / "recovery.bin"), destination, destination_stride);
                require(model.read(0x50) == ((width + 7) & ~7U) * height && model.read(0x54) == width * height,
                        "Recovery did not reset operation counters");
                std::cout << "FRAMEFX_RECOVERY_PASS\n";
            }
            model.write(0xc, 2);
            require(model.read(0x10) == 0 && model.read(0x44) == 0 && model.read(0x3c) == 0 && counter(model) == 0 &&
                    model.read(0x50) == 0 && model.read(0x54) == 0, "Idle soft reset");
            require(model.read(0x24) == width && model.read(0x40) == 3, "Soft reset changed retained configuration");
            require(snapshot.good() && result.good() && events.good(), "Evidence write failed");
            std::cout << "FRAMEFX_PASS cycles=" << measured << " repetitions=" << repetitions << "\n";
        } catch (...) {
            std::cerr << "RECENT_AXI\n" << model.history.tail();
            throw;
        }
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "FRAMEFX_FAIL " << error.what() << "\n";
        return 1;
    }
}

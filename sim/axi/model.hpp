#ifndef AXI_LAB_MODEL_HPP
#define AXI_LAB_MODEL_HPP

#include "verilated_vcd_c.h"
#include "fabric.hpp"
#include <memory>

namespace axi {

template<class Dut> class ClockedModel {
    std::unique_ptr<VerilatedVcdC> trace;

    void edge(bool rising)
    {
        dut.clk = rising;
        dut.eval();
        if (trace) trace->dump(cycles * 10000 + (rising ? 5000 : 0));
    }

public:
    Dut dut;
    History history;
    LiteMaster<> lite;
    MemorySlave<> memory;
    uint64_t cycles = 0;

    ClockedModel(Memory &backend, unsigned seed, const std::string &waveform = "",
               std::function<void(const std::string &)> events = {})
        : history(events), lite(seed), memory(backend, history, seed ^ 0xfedcba98)
    {
        if (!waveform.empty()) {
            trace = std::make_unique<VerilatedVcdC>();
            dut.trace(trace.get(), 99);
            trace->open(waveform.c_str());
            require(trace->isOpen(), "Cannot open VCD trace");
        }
        reset();
    }

    ~ClockedModel()
    {
        dut.final();
        if (trace) trace->close();
    }

    void reset()
    {
        dut.reset_n = 0;
        lite.reset(dut); memory.reset(dut);
        for (unsigned count = 0; count < 4; count++) { edge(false); edge(true); cycles++; }
        dut.reset_n = 1;
        history.add(cycles, "RESET");
    }

    bool step()
    {
        lite.drive(dut, cycles); memory.drive(dut, cycles);
        edge(false);
        lite.sample(dut); memory.sample(dut);
        edge(true);
        cycles++;
        memory.commit(cycles);
        return lite.commit(dut);
    }

    LiteResult access(bool write, uint32_t address, uint32_t data = 0, uint8_t strobes = 15)
    {
        lite.begin(write, address, data, strobes, cycles);
        while (lite.busy()) step();
        return lite.value();
    }

    uint32_t read(uint32_t address)
    {
        LiteResult result = access(false, address);
        require(result.response == Okay, "AXI-Lite read failed at " + std::to_string(address));
        return result.data;
    }

    void write(uint32_t address, uint32_t data)
    {
        require(access(true, address, data).response == Okay, "AXI-Lite write failed at " + std::to_string(address));
    }

};

}
#endif

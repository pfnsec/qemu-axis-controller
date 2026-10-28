#ifndef AXI_LAB_FABRIC_HPP
#define AXI_LAB_FABRIC_HPP

#include <algorithm>
#include <array>
#include <cstdint>
#include <deque>
#include <functional>
#include <limits>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace axi {

inline void require(bool condition, const std::string &message)
{
    if (!condition) throw std::runtime_error(message);
}

enum Response : unsigned { Okay = 0, SlaveError = 2, DecodeError = 3 };

struct Transfer {
    unsigned response = Okay;
    std::vector<uint8_t> bytes;
};

class Memory {
public:
    virtual ~Memory() = default;
    virtual Transfer read(uint64_t address, unsigned count) = 0;
    virtual unsigned write(uint64_t address, const std::vector<uint8_t> &bytes,
                           const std::vector<uint8_t> &enables) = 0;
};

class Ram : public Memory {
    uint64_t base;
    bool contains(uint64_t address, unsigned count) const
    {
        return count && address >= base && address - base < bytes.size() &&
               count <= bytes.size() - (address - base);
    }

public:
    std::vector<uint8_t> bytes;
    unsigned reads = 0, writes = 0;
    unsigned fail_read = 0, fail_write = 0;
    explicit Ram(uint64_t address, unsigned size) : base(address), bytes(size)
    {
        require(size && address <= UINT64_MAX - size, "RAM empty/overflowing range");
    }

    Transfer read(uint64_t address, unsigned count) override
    {
        reads++;
        if (fail_read && reads == fail_read) return {SlaveError, {}};
        if (!contains(address, count)) return {DecodeError, {}};
        auto first = bytes.begin() + (address - base);
        return {Okay, std::vector<uint8_t>(first, first + count)};
    }

    unsigned write(uint64_t address, const std::vector<uint8_t> &data,
                   const std::vector<uint8_t> &enables) override
    {
        writes++;
        if (fail_write && writes == fail_write) return SlaveError;
        if (data.size() != enables.size() || data.size() > std::numeric_limits<unsigned>::max() ||
            !contains(address, data.size())) return DecodeError;
        for (uint8_t enabled : enables) if (enabled > 1) return DecodeError;
        for (unsigned index = 0; index < data.size(); index++) {
            if (enables[index]) bytes[address - base + index] = data[index];
        }
        return Okay;
    }
};

struct Stall {
    unsigned maximum = 7;
    unsigned initial = 0;
    bool blocked = false;
};

class Gate {
    std::mt19937 generator;
    Stall settings;
    unsigned remaining;

public:
    Gate(unsigned seed, Stall configuration = {})
        : generator(seed), settings(configuration), remaining(configuration.initial) {}
    bool open()
    {
        if (settings.blocked) return false;
        if (remaining) { remaining--; return false; }
        return true;
    }
    void used() { remaining = settings.maximum ? generator() % (settings.maximum + 1) : 0; }
    void reset() { remaining = settings.initial; }
};

class History {
    std::deque<std::string> recent;
    std::function<void(const std::string &)> sink;

public:
    explicit History(std::function<void(const std::string &)> output = {}) : sink(output) {}
    void add(uint64_t cycle, const std::string &event)
    {
        std::string line = std::to_string(cycle) + " " + event;
        recent.push_back(line);
        if (recent.size() > 32) recent.pop_front();
        if (sink) sink(line);
    }
    std::string tail() const
    {
        std::string output;
        for (const auto &line : recent) output += line + "\n";
        return output;
    }
};

struct Stable {
    bool held = false;
    std::vector<uint64_t> payload;
    void check(bool valid, bool ready, std::initializer_list<uint64_t> values, const char *channel)
    {
        std::vector<uint64_t> current(values);
        require(!held || (valid && payload == current), std::string(channel) + ": VALID/payload changed under stall");
        held = valid && !ready;
        payload = std::move(current);
    }
    void reset() { held = false; payload.clear(); }
};

struct LiteResult { uint32_t data; unsigned response; };

template<unsigned AddressBits = 12>
class LiteMaster {
    static_assert(AddressBits >= 2 && AddressBits <= 32);
    Gate address_gate, data_gate, response_gate;
    bool active = false, writing = false, address_sent = false, data_sent = false;
    uint32_t address = 0, data = 0;
    uint8_t strobes = 0;
    uint64_t started = 0, limit;
    bool accept_address = false, accept_data = false, accept_response = false;
    LiteResult result{};
    Stable response_stability;

public:
    explicit LiteMaster(unsigned seed, uint64_t cycle_limit = 100000,
                        Stall address_stalls = {}, Stall data_stalls = {}, Stall response_stalls = {})
        : address_gate(seed, address_stalls), data_gate(seed ^ 0x12345678, data_stalls),
          response_gate(seed ^ 0x87654321, response_stalls), limit(cycle_limit) {}
    bool busy() const { return active; }
    LiteResult value() const { require(!active, "AXI-Lite result requested before response"); return result; }

    void begin(bool write, uint32_t offset, uint32_t value, uint8_t mask, uint64_t cycle)
    {
        require(!active, "AXI-Lite already active");
        require(offset % 4 == 0 && (AddressBits == 32 || uint64_t(offset) < (UINT64_C(1) << AddressBits)),
                "AXI-Lite address outside configured aperture/alignment");
        require(mask <= 15, "AXI-Lite invalid strobes");
        active = true; writing = write; address = offset; data = value; strobes = mask;
        address_sent = data_sent = false;
        started = cycle;
    }

    template<class Pins> void reset(Pins &pins)
    {
        active = false;
        pins.s_awvalid = pins.s_wvalid = pins.s_arvalid = pins.s_bready = pins.s_rready = 0;
        address_gate.reset(); data_gate.reset(); response_gate.reset(); response_stability.reset();
    }

    template<class Pins> void drive(Pins &pins, uint64_t cycle)
    {
        require(!active || cycle - started < limit, "AXI-Lite cycle timeout");
        pins.s_bready = pins.s_rready = 0;
        if (!active) return;
        if (writing) {
            pins.s_awaddr = address; pins.s_wdata = data; pins.s_wstrb = strobes;
            if (!address_sent && address_gate.open()) pins.s_awvalid = 1;
            if (!data_sent && data_gate.open()) pins.s_wvalid = 1;
            pins.s_bready = response_gate.open();
        } else {
            pins.s_araddr = address;
            if (!address_sent && address_gate.open()) pins.s_arvalid = 1;
            pins.s_rready = response_gate.open();
        }
    }

    template<class Pins> void sample(const Pins &pins)
    {
        require(!pins.s_bvalid || (active && writing && address_sent && data_sent),
                "AXI-Lite BVALID before write acceptance or unsolicited response");
        require(!pins.s_rvalid || (active && !writing && address_sent),
                "AXI-Lite RVALID before read acceptance or unsolicited response");
        bool valid = writing ? pins.s_bvalid : pins.s_rvalid;
        bool ready = writing ? pins.s_bready : pins.s_rready;
        unsigned response = writing ? pins.s_bresp : pins.s_rresp;
        uint32_t returned = writing ? 0 : pins.s_rdata;
        response_stability.check(valid, ready, {response, returned}, "AXI-Lite response");
        if (valid)
            require(response == Okay || response == SlaveError || response == DecodeError, "AXI-Lite reserved response");
        accept_address = active && (writing ? pins.s_awvalid && pins.s_awready : pins.s_arvalid && pins.s_arready);
        accept_data = active && writing && pins.s_wvalid && pins.s_wready;
        accept_response = active && valid && ready;
        if (accept_response) {
            require(address_sent && (!writing || data_sent), "AXI-Lite response before request acceptance");
            require(response == Okay || response == SlaveError || response == DecodeError, "AXI-Lite reserved response");
            result = {returned, response};
        }
    }

    template<class Pins> bool commit(Pins &pins)
    {
        if (accept_address) {
            address_sent = true; address_gate.used(); pins.s_awvalid = pins.s_arvalid = 0;
        }
        if (accept_data) { data_sent = true; data_gate.used(); pins.s_wvalid = 0; }
        if (accept_response) { active = false; response_gate.used(); pins.s_bready = pins.s_rready = 0; }
        return accept_response;
    }
};

template<unsigned DataBytes = 8, unsigned MaximumBeats = 16>
class MemorySlave {
    static_assert(DataBytes == 4 || DataBytes == 8);
    static_assert(MaximumBeats >= 1 && MaximumBeats <= 16);
    static constexpr unsigned Size = DataBytes == 8 ? 3 : 2;
    Memory &backend;
    History &history;
    Gate read_address_gate, write_address_gate, write_data_gate, read_gate, response_gate;
    Stable read_address_stable, write_address_stable, write_data_stable, read_stable, response_stable;
    bool reading = false, read_valid = false, have_address = false, responding = false, response_valid = false;
    bool write_last = false;
    uint64_t read_address = 0, write_address = 0;
    unsigned read_beats = 0, read_index = 0, read_response = Okay, write_beats = 0, write_response = Okay;
    std::vector<uint8_t> read_bytes, write_bytes, write_enables;
    uint64_t read_start = 0, write_start = 0, limit;
    bool take_ar = false, take_aw = false, take_w = false, take_r = false, take_b = false;
    uint64_t sampled_ar = 0, sampled_aw = 0, sampled_data = 0, sampled_strobes = 0;
    unsigned sampled_read_beats = 0, sampled_write_beats = 0;
    bool sampled_last = false, sampled_read_last = false;

    void validate(uint64_t address, unsigned beats, unsigned size, unsigned burst, const char *channel)
    {
        std::string prefix = std::string(channel) + " address=" + std::to_string(address) + " beats=" + std::to_string(beats) + ": ";
        require(beats >= 1 && beats <= MaximumBeats, prefix + "unsupported burst length");
        require(size == Size && burst == 1 && address % DataBytes == 0, prefix + "requires aligned full-width INCR");
        unsigned count = beats * DataBytes;
        require(address <= UINT64_MAX - count, prefix + "address overflow");
        require((address & 4095) + count <= 4096, prefix + "burst crosses 4-KiB boundary");
    }

public:
    unsigned read_calls = 0, write_calls = 0, read_handshakes = 0, write_handshakes = 0;
    unsigned longest_read = 0, longest_write = 0;
    bool corrupt_read_last = false;
    bool hold_ar = false, hold_aw = false, hold_w = false, hold_r = false, hold_b = false;

    MemorySlave(Memory &memory, History &events, unsigned seed, uint64_t cycle_limit = 100000,
                Stall ar = {}, Stall aw = {}, Stall data = {}, Stall read = {}, Stall response = {})
        : backend(memory), history(events), read_address_gate(seed, ar), write_address_gate(seed ^ 0x1111, aw),
          write_data_gate(seed ^ 0x2222, data), read_gate(seed ^ 0x3333, read),
          response_gate(seed ^ 0x4444, response), limit(cycle_limit) {}

    bool busy() const { return reading || have_address || !write_bytes.empty() || responding; }

    template<class Pins> void reset(Pins &pins)
    {
        reading = read_valid = have_address = responding = response_valid = write_last = false;
        read_bytes.clear(); write_bytes.clear(); write_enables.clear();
        read_address_stable.reset(); write_address_stable.reset(); write_data_stable.reset();
        read_stable.reset(); response_stable.reset();
        read_address_gate.reset(); write_address_gate.reset(); write_data_gate.reset(); read_gate.reset(); response_gate.reset();
        pins.m_arready = pins.m_awready = pins.m_wready = pins.m_rvalid = pins.m_bvalid = 0;
    }

    template<class Pins> void drive(Pins &pins, uint64_t cycle)
    {
        require(!reading || cycle - read_start < limit, "AXI read cycle timeout");
        require(!(have_address || !write_bytes.empty() || responding) || cycle - write_start < limit, "AXI write cycle timeout");
        pins.m_arready = !hold_ar && !reading && read_address_gate.open();
        pins.m_awready = !hold_aw && !have_address && !responding && write_address_gate.open();
        pins.m_wready = !hold_w && !responding && !write_last && write_bytes.size() < MaximumBeats * DataBytes && write_data_gate.open();
        if (reading && !read_valid && !hold_r && read_gate.open()) read_valid = true;
        pins.m_rvalid = read_valid;
        pins.m_rdata = 0;
        if (read_valid && read_response == Okay) {
            for (unsigned lane = 0; lane < DataBytes; lane++)
                pins.m_rdata |= uint64_t(read_bytes[read_index * DataBytes + lane]) << (lane * 8);
        }
        pins.m_rresp = read_response;
        pins.m_rlast = reading && ((read_index + 1 == read_beats) != corrupt_read_last);
        if (responding && !response_valid && !hold_b && response_gate.open()) response_valid = true;
        pins.m_bvalid = response_valid;
        pins.m_bresp = write_response;
    }

    template<class Pins> void sample(const Pins &pins)
    {
        read_address_stable.check(pins.m_arvalid, pins.m_arready,
                                 {pins.m_araddr, pins.m_arlen, pins.m_arsize, pins.m_arburst}, "AR");
        write_address_stable.check(pins.m_awvalid, pins.m_awready,
                                  {pins.m_awaddr, pins.m_awlen, pins.m_awsize, pins.m_awburst}, "AW");
        write_data_stable.check(pins.m_wvalid, pins.m_wready, {pins.m_wdata, pins.m_wstrb, pins.m_wlast}, "W");
        read_stable.check(pins.m_rvalid, pins.m_rready, {pins.m_rdata, pins.m_rresp, pins.m_rlast}, "R");
        response_stable.check(pins.m_bvalid, pins.m_bready, {pins.m_bresp}, "B");
        take_ar = pins.m_arvalid && pins.m_arready;
        take_aw = pins.m_awvalid && pins.m_awready;
        take_w = pins.m_wvalid && pins.m_wready;
        take_r = pins.m_rvalid && pins.m_rready;
        take_b = pins.m_bvalid && pins.m_bready;
        sampled_read_last = pins.m_rlast;
        sampled_ar = pins.m_araddr; sampled_aw = pins.m_awaddr;
        sampled_read_beats = unsigned(pins.m_arlen) + 1;
        sampled_write_beats = unsigned(pins.m_awlen) + 1;
        sampled_data = pins.m_wdata; sampled_strobes = pins.m_wstrb; sampled_last = pins.m_wlast;
        if (pins.m_arvalid) validate(sampled_ar, sampled_read_beats, pins.m_arsize, pins.m_arburst, "AR");
        if (pins.m_awvalid) validate(sampled_aw, sampled_write_beats, pins.m_awsize, pins.m_awburst, "AW");
        if (pins.m_wvalid) require(sampled_strobes < (UINT64_C(1) << DataBytes), "W: strobes exceed data width");
    }

    void commit(uint64_t cycle)
    {
        if (take_r) {
            history.add(cycle, "R beat=" + std::to_string(read_index + 1) + " last=" + std::to_string(sampled_read_last) +
                        " response=" + std::to_string(read_response));
            read_handshakes++; read_index++; read_valid = false; read_gate.used();
            if (read_index == read_beats) reading = false;
        }
        if (take_b) {
            history.add(cycle, "B response=" + std::to_string(write_response));
            responding = response_valid = false; response_gate.used();
        }
        if (take_ar) {
            reading = true; read_index = 0; read_start = cycle;
            read_address = sampled_ar; read_beats = sampled_read_beats;
            Transfer transfer = backend.read(read_address, read_beats * DataBytes);
            read_calls++; longest_read = std::max(longest_read, read_beats);
            require(transfer.response == Okay || transfer.response == SlaveError || transfer.response == DecodeError, "backend read response");
            require(transfer.response != Okay || transfer.bytes.size() == read_beats * DataBytes, "backend read length");
            read_response = transfer.response; read_bytes = std::move(transfer.bytes); read_address_gate.used();
            history.add(cycle, "AR address=" + std::to_string(read_address) + " beats=" + std::to_string(read_beats) +
                        " response=" + std::to_string(read_response));
        }
        if (take_aw) {
            if (write_bytes.empty()) write_start = cycle;
            have_address = true; write_address = sampled_aw; write_beats = sampled_write_beats; write_address_gate.used();
            history.add(cycle, "AW address=" + std::to_string(write_address) + " beats=" + std::to_string(write_beats));
        }
        if (take_w) {
            if (!have_address && write_bytes.empty()) write_start = cycle;
            for (unsigned lane = 0; lane < DataBytes; lane++) {
                write_bytes.push_back(sampled_data >> (lane * 8));
                write_enables.push_back((sampled_strobes >> lane) & 1);
            }
            write_last = sampled_last; write_handshakes++; write_data_gate.used();
            history.add(cycle, "W beat=" + std::to_string(write_bytes.size() / DataBytes) + " last=" + std::to_string(write_last));
        }
        if (have_address && !write_bytes.empty()) {
            unsigned accepted = write_bytes.size() / DataBytes;
            require(accepted <= write_beats, "W: too many beats");
            require(!write_last || accepted == write_beats, "W: early WLAST");
            require(accepted != write_beats || write_last, "W: missing WLAST");
            if (write_last) {
                write_response = backend.write(write_address, write_bytes, write_enables);
                require(write_response == Okay || write_response == SlaveError || write_response == DecodeError, "backend write response");
                write_calls++; longest_write = std::max(longest_write, write_beats);
                have_address = write_last = false; write_bytes.clear(); write_enables.clear(); responding = true;
                history.add(cycle, "WRITE_COMMIT response=" + std::to_string(write_response));
            }
        }
    }
};

}
#endif

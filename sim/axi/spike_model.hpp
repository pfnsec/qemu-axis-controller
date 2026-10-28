#ifndef AXI_LAB_SPIKE_MODEL_HPP
#define AXI_LAB_SPIKE_MODEL_HPP

#include "Vaxi_spike.h"
#include "model.hpp"

namespace axi {

class SpikeModel : public ClockedModel<Vaxi_spike> {
public:
    using ClockedModel::ClockedModel;

    void configure(uint64_t source, uint64_t destination, unsigned beats, uint8_t mask)
    {
        write(0x18, uint32_t(source)); write(0x1c, uint32_t(source >> 32));
        write(0x20, uint32_t(destination)); write(0x24, uint32_t(destination >> 32));
        write(0x28, mask); write(0x2c, beats);
    }
};

}
#endif

#include "Vtoolcheck.h"

#include <cstdlib>
#include <iostream>

static void tick(Vtoolcheck &dut)
{
    dut.clk = 0;
    dut.eval();
    dut.clk = 1;
    dut.eval();
}

int main()
{
    Vtoolcheck dut;
    dut.reset_n = 0;
    dut.enable = 0;
    tick(dut);
    if (dut.count != 0)
        return EXIT_FAILURE;
    dut.reset_n = 1;
    dut.enable = 1;
    for (unsigned cycle = 1; cycle <= 300; ++cycle) {
        tick(dut);
        if (dut.count != (cycle & 255))
            return EXIT_FAILURE;
    }
    dut.enable = 0;
    tick(dut);
    if (dut.count != 44)
        return EXIT_FAILURE;
    dut.reset_n = 0;
    tick(dut);
    if (dut.count != 0)
        return EXIT_FAILURE;
    dut.final();
    std::cout << "PASS: clock, synchronous reset, enable, wraparound\n";
    return EXIT_SUCCESS;
}

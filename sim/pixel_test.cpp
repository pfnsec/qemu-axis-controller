#include "Vframefx_pixel.h"
#include <array>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <random>
#include <stdexcept>

int main()
{
    Vframefx_pixel dut;
    unsigned cases = 0;
    auto check = [&](unsigned expected) {
        dut.eval();
        if (dut.pixel != expected) throw std::runtime_error("Pixel arithmetic mismatch at case " + std::to_string(cases));
        cases++;
    };
    try {
        for (unsigned value = 0; value < 256; value++) {
            dut.center = value;
            dut.mode = 0; check(value);
            dut.mode = 1; check(255 - value);
            dut.mode = 2;
            for (unsigned threshold = 0; threshold < 256; threshold++) {
                dut.threshold = threshold;
                check(value >= threshold ? 255 : 0);
            }
        }
        std::mt19937 generator(2026);
        for (unsigned index = 0; index < 10256; index++) {
            std::array<int, 9> pixels{};
            for (unsigned position = 0; position < pixels.size(); position++)
                pixels[position] = index < 256 ? ((index >> (position < 4 ? position : position - 1)) & 1) * 255 : generator() & 255;
            dut.mode = 3; dut.center = pixels[4];
            dut.north_left = pixels[0]; dut.north_middle = pixels[1]; dut.north_right = pixels[2];
            dut.middle_left = pixels[3]; dut.middle_right = pixels[5];
            dut.south_left = pixels[6]; dut.south_middle = pixels[7]; dut.south_right = pixels[8];
            std::array<int, 9> horizontal = {-1, 0, 1, -2, 0, 2, -1, 0, 1};
            std::array<int, 9> vertical = {-1, -2, -1, 0, 0, 0, 1, 2, 1};
            int gradient_x = 0, gradient_y = 0;
            for (unsigned position = 0; position < pixels.size(); position++) {
                gradient_x += pixels[position] * horizontal[position];
                gradient_y += pixels[position] * vertical[position];
            }
            check(std::min(255, (std::abs(gradient_x) + std::abs(gradient_y)) / 4));
        }
        printf("PIXEL_PASS cases=%u\n", cases);
        return 0;
    } catch (const std::exception &error) {
        fprintf(stderr, "PIXEL_FAIL %s\n", error.what());
        return 1;
    }
}

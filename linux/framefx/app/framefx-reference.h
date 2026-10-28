#ifndef FRAMEFX_REFERENCE_H
#define FRAMEFX_REFERENCE_H

#include <stdint.h>
#include <stdlib.h>

static inline uint8_t input_pixel(unsigned row, unsigned column, unsigned seed)
{
    return ((row * UINT32_C(0x45d9f3b) + column * UINT32_C(0x119de1f3) + seed * 17U) ^
            (column >> 3) ^ (row << 5)) & 255;
}

static inline uint8_t golden(const uint8_t *source, unsigned width, unsigned height, unsigned stride,
                             unsigned row, unsigned column, unsigned mode, unsigned threshold)
{
    unsigned value = source[row * stride + column];
    if (mode == 0) return value;
    if (mode == 1) return value ^ 255;
    if (mode == 2) return value >= threshold ? 255 : 0;
    if (!row || !column || row == height - 1 || column == width - 1) return 0;
    const int horizontal[3][3] = {{-1, 0, 1}, {-2, 0, 2}, {-1, 0, 1}};
    const int vertical[3][3] = {{-1, -2, -1}, {0, 0, 0}, {1, 2, 1}};
    int gradient_x = 0, gradient_y = 0;
    for (unsigned line = 0; line < 3; line++) {
        for (unsigned pixel = 0; pixel < 3; pixel++) {
            int sample = source[(row + line - 1) * stride + column + pixel - 1];
            gradient_x += horizontal[line][pixel] * sample;
            gradient_y += vertical[line][pixel] * sample;
        }
    }
    unsigned magnitude = (abs(gradient_x) + abs(gradient_y)) / 4;
    return magnitude > 255 ? 255 : magnitude;
}

#endif

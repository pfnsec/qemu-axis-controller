#define main mikro_display_application_main
#include "../linux/hardware/ultra96-mikro/display.c"
#undef main
#include <assert.h>

int main(void)
{
    struct fb_fix_screeninfo fixed = {.type = FB_TYPE_PACKED_PIXELS, .visual = FB_VISUAL_TRUECOLOR,
                                     .line_length = 672 * 4, .smem_len = 672 * 500 * 4};
    struct fb_var_screeninfo variable = {.xres = 640, .yres = 480, .xres_virtual = 672, .yres_virtual = 500,
        .xoffset = 3, .yoffset = 5, .bits_per_pixel = 32,
        .red = {16, 8, 0}, .green = {8, 8, 0}, .blue = {0, 8, 0}, .transp = {24, 8, 0}};
    assert(layout_valid(&fixed, &variable));
    struct fb_var_screeninfo invalid = variable;
    invalid.xoffset = UINT32_MAX;
    assert(!layout_valid(&fixed, &invalid));
    invalid = variable;
    invalid.yoffset = UINT32_MAX;
    assert(!layout_valid(&fixed, &invalid));
    invalid = variable;
    invalid.red.offset = 32;
    assert(!layout_valid(&fixed, &invalid));
    invalid = variable;
    invalid.red.length = 9;
    assert(!layout_valid(&fixed, &invalid));
    invalid = variable;
    invalid.red.length = 0;
    assert(!layout_valid(&fixed, &invalid));
    invalid = variable;
    invalid.transp.offset = 16;
    assert(!layout_valid(&fixed, &invalid));
    invalid = variable;
    invalid.blue.msb_right = 1;
    assert(!layout_valid(&fixed, &invalid));
    invalid = variable;
    invalid.grayscale = 1;
    assert(!layout_valid(&fixed, &invalid));
    struct fb_fix_screeninfo broken = fixed;
    broken.line_length = 640 * 4;
    assert(!layout_valid(&broken, &variable));
    broken = fixed;
    broken.smem_len = 256;
    assert(!layout_valid(&broken, &variable));
    broken = fixed;
    broken.visual = FB_VISUAL_PSEUDOCOLOR;
    assert(!layout_valid(&broken, &variable));
    struct surface screen = {640, 480, calloc(640 * 480, sizeof(uint32_t))};
    assert(screen.pixels);
    for (size_t index = 0; index < 640 * 480; index++)
        screen.pixels[index] = 0x112233;
    for (unsigned format = 0; format < 3; format++) {
        if (format == 1) {
            variable.bits_per_pixel = 24;
            variable.transp = (struct fb_bitfield){0, 0, 0};
        } else if (format == 2) {
            variable.bits_per_pixel = 16;
            variable.red = (struct fb_bitfield){11, 5, 0};
            variable.green = (struct fb_bitfield){5, 6, 0};
            variable.blue = (struct fb_bitfield){0, 5, 0};
        }
        assert(layout_valid(&fixed, &variable));
        uint8_t *memory = malloc(fixed.smem_len + 32);
        assert(memory);
        memset(memory, 0xa5, fixed.smem_len + 32);
        present(memory, &fixed, &variable, &screen);
        uint32_t bytes = variable.bits_per_pixel / 8;
        uint32_t expected = format == 0 ? 0xff112233 : format == 1 ? 0x112233 : 0x1106;
        for (size_t offset = 0; offset < fixed.smem_len + 32; offset++) {
            size_t row = offset / fixed.line_length, column = offset % fixed.line_length;
            if (row >= variable.yoffset && row < variable.yoffset + variable.yres
                && column >= variable.xoffset * bytes && column < (variable.xoffset + variable.xres) * bytes) {
                unsigned byte = (unsigned)(column - variable.xoffset * bytes) % bytes;
                assert(memory[offset] == (uint8_t)(expected >> (byte * 8)));
            } else {
                assert(memory[offset] == 0xa5);
            }
        }
        free(memory);
    }
    assert(component(255, (struct fb_bitfield){11, 5, 0}) == 0xf800);
    assert(component(0, (struct fb_bitfield){11, 5, 0}) == 0);
    for (uint32_t mode = 0; mode < 4; mode++)
        scene(&screen, UINT32_MAX, mode, true);
    free(screen.pixels);
    puts("MIKRO_DISPLAY_UNIT_PASS geometry, RGB565/RGB888/XRGB8888, offsets, stride, canaries and scenes");
    return 0;
}

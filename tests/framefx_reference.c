#define main framefx_application_main
#include "../linux/framefx/app/framefx-test.c"
#undef main

int main(int argc, char **argv)
{
    check(argc == 6, "reference arguments");
    unsigned width = number(argv[1], 1024), height = number(argv[2], 1024);
    unsigned seed = number(argv[3], UINT32_MAX), mode = number(argv[4], 3), threshold = number(argv[5], 255);
    uint8_t *pixels = malloc(width * height);
    check(pixels && width && height, "reference allocation");
    for (unsigned row = 0; row < height; row++)
        for (unsigned column = 0; column < width; column++)
            pixels[row * width + column] = input_pixel(row, column, seed);
    for (unsigned row = 0; row < height; row++)
        for (unsigned column = 0; column < width; column++)
            check(putchar(golden(pixels, width, height, width, row, column, mode, threshold)) != EOF, "reference output");
    free(pixels);
    return 0;
}

#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <linux/kd.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define IMAGE_WIDTH 256U
#define IMAGE_HEIGHT 192U
#define INPUT_COUNT 32U

struct surface {
    uint32_t width;
    uint32_t height;
    uint32_t *pixels;
};

static volatile sig_atomic_t stopping;

static void stop_display(int signal_number)
{
    (void)signal_number;
    stopping = 1;
}

static bool number(const char *text, uint32_t maximum, uint32_t *result)
{
    char *end;
    if (!*text)
        return false;
    for (const char *cursor = text; *cursor; cursor++)
        if (*cursor < '0' || *cursor > '9')
            return false;
    errno = 0;
    unsigned long value = strtoul(text, &end, 10);
    if (errno || *end || value > maximum)
        return false;
    *result = (uint32_t)value;
    return true;
}

static uint8_t filtered(const uint8_t *source, uint32_t width, uint32_t height,
                        uint32_t row, uint32_t column, uint32_t mode, uint32_t threshold)
{
    uint32_t offset = row * width + column;
    if (mode == 0)
        return source[offset];
    if (mode == 1)
        return 255 - source[offset];
    if (mode == 2)
        return source[offset] >= threshold ? 255 : 0;
    if (!row || !column || row + 1 == height || column + 1 == width)
        return 0;
    uint32_t top = offset - width, bottom = offset + width;
    int horizontal = -source[top - 1] + source[top + 1] - 2 * source[offset - 1]
        + 2 * source[offset + 1] - source[bottom - 1] + source[bottom + 1];
    int vertical = -source[top - 1] - 2 * source[top] - source[top + 1]
        + source[bottom - 1] + 2 * source[bottom] + source[bottom + 1];
    unsigned magnitude = (unsigned)(abs(horizontal) + abs(vertical)) / 4;
    return magnitude > 255 ? 255 : (uint8_t)magnitude;
}

static int reference(int count, char **arguments)
{
    uint32_t width, height, mode, threshold, seed;
    if (count != 7 || !number(arguments[2], 1024, &width) || !width
        || !number(arguments[3], 1024, &height) || !height
        || !number(arguments[4], 3, &mode) || !number(arguments[5], 255, &threshold)
        || !number(arguments[6], UINT32_MAX, &seed) || (mode == 3 && (width < 3 || height < 3)))
        return 2;
    uint8_t *source = malloc((size_t)width * height);
    if (!source)
        return 1;
    for (uint32_t row = 0; row < height; row++)
        for (uint32_t column = 0; column < width; column++)
            source[row * width + column] = (uint8_t)((row * 0x45d9f3bU + column * 0x119de1f3U + seed * 17U)
                                                   ^ (column >> 3) ^ (row << 5));
    bool success = true;
    for (uint32_t row = 0; row < height && success; row++)
        for (uint32_t column = 0; column < width; column++)
            if (putchar(filtered(source, width, height, row, column, mode, threshold)) == EOF) {
                success = false;
                break;
            }
    free(source);
    return success && fflush(stdout) == 0 ? 0 : 1;
}

static const uint8_t font[36][7] = {
    {14,17,17,31,17,17,17}, {30,17,17,30,17,17,30}, {14,17,16,16,16,17,14},
    {30,17,17,17,17,17,30}, {31,16,16,30,16,16,31}, {31,16,16,30,16,16,16},
    {14,17,16,23,17,17,14}, {17,17,17,31,17,17,17}, {31,4,4,4,4,4,31},
    {7,2,2,2,18,18,12}, {17,18,20,24,20,18,17}, {16,16,16,16,16,16,31},
    {17,27,21,21,17,17,17}, {17,25,21,19,17,17,17}, {14,17,17,17,17,17,14},
    {30,17,17,30,16,16,16}, {14,17,17,17,21,18,13}, {30,17,17,30,20,18,17},
    {15,16,16,14,1,1,30}, {31,4,4,4,4,4,4}, {17,17,17,17,17,17,14},
    {17,17,17,17,17,10,4}, {17,17,17,21,21,21,10}, {17,17,10,4,10,17,17},
    {17,17,10,4,4,4,4}, {31,1,2,4,8,16,31}, {14,17,19,21,25,17,14},
    {4,12,4,4,4,4,14}, {14,17,1,2,4,8,31}, {30,1,1,14,1,1,30},
    {2,6,10,18,31,2,2}, {31,16,16,30,1,1,30}, {14,16,16,30,17,17,14},
    {31,1,2,4,8,8,8}, {14,17,17,14,17,17,14}, {14,17,17,15,1,1,14}
};

static void rectangle(struct surface *screen, uint32_t left, uint32_t top,
                      uint32_t width, uint32_t height, uint32_t color)
{
    if (left >= screen->width || top >= screen->height)
        return;
    if (width > screen->width - left)
        width = screen->width - left;
    if (height > screen->height - top)
        height = screen->height - top;
    for (uint32_t row = top; row < top + height; row++)
        for (uint32_t column = left; column < left + width; column++)
            screen->pixels[(size_t)row * screen->width + column] = color;
}

static void text(struct surface *screen, uint32_t left, uint32_t top, const char *message,
                 uint32_t scale, uint32_t color)
{
    while (*message && left < screen->width) {
        unsigned char character = (unsigned char)*message++;
        const uint8_t *glyph = NULL;
        if (character >= 'A' && character <= 'Z')
            glyph = font[character - 'A'];
        else if (character >= '0' && character <= '9')
            glyph = font[26 + character - '0'];
        for (uint32_t row = 0; row < 7; row++) {
            uint8_t bits = glyph ? glyph[row] : character == '-' && row == 3 ? 31 : 0;
            for (uint32_t column = 0; column < 5; column++)
                if (bits & (1U << (4 - column)))
                    rectangle(screen, left + column * scale, top + row * scale, scale, scale, color);
        }
        left += scale * 6;
    }
}

static void scene(struct surface *screen, uint32_t frame, uint32_t mode, bool paused)
{
    uint8_t source[IMAGE_WIDTH * IMAGE_HEIGHT];
    const char *names[] = {"COPY", "INVERT", "THRESHOLD", "SOBEL"};
    uint32_t scale = (screen->width / 2 - 32) / IMAGE_WIDTH;
    uint32_t vertical_scale = (screen->height - 240) / IMAGE_HEIGHT;
    if (scale > vertical_scale)
        scale = vertical_scale;
    if (!scale)
        scale = 1;
    int phase = (int)(frame % 160);
    int center = 88 + (phase < 80 ? phase : 160 - phase);
    for (uint32_t row = 0; row < IMAGE_HEIGHT; row++)
        for (uint32_t column = 0; column < IMAGE_WIDTH; column++) {
            int horizontal = (int)column - center, vertical = (int)row - 96;
            int radius = horizontal * horizontal + vertical * vertical;
            uint8_t pixel = ((row / 24 + column / 24) % 2) ? 42 : 78;
            if (column > 16 && column < 65 && row > 12 && row < 180)
                pixel = (uint8_t)(row * 255 / (IMAGE_HEIGHT - 1));
            if (radius < 52 * 52)
                pixel = 220;
            if (radius < 28 * 28)
                pixel = 25;
            if (column > 218 && row % 24 < 12)
                pixel = 245;
            source[row * IMAGE_WIDTH + column] = pixel;
        }
    rectangle(screen, 0, 0, screen->width, screen->height, 0x101722);
    text(screen, 24, 20, "HELLO ULTRA96 V1", 3, 0x5fe2c6);
    text(screen, 24, 58, "MIKRO LINUX - CPU REFERENCE - NOT FPGA", 2, 0xffdb9c);
    text(screen, 24, 84, "FRAMEFX PL AND PHYSICAL DMA IRQ NOT TESTED", 1, 0xffdb9c);
    text(screen, 24, 114, "GENERATED INPUT", 1, 0xe9f1fb);
    text(screen, screen->width / 2 + 8, 114, names[mode], 2, 0x5fe2c6);
    for (uint32_t row = 0; row < IMAGE_HEIGHT; row++)
        for (uint32_t column = 0; column < IMAGE_WIDTH; column++) {
            uint32_t input = source[row * IMAGE_WIDTH + column];
            uint32_t output = filtered(source, IMAGE_WIDTH, IMAGE_HEIGHT, row, column, mode, 127);
            rectangle(screen, 24 + column * scale, 140 + row * scale, scale, scale, input * 0x010101);
            rectangle(screen, screen->width / 2 + 8 + column * scale, 140 + row * scale,
                      scale, scale, output * 0x010101);
        }
    uint32_t footer = 160 + IMAGE_HEIGHT * scale;
    char caption[80];
    snprintf(caption, sizeof(caption), "FRAME %u - %s - CPU ONLY", frame, paused ? "PAUSED" : "MOVING");
    text(screen, 24, footer, caption, 1, 0xe9f1fb);
    text(screen, 24, footer + 24, "1-4 FILTER   SPACE PAUSE   ESC EXIT", 1, 0x5fe2c6);
    text(screen, 24, footer + 48, "USB SSH 192 168 3 1 - KEY AUTH ONLY", 1, 0xa5b6cc);
    text(screen, 24, footer + 72, "DISPLAY ACCEPTANCE REQUIRES OPERATOR OBSERVATION", 1, 0xa5b6cc);
}

static bool layout_valid(const struct fb_fix_screeninfo *fixed, const struct fb_var_screeninfo *variable)
{
    if (fixed->type != FB_TYPE_PACKED_PIXELS || fixed->visual != FB_VISUAL_TRUECOLOR
        || variable->nonstd || variable->grayscale || variable->xres < 640 || variable->xres > 4096
        || variable->yres < 480 || variable->yres > 2160
        || (variable->bits_per_pixel != 16 && variable->bits_per_pixel != 24 && variable->bits_per_pixel != 32)
        || (uint64_t)variable->xoffset + variable->xres > variable->xres_virtual
        || (uint64_t)variable->yoffset + variable->yres > variable->yres_virtual)
        return false;
    uint64_t row_end = ((uint64_t)variable->xoffset + variable->xres) * (variable->bits_per_pixel / 8);
    uint64_t end = ((uint64_t)variable->yoffset + variable->yres - 1) * fixed->line_length + row_end;
    if (row_end > fixed->line_length || end > fixed->smem_len || fixed->smem_len > 128U * 1024 * 1024)
        return false;
    struct fb_bitfield channels[] = {variable->red, variable->green, variable->blue, variable->transp};
    uint32_t occupied = 0;
    for (size_t index = 0; index < 4; index++) {
        struct fb_bitfield channel = channels[index];
        if ((!channel.length && index != 3) || channel.length > 8 || channel.offset > variable->bits_per_pixel
            || channel.length > variable->bits_per_pixel - channel.offset || channel.msb_right)
            return false;
        if (!channel.length)
            continue;
        uint32_t mask = ((1U << channel.length) - 1) << channel.offset;
        if (mask & occupied)
            return false;
        occupied |= mask;
    }
    return true;
}

static uint32_t component(uint32_t value, struct fb_bitfield channel)
{
    if (!channel.length)
        return 0;
    return ((value * ((1U << channel.length) - 1) + 127) / 255) << channel.offset;
}

static void present(uint8_t *memory, const struct fb_fix_screeninfo *fixed,
                    const struct fb_var_screeninfo *variable, const struct surface *screen)
{
    uint32_t pixel_bytes = variable->bits_per_pixel / 8;
    for (uint32_t row = 0; row < screen->height; row++) {
        uint8_t *destination = memory + (size_t)(row + variable->yoffset) * fixed->line_length
            + (size_t)variable->xoffset * pixel_bytes;
        for (uint32_t column = 0; column < screen->width; column++) {
            uint32_t color = screen->pixels[(size_t)row * screen->width + column];
            uint32_t pixel = component((color >> 16) & 255, variable->red)
                | component((color >> 8) & 255, variable->green)
                | component(color & 255, variable->blue) | component(255, variable->transp);
            for (uint32_t byte = 0; byte < pixel_bytes; byte++)
                *destination++ = (uint8_t)(pixel >> (byte * 8));
        }
    }
}

static int render_file(const char *filename, uint32_t mode, uint32_t frame)
{
    struct surface screen = {800, 600, NULL};
    screen.pixels = calloc((size_t)screen.width * screen.height, sizeof(*screen.pixels));
    if (!screen.pixels)
        return 1;
    scene(&screen, frame, mode, false);
    FILE *output = fopen(filename, "wb");
    if (!output) {
        free(screen.pixels);
        return 1;
    }
    bool success = fprintf(output, "P6\n%u %u\n255\n", screen.width, screen.height) > 0;
    for (size_t index = 0; index < (size_t)screen.width * screen.height && success; index++) {
        uint32_t color = screen.pixels[index];
        uint8_t pixel[] = {(uint8_t)(color >> 16), (uint8_t)(color >> 8), (uint8_t)color};
        success = fwrite(pixel, 1, sizeof(pixel), output) == sizeof(pixel);
    }
    success = fclose(output) == 0 && success;
    free(screen.pixels);
    return success ? 0 : 1;
}

static void keyboard(int *descriptors, uint32_t tick, uint32_t *mode, bool *paused, bool *automatic)
{
    for (uint32_t device = 0; device < INPUT_COUNT; device++) {
        if (descriptors[device] < 0 && tick % 30 == 0) {
            char filename[64];
            snprintf(filename, sizeof(filename), "/dev/input/event%u", device);
            descriptors[device] = open(filename, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
        }
        if (descriptors[device] < 0)
            continue;
        for (unsigned batch = 0; batch < 8; batch++) {
            struct input_event events[32];
            ssize_t received = read(descriptors[device], events, sizeof(events));
            if (received <= 0) {
                if (!received || (errno != EAGAIN && errno != EINTR)) {
                    close(descriptors[device]);
                    descriptors[device] = -1;
                }
                break;
            }
            for (size_t index = 0; index < (size_t)received / sizeof(events[0]); index++) {
                if (events[index].type != EV_KEY || events[index].value != 1)
                    continue;
                if (events[index].code >= KEY_1 && events[index].code <= KEY_4) {
                    *mode = events[index].code - KEY_1;
                    *automatic = false;
                } else if (events[index].code == KEY_SPACE) {
                    *paused = !*paused;
                } else if (events[index].code == KEY_ESC) {
                    stopping = 1;
                }
            }
        }
    }
}

static int framebuffer(const char *filename, uint32_t mode)
{
    int descriptor = -1;
    signal(SIGINT, stop_display);
    signal(SIGTERM, stop_display);
    for (unsigned attempt = 0; attempt < 30 && !stopping; attempt++) {
        descriptor = open(filename, O_RDWR | O_CLOEXEC);
        if (descriptor >= 0 || errno != ENOENT)
            break;
        sleep(1);
    }
    if (descriptor < 0) {
        perror("Framebuffer unavailable");
        return 1;
    }
    struct fb_fix_screeninfo fixed;
    struct fb_var_screeninfo variable;
    if (ioctl(descriptor, FBIOGET_FSCREENINFO, &fixed) < 0
        || ioctl(descriptor, FBIOGET_VSCREENINFO, &variable) < 0 || !layout_valid(&fixed, &variable)) {
        fprintf(stderr, "Unsupported or invalid framebuffer geometry/layout\n");
        close(descriptor);
        return 1;
    }
    uint8_t *memory = mmap(NULL, fixed.smem_len, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, 0);
    struct surface screen = {variable.xres, variable.yres, NULL};
    screen.pixels = calloc((size_t)screen.width * screen.height, sizeof(*screen.pixels));
    if (memory == MAP_FAILED || !screen.pixels) {
        if (memory != MAP_FAILED)
            munmap(memory, fixed.smem_len);
        free(screen.pixels);
        close(descriptor);
        return 1;
    }
    int terminal = open("/dev/tty0", O_RDWR | O_CLOEXEC | O_NOCTTY);
    int prior_mode = -1;
    if (terminal >= 0 && ioctl(terminal, KDGETMODE, &prior_mode) == 0)
        ioctl(terminal, KDSETMODE, KD_GRAPHICS);
    ioctl(descriptor, FBIOBLANK, FB_BLANK_UNBLANK);
    printf("AXI_LAB_MIKRO_DISPLAY_STARTED %ux%u bpp=%u CPU_ONLY physical_visibility=OPERATOR_CHECK\n",
           screen.width, screen.height, variable.bits_per_pixel);
    fflush(stdout);
    int inputs[INPUT_COUNT];
    for (size_t index = 0; index < INPUT_COUNT; index++)
        inputs[index] = -1;
    bool paused = false, automatic = true;
    uint32_t frame = 0, tick = 0;
    while (!stopping) {
        keyboard(inputs, tick++, &mode, &paused, &automatic);
        if (!paused) {
            if (automatic && frame && frame % 120 == 0)
                mode = (mode + 1) % 4;
            frame++;
        }
        scene(&screen, frame, mode, paused);
        present(memory, &fixed, &variable, &screen);
        struct timespec delay = {0, 100000000};
        nanosleep(&delay, NULL);
    }
    for (size_t index = 0; index < INPUT_COUNT; index++)
        if (inputs[index] >= 0)
            close(inputs[index]);
    if (terminal >= 0) {
        if (prior_mode >= 0)
            ioctl(terminal, KDSETMODE, prior_mode);
        close(terminal);
    }
    munmap(memory, fixed.smem_len);
    free(screen.pixels);
    close(descriptor);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "--reference") == 0)
        return reference(argc, argv);
    if (argc == 3 && strcmp(argv[1], "--framebuffer") == 0)
        return framebuffer(argv[2], 3);
    if ((argc == 3 || argc == 5) && strcmp(argv[1], "--render") == 0) {
        uint32_t mode = 3, frame = 0;
        if (argc == 5 && (!number(argv[3], 3, &mode) || !number(argv[4], UINT32_MAX, &frame)))
            return 2;
        return render_file(argv[2], mode, frame);
    }
    fprintf(stderr, "Usage: %s --framebuffer /dev/fb0 | --render output.ppm [mode frame] | --reference width height mode threshold seed\n", argv[0]);
    return 2;
}

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include "framefx.h"

int main(void)
{
    struct framefx_caps initial = {0}, current = {0};
    int descriptor = open("/dev/framefx0", O_RDWR | O_CLOEXEC);

    if (descriptor < 0) {
        perror("framefx-probe open");
        return 1;
    }
    for (unsigned iteration = 0; iteration < 64; iteration++) {
        if (ioctl(descriptor, FRAMEFX_QUERY_CAPS, &current) ||
            current.abi_version != FRAMEFX_ABI_VERSION ||
            current.hardware_version != 0x00010000 || current.capabilities != 0x10f ||
            current.max_width != 1024 || current.max_height != 1024 ||
            current.stride_alignment != 64 || current.max_buffer_bytes != FRAMEFX_MAX_BUFFER ||
            current.reserved || (iteration && memcmp(&initial, &current, sizeof(current)))) {
            fprintf(stderr, "FRAMEFX_CSR_FAIL iteration=%u\n", iteration);
            close(descriptor);
            return 1;
        }
        initial = current;
        usleep(1000);
    }
    if (close(descriptor)) {
        perror("framefx-probe close");
        return 1;
    }
    puts("FRAMEFX_CSR_PASS reads=64 ABI=1 hardware=0x00010000 caps=0x10f DMA_NOT_TESTED");
    return 0;
}

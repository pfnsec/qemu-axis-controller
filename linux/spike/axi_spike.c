#include <linux/completion.h>
#include <linux/dma-mapping.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>

#define SPIKE_MAGIC 0x53504b31
#define CONTROL 0x08
#define STATUS 0x0c
#define IRQ_STATUS 0x10
#define IRQ_ENABLE 0x14
#define SOURCE_LOW 0x18
#define SOURCE_HIGH 0x1c
#define DEST_LOW 0x20
#define DEST_HIGH 0x24
#define BYTE_MASK 0x28
#define TRANSFER_BEATS 0x2c
#define BUFFER_SIZE 16384
#define SOURCE_OFFSET 32
#define DEST_OFFSET 128
#define TEST_COUNT 40

struct spike_device {
    struct device *device;
    void __iomem *registers;
    void *buffer;
    dma_addr_t dma_address;
    struct completion completion;
    unsigned int interrupts;
    int irq;
};

static int result = -EINPROGRESS;
module_param(result, int, 0444);

static irqreturn_t spike_interrupt(int irq, void *opaque)
{
    struct spike_device *spike = opaque;
    u32 pending = readl(spike->registers + IRQ_STATUS);
    if (pending == U32_MAX || !(pending & 1))
        return IRQ_NONE;
    writel(1, spike->registers + IRQ_STATUS);
    spike->interrupts++;
    complete(&spike->completion);
    return IRQ_HANDLED;
}

static int spike_test(struct spike_device *spike, resource_size_t mmio_address)
{
    u8 *bytes = spike->buffer;
    u8 *expected;
    unsigned int test, index;
    int outcome = 0;
    expected = kmalloc(BUFFER_SIZE, GFP_KERNEL);
    if (!expected)
        return -ENOMEM;
    for (test = 0; test < TEST_COUNT; test++) {
        static const unsigned int burst_counts[] = {16, 17, 33, 257};
        unsigned int beats = test < 36 ? 1 : burst_counts[test - 36];
        unsigned int source_offset = test < 36 ? SOURCE_OFFSET : (test & 1 ? 0xff8 : 0xfc0);
        unsigned int destination_offset = test < 36 ? DEST_OFFSET : (test & 1 ? 0x2ff8 : 0x2f80);
        dma_addr_t source = spike->dma_address + source_offset;
        dma_addr_t destination = spike->dma_address + destination_offset;
        u64 original = 0x1020304050607080ULL ^ ((u64)test * 0x0101010101010101ULL);
        u8 mask = test % 4 == 0 ? 0x55 : test % 4 == 1 ? 0xaa : test % 4 == 2 ? 0xff : 0;
        bool masked_irq = test % 4 == 0;
        bool error_expected = test >= 32 && test < 36;
        u32 status;
        unsigned int before = spike->interrupts;
        if (test == 32) source = mmio_address;
        if (test == 33) destination = mmio_address;
        if (test == 34) source = 0xfffffffffffff000ULL;
        if (test == 35) destination = 0xfffffffffffff000ULL;
        if (test >= 36) mask = 0xff;
        memset(bytes, 0xa5, BUFFER_SIZE);
        if (test < 36)
            memcpy(bytes + source_offset, &original, sizeof(original));
        else
            for (index = 0; index < beats * 8; index++)
                bytes[source_offset + index] = test * 17 + index * 29;
        memcpy(expected, bytes, BUFFER_SIZE);
        if (!error_expected) {
            for (index = 0; index < beats * 8; index++) {
                if (mask & BIT(index % 8))
                    expected[destination_offset + index] = bytes[source_offset + index] ^
                        (u8)(0xd3c2b1a08796e5f4ULL >> ((index % 8) * 8));
            }
        }
        reinit_completion(&spike->completion);
        writel(masked_irq ? 0 : 1, spike->registers + IRQ_ENABLE);
        writel(lower_32_bits(source), spike->registers + SOURCE_LOW);
        writel(upper_32_bits(source), spike->registers + SOURCE_HIGH);
        writel(lower_32_bits(destination), spike->registers + DEST_LOW);
        writel(upper_32_bits(destination), spike->registers + DEST_HIGH);
        writel(mask, spike->registers + BYTE_MASK);
        writel(beats, spike->registers + TRANSFER_BEATS);
        dma_wmb();
        writel(test & 1 ? 3 : 1, spike->registers + CONTROL);
        if (masked_irq) {
            outcome = readl_poll_timeout(spike->registers + STATUS, status,
                                         !(status & 1), 100, 3000000);
            if (outcome || spike->interrupts != before ||
                readl(spike->registers + IRQ_STATUS) != 1) {
                outcome = -EIO;
                break;
            }
            writel(1, spike->registers + IRQ_ENABLE);
        }
        if (!wait_for_completion_timeout(&spike->completion, msecs_to_jiffies(3000))) {
            outcome = -ETIMEDOUT;
            break;
        }
        dma_rmb();
        status = readl(spike->registers + STATUS);
        if (status != (error_expected ? 2 : 0) || spike->interrupts != before + 1 ||
            readl(spike->registers + IRQ_STATUS) != 0 || memcmp(bytes, expected, BUFFER_SIZE)) {
            outcome = -EIO;
            break;
        }
        dev_info(spike->device,
                 "SPIKE_CASE test=%u async=%u masked_irq=%u mask=%02x error=%u src=%*phN dst=%*phN bytes=%u irqs=%u PASS\n",
                 test, test & 1, masked_irq, mask, error_expected,
                 8, bytes + source_offset, 8, bytes + destination_offset, beats * 8, spike->interrupts);
    }
    if (outcome)
        dev_err(spike->device, "SPIKE_FAILURE test=%u error=%d status=%08x\n",
                test, outcome, readl(spike->registers + STATUS));
    kfree(expected);
    return outcome;
}

static int spike_probe(struct platform_device *platform)
{
    struct device *device = &platform->dev;
    struct resource *resource;
    struct spike_device *spike;
    int outcome;
    if (!of_property_read_bool(device->of_node, "example,concurrency-spike"))
        return -ENODEV;
    spike = devm_kzalloc(device, sizeof(*spike), GFP_KERNEL);
    if (!spike)
        return -ENOMEM;
    spike->device = device;
    spike->registers = devm_platform_get_and_ioremap_resource(platform, 0, &resource);
    if (IS_ERR(spike->registers))
        return PTR_ERR(spike->registers);
    if (readl(spike->registers) != SPIKE_MAGIC || readl(spike->registers + 4) != 1)
        return -ENODEV;
    outcome = dma_set_mask_and_coherent(device, DMA_BIT_MASK(64));
    if (outcome)
        return outcome;
    spike->irq = platform_get_irq(platform, 0);
    if (spike->irq < 0)
        return spike->irq;
    init_completion(&spike->completion);
    outcome = devm_request_irq(device, spike->irq, spike_interrupt, 0, "axi-spike", spike);
    if (outcome)
        return outcome;
    spike->buffer = dma_alloc_coherent(device, BUFFER_SIZE, &spike->dma_address, GFP_KERNEL);
    if (!spike->buffer)
        return -ENOMEM;
    platform_set_drvdata(platform, spike);
    dev_info(device, "SPIKE_BIND mmio=%pa irq=%d dma=%pad bytes=%u\n",
             &resource->start, spike->irq, &spike->dma_address, BUFFER_SIZE);
    result = spike_test(spike, resource->start);
    writel(0, spike->registers + IRQ_ENABLE);
    synchronize_irq(spike->irq);
    dev_info(device, "SPIKE_RESULT result=%d tests=%u irqs=%u\n", result, TEST_COUNT, spike->interrupts);
    return 0;
}

static const struct of_device_id spike_match[] = {
    { .compatible = "example,axi-framefx-1.0" },
    { }
};
MODULE_DEVICE_TABLE(of, spike_match);

static struct platform_driver spike_driver = {
    .probe = spike_probe,
    .driver = {
        .name = "axi-spike",
        .of_match_table = spike_match,
        .suppress_bind_attrs = true,
    },
};

static int __init spike_init(void)
{
    return platform_driver_register(&spike_driver);
}
module_init(spike_init);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Boot-lifetime M1 concurrency test, not the framefx driver");

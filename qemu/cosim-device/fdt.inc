#include "hw/boards.h"

static int add_axi_cosim_node(SysBusDevice *sbdev, void *opaque)
{
    PlatformBusFDTData *data = opaque;
    uint64_t offset = platform_bus_get_mmio_addr(data->pbus, sbdev, 0);
    int interrupt = platform_bus_get_irqn(data->pbus, sbdev, 0) + data->irq_start;
    MachineState *machine = MACHINE(qdev_get_machine());
    uint64_t ram_base = machine->ram->addr;
    uint64_t ram_size = memory_region_size(machine->ram);
    if (ram_size > UINT32_MAX || ram_base > UINT32_MAX - ram_size) {
        error_report("axi-cosim v1 requires main RAM below 4 GiB");
        exit(1);
    }
    g_autofree char *node = g_strdup_printf("%s/axi-cosim@%"PRIx64, data->pbus_node_name, offset);
    qemu_fdt_add_subnode(data->fdt, node);
    qemu_fdt_setprop_string(data->fdt, node, "compatible", "example,axi-framefx-1.0");
    if (object_property_get_bool(OBJECT(sbdev), "spike", &error_abort)) {
        qemu_fdt_setprop(data->fdt, node, "example,concurrency-spike", NULL, 0);
    }
    qemu_fdt_setprop_sized_cells(data->fdt, node, "reg", 1, offset, 1, 0x1000);
    qemu_fdt_setprop_cells(data->fdt, node, "interrupts", GIC_FDT_IRQ_TYPE_SPI, interrupt,
                          GIC_FDT_IRQ_FLAGS_LEVEL_HI);
    qemu_fdt_setprop_cells(data->fdt, data->pbus_node_name, "dma-ranges", ram_base,
                          0, ram_base, ram_size);
    qemu_fdt_setprop(data->fdt, node, "dma-coherent", NULL, 0);
    fprintf(stderr, "AXI_DT offset=0x%"PRIx64" size=0x1000 spi=%d node=%s\n", offset, interrupt, node);
    return 0;
}

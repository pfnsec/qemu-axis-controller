import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path, before, after):
    original = path.read_text()
    if original.count(after) == 1:
        return
    if original.count(before) != 1:
        raise RuntimeError(f"QEMU integration anchor changed: {path}")
    path.write_text(original.replace(before, after, 1))


def integrate(source):
    lock = json.loads((ROOT / "versions.lock").read_text())
    if (source / "VERSION").read_text().strip() != lock["sources"]["qemu"]["directory"].removeprefix("qemu-"):
        raise RuntimeError("QEMU version does not match integration hooks")
    replace_once(source / "hw/arm/virt.c",
                 "    machine_class_allow_dynamic_sysbus_dev(mc, TYPE_RAMFB_DEVICE);",
                 '    machine_class_allow_dynamic_sysbus_dev(mc, "axi-cosim");\n'
                 "    machine_class_allow_dynamic_sysbus_dev(mc, TYPE_RAMFB_DEVICE);")
    replace_once(source / "hw/core/sysbus-fdt.c", "static int no_fdt_node(",
                 '#include "axi-cosim-fdt.inc"\n\nstatic int no_fdt_node(')
    replace_once(source / "hw/core/sysbus-fdt.c", "static const BindingEntry bindings[] = {",
                 'static const BindingEntry bindings[] = {\n    TYPE_BINDING("axi-cosim", add_axi_cosim_node),')
    replace_once(source / "hw/misc/meson.build",
                 "system_ss.add(when: 'CONFIG_APPLESMC', if_true: files('applesmc.c'))",
                 "system_ss.add(when: 'CONFIG_ARM_VIRT', if_true: files('axi-cosim.c'))\n"
                 "system_ss.add(when: 'CONFIG_APPLESMC', if_true: files('applesmc.c'))")
    for local, destination in (("qemu/cosim-device/axi-cosim.c", "hw/misc/axi-cosim.c"),
                               ("qemu/cosim-device/fdt.inc", "hw/core/axi-cosim-fdt.inc"),
                               ("transport/wire.h", "hw/misc/wire.h")):
        content = (ROOT / local).read_bytes()
        target = source / destination
        if not target.exists() or target.read_bytes() != content:
            target.write_bytes(content)


if __name__ == "__main__":
    integrate(Path(sys.argv[1]))

# ADR 0002 Ultra96 V1 volatile PL integration

Status: physical volatile PL/CSR, DMA/IRQ and all-filter gates passed after
reviewed vendor-UIO IRQ handoff; repeatable SD boot integration remains pending.

## Context

The V1 boots the preserved PYNQ 3.0.1/2022.1 firmware and Linux 5.15.19. The host
has Vivado 2026.1 and the pinned 2018.2 V1 board preset. DisplayPort acceptance is
blocked by the adapter, but is not a prerequisite for headless accelerator work.
Replacing the full firmware stack would unnecessarily put working DDR, SD and
USB recovery at risk before any PL transaction is established.

## Decision

Build a complete, volatile PL configuration for the exact V1 part. Apply the V1
preset in Vivado but deploy no generated PS initialization, firmware or base DTB.
Use the shipped V1 sensors overlay's 32-bit HPM0 LPD control interface and
Vivado-assigned `0x80000000` aperture, instead of changing HPM control ports.
Use HP0 FPD at 128 bits behind a 64-bit SmartConnect DMA path, restricted to low
DDR, without coherence or IOMMU claims. Share a 100 MHz PL clock and synchronized
reset; route the level interrupt through `pl_ps_irq0[0]`. No external PL IO.

Keep the synthesizable core and UAPI unchanged. The board wrapper adds interface
metadata and fixed AXI attributes only. The common kernel driver owns its
optional clock for the same lifetime as its buffers/quarantine references.

Use the existing FPGA manager, then the vendor AFI kernel driver, then the device
overlay, then the common framefx driver. Do not use PYNQ MMIO or `/dev/mem` as the
accelerator driver. Require a source/artifact-current build, hashed native board
binaries and explicit confirmation. Query CSRs without allocating or submitting
DMA first. Small copy/IRQ and all-filter checks are separate later gates. These
now pass on the V1: one initial copy followed by 148 filter jobs, with native C
and independent host Python full-allocation comparisons and exact GIC delivery
counts. No new PL configuration or boot-firmware change was needed between gates.

Place framefx directly under the existing FPGA region and its `dma-ranges` on
that parent. A nested dynamically added `simple-bus` was not recursively
populated by the vendor kernel in the first physical attempt. Validate the
merged DT hierarchy, then the live platform device and driver binding; an
offline overlay application or successful `insmod` is not proof of probing.

Check GIC interrupt ownership as well as MMIO ownership before programming.
The vendor `fabric` UIO placeholder reserves the same IRQ even with an empty
PL region. Reject any current owner; releasing the exact verified-unused
placeholder is a separate reviewed action, never implicit IRQ sharing or
blanket UIO removal. The first CSR pass required this ownership handoff and
reprobing the cleanly unwound framefx device, without PL reconfiguration.

## Recovery and limitations

The loader refuses an occupied PL region, an operating FPGA, wrong boot hashes,
an MMIO/IRQ collision, unreviewed SMMU configuration or a kernel with fatal/stall
diagnostics. It does not hot-reload, unbind a failed design, modify boot media or
promise to recover a hung AXI/firmware transaction with a userspace timeout.
Preserve root-private stage reports; recover through the unchanged vendor boot
baseline, with a power cycle available. Warm-reboot PL persistence is unverified.

The vendor FPGA-manager `status` attribute invoked configuration readback before
configuration and stalled a CPU for several minutes. It is explicitly excluded
from diagnostics; cached `state` is used instead. The stalled boot must still be
replaced before loading. This workaround is not a firmware repair.

Build/timing success is not physical CSR/DMA/IRQ acceptance or complete M5. A
repeatable SD boot package and reviewed reload/isolation mechanism remain work
after the first supervised hardware gates. Exact commands and primary-source
references are in [board bring-up](../bringup-ultra96.md#headless-pl-integration).

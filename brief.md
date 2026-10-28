# Development Brief: AArch64 Linux ↔ AXI RTL Co-Simulation, with Ultra96-V1 Deployment

> **Original scope and acceptance brief, not current implementation status.**
> Continue from [current status](docs/progress.md) and the [documentation map](docs/README.md).
> Proposed paths, port choices and example commands below are design targets;
> the implemented contracts are in the topic guides and ADRs. In particular,
> V1 control uses HPM0 **LPD**, M4 uses fixed test profiles (not Makefile
> WIDTH/HEIGHT/SEED overrides), and `framefx-ui` remains unimplemented.

- **Project working name:** `axi-cosim-lab`
- **Original date:** 2026-10-15
- **Audience:** Engineering agents and human reviewers
- **Status:** Original design/acceptance scope; implementation state is maintained separately
- **Target hardware:** Avnet Ultra96 **V1**, Zynq UltraScale+ MPSoC XCZU3EG, without JTAG
- **Tool baseline:** See [setup](docs/setup.md), `versions.lock` and recorded inventory rather than redoing the original discovery phase.

> **Agent mandate:** Build and demonstrate the system, not merely propose an architecture. Work through the numbered gates below. Produce runnable code, tests, automation, documentation, and evidence of what passed. Do not fabricate hardware observations. Resolve reasonable implementation details autonomously, record choices in ADRs, and escalate only genuine blockers. It is preferable to deliver a measured, reproducible subset than an elaborate untested integration.

## 1. Objective and definition of done

Build a **generic** AArch64/Linux development machine in QEMU that can interact with **real, synthesizable SystemVerilog RTL** through AXI transactions. The exact ZynqMP PS is **not** the emulation target. The RTL and Linux device-driver source must also be deployable to the real Ultra96 V1, connected through the actual ZynqMP PS–PL AXI interfaces.

The core experiment is a nontrivial **`framefx` 2-D DMA image-processing accelerator**:

1. A Linux program allocates two DMA-safe image buffers, generates a deterministic grayscale image, configures the accelerator, and issues `START` through AXI4-Lite registers.
2. The RTL itself is an AXI4 **master**. It reads source pixels from guest/emulated DDR, transforms them, writes a destination image back to memory, and raises an interrupt.
3. The same Linux driver handles completion, reads counters, and verifies the output against an independently implemented reference algorithm.
4. On Ultra96, the *same RTL module, register layout, UAPI, and driver source* execute using Vivado-generated AXI interconnect/clock/reset integration and a real PS–PL memory path.
5. Optionally, a lightweight application displays the source/result through Linux DRM/KMS: virtio-gpu in QEMU; the Ultra96's PS-driven mini DisplayPort on the board. A USB keyboard chooses filter mode.

### Required final demonstrations

**D1 — Simulated system (required):** Fresh-start command builds/launches QEMU + Verilator, boots AArch64 Linux, runs `framefx-test`, demonstrates register reads, at least two correctly transformed images **including Sobel**, DMA in **both** directions, and interrupt-driven completion; exports images, checksums, traces, and a machine-readable PASS report.

**D2 — Real RTL (required):** Direct Verilator RTL test with AXI backpressure/error injection and an independent scoreboard; not a fake QEMU register implementation or a CPU-side substitute for the pixel transform.

**D3 — Ultra96 (required target, conditional on physical access):** Bitstream + SD-bootable Linux package and instructions; board boots without JTAG and `framefx-test` passes on real DDR/AXI/interrupts. If the agent cannot physically access the board, mark **BLOCKED: physical validation**, preserve reproducible bitstream/build artifacts and exact operator steps, and never say hardware passed.

**D4 — Graphics/input (priority P1):** A DRM/KMS app displays the processed image and reacts to a USB keyboard. Prefer success on both virtual and physical machines, but do not let graphics drivers block D1–D3. Also provide a no-display (`PGM`/PNG file) mode.

**D5 — Synthesizable fast verification (P2 experiment):** A small self-checking FPGA-resident test structure can run seeded tests at an FPGA clock, report errors/counters via registers, and use the same core RTL where practical. A quantitative comparison with Verilator is required before making any speedup claim.

**Never claim:** cycle-accurate A53/Linux timing, bit-for-bit emulation of the Ultra96 PS, AXI coherence absent a defined hardware path, equivalent video hardware in QEMU, or verified physical operation without board evidence.

## 2. Priorities, scope and rules

| Priority | Must include | Explicit exclusions / deferrals |
|---|---|---|
| P0 | Bootable QEMU AArch64 Linux; MMIO proxy; honest AXI4-Lite transactor; Verilator harness; RTL AXI4 DMA master; QEMU guest-RAM DMA and IRQ; portable Linux kernel driver; passing image test; automated build/test | Zynq-specific QEMU machine, simulated R5/PMU, PCI device emulation, KVM requirement, full AXI coherency/IOMMU, dynamic partial reconfiguration |
| P0 hardware | Reproducible Vivado Tcl block design, SD boot path, on-board Linux driver + test, real PL AXI connection | Dependence on JTAG, proprietary simulation-only IP in the main datapath |
| P1 | Display and USB keyboard; better tracing and observability; repeated operations; transport fault handling | Desktop environment, 3-D graphics, high frame rate, exact PS DisplayPort simulation |
| P2 | Synthesizable BIST/scoreboard/tracing, throughput characterization, optional advanced AXI contention tests | Formal proof claims, general FPGA emulation framework, large GUI/IDE |

Engineering policies:

- **One synthesizable DUT** in `rtl/core/`; only narrow top-level wrappers may differ between Verilator and Vivado.
- **One register map and UAPI**, versioned and documented; physical base addresses/IRQ numbers may differ and must be device-tree-provided.
- **One Linux driver source tree**, compiled separately against the guest kernel and board kernel; binary kernel modules are not assumed portable between them.
- **No `/dev/mem` as the product driver**, no user-provided physical DMA addresses, and no unsafe hardcoded DDR physical address.
- Use **TCG** first (`-accel tcg`, one vCPU initially). Treat multi-vCPU, faster scheduling, KVM on compatible hosts, and IOMMU as later performance/robustness work.
- All build inputs need pinned or recorded revisions, tool versions, source hashes, and documented licensing. Do not package a Vivado installation in the repository.
- Do not replace failing RTL behavior with an idealized register/memory model and report it as co-simulation.
- Build/lint/unit-test RTL **before** integrating Linux; keep a short feedback loop even if a full guest boot is slower.

## 3. Proposed architecture

```text
Host (Linux recommended)
┌──────────────────────────────────────────────────────────────────┐
│ QEMU system-aarch64, machine `virt`, TCG                         │
│                                                                  │
│  AArch64 Linux + framefx driver + test/UI                        │
│            │                         ▲                           │
│         MMIO (readl/writel)          │ IRQ via emulated GIC       │
│            ▼                         │                           │
│  custom SysBus `axi-cosim` QEMU device                           │
│  - register-window proxy    - guest DMA AddressSpace operations  │
│  - device-tree node         - IRQ line                            │
└──────────────┬───────────────────────▲───────────────────────────┘
               │ ordered MMIO RPC      │ DMA RPC / IRQ events
               │ socket A              │ socket B (independent)
               ▼                       │
┌──────────────────────────────────────────────────────────────────┐
│ External C++ Verilator simulator                                │
│  transport front end + deterministic event queue                │
│  AXI4-Lite master transactor ──► [ RTL slave: CSRs ]             │
│  AXI4 DDR slave/memory adapter ◄── [ RTL master: DMA ]           │
│  clock/reset engine; IRQ sampling; trace/metrics                 │
│                                                                  │
│              SYNTHESIZABLE framefx core                         │
│    AXI4-Lite control + AXI4 master + image pipeline + IRQ        │
└──────────────────────────────────────────────────────────────────┘

Ultra96 hardware (no QEMU or Verilator):
 A53 Linux ── PS M_AXI_HPM*_FPD ── AXI interconnect ── framefx AXI-Lite slave
 PS DDR   ◄── PS S_AXI_HP*_FPD ◄── AXI interconnect ◄── framefx AXI master
 GIC      ◄── PL-to-PS interrupt ◄── framefx IRQ
                   (clock/reset generated by PS/PL infrastructure)
```

**AXI directions are important:** Linux reads/writes an RTL **AXI slave** for control; RTL acts as an AXI **master** when it fetches/writes images in DDR. The host-side DDR transactor is an AXI **slave** facing that RTL master.

The virtual implementation need not reproduce the number, interconnect topology, timing, or clock domains of Ultra96 PS ports. It **must** preserve architecturally meaningful AXI request/response behavior, driver-visible MMIO semantics, DMA visibility, and IRQ semantics.

### Clock/time model

- RTL: a single deterministic simulated clock (nominal **100 MHz** in metadata) and synchronous reset. All AXI `VALID/READY` handshakes happen on clock edges. Host wall time is **not** 100 MHz real time.
- QEMU: free-running TCG execution. Synchronize at MMIO transactions, DMA transfers, IRQ events, and an explicit reset/finish. The guest CPU is **not cycle-synchronous** with RTL. Do not use guest wall-clock measurements as RTL cycle counts.
- Verilator must continue stepping after the CPU's `START` write even if the guest blocks waiting for an interrupt. Avoid a design that only advances RTL on the next MMIO request.
- Log QEMU/transport wall durations separately from RTL `PERF_CYCLES`; implement a deterministic seeded RTL-only test runner.
- Optional later mode: tighter quantum-based synchronization / recorded transaction replay. Do not spend P0 schedule attempting full CPU/RTL cycle-accurate co-simulation.

## 4. Technology choices and early risk-retirement spike

**Preferred stack:** QEMU's **Arm `virt`** machine; an in-tree/pinned **custom SysBus QEMU device**; Linux device tree; Buildroot-generated AArch64 kernel/rootfs; Verilator C++ harness with direct pin-level AXI4/AXI4-Lite transactors; SystemVerilog RTL; Vivado Tcl + IP Integrator on Ultra96; C Linux driver; C/C++ CLI test/UI and Python golden reference.

Why:

- QEMU `virt` provides generic AArch64 Linux and a generated device tree. Its undocumented peripheral addresses are not stable between versions: integrate with the machine's actual memory map rather than assume an arbitrary base.
- A small custom MMIO/IRQ/DMA proxy makes ownership of transport, time, fault behavior and source-level debugging clear.
- Verilator exposes actual clocked RTL inputs/outputs; no SystemC dependency is necessary for P0.
- Existing Xilinx `libsystemctlm-soc` + Remote-Port and `systemctlm-cosim-demo` are valuable **reference implementations**, especially for bus-master DMA and simulator synchronization. Compare the cost of reuse, but do not bind the first generic `virt` milestone to legacy Xilinx-specific QEMU/firmware versions.

**Mandatory first spike (before implementing the full accelerator):**

1. Boot a minimal Buildroot AArch64 guest using a pinned stock QEMU `virt` build; save boot command, kernel config, DT dump, console log.
2. Add the smallest SysBus peripheral with 4-KiB MMIO, one GIC IRQ, and a correctly generated DT node. Prove an in-guest test/driver can find it by `compatible`, not fixed address. **Inspect actual `virt` source: its platform bus and DT generation may or may not support arbitrary new devices automatically.** A small `virt` machine patch or a documented custom machine variant is acceptable.
3. Build a two-channel local socket transport and, before implementing any complicated RTL, demonstrate an MMIO request **and** a reverse-direction `address_space_read/write` into allocated guest RAM, with no deadlock if a DMA request arrives while a guest MMIO callback is in progress.
4. Check QEMU's memory access rules, thread/AioContext/BQL constraints, error reporting, and `MemTxResult`. The external simulator must **never** directly cast guest physical addresses to host pointers. Do not invoke QEMU internals from an unregistered arbitrary thread.
5. Write a `docs/adr/0001-qemu-device-and-concurrency.md` with the *tested* concurrency design. If blocked by QEMU lock semantics, consider QEMU coroutines, a dedicated QEMU IOThread, or an in-process model adapter. **Do not** silently serialize all traffic onto a mutex that deadlocks on DMA or IRQ.

Implement the rest only after this spike is green. A **custom fixed MMIO region on `virt` must be checked for collisions**, with the base/IRQ emitted by QEMU into the guest DT. Do not commit a speculative `0xA0000000` QEMU address (it can be useful on Ultra96's PL address map, but is not a universal virtual address).

## 5. Repository contract

Create an actual repository with approximately the following layout; simplify where justified but keep the boundaries:

```text
axi-cosim-lab/
├── README.md                        # 10-minute start, exact commands, known limitations
├── LICENSE / NOTICE                  # licenses and provenance
├── versions.lock                     # pinned source revisions / toolchain versions
├── Makefile or justfile              # orchestrated human-facing targets
├── scripts/
│   ├── setup-host.sh                 # validate deps; no destructive global installer
│   ├── build-guest.sh
│   ├── run-sim.sh
│   ├── run-tests.sh
│   ├── collect-evidence.sh
│   └── pack-sd.sh                    # MUST require explicit target path/confirmation
├── qemu/
│   ├── patches/                      # reproducible patch against pinned upstream QEMU
│   ├── cosim-device/                 # SysBus proxy + IRQ + AddressSpace operations
│   └── README.md
├── transport/
│   ├── protocol.md
│   ├── include/
│   ├── src/
│   └── tests/                        # framing, timeouts, disconnects, burst masks
├── sim/
│   ├── verilator_main.cpp
│   ├── axi_lite_master.*
│   ├── axi4_memory_slave.*
│   ├── scheduler.*
│   └── tests/
├── rtl/
│   ├── core/                         # portable synthesizable DUT
│   ├── sim/                          # simulation-only wrappers
│   ├── board/                        # board wrapper; avoid hardcoding PS here
│   ├── tb/                           # assertions, monitors, test-only code
│   └── bist/                         # P2 synthesizable checker
├── linux/
│   ├── driver/                       # Kbuild, .c and common UAPI header
│   ├── bindings/                     # DT binding YAML + sample nodes
│   ├── buildroot/                    # defconfig, packages, overlay
│   ├── ultra96/                      # board DT additions, package/config notes
│   └── uapi/framefx.h
├── apps/
│   ├── framefx-test/                 # deterministic CLI + reference comparisons
│   ├── framefx-ui/                   # optional DRM/KMS + evdev
│   └── reference/                    # independently coded reference algorithm
├── fpga/
│   ├── ultra96-v1/
│   │   ├── create_project.tcl
│   │   ├── create_bd.tcl
│   │   ├── constraints/
│   │   ├── boot/
│   │   └── README.md
│   └── README.md
├── tests/
│   ├── integration/
│   ├── fault-injection/
│   └── golden-vectors/
├── docs/
│   ├── architecture.md
│   ├── register-map.md
│   ├── portability.md
│   ├── bringup-ultra96.md
│   ├── demo-script.md
│   ├── verification.md
│   ├── performance.md
│   └── adr/
└── artifacts/                        # ignored generated logs, images, traces, builds
```

**Automation targets required:** `make doctor`, `make rtl-lint`, `make rtl-test`, `make guest`, `make qemu`, `make sim`, `make demo-sim`, `make test`, `make fpga` (where licensed tools are available), `make report`. These are **requested future repository targets**, not claims that commands exist yet. `make demo-sim` must run unattended from a fresh build and exit nonzero on failure. A timeout prevents permanently hanging CI.

Use out-of-tree build directories; avoid downloading unpinned `latest` at each invocation. Store code/configuration and hashes in git; keep generated bitstreams, rootfs images, captured frames, binaries and proprietary tool files outside ordinary source commits unless their license permits redistribution. Favor a CI job that exercises P0 in headless mode without Vivado.

## 6. QEMU virtual device and DT behavior

Implement a device representing the **host-side bridge**, not a fake `framefx` accelerator:

- SysBus MMIO aperture: **4 KiB**, little-endian, 32-bit aligned accesses first; only legal accesses are forwarded to the Verilator AXI4-Lite master. Unexpected sizes/misalignment must be diagnosed and tested. Do not simply return a fake `STATUS=DONE` in QEMU.
- One level-high interrupt line into the guest GIC; level follows the DUT's exposed IRQ signal. Do not pulse it so briefly that guest Linux misses it. Clearing the DUT's IRQ source must deassert the line.
- The guest DT node shall contain `compatible = "example,axi-framefx-1.0"`, `reg`, `interrupts`, and a correct DMA addressing/coherency description for this **device**. The exact MMIO base and GIC SPI are discovered/allocated by the selected machine and recorded at startup.
- The device shall expose a bus-master view of guest memory. For the first virtual configuration use **identity-mapped DMA bus addresses into ordinary guest RAM**, with no virtual IOMMU. The C Linux driver must still use Linux's DMA API to obtain addresses; never treat guest virtual addresses as DMA addresses.
- QEMU-side DMA servicing uses its **AddressSpace/memory APIs** and checks transaction results. Reject unmapped, non-RAM, ROM, or MMIO-targeting DMA accesses; do not recurse into the cosim MMIO window. Split transfers at mapping boundaries if needed. Validate overflow (`addr + len`), size caps, and permissions.
- On bridge disconnect, timeout, malformed frame, or reset, log one clear fault; don't leave Linux permanently wedged in an MMIO read or interrupt wait. Define fail-closed behavior (e.g., safe MMIO sentinel and error/IRQ state) and regression-test it. Linux driver recovery should return an error to callers. Guest may need a clean restart after a serious transport fault.
- Machine resets cleanly reset transactors, outstanding queues and guest-visible interrupt state. Document that VM live migration and QEMU snapshot/restore while the external RTL process is active are **unsupported** in v1.

### DT portability

Keep QEMU and board **two separate DT integration paths** with the **same `compatible` and register offsets**. The actual board DT will include clocks/resets and `dma-ranges`/bus relationships as dictated by the generated Vivado PS topology; QEMU may omit such entries. Do **not** globally mark a non-coherent physical HP port `dma-coherent` to make tests appear to work. First release does not rely on an SMMU.

A sample node shape (illustrative only; neither numerical IRQ nor address is specified):

```dts
framefx@<board-assigned-address> {
    compatible = "example,axi-framefx-1.0";
    reg = <...>;
    interrupts = <...>;
    /* DMA bus address translation and optional clocks depend on target. */
};
```

Actual DTS must be valid and generated/verified against the target's inherited `#address-cells`, `#size-cells`, GIC, clocks, and DMA topology. Test by inspecting `/proc/device-tree`, `dmesg`, `/proc/interrupts`, and the bound platform driver.

## 7. Inter-process transport protocol and deadlock rules

Use **two independent Unix-domain stream sockets** (or an equivalently proven full-duplex design), not a single process-wide critical section:

- **A — command channel:** QEMU-initiated `MMIO_READ`/`MMIO_WRITE` and corresponding replies; strict ordering per requester. A command reply is issued after the RTL AXI4-Lite transaction has completed its `R` or `B` handshake.
- **B — reverse channel:** RTL-initiated `DMA_READ` / `DMA_WRITE` and their replies, plus `IRQ_LEVEL` notifications and reset/event control. The QEMU-side service must stay runnable if the vCPU is waiting on channel A. IRQ changes must be marshaled through the appropriate QEMU execution context, never unsafely driven by a raw socket thread.

Create a small **versioned, length-framed, explicitly little-endian binary protocol**. Required header fields: magic, major/minor version, message kind, monotonically increasing request ID, address or register offset, payload length, flags/response code. Define exact byte offsets in `transport/protocol.md` and implement independent encode/decode tests; don't transmit native C structs with unexamined padding. Enforce a configurable maximum frame size (start at 1 MiB or less). Include a `HELLO` handshake advertising capabilities (MMIO word width, AXI master data width, max burst, IRQs, deterministic clock metadata), `RESET` and explicit session shutdown.

Message families: `HELLO`, `MMIO_RD_REQ/RSP`, `MMIO_WR_REQ/RSP`, `DMA_RD_REQ/RSP`, `DMA_WR_REQ/RSP`, `IRQ_LEVEL`, `RESET`, `ERROR`, `GOODBYE`. Include typed response statuses for OK, bad alignment, range error, unsupported access, timed out, protocol failure, and simulator stopped. Match request IDs; support partial socket reads/writes and EINTR; avoid unbounded allocations. Never interpret sockets as exposed to untrusted networks in P0 (bind local, restrict permissions).

**Important implementation detail:** the Verilator memory-side transactor should **coalesce a complete legal AXI burst** into one reverse DMA RPC when practical, rather than make one host socket round trip per byte or beat. For reads, fetch data in one transaction then present individual `RVALID/RDATA/RLAST` beats to the RTL, respecting backpressure. For writes, collect `AW`/`W`/`WSTRB` beats and send the masked burst to QEMU before issuing `BRESP` to RTL. QEMU applies byte strobes using runs of actual writes, **not** a read-modify-write that overwrites concurrent adjacent bytes. This optimization must not bypass RTL AXI channel timing or protocol checking.

**No deadlock invariant:** while the guest is blocked in an MMIO write to `START`, the RTL may issue a DMA read and then raise an IRQ; the reverse DMA service must complete. Build this as a minimal integration test before the real image pipeline. Show successful repetitions under backpressure and with an MMIO operation outstanding.

**Failure rules:** finite host-side timeouts, explicit transport error logs, deterministic process shutdown on broken pipe, proper wakeup of blocked QEMU callers, and no successful response for a failed DMA. Map a failed DMA read/write to an AXI `SLVERR`/`DECERR` and ultimately an RTL-visible error status, rather than silently returning zero-filled memory as if it were valid.

## 8. `framefx` hardware/IP specification

### 8.1 Block interface and minimum implementation

**RTL top-level:** `framefx_core.sv` with:

- one **AXI4-Lite slave**, 32-bit data, word aligned CSR registers;
- one **AXI4 master**, **64-bit data**, AXI address width **64 bits**, initially a single AXI ID and at most one outstanding read burst plus one outstanding write burst; support `AW/AR` addresses, `LEN`, `SIZE`, `BURST`, `VALID/READY`, `WSTRB`, `RESP`, `LAST`, and handshake backpressure;
- one synchronous active-low reset, single clock, one level-sensitive IRQ output;
- synthesizable streaming processing pipeline + counters + fault state. No DPI, file I/O, random testbench primitives, unsynthesizable delays, vendor IP inside `rtl/core/`.

Choose **8-bit grayscale input/output** for v1: uncomplicated portable buffers, cheap line buffers, easy software golden model, and straightforward display expansion. Support `width` and `height` up to **1024**, with a valid Sobel image requiring each >=3. Source and destination image bases are 64-byte-aligned, `src_stride` and `dst_stride` are multiples of 64, each >= `round_up(width,64)`. Input/output buffers must **not overlap**. The driver prevents overlap; the RTL checks obvious invalid configurations. Different pitches are allowed. The extra bytes at row ends are padding and must never be treated as image pixels or overwritten by partial output beats.

#### Required filters (selectable at runtime)

| `MODE` | Behavior for pixel `p` | Priority |
|---|---|---|
| `0` | Copy / identity (`out=p`) | P0 |
| `1` | Invert (`out=255-p`) | P0 |
| `2` | Threshold (`out=255` if `p >= THRESHOLD`, else `0`) | P0 |
| `3` | Sobel edge magnitude, 3×3, specified below | **P0 final-demo requirement** |

For Sobel, use neighbors indexed `p00 p01 p02 / p10 p11 p12 / p20 p21 p22`:

```text
gx = -p00 + p02 - 2*p10 + 2*p12 - p20 + p22
gy = -p00 - 2*p01 - p02 + p20 + 2*p21 + p22
out = min(255, (abs(gx) + abs(gy)) >> 2)
```

All inputs are unsigned 8-bit; intermediate signed arithmetic must be sufficiently wide, and the right shift is on a nonnegative integer. **Every border pixel has output exactly 0**. This intentionally uses the L1 approximation and a defined scale rather than a square root: the Python/C and RTL outputs must match byte-for-byte. Implement on-chip row/line buffering (not nine independent DDR reads per output pixel) for Sobel, with sensible separation between DMA, buffering and transform. A full-frame on-chip BRAM is not required.

### 8.2 Control/status register ABI

All offsets below are from the base of the **AXI4-Lite** window, little-endian **32-bit words**. Unlisted offsets are reserved and should return `SLVERR` on illegal accesses. Preserve backwards-compatible register meanings after v1.

| Offset | Name | Access | Contract |
|---:|---|---|---|
| `0x000` | `MAGIC` | RO | `0x46524658` (fixed identifier; document as a numeric constant) |
| `0x004` | `VERSION` | RO | `0x00010000` for ABI 1.0 |
| `0x008` | `CAPS` | RO | Bits 0–3 indicate identity, invert, threshold, Sobel; bit 8 indicates 64-bit DMA data path |
| `0x00C` | `CONTROL` | WO | Bit 0 `START` (write-1 pulse); bit 1 `SOFT_RESET` (idle only); all other bits ignored or specified reserved |
| `0x010` | `STATUS` | RO | Bit 0 `BUSY`, bit 1 sticky `DONE`, bit 2 sticky `ERROR`; only one operation active |
| `0x014` | `SRC_LO` | RW idle | DMA source address [31:0] |
| `0x018` | `SRC_HI` | RW idle | DMA source address [63:32] |
| `0x01C` | `DST_LO` | RW idle | DMA destination address [31:0] |
| `0x020` | `DST_HI` | RW idle | DMA destination address [63:32] |
| `0x024` | `WIDTH` | RW idle | Pixel columns, 1–1024 (Sobel >=3) |
| `0x028` | `HEIGHT` | RW idle | Pixel rows, 1–1024 (Sobel >=3) |
| `0x02C` | `SRC_STRIDE` | RW idle | Bytes between input rows; 64-byte aligned |
| `0x030` | `DST_STRIDE` | RW idle | Bytes between output rows; 64-byte aligned |
| `0x034` | `MODE` | RW idle | 0=copy, 1=invert, 2=threshold, 3=Sobel |
| `0x038` | `THRESHOLD` | RW idle | Bits [7:0]; ignored by other modes |
| `0x03C` | `IRQ_STATUS` | RW1C | Bit 0 completion, bit 1 error; writing 1 clears the corresponding pending bit |
| `0x040` | `IRQ_ENABLE` | RW | Bit 0 completion, bit 1 error; IRQ is level-high iff enabled pending bit is set |
| `0x044` | `ERROR_CODE` | RO | 0=none; 1=bad configuration, 2=read failure, 3=write failure, 4=busy START, 5=unexpected protocol response |
| `0x048` | `PERF_CYCLES_LO` | RO | Count core clock cycles during most recent operation |
| `0x04C` | `PERF_CYCLES_HI` | RO | High 32 bits of cycle count |
| `0x050` | `BYTES_READ_LO` | RO | Payload **bus bytes requested** from DDR for last job, modulo 2^32 |
| `0x054` | `BYTES_WRITTEN_LO` | RO | Bytes actually written where `WSTRB=1`, modulo 2^32 |

Define how busy-time register writes behave: **reject control-parameter writes and report `SLVERR`** without corrupting in-flight state; a busy `START` sets sticky error and `ERROR_CODE=4` but does not abort the existing job. Reading counters while busy returns documented snapshots; each operation resets its own counters at accepted `START`. `DONE` and `ERROR` clear upon the next valid `START` and on idle-only `SOFT_RESET`; `IRQ_STATUS` is W1C and also cleared on valid `START`. Make IRQ assertion/deassertion testable over repeated jobs. Define a stable ordering for setting `DONE`, pending IRQ, clearing `BUSY`, and making DMA writes visible **before** Linux observes completion.

Treat 64-bit address pair writes as config only while idle, then latch the entire configuration on `START`. Use a tested strategy for reading multiword counters consistently (e.g., latch high on low read or expose counters only after done); document behavior.

### 8.3 AXI master requirements

- Use **incrementing bursts**, no more than **16 × 8-byte beats** per burst in v1; do not cross **4-KiB boundaries** and obey AXI 4-KiB rule. Split at row boundaries and at the configured maximum burst length.
- Every active row may read up to `round_up(width,8)` bus bytes, within the allocated stride; ignore padding when computing filters. Last write beat uses accurate `WSTRB` for pixel count; bytes outside the active image are **untouched**.
- `ARLEN/AWLEN` are **beats minus one**, `ARSIZE/AWSIZE` encode 8 bytes, `RLAST/WLAST` must be exact; maintain stable payload under `VALID&&!READY` stalls. Respond to and propagate non-OKAY `RRESP/BRESP`.
- Handle independently handshaken AW/W and AR/R channels without assuming a fixed ready latency. One ID / modest outstanding depth is acceptable and should be parameterized for later scaling.
- Prevent DMA from beginning on invalid width/height/stride, pointer alignment or size overflow. The Linux driver also verifies allocations and bounds.
- Set an accurate IRQ only after the final B response for the last write burst. Backpressure can stall arbitrarily for finite intervals. Define a simulator watchdog (host test timeout); an RTL hardware watchdog is optional unless it proves necessary.
- For physical use, let Vivado SmartConnect perform necessary AXI data-width / clock conversions without changing the core's **register ABI** or software logic.

## 9. Linux driver and user-space design

### Kernel module

Implement an OF-platform driver matching `example,axi-framefx-1.0`; e.g. `framefx.ko`:

- `devm_platform_ioremap_resource`, `platform_get_irq`, request IRQ and create `/dev/framefx0`; parse version and capabilities. No absolute base addresses in C.
- Verify/set a **DMA mask compatible with both target platforms** (prefer explicit 64-bit addressing in RTL; if physical hardware or DT limits require a smaller mask, probe the allowed mask and log it). Use `dma_addr_t`, not an unchecked `uint32_t` cast.
- Allocate separate source/destination DMA-safe buffers using `dma_alloc_coherent` for the first portable implementation; export them to the owning user process with `dma_mmap_coherent` (or equivalent supported mapping API). Validate sizes, alignment, stride and bounds against each allocation; enforce one owning job at a time. Restrict mapping to the correct buffers, prevent freed mappings being used, and serialize close/remove/unbind against in-flight DMA.
- The userspace-visible API must **never** accept arbitrary DMA addresses. Kernel computes/retains addresses returned by the DMA API and programs the RTL itself.
- Provide an intentional, small versioned UAPI in `linux/uapi/framefx.h`: `QUERY_CAPS`, `ALLOC_BUFS` (requested image geometry / capacity, returns mmap offsets), `SUBMIT` (mode/threshold/width/height/strides, no physical addresses), `WAIT` (bounded timeout/status/counters), and `RESET` (idle only). Optionally support `poll()` for readiness. Define ioctl numbers using `_IO*` macros, fixed-width types, reserved fields, and compatibility with 32-bit userspace structures even though AArch64 is the first target.
- Use `readl()/writel()` and appropriate barriers. `dma_alloc_coherent` removes cache-maintenance requirements **for these allocations**, not necessarily ordering requirements. Handle non-coherent AXI HP-port configurations correctly; do **not** use regular pageable `malloc` memory for DMA in the initial design.
- IRQ handler: read `IRQ_STATUS`, acknowledge **W1C**, capture status, wake waiters, count interrupts, deal with spurious/shared indications. Completion must wake waiting processes; interrupt wait must time out and propagate errors.
- Ensure coherent allocation limits and the ZynqMP Linux CMA configuration can support two 1024×1024-ish buffers plus any DisplayPort framebuffer allocations. Document actual CMA settings measured on board. Avoid excessive per-run allocations.
- Record error and recovery behavior on disconnect (sim), reset, kernel module unload, and transport faults. Kernel driver must not indefinitely poll for hardware present only in QEMU.

### Application and independent reference

`framefx-test` must:

1. Query driver version/caps; allocate buffers; fill source with deterministic synthetic gradients, rectangles, diagonals and seeded noise.
2. Fill destination and *every padding byte* with known sentinels to test `WSTRB` and row-stride correctness.
3. Submit one mode, wait on IRQ with a configurable timeout, collect counters/status.
4. Compare the full active image **byte-for-byte** to a **separate CPU golden model**; confirm destination padding remains unchanged and source is unchanged; print mismatch coordinates and surrounding pixels.
5. Run all four modes, varying dimensions, strides and threshold; save input/result as `.pgm` or `.png`, JSON result summary (environment, seed, checksums, cycle counts, timeout info).
6. Return nonzero if any check fails. Support `--width`, `--height`, `--seed`, `--mode`, `--repeat`, `--output`, `--headless`; avoid needing a compositor.

The gold model **must not** call into the RTL simulator or reuse a code-generated hardware reference. Include independent unit tests for the golden Sobel arithmetic and border definition.

## 10. External Verilator simulator implementation

- Compile the **same `rtl/core/`** used by Vivado; simulator wrapper provides an AXI4-Lite *master*, an AXI4 DDR *slave*, clock/reset, and IRQ sampling. Explicitly define AXI signal naming and data-width parameters in `docs/architecture.md`.
- Drive a single time-step engine with two half-cycles per logical cycle (or equivalent correct edge ordering). Only the simulator thread calls `eval()`; transport threads enqueue requests/events into bounded thread-safe queues. Use a deterministic event scheduler and sensible backpressure patterns.
- AXI4-Lite master: support exactly one outstanding read/write initially; honor independent AXI channels, response latency, `BRESP/RRESP`, address word alignment. Crucially, a guest MMIO request does not complete until actual RTL handshakes occur.
- AXI4 DDR slave: implement reads and writes with correct ID, `LEN`, `LAST`, `WSTRB`, `RESP`, stable stalled `R`/`B` signals. Allow reproducible injected `AWREADY/WREADY/ARREADY/RVALID/BVALID` delays and non-OKAY responses. Coalesce remote memory calls per legal burst as stated earlier.
- Permit a standalone *in-process RAM backend* for very fast RTL unit tests: the exact same AXI slave transactor calls a `MemoryBackend` interface implemented by either host RAM or QEMU reverse DMA RPC. This is an implementation detail of the **testbench**, not a second accelerator implementation.
- Tracing: optional `.fst` waveform trace, ring-buffered textual AXI transactions, `--seed`, `--max-cycles`, warnings/error counts, and a clear result on fatal assertions. Default CI runs without huge traces; on failure write a bounded diagnostic trace.
- Assertions/monitors: `VALID` stability until `READY`, length and `LAST`, no burst crossing 4 KiB, correct strobes, response matching, IRQ held until W1C, no activity after idle reset, and counters consistent with bus traffic.

## 11. Ultra96 V1 hardware path (must work without JTAG)

### Board discovery / preservation gate

Before modifying hardware images, record: Vivado/Vitis/PetaLinux version and installed board support; SoC marking / exact silicon speed-grade from authoritative board docs or board files; source of current SD boot image; Linux/kernel version on board; boot log; Wi-Fi/SSH access; `/proc/device-tree/model`; active DRM connectors and USB HID enumeration; available miniDP cable/monitor; serial-console access.

**Back up the working SD card**, including BOOT.BIN, DTB, rootfs, boot partition layout, checksums and recovery instructions. Prefer a spare card. `scripts/pack-sd.sh` must not blindly `dd` an arbitrary drive or erase an unconfirmed target. No design step requires a JTAG cable.

Ultra96 V1 is a **ZU3EG-based** board with microSD as the normal boot path, LPDDR4, PS mini DisplayPort and USB host connectors. Confirm board revision, PS DDR/IO configuration and constraints from the **Ultra96 V1** sources—not Ultra96 V2, another ZCU board, or a guessed preset. If Vivado's board definition is absent, use the exact supported device part and reproduce/verify the PS settings from the board's reference design; do not blindly instantiate a default ZynqMP PS preset.

### Vivado implementation

Build a reproducible **Tcl-driven Vivado** project (versions and IP upgrades recorded) for the real Ultra96 V1:

1. Instantiate **Zynq UltraScale+ MPSoC PS** with validated board-specific DDR, MIO, boot, and clock settings.
2. Enable a PS→PL master port such as **`M_AXI_HPM0_FPD`** to access `framefx` AXI4-Lite control registers via AXI interconnect/SmartConnect and address editor. Choose/record a collision-free PL MMIO region (for example `0xA0000000` only **after** Vivado address assignment confirms it).
3. Connect `framefx` AXI4 memory master through SmartConnect to a PL→PS high-performance **`S_AXI_HP*_FPD`** or suitable port that can reach PS DDR. Prefer a **non-coherent HP path + DMA API** first; coherent HPC/CCI and Linux `dma-coherent` flags are later work and must not be assumed.
4. Supply a stable PL clock (target **100 MHz** if validated), processor/system reset, AXI connection widths/converters, and one PL-to-PS IRQ into the GIC. Extract **actual** clock, IRQ, bus address and memory map into the Linux board DT.
5. Export RTL sources and the block-design build Tcl. Synthesize, implement, generate bitstream, and save utilization, timing summary, clock constraints, DRC, and `write_hw_platform`/hardware description as appropriate for the installed tool version.
6. Ensure the stock PS essentials for SD, DDR, DisplayPort, USB, and available connectivity are retained. The accelerator itself requires no fabric IO pins. Any PL LEDs added for debugging require confirmed V1 constraints.

Define acceptance in realistic stages: **H0** stock SD image boots; **H1** modified PS+PL image boots and returns stable `MAGIC/VERSION`; **H2** interrupt and DMA copy works; **H3** all image filters match golden result including Sobel. Keep one bootable rollback path at each stage.

### Boot without JTAG

A self-contained SD path should use the compatible versions of FSBL, PMU firmware, Arm Trusted Firmware (`bl31`), U-Boot, PL bitstream (as required by the actual boot packaging), kernel, DTB and rootfs. The exact `BOOT.BIN` and `image.ub` or separate `Image`/DTB packaging depends on the chosen release; use an internally consistent toolchain/firmware combination. Prefer adapting a **known-working Ultra96 V1** reference/PetaLinux image rather than assembling mismatched firmware from unrelated boards. Document how to replace boot artifacts and roll back using the SD card.

As an **optional** iteration optimization, investigate loading a complete PL bitstream through the Linux FPGA manager/configfs or U-Boot instead of rewriting SD boot for every RTL change. Validate safety around clocks, PS–PL interfaces, running DMA and driver unbinding **before** relying on it. Do not assert this flow is already proven.

Without JTAG, use a normal Linux console over available network, the board's documented **3.3-V UART header with a suitable USB-UART adapter** if necessary, `dmesg`, hardware error/status registers, GPIO/LEDs where confirmed, and software-visible counters. Do not connect a 5-V TTL serial adapter to 3.3-V pins. Prefer a serial log for failed boots if possible.

### Hardware ABI verification

Program `MAGIC/VERSION` before involving DMA, then run `framefx-test` against the **same source** driver/app, built for the board's kernel. Verify reported `dma_addr_t` widths, actual DDR bus address translation, coherent allocation success, interrupt count, boot provenance, and absence of SMMU/translation surprises. Do not claim that QEMU DMA coherency testing alone validates a physical HP DDR path.

## 12. DisplayPort and USB keyboard — deliberately separate track

**Important:** the Ultra96 V1 mini DisplayPort output comes from the **ZynqMP processing system**, not directly from the `framefx` PL accelerator. QEMU's generic `virt` guest does not emulate that board's actual DisplayPort controller. Treat **DRM/KMS plus keyboard input** as a portable *software interface*, backed by different real/virtual display/input hardware.

### `framefx-ui` design

- Render the input image and processed image to a Linux DRM/KMS scanout buffer, expanding grayscale `uint8_t` to a supported **32-bit RGB/XRGB** mode on the CPU as needed. Use page-flips or dumb buffers via `libdrm`; select a connected connector and supported mode at runtime. A minimum viable UI is a full-screen image plus a mode/threshold legend; no Qt/Wayland/X11 required.
- Keyboard via `evdev` `/dev/input/event*` (discover by capabilities, **not** fixed index). E.g. `1` copy, `2` invert, `3` threshold, `4` Sobel, `+/-` threshold, `s` save image, `q` exit. Maintain a serial/stdin fallback for remote/headless sessions.
- Keep accelerator DMA buffers **separate** from DRM scanout buffers. After IRQ completion, CPU-copy and expand the processed image into a display buffer. **Do not assume** a DRM dumb framebuffer's physical memory is suitable as a PL DMA target on both platforms.
- QEMU: bring up virtio-gpu (`virtio-gpu-pci` if supported by chosen machine/config), a virtual display (e.g., GTK or VNC), and emulated USB keyboard/input where needed. The guest kernel needs DRM virtio-gpu and input support. Core P0 must remain runnable with `-nographic` or equivalent headless mode.
- Ultra96: inspect and enable the **ZynqMP DisplayPort DRM/KMS + DPDMA** kernel/device-tree path, EDID/HPD, PS DisplayPort clocks/PHY, CMA needs and USB host/HID. Prefer first checking output and keyboard against the **stock known-good V1 image**, then merging the framefx hardware design and driver.
- Report supported actual monitor mode and any adapter caveats. If miniDP-to-HDMI conversion or connector training fails, retain headless P0/H3 success, log the reason, and provide recorded image files while the display subproject is addressed.

This track is P1 because PS display-driver bringup can become unrelated board integration work; do not allow it to mask faults in the actual DMA engine.

## 13. Synthesizable verification / FPGA-accelerated testing (P2)

Investigate whether **the same checker architecture** can be run in simulation and synthesized to run **many more RTL clock cycles per wall-second** on Ultra96. The goal is fast test execution and useful error evidence—not replacing formal verification or claiming exhaustive correctness.

Suggested self-checking block `framefx_bist`:

1. Instantiate the real `framefx_core` behind a small **synthesizable AXI4 memory slave** backed by BRAM for small images and a synthesizable AXI4-Lite configuration sequencer (or connect to the DUT control interface through a local mux). Do not use `$readmemh` or simulation-only constructs as the only path for providing patterns; support seeds and seeds/config values written from Linux.
2. A seeded PRBS/LFSR pattern generator initializes test images and can insert programmable READY stalls. Patterns, sizes, mode, thresholds and expected image CRCs are provided by a **separate host-side golden model**—prefer independent expected CRCs rather than a copy of the RTL transform logic.
3. A synthesizable scoreboard computes destination CRC, counts byte mismatches if the expected output is available in a second reference BRAM, and records first failure: case ID, transaction/pixel index, address, expected, actual, cycle counter. A small **BRAM ring buffer** of recent bus events aids diagnosis without JTAG.
4. A minimal register wrapper exposes `BIST_CONTROL`, `BIST_SEED`, `BIST_CASE`, `BIST_STATUS`, `BIST_FAILED`, `BIST_CYCLES`, `BIST_EXPECTED_CRC`, `BIST_ACTUAL_CRC`, and trace-readout registers to Linux. BIST may use a separate CSR region or a build-time mode; **do not break the main framefx register ABI**.
5. Run the same fixed seed set in Verilator and on FPGA, comparing pass/fail and CRC. Report sim cycles/sec, FPGA cycles/sec and end-to-end including bitstream/build/host transfer overhead separately. Distinguish **throughput of test patterns** from **strength of verification**.

Consider smaller subprojects if full AXI-BRAM BIST is too expensive: synthesizable AXI-lite master exercising CSRs, AXI protocol checker with sticky error counters, or BIST for the pixel kernel only. Prefer a **working, instrumented small checker** to an unimplemented universal verification fabric. Formal/SVA checks in simulation remain complementary.

## 14. Verification matrix and fault injection

Deliver runnable tests with names, seeds, expected failure modes and logs. Recommended matrix:

| Category | Required cases | Pass condition |
|---|---|---|
| Core arithmetic | All modes, random and crafted 3×3 neighborhoods, Sobel borders, saturation, threshold equal/below/above | Exact byte match to independent reference |
| MMIO | Magic/version, read/write, W1C, reserved addresses, invalid widths, busy reconfiguration, repeated runs | Correct AXI responses/register values/IRQ state |
| DMA geometry | Widths 3, 7, 8, 9, 63, 64, 65, 127, 320, 640, 1024; non-equal strides; varying heights; aligned buffers | Full active image matches; source and padding unchanged |
| AXI signaling | AW/W independent stalls, AR/R/B stalls, random backpressure, legal 16-beat bursts, exact `LAST`, strobes, 4-KiB splits | No protocol assertion, no hang, correct bytes |
| DMA errors | Unmapped RAM access, invalid size/overflow, injected `SLVERR` read/write, transport fault mid-transfer | Error status/IRQ and nonzero test result; no fake success |
| IRQ/lifecycle | IRQ masked/unmasked, level held until W1C, 100 consecutive jobs, reset while idle, process disconnect, guest shutdown | No stuck IRQ, leaked state, unbounded wait |
| Guest integration | Linux boot, DT match, coherent allocation, interrupt `WAIT`, multi-mode application, nonzero error paths | `make demo-sim` PASS from clean build |
| Board integration | SD boot without JTAG; CSR read, copy/threshold/Sobel, interrupt, DDR result compare | On-board log and machine-readable pass report |
| Graphics/input | DRM connected connector + mode, one displayed result, USB key changes mode | Visual proof or clearly logged blocker |
| P2 BIST | Fixed seeds mirrored in sim/FPGA, injected DUT fault detected, event snapshot | Comparable CRC + meaningful error capture |

In particular **test one MMIO write whose RTL effect issues a DMA read before the write response**; this is the concurrency test most likely to uncover naive QEMU socket/lock deadlocks.

Make failure output actionable: seed, dimensions, strides, offset/beat, AXI channel, outstanding request ID, expected vs observed, bus response, last few bus transitions, and elapsed/cycle time. Keep tests deterministic by default and allow a seed sweep.

## 15. Milestones and hard gates for the engineering agent

### M0 — Inventory and ADR (start here)

- Detect OS, compilers, Verilator, QEMU, Vivado/Vitis/PetaLinux, Ultra96 resources, existing board images, tool compatibility; add `make doctor`.
- Compare custom QEMU SysBus vs Xilinx Remote-Port + SystemC in a short ADR. Default **custom generic SysBus** unless a concrete tested reuse path is clearly less work.
- Freeze versions/commits in `versions.lock`, note license obligations and unsupported combinations.
- **Exit:** reproducible host environment; risks and constraints documented.

### M1 — QEMU/RTL handshake and concurrency spike

- Boot AArch64 Linux; QEMU MMIO aperture+GIC IRQ+DT; barebones RTL AXI4-Lite target; reverse DMA to guest RAM; dual-channel protocol.
- **Exit:** device found via DT and an end-to-end MMIO + asynchronous reverse DMA + IRQ test passes **without deadlock**. Log source and destination memory bytes.

### M2 — Real Verilator/AXI fabric

- Parameterized AXI4-Lite master and AXI4 memory slave; in-process RAM backend and QEMU backend; trace and stall/fault injection; synthesizable core skeleton.
- **Exit:** bus regressions and assertions green under seeded backpressure.

### M3 — Nontrivial `framefx` RTL + standalone validation

- Implement AXI4 DMA controller, row/stride handling and four image modes **including Sobel**; preserve padding; error and IRQ counters.
- **Exit:** independent byte-for-byte RTL-only golden tests on multiple widths and 4-KiB boundaries; synthesis lint clean; clear waveforms on failure.

### M4 — Linux integration / simulator demo (P0 shipping gate)

- Common driver/UAPI; build guest kernel/rootfs; `framefx-test`; automated `make demo-sim`/JSON report.
- **Exit:** cold boot -> test -> images/checksums/IRQ/counters -> clean QEMU/Verilator shutdown all successful, reproducible from documentation.

### M5 — Ultra96 bitstream + SD Linux integration (P0 hardware gate)

- Validated V1 PS preset; Vivado Tcl implementation; board DT; firmware/SD artifact packaging; backup and rollback procedure; H0–H3 smoke tests.
- **Exit:** documented SD-only board boot and full driver/RTL demo, **if physically run**. If no board access, deliver complete artifact/instructions and mark verification blocked with exact missing action.

### M6 — Video/keyboard UI (P1)

- QEMU virtio-gpu and HID bringup; Ultra96 PS DisplayPort and USB/HID check; DRM/KMS demo app with fallback.
- **Exit:** live image and mode switching on at least the virtual target; on board if DP path confirmed. Record honest blockers.

### M7 — Synthesizable verification (P2)

- Small BIST/trace/scoreboard IP and seeded reference CRC tests; document acceleration data and limitations.
- **Exit:** reproducible pass/fail and at least one intentionally corrupted case detected; quantify speed only if measured.

**Sequencing:** M0 → M1 → M2 → M3 → M4 are the critical path. Start board **stock-image inventory and safe SD backup** early in parallel. M5 follows verified IP/driver ABI; M6 and M7 are independent stretch tracks. If delivery is constrained, prioritize **M4 + validated M5** over UI/BIST polish. Do not expand project scope to general-purpose AXI4 IP discovery or automatic PetaLinux generation until these gates pass.

## 16. Reproducibility, evidence and acceptance outputs

Create `docs/demo-script.md` with exact host/guest/hardware operator steps. A successful automated simulator demo should produce:

```text
artifacts/<run-id>/
  versions.json           # tool versions, commits, kernel, RTL hash
  qemu-console.log
  transport.log
  verilator.log
  framefx-results.json    # all cases, seed, pass/fail, checksums, DMA counters
  input.pgm
  output-copy.pgm
  output-invert.pgm
  output-threshold.pgm
  output-sobel.pgm
  wave.fst                 # optional/on failure or --trace
  junit.xml               # optional but valuable for CI
```

Hardware evidence (if board physically exercised): board `/proc/device-tree/model`, boot version, bitstream SHA256, actual CSR/version, Linux IRQ counters, DMA addresses **redacted from public logs if sensitive**, output checksums, timing/utilization and photo/screenshot if a display works. Keep simulated and board results separate; do not silently substitute output from the CPU golden model for the RTL result.

**Exact smoke transcript desired** (commands are acceptance targets for the repository to implement):

```sh
make doctor
make rtl-lint
make rtl-test
make demo-sim WIDTH=320 HEIGHT=240 SEED=42
make test
# On a host with appropriate AMD tools and Ultra96 support:
make fpga
# On a booted Ultra96 Linux shell:
framefx-test --width 320 --height 240 --mode sobel --seed 42 --output /tmp/demo
framefx-test --width 640 --height 480 --mode threshold --seed 123 --output /tmp/demo2
# If a display and USB keyboard are available:
framefx-ui --device /dev/framefx0
```

At least a 320×240 and a 640×480 image must be checked byte-for-byte in simulation. Smaller degenerate geometries and 1024-wide images are required in RTL tests; board testing may be limited by actual RAM/clock constraints but should record any deviation. Results must include all required modes and a **demonstrated PL/RTL-master DMA write into guest/board DDR**, not only MMIO.

### Reporting discipline

At the end of each milestone, write a concise `docs/progress.md`: what was implemented, exact command tested, environment, result, observed blockers, remaining risks, and links to artifacts. Never mark a milestone complete from compilation alone if its exit criterion demands runtime behavior. Keep TODOs prioritized, with honest open items.

## 17. Open questions and sensible defaults

These are questions to ask the project owner at natural review points, **not reasons to stop initial work**:

| Question | Default until answered | Importance |
|---|---|---|
| What host OS and CPU? | Linux x86_64, software TCG, headless CI first | Important for build tools; no architecture dependency |
| What versions of Vivado/Vitis/PetaLinux, and what V1 board files? | Inventory with `make doctor`, then choose a mutually compatible board/kernel/firmware set and pin it | **Critical before M5** |
| What is currently on the Ultra96 SD card? | Back up; preserve known-working boot + wireless/DP until new image confirmed | **Critical before M5** |
| Is a USB–UART adapter available, and what SSH/network access works? | SD recovery + whatever existing console works; serial adapter strongly recommended, not JTAG | Board-debug convenience |
| Which miniDP cable/monitor and USB keyboard? | Start headless; then use direct DP monitor + ordinary HID keyboard; log EDID and connector status | P1 |
| Is using a Xilinx-specific kernel/PetaLinux on hardware acceptable? | Yes for hardware boot/PS devices, provided the **driver/UAPI/RTL remain shared**; Buildroot generic Linux in QEMU | Important |
| Should the first accelerator be grayscale or RGB? | **8-bit grayscale**; convert to RGB in UI. This contains core risk while supporting a real image demo | Fixed v1 default |
| Are high simulation speed or cycle-accurate CPU timings essential? | Prioritize functional/AXI handshakes and reproducible debug, not CPU cycle equivalence | Architectural |
| Is Linux FPGA-manager reconfiguration mandatory? | No, SD `BOOT.BIN` update is the P0 fallback; Linux manager is a validated later convenience | Board iteration |
| Is a hardware BIST speedup a delivery blocker? | No, P2 after DMA + IRQ + hardware image demo | Scope |

**No unresolved question should block M0–M3.** Use ADRs for local choices. If physical access is impossible, deliver the tested simulator and buildable hardware handoff with explicit BLOCKED status rather than speculate about board validation.

## 18. Background sources to consult (and verify against pinned versions)

These links justify important implementation constraints. They are references, **not** promises of plug-and-play compatibility:

- QEMU Arm `virt` and DT rules: https://www.qemu.org/docs/master/system/arm/virt.html
- QEMU memory regions and AddressSpace: https://www.qemu.org/docs/master/devel/memory.html
- QEMU `address_space_read/write` guidance: https://www.qemu.org/docs/master/devel/loads-stores.html
- QEMU Arm `virt` source (machine/platform bus/DT integration): https://github.com/qemu/qemu/blob/master/hw/arm/virt.c
- Verilator C++ integration: https://verilator.org/guide/latest/connecting.html
- Verilator tracing/options: https://verilator.org/guide/latest/exe_verilator.html
- Xilinx `libsystemctlm-soc` reference (Remote-Port, TLM, AXI): https://github.com/Xilinx/libsystemctlm-soc
- Xilinx SystemC/QEMU co-simulation demo (includes bus mastering): https://github.com/Xilinx/systemctlm-cosim-demo
- Linux DMA coherent allocation and DMA mask: https://www.kernel.org/doc/html/latest/core-api/dma-api.html
- Linux platform-driver infrastructure: https://www.kernel.org/doc/html/latest/driver-api/infrastructure.html
- Ultra96 **V1** getting started, ports and SD boot: https://www.96boards.org/documentation/consumer/ultra96/ultra96-v1/getting-started/
- Ultra96 **V1** PetaLinux build/SD boot example (older tool versions, for historical guidance only): https://www.96boards.org/documentation/consumer/ultra96/ultra96-v1/build/peta-linux.md.html
- AMD/Xilinx ZynqMP DisplayPort DRM driver: https://xilinx-wiki.atlassian.net/wiki/spaces/A/pages/18842105/ZynqMP
- AMD/Xilinx Linux DisplayPort driver Kconfig: https://github.com/Xilinx/linux-xlnx/blob/master/drivers/gpu/drm/xlnx/Kconfig

Verify exact repository URLs, API signatures, SoC part suffixes, board files, IP parameters, interrupts and boot recipe against the versions actually selected. Do not mix instructions from the Ultra96 V2 or recent generic ZynqMP examples without checking V1 differences.

## 19. Continuing an existing checkout

The original bootstrap handoff has been retired: M0–M4 are implemented and
physical H1–H3 have evidence. Use [AGENTS.md](AGENTS.md) for repository-specific
guidance, [current status](docs/progress.md) for acceptance and next work, and
the relevant topic guide for implementation details. Consult this brief when
deciding scope or milestone acceptance; do not restart completed milestones
or assume the original illustrative commands are implemented interfaces.

---

**Success criterion in one sentence:** Linux on a generic emulated Arm machine and Linux on the real Ultra96 can both command the **same synthesizable AXI accelerator** to DMA a grayscale image, transform it—including Sobel—DMA the result into DDR, interrupt Linux, and pass an independent pixel-perfect verification, with reproducible tooling and a credible path to DRM display, USB keyboard, and FPGA-speed self-test.

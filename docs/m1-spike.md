# M1: real Linux / QEMU / RTL concurrency spike

Run from the repository root:

```sh
make test
make spike-test JOBS=8 BOOT_TIMEOUT=60
make boot-smoke JOBS=8 BOOT_TIMEOUT=60
make report
```

`spike-test` builds the patched QEMU, Buildroot guest and Verilator executable.
After building, `/usr/bin/python3 scripts/lab.py spike-test --timeout 60` repeats
only runtime tests. Rebuild after source changes. No root access, board, network
interface, GUI or installed SystemC is required. The production framefx demo
is a separate [M4 target](m4-linux.md); the spike is not an image accelerator.

## What runs

- `qemu/integrate.py` adds small version/anchor-checked hooks to the pinned QEMU
  extraction and copies the tracked bridge files into its build inputs. Repeated
  integration is idempotent. Edit `qemu/cosim-device/` and `transport/`, not the
  generated copies under `third_party/`. Other existing QEMU edits are preserved.
- `axi-cosim` allocates a 4-KiB aperture and IRQ through virt's platform bus.
  The generated child has the requested `example,axi-framefx-1.0` compatible
  and an additional `example,concurrency-spike` guard. The test driver requires
  both that guard and a distinct experimental magic/version.
- The observed MMIO base is `0x0c000000`, SPI 112 (GIC interrupt 144); Linux
  assigns its own virtual IRQ number. The driver obtains resources from OF;
  none of these numbers is hardcoded into it. The DT parent maps DMA addresses
  identically over the actual main RAM region, obtained from the QEMU machine.
  The selected 512-MiB guest maps RAM at `0x40000000..0x5fffffff`. V1 bridge DT
  integration deliberately rejects main RAM extending beyond the 32-bit bus.
- The device is `dma-coherent` **in this virtual system only**. This is not an
  Ultra96 coherency claim. Linux allocates a 16384-byte coherent buffer through
  the DMA API, logs its bus address, and checks every byte after each job.
- The Verilator process drives real AXI-Lite and 64-bit AXI master
  signals. The only data transformation is the RTL expression
  `read_data ^ 64'hd3c2b1a08796e5f4`; neither QEMU nor C++ computes the result.
  AXI address/data acceptance is independent and seeded stalls are applied.
  Assertions check that RTL VALID/payload remains stable under backpressure.
  The M2 extension shares these transactors with the standalone RAM tests and
  coalesces 1–16-beat bursts into one reverse RPC each. Both address streams
  split at 4-KiB boundaries. See [M2](m2-fabric.md).
- Deferred START deliberately withholds AXI-Lite BRESP until a reverse DMA
  read finishes. Alternate jobs return START before DMA; the simulator keeps
  clocking while Linux sleeps for its interrupt. IRQ is a persistent level,
  with a W1C latch and separate enable.

## Experimental registers

This is **not** the brief's framefx ABI. It will not be silently reused as that
ABI. M4 supplies the production driver/UAPI and M3 supplies the accelerator.

| Offset | Register | Meaning |
|---|---|---|
| `0x00` | MAGIC | `0x53504b31` |
| `0x04` | VERSION | `1` |
| `0x08` | CONTROL | Bit 0 START; bit 1 selects early response |
| `0x0c` | STATUS | Bit 0 busy; bit 1 DMA error |
| `0x10` | IRQ_STATUS | Bit 0 completion, W1C |
| `0x14` | IRQ_ENABLE | Bit 0 enable |
| `0x18`, `0x1c` | SOURCE | Low/high halves of DMA address |
| `0x20`, `0x24` | DESTINATION | Low/high halves of DMA address |
| `0x28` | BYTE_MASK | Eight AXI WSTRB bits |
| `0x2c` | TRANSFER_BEATS | Number of 64-bit words, RW; reset value 1 |
| `0x30` | ERROR_DETAIL | RO: 0 success, 1 invalid config, 2 read error, 3 write error, 4 malformed RLAST |

All accesses are aligned 32-bit. Configuration is writable only while idle;
undefined accesses, incomplete AXI-Lite write strobes and busy START return
SLVERR. Source/destination registers are not readable in this intentionally small
fixture. Transport RESET is separate from the CSR map.

The module is a **boot-lifetime test harness**, not a userspace DMA API: no
unload function, no sysfs unbind, no user-supplied addresses. It deliberately
retains the 16-KiB DMA buffer until guest destruction, including on transport
failure, so a timeout cannot turn into use-after-free DMA. Do not deploy this
module on the board or use it as the production driver's cleanup pattern.

## Checks and evidence

Each healthy Linux run now performs 40 jobs: the original 32 data/mask cases,
four expected read/write failures targeting device MMIO or unmapped space,
and four multi-burst transfers of 128, 136, 264 and 2056 bytes. Every job
checks all 16384 bytes, preserving source and guard bytes. Masks include `0x00`,
`0x55`, `0xaa`, and `0xff`; ten jobs hold completion pending with IRQ disabled
and then enable the already-pending level. Exactly 40 interrupts must be handled
and acknowledged. Source and destination bytes are printed for every case.

The suite repeats this at seeds 1, 42 and 2026, with unrestricted, 7-byte and
1-byte socket writes: **120 Linux jobs and 120 handled IRQs**. Each seed must
produce exactly 61 read and 59 write reverse RPCs, with maximum bursts of 16
beats in both directions; multi-beat transfers cannot silently regress to
per-beat RPCs. The scoreboard
requires a successful reverse read before the first START reply, DMA outside
MMIO, and distinct vCPU/main-loop thread IDs with asserted BQL ownership.
Guest CPU time and RTL cycles are not synchronized. Seeded backpressure does
not make whole-system cycle counts independent of host scheduling.

Four additional boots inject reverse disconnect, no response, an oversized
frame, and a truncated frame. A PASS here means **expected guest failure with
bounded shutdown**, not successful acceleration. The scoreboard requires the
bridge fault and guest FAIL markers, rejects PASS/panic and rejects DMA after
the fault. Each failed session gets fresh sockets and a fresh guest.

The separate `bridge-safety` probe uses a synthetic Python peer, a paused CPU
and no RTL/Linux. It checks reverse reads/writes, masked-byte preservation,
independent QMP physical-memory visibility, alignment, count bounds, overflow,
MMIO/flash/unmapped rejection and whole-range prevalidation at a RAM boundary.
It also checks QMP system reset and rejection of migration. This probe is not
counted as accelerator or RTL validation.

`artifacts/latest-spike.json` points to the complete run. Its directory contains:

- `versions.json`: lock and project/binary hashes; configs are saved alongside it.
- `dt/`: generated DTB/DTS and decoding log (no DMA-cell mismatch warnings).
- `seed-*/`: exact launch commands, QEMU/guest console, RTL log and case result.
- `disconnect/`, `stall/`, `malformed/`, `partial/`: negative-test evidence.
- `bridge-safety/`: independent bridge probe, QMP transcript and eight-byte RAM dump.
- `result.json`: suite result, per-case scope/status and host elapsed times.

Initial failed bring-up runs are retained, not overwritten. They exposed a
stale fd notification after the MMIO pump consumed an IRQ frame, and a reverse
EOF race during GOODBYE. Both root causes are fixed and checked by repeated runs.
The synthetic probe initially used RAM occupied by the generated DT; it now
uses a separate address in the paused guest, so a system reset may reload the
DT without invalidating its memory-preservation test.

## Deliberate stopping point

M1 retires the demonstrated deadlock risk. It does not establish throughput,
full AXI4 compliance, arbitrary outstanding transactions, socket reset-abort recovery,
physical coherency or hardware correctness. Long/partial socket transactions
can hold the BQL until their finite deadline. Use one TCG vCPU and restart both
processes after a terminal fault. Snapshot/migration is explicitly unsupported.

M2 now exercises local RTL/transactor reset across partially accepted channels,
pending responses and IRQ. This is not a claim that QEMU can interrupt a blocked
reverse RPC. Framefx/Sobel is covered separately in [M3](m3-framefx.md).

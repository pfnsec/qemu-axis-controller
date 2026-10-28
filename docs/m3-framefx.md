# M3: framefx RTL and standalone image validation

This guide covers the image accelerator and standalone verification, not a
host-side emulation of its registers or filters. The production driver/Linux
demo is documented in [M4](m4-linux.md); routed V1 integration and physical
evidence are documented in the [hardware guide](bringup-ultra96.md).

## Run

```sh
make test
make rtl-lint
make framefx-test JOBS=8
make rtl-test JOBS=8       # complete M2 + M3 standalone suite
make spike-test JOBS=8 BOOT_TIMEOUT=60
make framefx-synth        # optional: installed Vivado/license/device files
make report
```

`framefx-test` builds Verilator binaries at maximum burst lengths 1, 4 and 16,
plus an arithmetic-only pixel model. No guest, SystemC or FPGA tools are needed
for this target. Each execution is bounded by 60 seconds and each image by
20 million simulated cycles. Bus operations retain their shorter cycle bounds.
The report marks M3 PASS only with matching current project/binary hashes for
M1, M2 and M3. A checked-in CI workflow is not evidence of a remote CI run.

## Architecture

- `rtl/core/framefx_core.sv`: AXI-Lite, configuration validation/latching,
  sticky status, counters and interrupt control. Top-level interfaces match
  the brief: 32-bit AXI-Lite, 64-bit AXI address/data, synchronous active-low reset.
- `framefx_dma.sv`: aligned full-width INCR read/write engine. Splits at row
  ends, 4-KiB boundaries and `MAX_BEATS` (1–16), with independent AW/W handshakes
  and exact LAST/strobes. It drains an errored read burst before stopping.
- `framefx_pipeline.sv`: three rotating 1024-byte input row buffers, a 1024-byte
  output row buffer, a sliding 3×3 window and byte packing. Each input row is
  fetched **once**, including for Sobel; there are no per-neighbor DDR reads or
  full-frame on-chip buffers. Read/transform/write stages are serialized for
  a deliberately simple first implementation, not overlapped for throughput.
- `framefx_pixel.sv`: copy, invert, inclusive threshold and signed Sobel.
  Sobel uses `min(255, (abs(gx)+abs(gy))/4)` with integer truncation; every
  border pixel is exactly zero. Padding never enters the active window.

`sim/axi/model.hpp` supplies the same clocking, AXI models, assertions and reset
behavior used by M2. `sim/framefx_test.cpp` drives actual CSR pins and verifies
the image, counters and lifecycle. `scripts/framefx.py` independently generates
images, expected RAM contents and row/burst address sequences. It does not
reuse RTL-generated logic. An additional C++ pixel reference uses convolution
coefficient tables, independently of the Python neighborhood sums.

## ABI 1.0

`MAGIC` is numeric `0x46524658`, `VERSION=0x00010000`, `CAPS=0x0000010f`.
Addresses are little-endian, aligned 32-bit words in a 4-KiB aperture. All
writes require `WSTRB=0xf`; partial writes, reserved offsets, reads of CONTROL
and writes to read-only registers return SLVERR without changing configuration.

| Offset | Register | Semantics |
|---|---|---|
| `0x000`, `0x004`, `0x008` | MAGIC, VERSION, CAPS | Read-only constants |
| `0x00c` | CONTROL | START bit 0; idle-only SOFT_RESET bit 1; other bits ignored; both low bits set is SLVERR |
| `0x010` | STATUS | BUSY bit 0, sticky DONE bit 1, sticky ERROR bit 2 |
| `0x014`, `0x018` | SRC_LO/HI | Source DMA address, RW while idle |
| `0x01c`, `0x020` | DST_LO/HI | Destination DMA address, RW while idle |
| `0x024`, `0x028` | WIDTH, HEIGHT | 1–1024, Sobel requires both ≥3 |
| `0x02c`, `0x030` | SRC_STRIDE, DST_STRIDE | Independent byte pitches, multiples of 64, at least width rounded to 64 |
| `0x034`, `0x038` | MODE, THRESHOLD | Modes 0–3; threshold retains low eight bits |
| `0x03c`, `0x040` | IRQ_STATUS, IRQ_ENABLE | Completion bit 0, error bit 1; status W1C, enable RW |
| `0x044` | ERROR_CODE | 0 none, 1 configuration, 2 read, 3 write, 4 busy START, 5 protocol response |
| `0x048`, `0x04c` | PERF_CYCLES_LO/HI | 64-bit operation cycles; low read snapshots high |
| `0x050`, `0x054` | BYTES_READ_LO, BYTES_WRITTEN_LO | Read bus bytes requested; strobed write bytes acknowledged OKAY; modulo 2^32 |

Source and destination must be 64-byte aligned. Validation conservatively
compares the full `stride * height` buffer extents, including final-row padding,
for overlap and address overflow. No DMA starts on invalid geometry. Only active
pixels are written; reads may include the last beat's padding, within the stride.

An idle START returns OKAY and either begins the latched configuration or
immediately terminates with DONE+ERROR/code 1/error IRQ for invalid parameters.
Both paths clear previous pending status and counters first. A valid subsequent
START clears DONE/ERROR and pending IRQ without requiring a reset. Configuration
writes while busy return SLVERR. Busy START also returns SLVERR, latches error 4
and error IRQ, **but does not abort the image**. A later DMA fault takes error-code
precedence. Successful completion after busy START adds completion pending while
retaining the busy-START error, so both IRQ status bits can be set.

DONE is set for both successful and failed terminal jobs. Normal success sets
completion pending; a DMA/configuration/protocol failure sets error pending.
Completion only becomes visible after the final acknowledged write response.
IRQ is a level from enabled pending bits, held until W1C or reset/new START;
masking does not discard pending status. A newly arriving event wins over a
simultaneous W1C. Idle SOFT_RESET clears status, errors, pending IRQ and counters,
but retains configuration and IRQ_ENABLE. External reset clears everything and
flushes the local fabric; it cannot undo already committed RAM writes.

Counters are live while BUSY and stable after completion. CSR reads sample state
at AR acceptance. Read PERF_CYCLES_LO then HI: HI returns the high half captured
at the low read, not a fresh value. Each operation starts counters at zero.
Tests check live/stopped counts and resets; a 2^32-cycle rollover stress run has
not been performed. Read-byte counts include errored bursts that were requested.
Write-byte counts include only successful B responses; an external bus error
may have partially committed memory, which no counter can reconstruct or undo.

Memory failures drain/finish the current burst, stop further requests and can
be followed by a new job. The malformed-LAST fixture still supplies ARLEN+1
beats; missing beats or a permanently wedged bus require the simulator timeout
and reset/session restart. No unbounded hardware-side recovery is claimed.

## Verification and artifacts

The main matrix contains 211 cases at seeds 1, 42 and 2026 (633 executions),
plus 44 selected cases each at burst limits 1 and 4: **721 cases** and one traced
replay. Each case includes illegal CSR tests and initial invalid START/cleanup.
The suite covers:

- All four filters at widths 1/2/3/7/8/9/63/64/65/127/320/640/1024 where legal;
  heights 1/3/9, plus 320×240, 640×480 and 1024×1024 for every filter.
- Coordinate patterns, ramps, checkerboards, impulses, threshold equality and
  endpoints, saturation corners and constant fields; non-equal strides,
  nonzero padding/guards and addresses above 4 GiB.
- Independent exact full-RAM comparisons and AR/AW sequence/counter checks,
  including requested read padding and partial last-beat WSTRB preservation.
- First/second/later read/write failures, unmapped addresses, malformed RLAST,
  invalid geometry, alignment, overlap and overflow. Mapped DMA faults are
  followed by a successful image **without reset**, checking error/counter cleanup.
- Busy configuration rejection, busy START, idle soft reset, external reset with
  pending AR, masked/held IRQ, independent W1C bits and 100 consecutive jobs that
  rotate through all four modes. Each repeated job is independently compared.
- Exact traced replay of one Sobel case, including cycles and complete recorded
  burst history; automatic diagnostic replay for failures, never converting a
  failed primary run into PASS.

The arithmetic model checks **76,304** vectors: every pixel/threshold pair,
copy/invert values, all 256 binary-valued Sobel neighborhoods and 10,000 seeded
neighborhoods. Python unit tests independently check signed gradients, scaling,
saturation and borders, and deliberately corrupt image/counter/address evidence.

`artifacts/latest-framefx.json` points to the run. Each `seed-N/case-ID/` retains
case/configuration metadata, exact command, console, bus events, input/expected/
actual full-RAM snapshots and JSON results. Successful images also have
`input.pgm`, `output.pgm` and SHA256 hashes. Error-recovery cases include a separate
recovery golden. `trace/wave.vcd` contains the checked replay. In this matrix,
`case-149` is 640×480 Sobel and `case-150` is 1024×1024 Sobel.

Replay an existing case with its recorded simulator seed (the seed in
`command.json`, not necessarily the matrix seed), preferably after copying its
case directory so original evidence remains untouched:

```sh
artifacts/build/framefx-16/framefx-test /absolute/path/to/copied-case SIMULATOR_SEED trace
```

The direct binary checks stored golden files. `make framefx-test` additionally
regenerates and independently scores those files and the bus/counter evidence.

## Synthesis is not board validation

Vivado 2026.1 out-of-context synthesis for `xczu3eg-sbva484-1-e` passes:
5,666 LUTs, 9,611 registers, eight DSPs, no latches and zero block RAM tiles.
This first implementation maps the row storage into distributed RAM/registers;
it does **not** claim BRAM inference or area optimization. See the
[AMD synthesis memory-inference documentation](https://docs.amd.com/r/en-US/ug901-vivado-synthesis/Memory-Inference-Capabilities)
before changing storage templates for a physical target.

Reports/checkpoint are in `artifacts/build/framefx-synth/`; the log and source
hashes are in `artifacts/logs/framefx-synth.log` and `artifacts/framefx-synth.json`.
Warnings cover removed unused sequential elements, hierarchy/floorplanning and
the unspecified OOC clock location. The 10-ns pre-placement timing estimate has
positive slack, **not routed timing closure**. No PS preset, SmartConnect, DDR,
interrupt wiring, firmware, bitstream or board execution is validated by this
step. The independently established Linux and physical paths have their own
evidence; see [current status](progress.md) rather than treating this OOC result
as a board acceptance result.

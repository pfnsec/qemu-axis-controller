# Foundation through M4 demonstrations

Run from the repository root:

```sh
make doctor
make test
make toolcheck
make rtl-test JOBS=8
make fetch fetch-refs
make boot-smoke JOBS=8 BOOT_TIMEOUT=180
make spike-test JOBS=8 BOOT_TIMEOUT=60
make demo-sim JOBS=8 BOOT_TIMEOUT=180
make fpga-inventory
make fpga-toolcheck
make framefx-synth
make report
```

The Vivado steps are optional and require installed proprietary tools. All other
steps run headless. `make boot-smoke` is deliberately named differently from
`make demo-sim`: it demonstrates the stock guest only.

The smoke guest automatically runs `/etc/init.d/S99boot-smoke`, validates its
AArch64 architecture, generic virt compatible string, DT sysfs and PL011 console,
prints kernel/model/interrupt information and powers off. It does not access
framefx registers, allocate accelerator buffers or verify AXI DMA.

`artifacts/latest-boot.json` points to the most recent run. In that directory:

| File | Evidence |
|---|---|
| `command.json`, `command.sh` | Exact QEMU launch arguments |
| `qemu-console.log` | Kernel boot, guest checks and shutdown |
| `virt.dtb`, `virt.dts` | QEMU-generated device tree |
| `buildroot.config`, `linux.config` | Resolved build configurations |
| `versions.json` | Dependency lock, project hashes and binary/image hashes |
| `result.json` | PASS/FAIL, elapsed host time and scope |

To repeat only the runtime check after building:

```sh
/usr/bin/python3 scripts/lab.py boot-smoke --timeout 180
```

For an interactive serial console, run `make guest-shell`, log in as `root`
with no password, and use `poweroff` to exit. This omits `axi_lab.smoke=1` from
the kernel command line, so the same image waits for login instead of running
the automatic smoke check. The initramfs is ephemeral. Do not expose its
development root account over a network.

Failure is nonzero and retains its console/result evidence. Timeouts terminate
the QEMU process group. Success requires both a complete marker line from the
guest and an orderly shutdown, not simply a QEMU process starting successfully.

`make spike-test` runs the real Linux/RTL concurrency milestone, separately from
the stock-machine smoke check. See [the M1 guide](m1-spike.md) for job counts,
negative-test interpretation, register map, limits and evidence layout. The
latest result is `artifacts/latest-spike.json`. This remains an experimental
transport test, separate from the production Linux image driver.

`make rtl-test` runs the shared AXI fabric against local RAM, including burst
boundaries, masks, response faults, partial-channel reset and 100 consecutive
jobs. It records independent full-RAM scoreboards, exact seeded replay and VCD
evidence in `artifacts/latest-rtl.json`. See [the M2 guide](m2-fabric.md) for replay
commands. `make report` marks M2 PASS only when both standalone and guest suites
pass against the current project and binary hashes.

It now also runs M3: real framefx image RTL, standalone RAM, independent Python
golden images, arithmetic tests, DMA faults and recovery. Use `make framefx-test`
to run only M3. `artifacts/latest-framefx.json` points to PGM images, checksums,
counter results and waveforms. `make framefx-synth` is optional Vivado
out-of-context synthesis, not a board image. See [M3](m3-framefx.md).

`make demo-sim` runs the production Linux/RTL path and independently checks
exported images and full coherent allocations. `artifacts/latest-demo.json`
points to healthy seeded boots, recoverable read/write faults, four terminal
transport faults, lifecycle/IRQ evidence, exact commands and hashes. To rerun
without rebuilding: `/usr/bin/python3 scripts/lab.py demo-sim --timeout 180`.
See [M4](m4-linux.md) for UAPI, CLI and fail-closed lifetime semantics. The
[hardware runbook](bringup-ultra96.md) covers the separate V1 bitstream/load
workflow. This simulator demonstration never programs the board.

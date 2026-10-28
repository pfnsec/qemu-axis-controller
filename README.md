# axi-cosim-lab

A functional soft-IP verification platform: AArch64 Linux → QEMU MMIO proxy →
real Verilator AXI RTL → guest RAM DMA → interrupt-driven completion. The same
framefx core, register ABI, UAPI and driver source also run on an **Ultra96 V1**.
Copy, invert, threshold and Sobel are implemented in RTL, not by the bridge.

**Start with [current status and next work](docs/progress.md).** M1–M4 pass;
149 physical DMA/filter jobs and interrupts have passed on a volatile V1
configuration. M5 is not complete: repeatable SD deployment remains work.
Visible DisplayPort is unverified; the existing H0 GUI is CPU-reference only.

## Develop locally

Linux x86_64, Python 3.12+ and the dependencies in [setup](docs/setup.md) are
required. From the repository root:

```sh
make doctor
make test
make rtl-test JOBS=4
make demo-sim JOBS=4 BOOT_TIMEOUT=180
make report
```

The first integrated build downloads and compiles pinned QEMU, Buildroot and
Linux inputs; it is not a prebuilt guest download. Use `JOBS=N`, not top-level
`make -j`: lab commands share a locked build tree. Simulator targets are
headless, use disposable guests and do not access the physical board.

`make help` lists targets. See [verification](docs/verification.md) for selecting
unit, standalone RTL, Linux concurrency, stress and ARM32 compatibility gates;
see [demo commands](docs/demo-script.md) for an end-to-end walkthrough.
`make report` checks saved evidence/freshness; it does not run tests.

## Hardware is a separate workflow

`make fpga` builds the V1 routed bitstream/overlays; it never programs a board.
`make board-image` and `make board-mikro-image` assemble local images; neither
writes an SD card. The [mikro candidate](docs/mikro-ultra96.md) is 259 MiB versus
the 9.86-GB vendor-derived image, but has not been SD-boot-tested.
Its optional PL profile packages the guarded loader and exact-kernel artifacts
without automatic programming; the default minimal profile stays Python-free.

Use the [hardware runbook](docs/bringup-ultra96.md) before board access, loading
or media writes. Do not assume `/dev/sda` is an SD card: it has also been the
board's USB storage gadget. Hardware DMA has no fault-containment guarantee,
and successful simulation/builds are not physical acceptance.

## Find the contract and evidence

- [Documentation map](docs/README.md): task-specific guides and source locations.
- [Platform contract](docs/architecture.md): timing, AXI subset, ownership,
  security limits and how to add another IP.
- [Original brief](brief.md): scope and milestone acceptance, not current state.
- `artifacts/report.json`: gate results and freshness; `artifacts/latest-*.json`
  index individual runs. These ignored local files may be absent in a clone.
- `artifacts/build/` and `third_party/`: generated builds and pinned dependencies;
  edit first-party sources rather than their generated copies.

Review [NOTICE](NOTICE) before distributing dependencies or generated images.

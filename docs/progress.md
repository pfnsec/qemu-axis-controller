# Current status and next work

Reviewed **2026-10-22** (PL evidence uses UTC 2026-10-23): mikro runs the custom
PL. After the initial 149-job all-filter gate, **1,124 additional physical DMA
jobs/interrupts** and **504,282 concurrent capability calls** passed. Median
control calls were 1.60 µs; full-size jobs were about 19.93 ms / 50.2 MiB/s
payload. These are active-job measurements, not application/DDR peak rates.
See [measurement definitions and results](pl-performance.md). PL is left
operating; a later idle CSR/IRQ/refcount/kernel-health check passed.
Earlier flash/readback,
SD/ext4 boot, pinned USB SSH, 8-MiB transfer and 78 CPU-reference cases passed.
DisplayPort and Wi-Fi association remain unverified. Power-cycle/load
repeatability is still pending; there is no PL autoload or hot reload.

One pre-DMA gate caught mdev changing the driver's 0600 device mode to 0660.
No DMA had started and GIC 121 remained at zero. The failure was preserved;
an exact `framefx0 root:root 600` rule and targeted chmod repaired it without
reloading PL. A targeted mdev add event later preserved 0600. The fix is live
and in source. Latest verification passed **149 host tests**, the full M4
Linux regression and 96 new benchmark jobs in one-/four-vCPU QEMU/RTL guests.
M4 and benchmark simulation are source-current; cached mikro images and broad
M1–M3/stress evidence are not. Rebuild before another flash;
do not confuse the unchanged flashed recipe with its recorded live mdev patch.
Before flashing, the old card was mounted read-only (`ro,noload`) and diagnostics
preserved. Its recipe-matching V1/vendor-kernel boot report establishes that the
earlier minimal image reached Linux userspace, though USB access never worked.

The audit found a concrete software bug: USB function links used ordinary
link-relative paths, but configfs resolves targets from the caller's working
directory. This aborts normal startup before binding. The new iteration uses
absolute paths, adds bounded controller/bind retries, preserves the vendor's
initial rebind sequence, and records early service/kernel diagnostics. The old
failure is reproduced in a configfs-semantics fixture. The corrected image now
enumerates ECM/ACM and passes physical USB checks; its startup log records both
binds succeeding and every init service returning zero. See
[startup diagnostics](mikro-ultra96.md#usb-startup-and-early-boot-diagnostics).

## Acceptance state

| Gate | Established | Still missing |
| --- | --- | --- |
| M0–M4 | Tooling, real RTL/AXI, Linux DMA/IRQ, independent images and failure handling | No open critical-path simulator implementation gate |
| Additional hardening | Seeded stress, debug-kernel SMP and runtime ARM32 passed previously | Their latest saved runs are **not source-current**; rerun before claiming current coverage |
| V1 PL build | Routed 100-MHz design, exact part/DT/clock/reset/IRQ checks, 211-case wrapper validation | Build success alone is not hardware acceptance |
| H0 | Full-image SD readback, V1 Linux/USB, CPU GUI and reboot/autostart observed | Operator-visible DisplayPort; H0 is therefore partial |
| H1 | Guarded volatile load and CSR checks passed on mikro without the PYNQ UIO handoff | Repeatable fresh-boot deployment |
| H2/H3 | **149 real DMA jobs/interrupts on mikro**, all filters through 1024×1024, independent source/destination/guard checks and release/reopen checks | Exact CMA allocation accounting, hardware fault containment/recovery |
| PS/PL measurements | **1,124 further jobs**, latency distributions, full-buffer native/host checks and >500,000 concurrent capability queries; no PL/driver replacement | Cached-buffer producer/consumer throughput, continuous-duty stress and raw bus saturation are not measured |
| M5 | Headless mikro SD/USB boot, guarded PL load and all-filter DMA/IRQ gates passed | Power-cycle/reload acceptance and physical display integration; not complete |
| M6/M7 | CPU-reference display exists for H0 only | Accelerated display/keyboard UI and synthesized BIST are deferred |
| Mikro image | USB-fixed 259-MiB PL profile flashed/readback and physical SD/USB/PL gates PASS; device permissions patched live | Rebuild with mdev fix, then power-cycle repeatability; Wi-Fi/display unverified |

No claim of malicious-IP isolation, safe live reload, exhaustive AXI compliance
or cycle-synchronized CPU/RTL timing. See [the platform contract](architecture.md).

## Next work

1. PL is already operating in the last observed boot: **do not run the loader
   again on it**. Reidentify USB/boot/health before additional supervised tests.
   For repeatability, arrange a deliberate power cycle, then fresh preflight,
   one load and DMA validation; do not infer reset/reload safety from this pass.
2. For subsequent boots, run `scripts/mikro_usb.py` against the **flashed** dated
   manifest, not whichever profile was built last. The Dropbear host key was
   compared with the identified ACM console and pinned in
   `artifacts/hardware/keys/ultra96-mikro-known_hosts`. Use
   `root@192.168.3.1`, key `artifacts/hardware/keys/ultra96-mikro`; see the
   [USB checker](mikro-ultra96.md#verify-a-boot-over-usb). It needs no target
   Python/compiler and does not touch PL, framebuffer or raw media.
3. Rebuild the PL-profile image and run its QEMU smoke before any future flash
   so mdev preserves root-only device permissions. Reidentify/reapprove media
   for that separate action. Generic UIO remains excluded; live IRQ ownership
   checks are still mandatory on every fresh boot.
4. Use the [benchmark](pl-performance.md) to measure bulk PS readback into
   cached buffers and a realistic producer/consumer loop. Direct byte-wise
   verification of full-size DMA mappings took ~0.52 seconds, outside the
   ~20-ms job interval. Do not mistake this test for sustained video throughput.
   After that, investigate row/DMA overlap in simulation before a fresh PL build.
5. Re-run stress/compatibility gates when changing those paths or claiming
   current broad platform validation. Exact physical CMA accounting and a
   reset/isolation design are separate hardening work, not prerequisites for
   every safe host-side change. M6/M7 follow the critical hardware deployment.

DisplayPort/keyboard acceptance remains deferred, not a blocker for headless M5.

## Evidence to resume from

Run `make report` to refresh source/artifact freshness without contacting the
board. The latest host and core regression commands were:

```sh
make test JOBS=4
make benchmark-sim demo-sim JOBS=4 BOOT_TIMEOUT=180
make report
```

The 2026-10-22 pass completed **149 host tests**, M4 and benchmark simulation.
Logs: `artifacts/logs/pl-benchmark-host-tests.log`,
`artifacts/logs/pl-benchmark-demo-regression.log`,
`artifacts/logs/benchmark-sim-current.log`. Latest M4:
`artifacts/demo-20261023T004044-470860Z/`; benchmark simulation:
`artifacts/benchmark-sim-20261023T004357-628961Z/`. ASan/UBSan also passed a
maximum-size concurrent native fixture in
`artifacts/build/framefx-bench-sanitizer/case.ubPOkH/` (not physical hardware).
The initial new benchmark's direct DMA-VMA export failed under 9p; ordinary-RAM
staging fixed it before any hardware run. Preserved failure:
`artifacts/benchmark-sim-20261023T003634-069761Z/`.

On 2026-10-17 earlier gates passed **143 host tests** and then-source-current M1–M4.
Logs: `artifacts/logs/tests.log` and
`artifacts/logs/mikro-usb-hardening-regression.log`; M4 run:
`artifacts/demo-20261017T222623-580452Z/`. All **11 startup fixture tests** also
passed under the AArch64 BusyBox shell (mocked hardware, not physical USB):
`artifacts/logs/mikro-startup-aarch64-shell.log`. The pre-fix fixture failures are
preserved in `artifacts/logs/mikro-configfs-bug-reproduction.log`.

Both USB-fixed profiles built and passed QEMU userspace smoke, with installed
startup/diagnostic scripts checked against source. Minimal candidate:
`artifacts/hardware/mikro-20261017T222137-649680Z/manifest.json`; PL candidate:
`artifacts/hardware/mikro-20261017T222300-939456Z/manifest.json`. Build/smoke logs:
`artifacts/logs/mikro-usb-fixed-minimal.log` and
`artifacts/logs/mikro-usb-fixed-pl.log`. PL smoke validates target Python and
bundle hashes without programming anything. The latest image index is the
**PL profile flashed on 2026-10-22**, still 259 MiB; minimal remains Python/PL-free.
Extended stress, hardening and the full vendor-derived image retain older
evidence; the 2026-10-22 mdev source fix also makes these cached image/core
records stale. The preceding host suite passed **144 tests**; its command log is
`artifacts/logs/mikro-pl-device-rule-tests.log`.

Initial mikro physical PL evidence: `artifacts/hardware/mikro-pl-20261023T001718Z/`.
`load-report.stdout` records H1; `session.json` contains all nine profiles and
host checks; `post-health.stdout` records idle CSR/IRQ/refcount/kernel health.
`blocked-before-dma.json` and `copy-16.stderr` preserve the permissions gate;
`permission-repair.stdout` records the exact repair. These scripts are bound to
this boot/workspace, not reusable authorization. Full-size jobs took about
19.9 ms each at the existing 100-MHz PL clock, including native ioctl timing;
this is not end-to-end application throughput or a CPU speedup benchmark.

Current physical performance evidence:
`artifacts/hardware/pl-benchmark-20261023T003037Z/session.json` records 25
profiles, 1,124 jobs, exact IRQ accounting, independent checks, native/compiler
and payload provenance. `final-idle-health.stdout` records the later check.
`artifacts/logs/pl-benchmark-physical.log` summarizes each profile. Source,
destination, guards and per-job counters passed; exact CMA balance remains
unmeasured. Scripts in this evidence directory are bound to this boot/session.

| Evidence index | Purpose |
| --- | --- |
| `artifacts/report.json` | Gate results and separate `inputs_current` flags |
| `artifacts/ultra96-pl-bitstream.json` | Routed PL build and payload hashes |
| `artifacts/latest-pl-attempt.json` | Current mikro H1 observation; original PYNQ failure/recovery remains in its dated evidence |
| `artifacts/latest-pl-dma.json`, `artifacts/latest-mikro-pl-session.json` | Current mikro H2/H3 profiles, exports, full-buffer verification, health and payload hashes |
| `artifacts/latest-pl-benchmark.json`, `artifacts/latest-benchmark-sim.json` | Physical performance observations and the separate source-current simulator correctness gate |
| `artifacts/hardware/pl-20261016/native-build.json` | Native 5.15 module/application provenance; validate before reusing |
| `artifacts/latest-usb-check.json` | Independent CPU/userspace/USB checks, not display or PL acceptance |
| `artifacts/latest-board-image.json` | Full vendor-derived image; predates the packer's home-ownership fix |
| `artifacts/latest-mikro-image.json`, `artifacts/latest-mikro-smoke.json` | Mikro candidates and userspace scope, not board boot acceptance |
| `artifacts/latest-mikro-flash.json` | Current flashed PL candidate and full readback evidence; boot tracked separately |
| `artifacts/hardware/mikro-flash-20261022T221204Z/` | Fresh H1n identity at `/dev/sda`, old-card diagnostics, candidate snapshot and successful write/readback record |
| `artifacts/hardware/mikro-20261017T193931-626119Z/manifest.json` | Exact minimal image previously flashed; retain this identity separately from newer builds |
| `artifacts/latest-mikro-usb.json` | Physical SD/USB userspace PASS, `artifacts/hardware/mikro-usb-8ug4sape/`; startup logs in its `startup/` directory |
| `artifacts/hardware/mikro-access-20261017.json` | Host-only missing-USB inventory; not board boot evidence |
| `artifacts/hardware/mikro-20261017T193931-626119Z/sd-write.json` | H1n card write/readback PASS: 271,581,184 bytes, 154.687 seconds; boot NOT_TESTED |
| `artifacts/latest-stress.json`, `artifacts/latest-hardening.json` | Earlier extended gates; freshness is checked separately |

Artifacts are ignored local files and may be absent in another checkout. A
missing artifact is missing evidence, not a hardware failure or permission to
reconstruct a PASS. Preserve original failures when recording a later recovery.

Checkout note at this review: Git has no commits and first-party work is
untracked. An empty `git diff` therefore does not describe the existing work
or mean those files are disposable.

## Last observed board state — not a live inventory

The recovered old-card report confirms the earlier minimal mikro image booted
the V1 vendor kernel `5.15.19-xilinx-v2022.1` into userspace. Its clock was unset;
the retrieval date does not establish the boot date. The card now contains the
newer PL profile, verified live over USB on 2026-10-22 with boot ID
`1de49891-918d-42bb-9492-935f8aa7dbaa`. USB interface then was
`enp197s0f3u1u2`, host address `192.168.3.11`, board `192.168.3.1`; these are
observations, not future connection assumptions. The H1n reader was identified
as `/dev/sda` on 2026-10-22, not a reusable future device selection.
Wi-Fi association and physical DP remain unverified. On this mikro boot the
remote frozen PL workspace is `/var/tmp/axi-pl-ox60rpgh`; GIC 121 belongs to
framefx (Linux IRQ 53), with **1,273 cumulative completions** and zero module references after
testing. The driver is bound and cached manager state is `operating`.
Historical results below belong to the earlier full PYNQ baseline.

After the reviewed release of the unused `a0000000.fabric` UIO binding, framefx
owned `0x80000000`/4 KiB and GIC 121 (Linux IRQ 53 **in that boot**).
`/dev/framefx0` was root-only 0600; PL0 was nominal 100 MHz. All 149 jobs passed
native C and host Python image/full-allocation checks, exact IRQ accounting and
close/unmap/reopen checks. A delayed 64-query CSR check passed with zero retained
module references, unchanged boot files and no new fatal kernel/DMA diagnostics.

Exact CMA balance was **not measured**: `CmaFree` fluctuated even without DMA,
and allocation debug accounting was unavailable. No leak-free or hardware
fault-recovery claim follows from zero module references.

The previous PYNQ USB login used `artifacts/hardware/keys/ultra96-h0` and the
`ultra96-h0-known_hosts` file for `xilinx@192.168.3.1`. Verify device identity,
direct USB route, host key, boot ID and manager state again before hardware work.
Recover boot-specific payload/workspace paths from the evidence rather than
copying a cached command. Mikro uses the separate client key above, preserving
the former minimal-image key's public identity; its board host key is generated
on first boot. See [the hardware runbook](bringup-ultra96.md).

## History

[2026-10-15–16 development history](history/2026-10-15-16.md) retains the original
milestone chronology, failed attempts and detailed measurements. Read it when
investigating a past decision, not as the default startup checklist.

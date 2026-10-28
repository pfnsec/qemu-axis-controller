# Ultra96 V1 hardware runbook

Use this page for board access, media preparation and PL loading. For the
**last observed** state, current blockers and evidence indexes, see
[current status](progress.md). Historical success does not establish the
current boot, connectivity or loaded payload.

## Safety and recovery boundaries

- Preserve the pinned vendor boot firmware/kernel/base DT and a known-good
  rollback image. The mikro candidate is separate and has not passed SD boot.
- Identify the actual board, USB route and media before acting. The board's
  small USB **storage gadget is not its removable boot card**. `/dev/sda` has
  referred to both at different times; old erase approvals are not reusable.
- Never read `/sys/class/fpga_manager/fpga0/status` or glob-read manager files.
  Use cached `state`; see [the vendor readback hazard](#vendor-status-read-hazard).
- No hot PL replacement, live overlay removal, forced module unload or blind
  retry after an AXI/DMA failure. A userspace timeout cannot stop a wedged bus.
  Preserve logs/state; arrange operator recovery with the unchanged vendor
  baseline. Warm-reboot PL persistence is unverified; a full power cycle may
  be needed to reach the loader's unconfigured fresh-boot state.
- HP0 DMA is non-coherent, SMMU is disabled and faulty RTL can corrupt Linux
  DDR. Driver bounds are not hardware isolation. Keep trusted USB access and
  physical power recovery available for development loads/tests.
- Vendor passwords/services remain enabled on the full image. Use an isolated,
  trusted link; do not expose it to shared/public networking without securing it.

## Connect and boot

1. With power off, insert the verified microSD. On V1 SW2, SD boot is switch 1
   OFF, switch 2 ON; check the printed table and the
   [V1 hardware guide, Figure 5](https://hacksterio.s3.amazonaws.com/uploads/attachments/1229096/ultra96-hw-user-guide-rev-1-0-v1_1_RUw9pNGpYM.pdf).
2. For display testing, connect miniDP-to-DP to a powered monitor before boot,
   or use a supported **active** HDMI adapter, not a passive cable. See
   [Avnet's accessory guidance](https://my.avnet.com/wcm/connect/85b19d2e-0f9e-4067-b325-9e5bee40b5c5/Ultra96_Accessories_201210.pdf?CACHEID=ROOTWORKSPACE-85b19d2e-0f9e-4067-b325-9e5bee40b5c5-nDn.wpQ&MOD=AJPERES).
3. Use the appropriate 12-V supply and cooling, attach a keyboard if needed,
   and press SW3/POWER. See the
   [V1 getting-started guide](https://www.96boards.org/documentation/consumer/ultra96/ultra96-v1/getting-started/).
4. Connect a data-capable cable to the upstream micro-USB port. The full image
   provides ECM networking at `192.168.3.1`, ACM serial and a small storage gadget.
   Verify the newly enumerated device and route before contacting that address.
5. Shut down with `sudo poweroff` on PYNQ, or `poweroff` as root on mikro,
   before removing power or media.

For mikro, follow its [access and acceptance guide](mikro-ultra96.md#access-and-physical-acceptance):
the login is `root` with the `ultra96-mikro` key, not `xilinx`. It has no sudo;
Python and a guarded framefx bundle exist only in the opt-in PL profile. Use
the [mikro USB checker](mikro-ultra96.md#verify-a-boot-over-usb) rather than the
PYNQ checker below. PYNQ USB/PL examples do not apply unchanged to mikro.
If mikro does not enumerate, use its
[early boot diagnostics](mikro-ultra96.md#usb-startup-and-early-boot-diagnostics)
before assuming a cable failure. The 2026-10-17 USB iteration fixes a configfs
function-link path bug; the PL profile flashed on 2026-10-22 contains that fix,
and subsequently passed physical SD boot and USB verification. Reverify the
connected device, route and boot identity for each hardware session.

USB enumeration/ACM occur after Linux starts, not during FSBL/U-Boot. For early
boot output use an appropriate V1 UART pod with verified pinout/voltage; do not
attach an arbitrary 3.3-V/5-V UART adapter. A previously observed `ttyUSB0` was
a Quectel modem, not this board. The PYNQ ACM by-id path was
`/dev/serial/by-id/usb-Xilinx_Inc._PYNQ-USB_0000-if02` at 115200 8N1; rediscover
it rather than assuming a fixed tty number.

## USB-only development and verification

The dedicated full-image login key and pinned host key live under ignored
`artifacts/hardware/keys/`. Do not print private key contents or disable host
verification. Confirm the direct USB route and source address with `lsusb`,
`ip -brief address` and `ip -j route get 192.168.3.1` before SSH.

```sh
ssh -F /dev/null -b '<verified-USB-source-IP>' \
  -i artifacts/hardware/keys/ultra96-h0 \
  -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o GlobalKnownHostsFile=/dev/null \
  -o UserKnownHostsFile=artifacts/hardware/keys/ultra96-h0-known_hosts \
  xilinx@192.168.3.1
```

The following CPU/userspace checker verifies USB VID/PID and a direct route,
pins the SSH host key, binds to the USB source address and checks the remote V1
identity. Substitute the actual interface; it never falls back to Wi-Fi/password
or accesses the FPGA manager, framefx device or raw media.

```sh
/usr/bin/python3 scripts/board_usb.py \
  --interface '<verified-USB-interface>' \
  --key artifacts/hardware/keys/ultra96-h0 \
  --known-hosts artifacts/hardware/keys/ultra96-h0-known_hosts
```

It targets the **full PYNQ image**, not mikro userspace: compiler/Python are
required. It tests an 8-MiB USB round trip, 78 independent filter cases, bounded
CPU-memory/scratch-file checks and native/host render equality. Its private
scratch path and evidence are recorded in `artifacts/latest-usb-check.json`.
This is not physical display, PL, full DRAM or raw SD acceptance.

## H0: offline Linux and DisplayPort baseline

`make board-image` derives from the pinned
[Avnet V1 PYNQ 3.0.1 release](https://github.com/Avnet/Ultra96-PYNQ/releases/tag/v3.0.1),
preserving the partition table/boot prefix, BOOT.BIN, boot.scr, image.ub,
kernel and base DT. Inputs are in `fpga/ultra96-v1/boot-image.lock.json`;
output/hashes are indexed by `artifacts/latest-board-image.json`.
It is a 9.86-GB vendor baseline, not a security-maintained general-purpose OS.
The [259-MiB mikro candidate](mikro-ultra96.md) uses separate userspace and evidence.

The full image's Fluxbox startup runs `/opt/axi-lab/display.py` as `xilinx`.
It shows animated input and CPU-reference copy/invert/threshold/Sobel output.
Keys 1–4 select filters, Space pauses, F11 toggles fullscreen, Escape leaves
fullscreen. The Tk/X11 app explicitly says **CPU reference, NOT FPGA**; it
does not load PL or access DMA. No browser or first-boot download is required.

The packer corrects the vendor's `/home/xilinx` ownership to numeric 1000:1000,
retaining 0750 and changing only that directory. The originally flashed image
predates this fix; the observed board was repaired live and passed subsequent
GUI autostart. A rebuild is needed for an image containing the packer fix;
do not mistake a live repair for a rewritten image artifact.

Collect diagnostics on the identified full-image board:

```sh
uname -a
cat /proc/sys/kernel/random/boot_id
tr '\000' '\n' < /proc/device-tree/model
sudo systemctl status axi-lab-h0 pynq-x11 --no-pager
sudo /usr/bin/python3 /opt/axi-lab/board_probe.py
cat /var/lib/axi-lab/boot-report.json
cat /home/xilinx/axi-lab-display.log
DISPLAY=:0 xrandr --query
sudo journalctl -b -u axi-lab-h0 -u pynq-x11 --no-pager
```

For a blank display, retain kernel/Xorg logs, connector status, EDID/modes and
the monitor/cable identity before changing services or firmware. Distinguish
no signal from a lit black desktop. LED heartbeat, DONE, X11 screenshots, a
connected connector or a running renderer are not proof of visible pixels.
H0 requires a real boot report **and operator-visible DisplayPort output**.

## Wi-Fi profiles

Private profiles belong in `artifacts/hardware/network/wpa_supplicant-lab.conf`
(0600 in a 0700 directory); the derived PSK is password-equivalent. Do not
commit or publish the file or credential-bearing images. The prepared open AP
profile is disabled by default. Association/DHCP have not been board-verified.

On the full vendor image, inspect existing network management before installing
a root-owned 0600 profile under `/etc/wpa_supplicant/`. Do not start a competing
supplicant, change host routes, or enable the open AP without securing vendor
services. Preserve USB rescue access. The mikro build accepts profiles only via
explicit `WIFI_CONFIG=...`; its service/recovery commands are documented in the
[mikro guide](mikro-ultra96.md#wi-fi-operation-and-recovery).

## Write the authorized card

This is a separate destructive action, not part of a build. Obtain current
target-specific authorization and a backup decision. Identify the reader/card
by model, serial, capacity and USB/removable status; unmount only its verified
partitions. Never select media using a cached device letter.

```sh
lsblk -o PATH,SIZE,TYPE,RM,RO,TRAN,MODEL,SERIAL,MOUNTPOINTS
/usr/bin/python3 scripts/sdcard.py inspect \
  --device '<reviewed-whole-disk-path>' --image '<manifest-image-path>'
```

Review the newly returned identity. Only with an explicit erase-without-backup
decision, substitute the current image hash/token and a new evidence path:

```sh
sudo /usr/bin/python3 scripts/sdcard.py write \
  --device '<reviewed-whole-disk-path>' --image '<manifest-image-path>' \
  --sha256 '<manifest-image_sha256>' \
  --expect-identity '<fresh-inspection-confirmation_token>' \
  --erase-without-backup --evidence '<new-write-evidence.json>'
```

The writer rejects mounted/mapped descendants, partitions, internal/non-USB or
non-removable disks, missing identity, undersized media and disks over 256 GiB.
It rechecks identity, opens exclusively, verifies source bytes while writing,
flushes and hashes the complete written image on readback. It clears the final
MiB outside the image when space permits to remove stale backup GPT metadata.
It does not back up, choose media or unmount automatically. An interrupted
write is not verified; `SD_WRITE_VERIFY_PASS` is not boot/display acceptance.

## Headless PL integration

Canonical platform values and boot hashes: `fpga/ultra96-v1/platform.json`.
Use the pinned V1 BDF checkout, not the V2-only Avnet snapshot. The selected
board is `em.avnet.com:ultra96v1:part0:1.2`, exact part
`xczu3eg-sbva484-1-e`, with Vivado 2026.1.

| Function | Reviewed connection |
| --- | --- |
| Control | 32-bit `M_AXI_HPM0_LPD` → SmartConnect → `0x80000000`/4 KiB |
| DMA | 64-bit core → SmartConnect → 128-bit `S_AXI_HP0_FPD` |
| Memory | Low 2-GiB DDR; non-coherent DMA API, no `dma-coherent` property |
| Clock/reset | 100-MHz `pl_clk0` (provider 71); `pl_resetn0` through `proc_sys_reset` |
| IRQ | Level-high `pl_ps_irq0[0]`, GIC 121, DT SPI 89; Linux IRQ number is dynamic |
| External PL IO | None |

The wrapper adds metadata/fixed AXI attributes, not a different algorithm.
AFI indices 4/5 set HP0 width; index 15 sets LPD HPM width. Other ports are not
changed. Framefx must be a **direct child of /fpga-full**, with `dma-ranges`
on that region: a dynamically added nested simple-bus did not populate on the
vendor kernel. The common driver owns its optional clock before first MMIO and
retains it through quarantine. Generated PS initialization is **not** executed:
no DDR retraining, MIO changes or replacement boot firmware.

Design rationale and pinned references: [ADR 0002](adr/0002-ultra96-volatile-pl.md).
The [vendor OF notifier](https://github.com/Xilinx/linux-xlnx/blob/xlnx_rebase_v5.15_LTS_2022.1/drivers/of/platform.c#L623)
explains the direct-child requirement; the
[AFI driver](https://github.com/Xilinx/linux-xlnx/blob/xlnx_rebase_v5.15_LTS_2022.1/drivers/fpga/xilinx-afi.c)
implements the width configuration.

### Reproducible host checks

```sh
make fpga-wrapper-test JOBS=4
make fpga-validate
make fpga
```

These never connect to or program the board. The build validates HWH wiring,
part, clock/reset, IRQ/address/AFI/DT properties, routed timing/coverage and
critical DRCs. Bootgen raw data must match the byte-swapped bitstream payload;
both overlays must apply to the pinned vendor DTB from H0 packaging at
`artifacts/hardware/vendor-system.dtb`. Output:
`artifacts/ultra96-pl-bitstream.json`. An XSA is not a bootable firmware image.

### Native board binaries and bundle

The board kernel is 5.15.19-xilinx-v2022.1; simulation uses 6.12.109.
**Binary modules are not interchangeable.** Build the shared driver and apps
against the exact board kernel, preserving `linux/framefx` / `linux/uapi`
layout. The shipped headers need generated asm headers/host tools: copy them
to a private workspace, retain Module.symvers, and run
`make ARCH=arm64 -j2 modules_prepare` there before the external-module build.
Do not modify the installed kernel header tree.

Record kernel/vermagic, the six native source hashes and hashes of the three
binaries plus build stdout/stderr, as required by `scripts/pl_bundle.py`.
Applications build with `gcc -std=c11 -O2 -Wall -Wextra -Werror -I<linux/uapi>`.
The [current handoff](progress.md#evidence-to-resume-from) indexes the last native
record; validate rather than assuming an old binary is current.

```sh
/usr/bin/python3 scripts/pl_bundle.py --native-record '<reviewed-native-build.json>'
```

The assembler rejects stale source/artifacts, copies the loader and produces a
private bundle without contacting hardware.

### Vendor IRQ ownership and observed CSR pass

The vendor `/fabric@A0000000` generic-uio placeholder reserves **GIC 121** even
though its address differs from framefx. An MMIO-only collision check misses
this. The loader now rejects **any** IRQ owner before programming. Normal vendor
boot therefore needs a separately reviewed ownership handoff first.

Do not add IRQF_SHARED, change the reviewed IRQ wiring or unload every UIO
driver. Review the exact placeholder DT/resource/driver, open or mapped clients,
IRQ/UIO events and runtime state; prevent new clients while handing it off.
Automatic handoff is not implemented. The original successful recovery unbound
only the verified-unused `a0000000.fabric` and reprobed a cleanly unwound
framefx probe. That boot-bound recovery script is **not** a reusable command
or permission to retry a failed DMA/AXI design.

Mikro excludes the modular generic-UIO driver, so that placeholder is not
automatically bound there. Its optional PL profile packages the same guarded
loader; actual IRQ ownership must still pass preflight on each boot.

Sources: [vendor generic UIO](https://github.com/Xilinx/linux-xlnx/blob/xlnx_rebase_v5.15_LTS_2022.1/drivers/uio/uio_pdrv_genirq.c),
[UIO unregister](https://github.com/torvalds/linux/blob/v5.15/drivers/uio/uio.c#L989).
The original loader FAIL and separate recovery PASS remain in H1 evidence.

### First load procedure

This supports one full load from an **unconfigured fresh boot**, not hot reload.
Verify V1 identity, boot ID, trusted USB access and recovery availability. Use a
source/artifact-current bundle and review IRQ ownership before loading.

Transfer the bundle over pinned-key USB SSH. On the board, set `bundle` to its
reviewed path and run preflight **without** confirmation:

```sh
sudo python3 "$bundle/pl_load.py" "$bundle"
```

Preflight checks exact boot/kernel hashes, free region/configfs/MMIO/IRQ, SMMU
state, available RAM/CMA and kernel health. It refuses an already operating
design. Only within the currently approved loading scope, after preflight passes:

```sh
sudo python3 "$bundle/pl_load.py" "$bundle" \
  --confirm LOAD_VOLATILE_FRAMEFX_ULTRA96_V1
```

The loader freezes payloads in a root-private directory, programs the FPGA,
checks cached state, applies AFI and device overlays, verifies platform binding
and runs 64 CSR queries. It writes stage reports before irreversible steps,
does not touch boot media, and does **not** submit DMA. Kernel/firmware calls
can outlive subprocess timeouts. A CSR PASS is H1 only.

On a proven H1 boot, begin H2 with one supervised small copy. Check output,
full source/destination allocations, IRQ deltas and kernel/ownership health
before advancing to H3 modes, larger images and repetition. Preserve all
failure evidence; do not proceed after an uncertain START, mismatch or timeout.
On mikro, also verify `/dev/framefx0` is root-owned 0600 and the explicit mdev
rule is installed; its default 0660 overrode the driver's requested mode in the
first PL-profile image. See [the profile notes](mikro-ultra96.md#optional-pl-profile).

### Physical DMA and all-filter gates passed

The recorded V1 run completed one 16×16 H2 copy, then 148 H3 jobs across all
four modes, odd tails, 4-KiB crossings, threshold endpoints, 100 repeated narrow
jobs and dimensions through 1024×1024. Native C and independent host Python
checked all image, source, stride-padding and guard bytes. Exact GIC delivery
and close/unmap/reopen checks passed. Full results and next work live in
[current status](progress.md), not in a presumed current board configuration.

`artifacts/latest-pl-dma.json` indexes profile exports and post-run health.
Recheck a saved profile offline without contacting hardware:

```sh
/usr/bin/python3 scripts/pl_dma.py '<export-directory>/maximum' --profile maximum
```

The verifier checks exact file sets, planned geometry/seeds/modes, allocation
sizes, full bytes and counters. `--profile` must match a defined suite profile.
The boot-bound execution scripts in the evidence are records, not an unattended
runner. No hardware fault injection, live reload or SD autoload was accepted.

For repeated latency/bandwidth measurements, use the separate
[PS/PL benchmark contract](pl-performance.md). Its supervisor must reidentify
the boot and unchanged loaded payload, serialize against other PL tools,
preserve failures and stop at the first uncertain transaction. The benchmark
neither reloads PL nor relaxes these DMA boundaries. Historical measurements
are indexed separately in `artifacts/latest-pl-benchmark.json`.

**CMA limit:** an exact CmaFree equality assertion failed by 8 KiB, with later
changes even without DMA. CmaFree is not an allocation counter. The
[kernel CMA debug interface](https://docs.kernel.org/admin-guide/mm/cma_debugfs.html)
was unavailable on this board. Zero module references and passing lifetime
tests do not establish exact leak freedom; that accounting remains unmeasured.

### Vendor status read hazard

Reading `/sys/class/fpga_manager/fpga0/status` on the vendor stack before
configuration occupied a CPU for minutes and emitted RCU stalls. It eventually
returned a firmware error; serial remained usable. The
[vendor status callback](https://github.com/Xilinx/linux-xlnx/blob/xlnx_rebase_v5.15_LTS_2022.1/drivers/fpga/zynqmp-fpga.c)
does configuration-register readback, unlike cached `state`. Avoid that
attribute; a recovered SSH connection is not enough to reuse the faulted boot.
This exclusion is a workaround, not a firmware fix or bounded-failure guarantee.

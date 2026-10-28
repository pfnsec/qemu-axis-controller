# Ultra96 V1 mikro H0 candidate

The minimal image is named **mikro**, distinct from the Lean theorem prover.
The former `board-lean-*` commands are now `board-mikro-*`; the script,
Buildroot configuration/package, payload directory and evidence indexes use
`mikro` too. Old build trees and dated evidence retain their historical names;
they are not relabeled as new results. Builds use a fresh
`artifacts/build/ultra96-mikro` tree rather than relocating Buildroot output.

Status **2026-10-22**: the USB-fixed **PL profile** was flashed to the H1n SD card
with **complete image SHA256 readback PASS**, then passed physical SD/ext4 boot
and USB verification: ECM/ACM, console-verified pinned SSH, an 8-MiB round trip
and 78 CPU-reference cases. Guarded PL loading and 149 DMA/IRQ jobs subsequently
passed on mikro; display and power-cycle/load repeatability remain unverified.
The old card's saved boot report confirms the earlier minimal image reached
V1 Linux userspace, despite missing USB access. DisplayPort remains deferred.
This is separate userspace from the full PYNQ H0 baseline. The current card
includes the optional framefx PL bundle, with **no automatic loading**;
packaging alone is not physical DMA/IRQ validation or M5. A live mdev permission
fix was also applied and is now in source; cached images must be rebuilt before
another flash. The earlier board-chroot check
below covers a historical candidate, not this rebuild. Check `make report`
and [current status](progress.md) for source/artifact freshness before reuse.

## Size and approach

| Component | Size / treatment |
| --- | --- |
| Full vendor-derived H0 image | 9,858,258,944 bytes (9.86 GB) |
| Mikro raw flash image | **271,581,184 bytes (259 MiB), 97.25% smaller** |
| Preserved prefix | 131 MiB, including the complete vendor FAT boot partition |
| Fresh ext4 root partition | 128 MiB budget; actual installed bytes recorded in the manifest |

The root partition contains free space plus ext4 metadata/journal; it is not
128 MiB of applications. The FAT partition is intentionally not resized yet:
preserving boot layout reduces first-boot risk. A smaller custom boot partition
or kernel is a later optimization, after hardware acceptance. The 2026-10-17
H1n write, flush and full image readback took **154.7 seconds**; other readers
and cards may differ. We do not fill or expand to the card's full capacity.

This is **not** Ubuntu with random files removed. The packer verifies the pinned
[Avnet V1 PYNQ 3.0.1](https://github.com/Avnet/Ultra96-PYNQ/releases/tag/v3.0.1)
image, retains `BOOT.BIN`, `boot.scr`, `image.ub`, kernel and DT byte-for-byte,
and builds a fresh AArch64/glibc userspace with the project's pinned Buildroot
and Bootlin toolchain. The partition start and root boot arguments stay the
same. Only the root-partition sector count changes in the MBR. Bytes 512 through
the start of partition 2 are hash-checked against the pristine vendor image.

The vendor kernel remains **5.15.19-xilinx-v2022.1**. Its configuration is
checked for built-in SD/ext4, PS DisplayPort, PHY, USB host/gadget, input and
serial support. Wi-Fi adds only the dependency closure for `wl18xx` and
`wlcore_sdio`: three unmodified vendor modules, matching module metadata,
`wl18xx-fw-4.bin`, signed regulatory data and the vendor firmware copyright
notice. The packer checks vermagic and records every asset's hash. It does not
copy the bulk firmware collection or unrelated modules such as `zocl`.
Bluetooth remains absent. Do not insert the simulation's Linux 6.12 framefx module
into this kernel. The common driver has been validated on the full vendor
baseline. The optional PL profile packages that exact-kernel driver separately;
the default minimal profile does not.

Private AP profiles are an explicit build input, never versioned sources.
Without `WIFI_CONFIG`, the build installs an empty profile file and Wi-Fi
startup skips association. With the supplied private profile, the protected AP
is preferred and the open AP remains disabled until manually enabled. Radio
association, DHCP, roaming and recovery still require physical testing.

## Included and deliberately absent

- BusyBox init, shell, mdev, basic diagnostics and a writable ext4 rootfs.
- A small native C display with generated input and copy/invert/threshold/Sobel
  CPU reference output, automatic cycling and optional USB keyboard controls.
- USB ECM networking, ACM console, local DHCP and public-key-only Dropbear SSH.
- Onboard Wi-Fi driver/firmware, `wpa_supplicant`, `wpa_cli`, `iw`, `rfkill` and
  reconnect-driven IPv4 DHCP/DNS, without blocking display/USB startup.
- SSH client and legacy SCP, TLS-enabled `curl` with CA certificates, optional
  BusyBox NTP client, module inspection and ext4 recovery utilities.
- Persistent boot diagnostics, source-recipe identity and renderer digest.
- No X11, browser, PYNQ, notebooks, GPU userspace, package manager or
  USB mass-storage gadget. No network access is needed to boot the demo.
- No Python in the default minimal profile; the opt-in PL profile adds only
  the interpreter needed by the existing guarded loader, not a build toolchain.

The display uses the vendor kernel's `/dev/fb0` emulation, not raw physical
addresses, `/dev/mem`, PL MMIO or DMA. It validates packed truecolor layouts,
channel masks, offsets, pitch and mapped bounds for 16/24/32-bit pixels at
640x480 through 4096x2160. The
[kernel framebuffer API](https://docs.kernel.org/fb/api.html) defines these
queries and layout fields; actual driver behavior still needs a board test.
There is no page-flip synchronization, so tearing is possible. Connect the
monitor before boot; hotplug/mode changes are not supported while rendering.
Restart the display service after such a change. Unsupported layouts fail
with a diagnostic rather than guessing the memory format.

Keys **1–4** select a filter and disable automatic cycling, **Space** pauses,
and **Escape** exits. Without a keyboard the animation cycles by itself. The
screen explicitly says **CPU reference, NOT FPGA**. A connected DRM connector
or a renderer-start marker is not evidence of operator-visible pixels.

## Build and validate without touching the card

```sh
make board-mikro-image JOBS=4
make board-mikro-image JOBS=4 WIFI_CONFIG=artifacts/hardware/network/wpa_supplicant-lab.conf
make board-mikro-smoke BOOT_TIMEOUT=90
make test
make report
```

The smoke test requires the existing QEMU and simulation kernel built by
`make boot-smoke`. It boots the mikro initramfs as AArch64 userspace under QEMU's
`virt` machine, verifies startup, key installation, Dropbear, diagnostics and
the native renderer, compares its pixels against the host build's hash, then
powers down. USB gadget setup correctly skips this non-Ultra96 machine.
It also parses the actual Wi-Fi config with a deliberately unavailable driver,
verifies private-file permissions and vendor payload hashes, and checks the
network/TLS/file-transfer utilities. Wi-Fi activation skips the non-board DT.
**It does not test vendor module loading, radio association, TLS connectivity,
the vendor kernel, ext4 boot, USB gadget hardware or DP.**

The image build checks ext4 consistency and restricts features to the audited
older-kernel-compatible set. Host tests compare filters against an independent
Python oracle, check RGB packing/stride/padding canaries and rejected geometry,
and exercise MBR assembly and evidence freshness. Startup fixtures exercise
late/absent controllers, transient/permanent bind failures, competing operations,
foreign gadget ownership, repeated starts, DHCP repair and boot-log rotation.
They run shell scripts against mocked hardware commands/files, not real configfs;
they can also use the built AArch64 BusyBox shell via `MIKRO_TEST_SHELL` in
`tests/test_mikro_startup.py`. These tests do not open SD
devices. The report keeps mikro evidence separate from M1–M4 and hardware H0.

`artifacts/latest-mikro-image.json` points to the dated image, frozen initramfs,
hashes, source recipe, SSH public-key fingerprint, kernel feature audit and
build logs. `artifacts/latest-mikro-smoke.json` binds the tested userspace to
that candidate and hashes the QEMU/kernel/initramfs used. Source or artifact
changes invalidate current evidence rather than silently reusing a PASS.

Build manifests retain `flashed: false`: assembly never writes media. A later
flash has separate `sd-write.json` evidence, bound to the exact image hash and
freshly inspected card identity. Readback success is not board-boot acceptance.

The two image commands above are alternatives, not two steps required for a
build. A private Wi-Fi config must be an owned regular file under ignored
`artifacts/`, with no group/other permissions. Generated build and image
directories are 0700; images/initramfs containing the PSK are 0600. Runtime
Wi-Fi config and SSH authorized keys are installed root-owned 0600. A derived
PSK is still a password-equivalent secret: do not publish these images or their
initramfs. The manifest records the private input's path/digest, not its content.
Building again without `WIFI_CONFIG` replaces cached profiles with the empty
template rather than silently retaining credentials.

## USB startup and early boot diagnostics

Missing host enumeration is **not proof of a bad cable**. It can also mean
Linux never reached userspace, the controller probed late, or gadget setup
failed. QEMU uses a different kernel/platform and cannot exclude these causes.
The preserved vendor kernel has the required USB support built in; mikro does
not need the full image's unrelated module collection to expose ECM/ACM.

The audit found a concrete setup bug: the earlier script supplied ordinary
link-relative `../../functions/...` targets to configfs. Unlike ordinary filesystem
symlinks, configfs resolves the supplied target at creation time **from the
caller's working directory**; see the
[preserved kernel family's implementation](https://github.com/Xilinx/linux-xlnx/blob/xlnx_rebase_v5.15_LTS_2022.1/fs/configfs/symlink.c).
That fails before UDC binding during normal init. Both links now use absolute
function paths. A regression fixture enforces configfs's lookup rules and
reproduces the old failure. This explains a definite software failure path,
and the corrected image subsequently passed physical SD/USB verification on
2026-10-22. This does not prove timing retries or the rebind were necessary.

`S30usbgadget` now waits up to 30 seconds for exactly one UDC, refuses an
already-owned controller, and retries a failed bind at most three times while
it remains unbound. Initial setup performs one bind/unbind/rebind sequence,
matching the working vendor image's approach. This is a compatibility measure,
**not a proven diagnosis or hardware fix**. The
[upstream PYNQ script](https://github.com/Xilinx/PYNQ/blob/master/sdbuild/packages/usbgadget/usbgadget)
also uses this sequence; the
[kernel configfs documentation](https://docs.kernel.org/usb/gadget_configfs.html)
defines binding and unbinding through `UDC`.

Repeated `start` preserves an already-bound connection and repairs network/DHCP
setup without duplicating the daemon. A lock excludes overlapping operations;
corrupt persistent USB identities and foreign gadget ownership fail visibly
rather than being replaced. There is no cable-state reconnect loop, automatic
reboot, PL access or mass-storage export. `AXI_LAB_USB_READY` means local setup
completed, **not** that a host enumerated or SSH authenticated. Kernel calls
themselves are not made safe or bounded by the shell's finite retry count.

Early init now records every service's output and exit status while retaining
serial-console output and continuing to later services after failure. Under
root-only `/var/lib/axi-lab/`, inspect:

- `init.log`: boot ID, recipe, kernel, command line, mounts and ordered service
  starts/results, including USB failure stage and controller snapshots.
- `boot-kernel.log`: kernel ring buffer captured before services start.
- `boot-report.txt`: later diagnostics, including USB binding and UDC state.
- Each has a single `.previous` generation; init syncs after each service.

Logs use mode 0600 in a 0700 directory. They do not dump private Wi-Fi profiles
or SSH keys, but can contain identifying network/device metadata. If USB remains
absent, shut the board down normally, move the card to the reader, reidentify it,
and inspect partition 2 **read-only with `ro,noload`**. Do not reflash before
preserving these logs. If userspace never starts or the rootfs cannot be written,
there may be no fresh persistent log; that requires a hardware UART boot trace,
not another blind USB retry. The ACM console itself depends on working USB.

## Optional PL profile

The minimal profile remains the default. To package the already proven PL,
exact-kernel module, native applications and existing guarded Python loader:

```sh
make board-mikro-image JOBS=4 \
  PL_NATIVE_RECORD=artifacts/hardware/pl-20261016/native-build.json \
  WIFI_CONFIG=artifacts/hardware/network/wpa_supplicant-lab.conf
make board-mikro-smoke BOOT_TIMEOUT=180
```

The native record path is a local historical artifact, not a portable download.
The packer rejects stale native source/binary hashes, wrong ELF architecture or
module kernel, and stale bitstream/overlay evidence. Rebuild those inputs when
needed; do not edit their records to force acceptance. It freezes the payload
under `/opt/axi-lab/pl`, records every digest and binds the recipe to the PL/native
inputs. The build uses `artifacts/build/ultra96-mikro-pl`, not the minimal cache,
so switching profiles cannot leave Python or PL files in a minimal image.
Both profiles keep the same 128-MiB root budget and preserved vendor boot prefix.
The latest image/smoke indexes refer to the most recent profile; use each dated
`manifest.json` when identifying a particular flashed image.

**There is no automatic PL programming, driver insertion or DMA.** QEMU smoke
checks the packaged hashes and imports/validates the loader using target Python;
`programming=NOT_ATTEMPTED` is not a physical load result. It does not execute
the board-kernel module under the simulation kernel.

The preserved kernel has a modular `uio_pdrv_genirq`; neither profile includes
that module. This avoids automatically binding the vendor generic-UIO IRQ
placeholder. The loader still rejects *any* live IRQ owner; do not add UIO or
steal an IRQ to bypass preflight. FPGA manager, AFI, configfs overlays and CMA
must be built into the retained kernel, which the PL profile audits.

Keep the explicit `framefx0 root:root 600` rule in `/etc/mdev.conf`. Without it,
mdev changed the driver's requested 0600 node to 0660 on the first physical
mikro load. The pre-DMA safety gate caught this before any transfer; the rule
and a narrowly scoped chmod repaired that boot without reprogramming. A later
targeted mdev add event preserved 0600. Require root ownership, exact character
device identity and 0600 before DMA; do not relax the gate to accept defaults.
This repair is recorded separately from the flashed image's build recipe.

After this profile has actually booted, verify USB/recipe/kernel identity first.
For an explicitly approved first load, follow the hardware runbook's fresh-boot
and recovery checks. Mount the identified SD boot partition read-only at `/boot`
and configfs at `/sys/kernel/config` if not already mounted, then use
`python3 /opt/axi-lab/pl/pl_load.py /opt/axi-lab/pl` for **preflight only**.
Do not copy PYNQ commands using `sudo` into mikro; its SSH session is root.
Confirmation/programming and later DMA gates remain separate supervised steps.
No live overlay removal, forced unload, automatic retry or hot replacement is
provided by this profile.

## Verify a boot over USB

`scripts/mikro_usb.py` performs host-driven checks without installing Python or
a compiler on the board. Identify the direct ECM interface, enroll and verify
the new Dropbear host key via the trusted local console/link, then use:

```sh
/usr/bin/python3 scripts/mikro_usb.py \
  --interface '<verified-USB-interface>' \
  --key artifacts/hardware/keys/ultra96-mikro \
  --known-hosts artifacts/hardware/keys/ultra96-mikro-known_hosts \
  --candidate '<dated-manifest-of-the-flashed-image.json>'
```

The checker refuses routed/non-USB connections and uses strict pinned host-key
authentication. It binds the board identity, SD/ext4 root, kernel, recipe,
renderer and authorized-key hashes to the specified frozen candidate. It tests
an 8-MiB SSH round trip in private tmpfs scratch space, 78 CPU-reference cases,
render equality, service assets and unchanged boot/kernel health. It never
opens the framebuffer, FPGA manager, PL device or raw media. Failed scratch
files are retained for diagnosis; successful runs remove only their own files.

Evidence is indexed by `artifacts/latest-mikro-usb.json` and included in
`make report`. A historical frozen image can be verified even when development
has moved on; `candidate_inputs_current` records that distinction. This is SD
boot/userspace/USB evidence, not Wi-Fi association, visible display or PL/DMA
acceptance. If no gadget enumerates, diagnose cable/boot/console first rather
than falling back to an unverified network route or guessing a serial port.

## Wi-Fi operation and recovery

The V1 uses a TI WL1831MOD; provide a **2.4-GHz** AP. Its radio does not gain
5-GHz support from software; see [TI's module specification](https://www.ti.com/product/WL1831MOD).
The supplied protected profile assumes WPA2-Personal/RSN, not WPA3-only or
enterprise authentication. Use the regulatory settings appropriate to the
board's actual location; the build does not guess a country from host metadata.

`S35wifi` starts a background worker only on the identified V1. It checks the
running kernel release before loading vendor modules, identifies the wl18xx
interface, starts the supplicant and handles connection/disconnection events.
DHCP retries in the background; absent APs do not delay the display or USB
rescue connection. Interface discovery is bounded to 30 seconds; if the radio
does not appear, inspect logs/rfkill and restart the service after correcting
the problem. The service does not override a radio block automatically.

```sh
axi-lab-wifi status
wpa_cli -i wlan0 list_networks
wpa_cli -i wlan0 status
ip route
cat /etc/resolv.conf
tail -n 100 /var/log/messages
/etc/init.d/S35wifi restart
```

Use the interface actually reported by `iw dev`, not an assumed name, when
different from `wlan0`. The DHCP hook validates IPv4 fields, changes addresses
and routes only on a wireless interface, uses metric 600 for its default route,
and replaces temporary DNS data on renew/disconnect. It does not rewrite the
USB address or advertise a route/DNS server to the host. Arbitrary DHCP search
domains and classless routes are not accepted by this small hook.
Leases whose subnet overlaps `192.168.3.0/24` are rejected to preserve USB
rescue connectivity. Change the AP subnet before using such a network.

To use the open profile deliberately, inspect `list_networks`, then run
`wpa_cli -i wlan0 enable_network <reviewed-id>` and `reassociate`. Disable it
again with `disable_network <reviewed-id>` when finished. `update_config=0`
means this change is not saved across reboot. An open SSID authenticates
neither the AP nor other clients: use SSH/HTTPS and avoid exposing additional
services. The mikro image has no default SSH password or notebook/web server.

For log collection, run `axi-lab-support` on the board. It prints the path of a
private tarball with boot/network/kernel diagnostics and capped log tails,
excluding the Wi-Fi config and SSH keys. Logs can contain SSIDs, MACs and IPs;
review them before sharing. Syslog rotates in volatile storage and display
logging retains the previous run rather than appending across every boot.
Collect `init.log`, `boot-kernel.log` and their `.previous` files separately
for startup failures; the support tarball currently includes the later
`boot-report.txt` and display/syslog tails, not those early-init transcripts.

Modern host `scp` defaults to SFTP, which this image does not include. Use
legacy SCP **over SSH** explicitly, for example:

```sh
scp -O -i artifacts/hardware/keys/ultra96-mikro root@192.168.3.1:/tmp/axi-lab-support-<stamp>.tar.gz .
```

`curl` verifies HTTPS certificates using the installed CA bundle. Check
`date -u` first. There is no automatic external time service or insecure TLS
bypass: set time from a trusted local source, or deliberately run
`ntpd -n -q -p <trusted-time-server>`. This BusyBox NTP client does not provide
authenticated NTS. `e2fsck` is for **unmounted** filesystems/recovery media;
do not run repairs against the mounted root partition. Power off cleanly.

## Remaining platform gaps

The frozen no-Wi-Fi-credentials candidate passed an isolated, read-only chroot
test on the V1 vendor kernel, without starting init/network/USB services.
Evidence: `artifacts/hardware/usb-20261016T212037Z/lean-chroot/`; details and
initial harness failures are in the [history](history/2026-10-15-16.md).
This establishes CPU/userspace ABI coverage, not mikro SD boot, services, module
loading, Wi-Fi, USB gadget or physical display acceptance. It does not validate
a later rebuilt candidate automatically.

- One physical SD/USB boot passed; repeated power-cycle recovery, DP and Wi-Fi
  still require board observations. Host/QEMU checks are not substitutes.
- The default minimal profile has no framefx deployment. The opt-in PL profile
  has passed boot, guarded load and all-filter H1–H3 tests, with the mdev fix
  above. Repeat cold-boot/load acceptance remains pending. The earlier full
  vendor baseline has separate H1–H3 evidence.
- No Bluetooth, GPU desktop, general package manager or on-board compiler.
  Cross-compile on the host and transfer binaries instead.
- No SFTP, authenticated time synchronization, secure boot, encrypted storage,
  automatic updates/A-B rollback or guaranteed sudden-power-loss recovery.
  Keep the pristine vendor baseline and a trusted USB/serial rescue path.

## Access and physical acceptance

The build creates a dedicated host key at
`artifacts/hardware/keys/ultra96-mikro` (0600, private parent directory). Only its
public key enters the image. Keep the private key; deleting it loses SSH access
to already-flashed copies unless another authorized key is installed. The
board generates its own persistent Dropbear host key on first boot. Do not
disable host-key checking: a reflash may legitimately change that host key,
but verify the connected board before changing known-host records.

When migrating an existing checkout, preserve the former `ultra96-lean` client
key as `ultra96-mikro` before building (private directory, mode 0600, no symlinks
or overwriting another key). This checkout retains both copies with identical
public identities. Fresh checkouts generate a dedicated mikro key normally.

After a verified flash, use the board/cable/power/SD-switch instructions
in the [hardware handoff](bringup-ultra96.md#connect-and-boot). The mikro image
uses the same guarded SD writer with the **mikro** manifest's path and hash;
reinspect the actual card and review its identity immediately before writing.
Neither build target writes physical media, interrupts an ongoing write or
selects a disk automatically. Do not reflash just to test userspace in QEMU.

The upstream micro-USB gadget uses
[configfs ECM + ACM](https://docs.kernel.org/usb/gadget_configfs.html), bound
only on the identified `avnet,ultra96-rev1` board. The USB network address is
`192.168.3.1/24`; DHCP offers `.2` through `.20` without default-router or DNS
advertisements. No host routing or interface settings are changed by our tools.
Confirm the new USB interface and absence of conflicting routes before use.
Linux ECM host support is expected; Windows plug-and-play is not promised.

```sh
ssh -i artifacts/hardware/keys/ultra96-mikro root@192.168.3.1
```

Inside that trusted board connection:

```sh
axi-lab-probe
cat /var/lib/axi-lab/boot-report.txt
cat /var/lib/axi-lab/display.log
cat /etc/axi-lab-release
cat /proc/fb
dmesg
poweroff
```

Both UART and USB serial consoles allow password-free local root; SSH rejects
password authentication. This is an **isolated development system**, not a
multi-user or remotely exposed appliance. Keys and host identity are not secure
boot or tamper resistance. Vendor firmware/kernel security is not modernized by
replacing userspace. Preserve the pristine vendor image for rollback, and
review `NOTICE` before distribution.

Physical H0 acceptance still requires the matching boot report, an identified
Ultra96 V1, operator-observed DisplayPort animation and input response if a
keyboard is attached. USB networking, shutdown and reboot persistence also
need real-board checks. These remain open until actual observation.

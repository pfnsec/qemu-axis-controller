# Host setup and reproducibility

## Scope and prerequisites

The recorded development host is Fedora 44 x86_64; CI is configured for Ubuntu
24.04. Reserve at least 20 GiB for the simulator toolchain, plus space for
retained test evidence; vendor images and FPGA builds need substantially more.
Check current disk space rather than relying on an earlier inventory.
Build paths must not contain spaces.
Python 3.12+ is needed for safe archive extraction. Commands default to
`/usr/bin/python3` to avoid the host's PlatformIO Python environment.

`make doctor` checks prerequisites and writes `artifacts/host/doctor.json`.
It never runs a package manager or changes the global environment. If packages
are missing, the following are operator-run examples, not automatic installers:

```sh
# Fedora
sudo dnf install gcc gcc-c++ make git ninja-build meson python3 python3-pip \
  glib2-devel pixman-devel zlib-devel pkgconf-pkg-config \
  bison flex perl patch bc cpio rsync file which diffutils findutils \
  curl wget tar xz bzip2 gzip unzip verilator

# Ubuntu 24.04
sudo apt-get install build-essential gcc-multilib git ninja-build meson python3 python3-venv \
  python3-pip libglib2.0-dev libpixman-1-dev libattr1-dev zlib1g-dev pkg-config \
  bison flex perl patch bc cpio rsync file wget curl tar xz-utils bzip2 gzip \
  unzip verilator
```

System QEMU, AArch64 GCC, dtc, SystemC, KVM and Vivado are not prerequisites for
the headless boot. Buildroot supplies the toolchain and host dtc. The QEMU build
disables optional graphics, audio, networking backends and documentation.
These features can be enabled deliberately at later gates.
Virtio-9p and host xattr support are enabled for per-run M4 evidence exports.
The compile-only x86 32-bit UAPI test needs multilib kernel headers; Ubuntu's
`gcc-multilib` supplies the required include setup. It does not run a 32-bit guest.

`make hardening-test` additionally requires host `clang` and `lld` (packages of
those names on Fedora/Ubuntu). These build a freestanding ARM EABI executable
using only the project's UAPI and Buildroot's fixed-width Linux headers, not
the AArch64 libc headers or libraries. The guest kernel enables `CONFIG_COMPAT`;
the test runs as real 32-bit EL0 userspace under that 64-bit kernel, not under
qemu-user. The hardening evidence records both compiler/linker versions and the
ELF32 ARM binary hash. Host LLVM versions are recorded, not hermetically pinned.

## Inputs and downloads

### Optional Ultra96 V1 H0 image

`make board-image` additionally needs `7z`, `curl`, GNU coreutils, `sudo`,
`losetup`, `sfdisk`, `mount`, `umount` and `e2fsck`. It requires working
noninteractive `sudo` for loop-device setup and local filesystem mounts. It
never chooses or writes a physical disk. The separately invoked SD writer
requires Linux `lsblk`, sysfs and root, an explicit removable USB target,
reviewed identity token, image SHA256 and erase acknowledgement.

Allow roughly 21 GiB for the 1.35-GB archive, 9.86-GB pristine image and derived
image on filesystems without reflinks; later images need additional space.
The V1 vendor release and locally measured checksums are pinned separately in
`fpga/ultra96-v1/boot-image.lock.json`. This is an old vendor image selected to
avoid changing the PS firmware/kernel/display stack during first bring-up,
not a promise of security maintenance. Keep it on a trusted isolated connection.

`make board-mikro-image JOBS=4` uses the same pinned vendor download but builds a
separate minimal userspace in `artifacts/build/ultra96-mikro`. It additionally
needs the normal Buildroot prerequisites, `ssh-keygen`, `dumpe2fs`, `debugfs`,
`modinfo` and `depmod` (kmod host utilities). It uses
local read-only loop mounts to audit the vendor kernel, never the physical SD.
The generated raw image is 259 MiB; the pristine 9.86-GB vendor download remains
on the host as provenance/rollback, not as part of the mikro card filesystem.
`make board-mikro-smoke` needs the already built QEMU and simulation kernel from
`make boot-smoke`; it does not emulate Ultra96 peripherals. See the
[mikro system guide](mikro-ultra96.md) for features, keys, safety and evidence.

An explicit `PL_NATIVE_RECORD=...` adds the verified board PL/module/apps and
guarded Python loader, using a separate `artifacts/build/ultra96-mikro-pl` output
tree. It never programs hardware or writes media. See the guide's
[optional PL profile](mikro-ultra96.md#optional-pl-profile) for input requirements.
AP credentials are included only with explicit `WIFI_CONFIG=<private-file>`;
the file must be private and under ignored `artifacts/`. Such images contain
password-equivalent secrets and must not be shared.

Host reference tests optionally use Node.js. Native display smoke tests use
Python Tk and an isolated X server: `DISPLAY=:99 /usr/bin/python3
linux/hardware/ultra96-v1/display.py --windowed --smoke-test` after starting Xvfb
on that display. They do not establish physical DisplayPort functionality.
The image already includes Python 3.10/Tk/X11; no browser, package installation
or network connection is required by the automatic on-board visualization.
See [hardware bring-up](bringup-ultra96.md) before writing media.

### Simulation and FPGA sources

`versions.lock` pins QEMU 10.2.4, Buildroot 2025.02.18, Linux 6.12.109, Bootlin's
stable 2024.05-1 AArch64 glibc toolchain and the reference repository commits.
Buildroot 2025.02 is the selected LTS line; this is a tested baseline, not a
promise to follow new upstream releases automatically.

Release archives use SHA256 verification before extraction and on reuse.
Unexpected roots, traversal, and links escaping the extraction directory are
rejected. Interrupted downloads retain only a `.part` file, never a completed
archive. A hash mismatch fails without replacing it or accepting new hashes.
Two unused absolute symlinks are explicitly excluded in the lock: QEMU's
`X11IncludeHack` in the EDK2 host emulator, and Buildroot's OpenRC `sysv-rcs`
runlevel link. Neither is used by this direct-kernel, BusyBox-init guest. Unsafe
link extraction is never enabled, and complete original archives remain in the
cache. Revisit these exclusions before building EDK2's host emulator or selecting
OpenRC; those configurations are outside the tested baseline.
Existing extracted trees are preserved for development; their contents are not
compared byte-for-byte to the archive on every invocation. For pristine-source
verification, use a new checkout or move the extracted tree aside and fetch again.

Reference repositories are detached at exact commits. Dirty or wrong-revision
checkouts fail without reset/overwrite. Their submodules are intentionally not
initialized because these are reading references, not selected build inputs.

QEMU obtains dtc at the revision in `subprojects/dtc.wrap`, also recorded in the
lock. Meson comes from the recorded host environment or QEMU's bundled pinned
wheel; pycotap is supplied by the release's bundled wheel. Buildroot verifies
package downloads against `.hash` files in its pinned tree; forced hash checking
is enabled. There is no floating `latest` dependency fetch.

Host compilers/libraries are recorded, not container-pinned. `BR2_REPRODUCIBLE`
is enabled, but cross-host bit-for-bit reproducibility has not been established.
Change versions deliberately, rebuild in fresh output directories, and compare
runtime evidence. Build commands prefer system tools and clear common compiler,
Python and library-path overrides to avoid accidentally importing Vivado's SDK.

## Build and recovery

```sh
make fetch fetch-refs
make qemu JOBS=8
make guest JOBS=8
make toolcheck
make rtl-test JOBS=8
make boot-smoke
make spike-test
make demo-sim
```

Build output streams to `artifacts/logs/`; use `tail -f` for a long build.
Failure prints the last 35 log lines. `JOBS` bounds package build parallelism.
The small BusyBox initramfs is regenerated with the checked-in overlay each time.
The guest defconfig is authoritative: `make guest` reapplies it. Save deliberate
config changes in `linux/buildroot/`, not just the generated `.config`.

An advisory lock rejects concurrent `lab.py` commands; use sequential make
targets and `JOBS`, not top-level `make -j`. Ctrl-C terminates a running build's
process group. Rerunning the target resumes normal incremental compilation.
Toolchain changes may require moving `artifacts/build/guest` aside for a clean
rebuild. `make guest` clears and rebuilds the generated `axi-spike` and `framefx`
packages so local driver/application/UAPI edits cannot be silently ignored.
It hashes the checked-in Linux config and rebuilds the kernel on config changes;
unchanged kernel/toolchain builds remain incremental. Project sources are never
cleared. A successful kernel stage is stamped even if a later package fails.

`make qemu` applies the version-checked hooks in `qemu/integrate.py` and copies
tracked bridge sources into the extracted tree. It does not reset unrelated
local QEMU edits. Source anchors that no longer match fail explicitly. The
stock-machine smoke test omits the device, but uses this patched binary; the
original unmodified-QEMU boot evidence remains in the historical progress log.

`make rtl-test` builds the standalone RAM-backed Verilator fixture without
building a guest, then runs M3 framefx image/arithmetic/fault tests. Use
`make framefx-test` for M3 alone. `make sim` builds the socket-backed executable; it does not
launch it. `make spike-test` owns both process lifetimes. The two executables
share the same pin-level models and burst RTL. See [M2](m2-fabric.md) for supported
AXI parameters, replay and reset semantics.

`make framefx-synth` optionally synthesizes the actual framefx core out of
context for the V1 FPGA part. It produces utilization, an estimated timing
report and a checkpoint under `artifacts/build/framefx-synth/`. This is neither
place-and-route nor a PS/DDR/interrupt/boot integration test. See [M3](m3-framefx.md).

For a genuinely fresh test, use a new repository checkout and run the documented
commands. No dependency cache or global tool installation is modified by cleanup.

Before distributing guest artifacts, run `make legal-info` and review the output
and any warnings. This can download additional corresponding source archives.

## Hardware tooling

Set `VIVADO=/absolute/path/to/bin/vivado` if it is not on PATH or under a common
`~/Applications/vivado`, `/tools/{AMD,Xilinx}`, or `/opt/{AMD,Xilinx}` layout.
`make fpga-inventory` launches Vivado in an ignored working directory and queries
versions, board definitions and available ZU3EG parts. It does not synthesize,
program hardware or modify the installation. `make fpga-toolcheck` separately
synthesizes the small counter fixture for the V1 part to exercise the installed
device files and synthesis license. It does not use the PS preset or implement
a board design. See [hardware handoff](bringup-ultra96.md).

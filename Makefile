PYTHON ?= /usr/bin/python3
JOBS ?= 8
WIFI_CONFIG ?=
BOOT_TIMEOUT ?= 180
STRESS_CASES ?= 512
STRESS_JOBS ?= 256
STRESS_SEED ?= 2026
STRESS_TIMEOUT ?= 600
LAB = $(PYTHON) scripts/lab.py

.PHONY: help doctor fetch fetch-refs qemu guest boot-smoke guest-shell toolcheck test report legal-info fpga-inventory fpga-toolcheck rtl-lint rtl-test sim demo-sim fpga spike-build spike-test
.PHONY: framefx-test framefx-synth demo-build stress-test hardening-test board-image board-mikro-image board-mikro-smoke fpga-validate fpga-wrapper-test
.PHONY: benchmark-sim

help:
	@printf '%s\n' 'PS/PL benchmark correctness (disposable guests, not board performance): benchmark-sim'
	@printf '%s\n' 'Foundation: doctor fetch fetch-refs qemu guest boot-smoke guest-shell toolcheck test report legal-info fpga-inventory fpga-toolcheck' 'M1 concurrency: spike-build spike-test' 'M2 AXI fabric: rtl-test sim (builds spike socket simulator)' 'M3 framefx: rtl-lint framefx-test framefx-synth (optional Vivado)' 'M4 Linux images: demo-build demo-sim' 'JOBS=8 controls build parallelism; BOOT_TIMEOUT=180 bounds each guest test.' 'Ultra96: fpga-validate (block design), fpga (routed bitstream; NEVER loads hardware).'
	@printf '%s\n' 'Platform hardening: stress-test (STRESS_CASES=512 STRESS_JOBS=256 STRESS_SEED=2026 STRESS_TIMEOUT=600)'
	@printf '%s\n' 'Control-path fault injection and runtime ARM32 ABI: hardening-test (requires clang and lld)'
	@printf '%s\n' 'Hardware H0: board-image (requires sudo for local loop mounts; NEVER writes an SD card)'
	@printf '%s\n' 'Mikro H0 candidate: board-mikro-image board-mikro-smoke (separate Buildroot rootfs; NEVER writes an SD card)'

doctor:
	$(LAB) doctor

fetch:
	$(LAB) fetch qemu buildroot

fetch-refs:
	$(LAB) fetch libsystemctlm-soc systemctlm-cosim-demo avnet-bdf avnet-bdf-ultra96v1

qemu:
	$(LAB) qemu --jobs $(JOBS)

guest:
	$(LAB) guest --jobs $(JOBS)

boot-smoke: qemu guest
	$(LAB) boot-smoke --timeout $(BOOT_TIMEOUT)

guest-shell: qemu guest
	$(LAB) guest-shell

spike-build:
	$(LAB) spike-build --jobs $(JOBS)

sim: spike-build

rtl-lint:
	$(LAB) rtl-lint

rtl-test:
	$(LAB) rtl-test --jobs $(JOBS)

framefx-test:
	$(LAB) framefx-test --jobs $(JOBS)

framefx-synth:
	$(LAB) framefx-synth

spike-test: qemu guest spike-build
	$(LAB) spike-test --timeout $(BOOT_TIMEOUT)

test:
	$(LAB) test

toolcheck:
	$(LAB) toolcheck --jobs $(JOBS)

report:
	$(LAB) report

board-image:
	$(LAB) board-image

board-mikro-image:
	$(LAB) board-mikro-image --jobs $(JOBS) $(if $(WIFI_CONFIG),--wifi-config "$(WIFI_CONFIG)") $(if $(PL_NATIVE_RECORD),--pl-native-record "$(PL_NATIVE_RECORD)")

board-mikro-smoke:
	$(LAB) board-mikro-smoke --timeout $(BOOT_TIMEOUT)

legal-info:
	$(LAB) legal-info --jobs $(JOBS)

fpga-inventory:
	$(LAB) fpga-inventory

fpga-toolcheck:
	$(LAB) fpga-toolcheck

demo-build:
	$(LAB) demo-build --jobs $(JOBS)

demo-sim: qemu guest demo-build
	$(LAB) demo-sim --timeout $(BOOT_TIMEOUT)

benchmark-sim: qemu guest demo-build
	$(LAB) benchmark-sim --timeout $(BOOT_TIMEOUT)

stress-test: qemu guest demo-build
	$(LAB) stress-test --jobs $(JOBS) --cases $(STRESS_CASES) --linux-jobs $(STRESS_JOBS) --seed $(STRESS_SEED) --timeout $(STRESS_TIMEOUT)

hardening-test: qemu guest demo-build
	$(LAB) hardening-test --timeout $(BOOT_TIMEOUT)

fpga:
	$(LAB) fpga

fpga-validate:
	$(LAB) fpga-validate

fpga-wrapper-test:
	$(LAB) fpga-wrapper-test --jobs $(JOBS)

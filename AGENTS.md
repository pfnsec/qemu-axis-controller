# Working in axi-cosim-lab

This repository verifies synthesizable AXI soft IP through Verilator, QEMU
AArch64 Linux and an Ultra96 **V1**. Framefx is the first workload. Keep one
production RTL core, register ABI, UAPI and driver source across both targets;
QEMU and the transport must not implement the image algorithm.

## Find the relevant context

- Continuing development: [current status and next work](docs/progress.md).
- Build/environment: [setup](docs/setup.md); command selection:
  [verification](docs/verification.md).
- Architecture or another IP: [platform contract](docs/architecture.md).
- Board access, loading or media: [hardware runbook](docs/bringup-ultra96.md).
- Topic-specific specifications and source locations: [documentation map](docs/README.md).

Read what the task needs, not every document. `brief.md` is the original scope
and acceptance brief, not a fresh-session instruction to restart M0. Material
under `docs/history/` is archival, and `artifacts/` contains generated evidence;
neither is a source of current operational permission.

## Local development

- Safe host tests and simulator runs use disposable fixtures/guests and have no
  board access. Run and iterate on relevant checks without a separate review
  for each command. Match validation to the change; prose-only edits do not
  require a full simulator/FPGA rebuild.
- Use `/usr/bin/python3` and `JOBS=N`. Run lab targets sequentially, not with
  top-level `make -j`: they share a locked build tree. `make help` lists targets.
- Edit first-party sources, not generated copies in `third_party/` or
  `artifacts/build/`. Pinned dependency inputs are in `versions.lock`.
- `make test` is the host unit suite; `make rtl-test` covers M2/M3;
  `make spike-test` and `make demo-sim` cover Linux integration. Additional
  stress, compatibility and FPGA gates are described in the verification guide.

## Hardware boundaries

- Hardware changes must fit the current request and verified target. Old
  erase approvals, boot IDs, interface names and recovery scripts are not
  reusable authorization. `/dev/sda` has also been the board's USB storage
  **gadget**, not its boot card; identify media afresh before any write.
- Never read `/sys/class/fpga_manager/fpga0/status` or glob-read manager
  attributes: this vendor firmware readback stalled a CPU. Use cached `state`.
- No hot PL replacement, live overlay removal, forced module unload or blind
  retry after a failed AXI/DMA transaction. Preserve evidence and use the
  runbook's recovery boundary. A userspace timeout cannot stop a wedged bus.
- Physical HP DMA is non-coherent and not isolated by the SMMU. Driver bounds
  do not contain faulty IP. Keep the vendor boot baseline and USB recovery
  intact; simulation fault-injection procedures are not board procedures.
- Private SSH keys, Wi-Fi profiles and credential-bearing images stay under
  ignored `artifacts/`; do not copy their contents into docs or source.

## Evidence and handoff

`make report` checks recorded evidence; it does not run the gates or contact the
board. Inspect `inputs_current`, not just `status: PASS`. Historical H1/H2/H3
observations do not prove the board is still configured or complete M5.
`scripts/lab.py:project_hashes` covers the source trees, Makefile and lock;
documentation under `docs/` and root guidance are outside that hash set.

Keep `docs/progress.md` a compact, dated current-state handoff with evidence
pointers and prioritized open work. Update the relevant contract/runbook when
behavior changes; put lengthy chronology in `docs/history/`, not in startup
instructions. Preserve failures and distinguish unmeasured results from PASS.

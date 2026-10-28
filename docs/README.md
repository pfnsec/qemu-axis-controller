# Documentation map

Use [current status](progress.md) for continuation, not the historical log.
[Repository guidance](../AGENTS.md) contains only durable working rules and
hardware hazards; the pages below are task-specific references.

| Task | Read | Main sources |
| --- | --- | --- |
| Install tools, build, recover a build | [Setup](setup.md) | `Makefile`, `versions.lock`, `scripts/lab.py` |
| Choose/run verification | [Verification](verification.md), [demo commands](demo-script.md) | `tests/`, `scripts/{rtl,framefx,demo,stress,hardening}.py` |
| Change platform assumptions or add IP | [Architecture](architecture.md) | `sim/axi/`, `rtl/`, `qemu/`, `linux/` |
| Change bridge/transport | [M1](m1-spike.md), [wire protocol](../transport/protocol.md), [ADR 0001](adr/0001-qemu-device-and-concurrency.md) | `qemu/cosim-device/`, `transport/`, `sim/spike_main.cpp` |
| Change AXI models | [M2](m2-fabric.md) | `sim/axi/`, `sim/rtl_test.cpp`, `rtl/core/axi_burst_copy.sv` |
| Change framefx RTL/registers | [M3](m3-framefx.md) | `rtl/core/framefx_*.sv`, `sim/framefx_test.cpp` |
| Change driver/UAPI/application | [M4](m4-linux.md) | `linux/framefx/`, `linux/uapi/framefx.h` |
| Work on Ultra96 V1 | [Hardware runbook](bringup-ultra96.md), [ADR 0002](adr/0002-ultra96-volatile-pl.md) | `fpga/ultra96-v1/`, `linux/hardware/ultra96-v1/`, `scripts/pl*.py` |
| Measure PS/PL interoperation | [Measurement contract](pl-performance.md) | `linux/framefx/app/framefx-bench.c`, `scripts/pl_benchmark.py`, `make benchmark-sim` |
| Build/evaluate mikro userspace or optional PL profile | [Mikro image](mikro-ultra96.md) | `linux/hardware/ultra96-mikro/`, `linux/buildroot/`, `scripts/mikro.py`, `scripts/mikro_usb.py` |
| Review scope/acceptance | [Original brief](../brief.md) | Design intent, not current commands/status |
| Investigate an earlier result | [History](history/2026-10-15-16.md) | Recorded evidence under ignored `artifacts/` |

## Keep context useful

- Current results, blockers and next actions belong in `progress.md`, dated
  and linked to evidence. Replace superseded status rather than appending
  competing versions throughout the docs.
- Runbooks explain how to operate safely; topic guides explain contracts and
  tests; ADRs explain decisions. Keep session transcripts and transient device
  names out of routine instructions.
- Preserve substantive historical evidence, but label archives as history.
  Do not make reading the archive or whole brief mandatory for unrelated work.
- Validate changed links, commands and claims against local source/evidence.
  Documentation-only changes outside hashed source trees need no full rebuild.
  Local artifact links may be unavailable on a clean checkout; source/doc links
  should resolve without a build.

This structure uses task-specific navigation rather than model-specific
itineraries. It follows the context-hygiene principles in OpenAI's
[GPT-6 Astra guidance](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra);
it does not change model selection, global Codex settings or installed skills.

import argparse
import datetime
import json
from pathlib import Path
import shutil

import lab
import pl


NATIVE_SOURCES = {"linux/framefx/driver/framefx.c", "linux/framefx/driver/Makefile", "linux/uapi/framefx.h",
                  "linux/framefx/app/framefx-test.c", "linux/framefx/app/framefx-probe.c",
                  "linux/framefx/app/framefx-reference.h"}
NATIVE_ARTIFACTS = {"framefx.ko", "framefx-test", "framefx-probe", "native-build.stdout", "native-build.stderr"}


def validate_native(record, directory):
    if record.get("schema") != 1 or record.get("stage") != "native-build-only" or record.get("kernel") != pl.PLATFORM["kernel"]:
        raise ValueError("Native board build record missing or wrong kernel")
    if set(record.get("source_sha256", {})) != NATIVE_SOURCES or set(record.get("artifact_sha256", {})) != NATIVE_ARTIFACTS:
        raise ValueError("Unexpected native source/artifact set")
    for name, digest in record["source_sha256"].items():
        if lab.sha256(lab.ROOT / name) != digest:
            raise ValueError("Native source changed: " + name)
    for name, digest in record["artifact_sha256"].items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or lab.sha256(path) != digest:
            raise ValueError("Native artifact changed: " + name)


def assemble(native_record):
    build = json.loads((lab.ARTIFACTS / "ultra96-pl-bitstream.json").read_text())
    if not pl.evidence_current(build):
        raise ValueError("PL build is not source/artifact-current; run make fpga")
    native = json.loads(native_record.read_text())
    validate_native(native, native_record.parent)
    build_directory = lab.ROOT / build["directory"]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    output = lab.ARTIFACTS / "hardware" / ("pl-bundle-" + stamp)
    output.mkdir(mode=0o700, parents=True)
    wanted = {name: (build_directory / name, build["artifact_sha256"][name]) for name in
              ("framefx.bit.bin", "framefx.dtbo", "framefx-afi.dtbo", "platform.json")}
    wanted.update({name: (native_record.parent / name, native["artifact_sha256"][name])
                   for name in ("framefx.ko", "framefx-test", "framefx-probe")})
    for name, (path, digest) in wanted.items():
        shutil.copyfile(path, output / name)
        if lab.sha256(output / name) != digest:
            raise ValueError("Payload changed during assembly: " + name)
    shutil.copyfile(lab.ROOT / "linux/hardware/ultra96-v1/pl_load.py", output / "pl_load.py")
    manifest = dict(schema=1, files={name: digest for name, (_, digest) in wanted.items()},
                    build_record_sha256=lab.sha256(build_directory / "manifest.json"),
                    native_record_sha256=lab.sha256(native_record), loader_sha256=lab.sha256(output / "pl_load.py"),
                    hardware_loaded=False)
    if not pl.evidence_current(build):
        raise ValueError("Build changed during assembly")
    validate_native(native, native_record.parent)
    lab.write_json(output / "load-manifest.json", manifest)
    print(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Assemble only current, verified PL and native board artifacts; never connects to hardware")
    parser.add_argument("--native-record", type=Path, required=True)
    assemble(parser.parse_args().native_record.resolve())

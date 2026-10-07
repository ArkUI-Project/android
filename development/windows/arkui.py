#!/usr/bin/env python3
"""Windows CLI and WSL worker for locally built ArkUI emulator ROMs."""

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import select
import shlex
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import zipfile


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "manifest" / "development" / "windows"
STATE = ROOT / ".state"
LOGS = ROOT / "logs"
FORKS = {
    "android": "android",
    "frameworks/base": "android_frameworks_base",
    "packages/apps/Settings": "android_packages_apps_Settings",
    "packages/apps/Launcher3": "android_packages_apps_Launcher3",
    "vendor/lineage": "android_vendor_lineage",
    "build/make": "android_build",
    "build/soong": "android_build_soong",
}
REQUIRED_IMAGES = ("system.img", "ramdisk.img", "userdata.img", "kernel-ranchu")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def config():
    cfg = read_json(ROOT / "arkui.config.json")
    if cfg["device"] != "sdk_phone_x86_64":
        raise RuntimeError("This Windows VM workflow requires sdk_phone_x86_64.")
    if cfg["variant"] not in ("eng", "userdebug"):
        raise RuntimeError("Use eng or userdebug for local ROM development.")
    if not cfg["source_dir"].startswith("/home/") or "/../" in cfg["source_dir"]:
        raise RuntimeError("source_dir must be an absolute path under /home in WSL.")
    if cfg["manifest_url"] != "https://github.com/ArkUI-Project/android.git":
        raise RuntimeError("The ROM must use the ArkUI-Project manifest.")
    for key in ("sync_jobs", "build_jobs", "emulator_memory_mb", "emulator_cores", "boot_timeout_seconds"):
        if not isinstance(cfg[key], int) or cfg[key] <= 0:
            raise RuntimeError(f"{key} must be a positive integer.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cfg["avd_name"]):
        raise RuntimeError("Invalid AVD name.")
    port = cfg["emulator_port"]
    if not isinstance(port, int) or port % 2 or not 5554 <= port <= 5682:
        raise RuntimeError("emulator_port must be even and between 5554 and 5682.")
    return cfg


def capture(args, *, cwd=None, env=None, timeout=60, check=True):
    result = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                            timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {shlex.join(map(str, args))}\n"
                           f"{result.stderr.strip()}\n{result.stdout.strip()}")
    return result.stdout.strip()


def run(args, *, cwd=None, env=None, log=None, heartbeat_seconds=60):
    LOGS.mkdir(parents=True, exist_ok=True)
    print("+ " + shlex.join(map(str, args)), flush=True)
    with contextlib.ExitStack() as stack:
        output = stack.enter_context((LOGS / log).open("a", encoding="utf-8")) if log else None
        if output:
            output.write(f"\n[{now()}] {shlex.join(map(str, args))}\n")
            output.flush()
        process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                                   start_new_session=os.name != "nt")
        lines = queue.Queue()

        def read_output():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                process.stdout.close()
                lines.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        started = last_output = time.monotonic()

        def emit(line):
            print(line, end="", flush=True)
            if output:
                output.write(line)
                output.flush()

        def terminate():
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.terminate()

        try:
            while True:
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    # repo may exit while failed network children still hold its stdout pipe.
                    if process.poll() not in (None, 0):
                        terminate()
                    elif time.monotonic() - last_output >= heartbeat_seconds:
                        elapsed = int(time.monotonic() - started)
                        emit(f"Still running ({elapsed}s elapsed)."
                             + (f" Log: logs/{log}" if log else "") + "\n")
                        last_output = time.monotonic()
                    continue
                if line is None:
                    break
                emit(line)
                last_output = time.monotonic()
            code = process.wait()
        except BaseException:
            terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        if code:
            raise RuntimeError(f"Command failed with exit code {code}. See logs/{log or '(console)' }.")


@contextlib.contextmanager
def operation_lock():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "operation.lock").open("a+b") as stream:
        stream.seek(0)
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another ArkUI setup, sync, build, export or VM operation is running.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def stage(name, function):
    write_json(STATE / "stage.json", {"stage": name, "status": "running", "started_at": now()})
    try:
        function()
    except BaseException as exc:
        write_json(STATE / "stage.json", {"stage": name, "status": "failed", "ended_at": now(),
                                          "error": str(exc)})
        raise
    write_json(STATE / "stage.json", {"stage": name, "status": "complete", "ended_at": now()})


def linux_env(cfg):
    env = os.environ.copy()
    env.update({"PATH": f"{Path.home()}/.local/bin:" + env["PATH"],
                "ARKUI_SOURCE": cfg["source_dir"], "ARKUI_DEVICE": cfg["device"],
                "ARKUI_VARIANT": cfg["variant"], "ARKUI_BUILD_JOBS": str(cfg["build_jobs"]),
                "ARKUI_CCACHE_SIZE": cfg["ccache_size"],
                "ARKUI_PRODUCT_OUT_FILE": str(STATE / "product-out.txt"),
                "GIT_TERMINAL_PROMPT": "0"})
    # The fallback identity applies only to this process, not the user's Git config.
    if not capture(["git", "config", "--get", "user.email"], check=False):
        env.update({"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "user.name",
                    "GIT_CONFIG_VALUE_0": "ArkUI Local Builder", "GIT_CONFIG_KEY_1": "user.email",
                    "GIT_CONFIG_VALUE_1": "builder@localhost"})
    return env


def repo_command():
    path = Path.home() / ".local/bin/repo"
    if not path.is_file():
        raise RuntimeError("repo is missing. Run arkui.ps1 setup.")
    return str(path)


def linux_init(cfg):
    source = Path(cfg["source_dir"])
    source.mkdir(parents=True, exist_ok=True)
    filesystem = capture(["stat", "-f", "-c", "%T", str(source)])
    if filesystem in ("9p", "drvfs", "fuseblk", "ntfs", "ntfs3"):
        raise RuntimeError("Android source must be on the WSL Linux filesystem, not NTFS/DrvFS.")
    guidance = source / "AGENTS.md"
    if not guidance.exists() and not guidance.is_symlink():
        guidance.symlink_to(ROOT / "AGENTS.md")
    sync_script = source / "sync-device.sh"
    if not sync_script.exists() and not sync_script.is_symlink():
        sync_script.symlink_to(TOOLS / "sync-device.sh")
    launcher = Path.home() / ".local/bin/repo"
    if not launcher.exists():
        launcher.parent.mkdir(parents=True, exist_ok=True)
        temporary = launcher.with_suffix(".download")
        run(["curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "20",
             "https://storage.googleapis.com/git-repo-downloads/repo", "--output", str(temporary)],
            log="setup.log")
        temporary.chmod(0o755)
        temporary.replace(launcher)
    env = linux_env(cfg)
    run(["git", "lfs", "install", "--skip-repo"], env=env, log="setup.log")
    if not (source / ".repo/manifest.xml").exists():
        run([str(launcher), "init", "-u", cfg["manifest_url"], "-b", cfg["branch"],
             "--git-lfs", "--partial-clone", "--clone-filter=blob:none",
             "--no-clone-bundle", "--platform=linux"], cwd=source, env=env, log="setup.log")
    else:
        origin = capture(["git", "-C", str(source / ".repo/manifests"),
                          "config", "--get", "remote.origin.url"])
        if origin.rstrip("/") != cfg["manifest_url"]:
            raise RuntimeError(f"Existing source has a different manifest: {origin}")
        revision = capture(["git", "-C", str(source / ".repo/manifests.git"),
                            "config", "--get", "branch.default.merge"])
        if revision != "refs/heads/" + cfg["branch"]:
            raise RuntimeError(f"Existing source is on {revision}; config requests {cfg['branch']}.")
    run([str(launcher), "manifest", "-o", str(LOGS / "resolved-manifest.xml")],
        cwd=source, env=env, log="setup.log")
    write_json(STATE / "source.json", {"source_dir": str(source), "manifest_url": cfg["manifest_url"],
                                       "branch": cfg["branch"], "initialized_at": now()})


def configure_forks(cfg):
    source = Path(cfg["source_dir"])
    for relative, name in FORKS.items():
        path = source / relative
        if not (path / ".git").exists():
            continue
        expected = f"https://github.com/ArkUI-Project/{name}"
        remote = capture(["git", "-C", str(path), "config", "--get", "remote.arkui.url"])
        if remote.removesuffix(".git").rstrip("/") != expected:
            raise RuntimeError(f"{relative} is not the ArkUI fork: {remote}")
        settings = {"remote.upstream.url": f"https://github.com/LineageOS/{name}.git",
                    "remote.upstream.fetch": "+refs/heads/*:refs/remotes/upstream/*",
                    "remote.arkui.pushurl": f"git@github.com:ArkUI-Project/{name}.git",
                    "remote.pushDefault": "arkui"}
        for key, value in settings.items():
            capture(["git", "-C", str(path), "config", key, value])


def linux_sync(cfg, projects=None):
    linux_init(cfg)
    source = Path(cfg["source_dir"])
    env = linux_env(cfg)
    stamp = STATE / "sync-success.json"
    stamp.unlink(missing_ok=True)
    command = [repo_command(), "sync", "-c", f"-j{cfg['sync_jobs']}", "--no-tags"]
    for attempt in range(1, 4):
        try:
            run(command + (projects or []), cwd=source, env=env, log="sync.log")
            break
        except RuntimeError:
            if attempt == 3:
                raise
            print(f"Source sync attempt {attempt} failed; retrying in 15 seconds with cached objects.", flush=True)
            time.sleep(15)
    configure_forks(cfg)
    if projects:
        print("Selected projects synced. A full sync is still required before a ROM build.")
        return
    lock = LOGS / "source-lock.xml"
    run([repo_command(), "manifest", "-r", "-o", str(lock)], cwd=source, env=env, log="sync.log")
    write_json(stamp, {"completed_at": now(), "branch": cfg["branch"],
                       "manifest_url": cfg["manifest_url"], "source_dir": str(source),
                       "source_lock_sha256": sha256(lock)})


def require_synced(cfg):
    stamp = STATE / "sync-success.json"
    if not stamp.is_file():
        raise RuntimeError("Full source synchronization has not completed. Run arkui.ps1 sync.")
    proof = read_json(stamp)
    if (proof["source_dir"] != cfg["source_dir"] or proof["branch"] != cfg["branch"]
            or proof["manifest_url"] != cfg["manifest_url"]
            or proof["source_lock_sha256"] != sha256(LOGS / "source-lock.xml")):
        raise RuntimeError("Source configuration changed. Run a full source sync again.")


def linux_build(cfg, modules=None, check_only=False):
    require_synced(cfg)
    env = linux_env(cfg)
    source = Path(cfg["source_dir"])
    args = ["bash", str(TOOLS / "build.sh")]
    if check_only:
        run(args + ["check"], env=env, log="check.log")
        return
    if modules:
        run(args + ["modules"] + modules, env=env, log="build.log")
        return
    (STATE / "build-success.json").unlink(missing_ok=True)
    lock = LOGS / "build-source-lock.xml"
    run([repo_command(), "manifest", "-r", "-o", str(lock)], cwd=source, env=env, log="build.log")
    with (LOGS / "build-source.diff").open("w", encoding="utf-8") as stream:
        subprocess.run([repo_command(), "diff"], cwd=source, env=env,
                       stdout=stream, stderr=subprocess.STDOUT, check=True)
    run(args, env=env, log="build.log")
    product_out = Path((STATE / "product-out.txt").read_text().strip())
    if not product_out.is_absolute():
        product_out = source / product_out
    image_zip = product_out / "sdk-repo-linux-system-images.zip"
    if not image_zip.is_file():
        raise RuntimeError(f"Build did not produce the emulator image archive: {image_zip}")
    build_properties = {}
    for relative in ("system/build.prop", "system/etc/build.prop", "vendor/build.prop",
                     "vendor/etc/build.prop", "product/build.prop", "product/etc/build.prop"):
        path = product_out / relative
        if path.is_file():
            build_properties.update(properties(path))
    if not build_properties.get("ro.lineage.version"):
        raise RuntimeError("Build output does not declare ro.lineage.version.")
    userdata = product_out / "userdata.img"
    if not userdata.is_file():
        raise RuntimeError("Build output is missing userdata.img.")
    build_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    proof = {"schema": 1, "local_build": True, "build_id": build_id,
             "completed_at": now(), "source_dir": str(source),
             "manifest_url": cfg["manifest_url"], "branch": cfg["branch"],
             "device": cfg["device"], "variant": cfg["variant"],
             "product_out": str(product_out), "image_zip": str(image_zip),
             "image_zip_sha256": sha256(image_zip), "source_lock_sha256": sha256(lock),
             "source_diff_sha256": sha256(LOGS / "build-source.diff"),
             "userdata_sha256": sha256(userdata),
             "lineage_version": build_properties["ro.lineage.version"],
             "fingerprint": build_properties.get("ro.build.fingerprint", "")}
    write_json(STATE / "build-success.json", proof)
    print(f"ROM build complete: {image_zip}")


def properties(path):
    result = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def extract_images(image_zip, destination):
    with zipfile.ZipFile(image_zip) as archive:
        candidates = [info for info in archive.infolist()
                      if PurePosixPath(info.filename).name == "system.img"]
        if len(candidates) != 1:
            raise RuntimeError("Expected one locally built system.img in the emulator archive.")
        prefix = PurePosixPath(candidates[0].filename).parent
        if prefix.name != "x86_64":
            raise RuntimeError(f"Wrong emulator ABI in build archive: {prefix}")
        destination.mkdir(parents=True, exist_ok=False)
        for info in archive.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in info.filename or ":" in info.filename:
                raise RuntimeError("Unsafe path in emulator image archive.")
            try:
                relative = name.relative_to(prefix)
            except ValueError:
                continue
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError("Unsafe extracted path in emulator image archive.")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as origin, target.open("wb") as output:
                    shutil.copyfileobj(origin, output, 8 * 1024 * 1024)
    for name in ("system.img", "vendor.img", "ramdisk.img", "kernel-ranchu", "source.properties", "build.prop"):
        if not (destination / name).is_file() or (destination / name).stat().st_size == 0:
            raise RuntimeError(f"Local ROM image archive is missing {name}.")
    source_properties = properties(destination / "source.properties")
    if source_properties.get("SystemImage.Abi") != "x86_64":
        raise RuntimeError("Exported ROM is not x86_64.")
    if source_properties.get("SystemImage.TagId") != "lineage":
        raise RuntimeError("Exported ROM is not a LineageOS emulator image.")
    build_properties = properties(destination / "build.prop")
    return source_properties, build_properties


def linux_export(cfg):
    require_synced(cfg)
    path = STATE / "build-success.json"
    if not path.is_file():
        raise RuntimeError("No successful local ROM build. Run arkui.ps1 build first.")
    proof = read_json(path)
    if (proof.get("local_build") is not True or proof["source_dir"] != cfg["source_dir"]
            or proof["branch"] != cfg["branch"] or proof["device"] != cfg["device"]
            or proof["variant"] != cfg["variant"] or proof["manifest_url"] != cfg["manifest_url"]):
        raise RuntimeError("Build receipt does not match the configured ArkUI ROM.")
    image_zip = Path(proof["image_zip"])
    if sha256(image_zip) != proof["image_zip_sha256"]:
        raise RuntimeError("Build archive changed since the successful build.")
    if sha256(LOGS / "build-source-lock.xml") != proof["source_lock_sha256"]:
        raise RuntimeError("Build source revision record changed.")
    if sha256(LOGS / "build-source.diff") != proof["source_diff_sha256"]:
        raise RuntimeError("Build source diff record changed.")
    userdata = Path(proof["product_out"]) / "userdata.img"
    if sha256(userdata) != proof["userdata_sha256"]:
        raise RuntimeError("Userdata image changed since the successful build.")
    artifact = ROOT / "artifacts" / (proof["build_id"] + "-" + str(time.time_ns()))
    image = artifact / "image"
    source_properties, build_properties = extract_images(image_zip, image)
    # The upstream zip contains data/ but not userdata.img; use the same build's image.
    shutil.copy2(userdata, image / "userdata.img")
    shutil.copy2(LOGS / "build-source-lock.xml", artifact / "source-lock.xml")
    shutil.copy2(LOGS / "build-source.diff", artifact / "source.diff")
    receipt = dict(proof, image_directory="image", exported_at=now(),
                   api_level=source_properties["AndroidVersion.ApiLevel"],
                   lineage_version=proof["lineage_version"], fingerprint=proof["fingerprint"],
                   files={str(file.relative_to(image)).replace(os.sep, "/"): sha256(file)
                          for file in image.rglob("*") if file.is_file()})
    write_json(artifact / "build.json", receipt)
    write_json(STATE / "current-artifact.json", {"path": str(artifact.relative_to(ROOT))})
    print(f"Local ROM exported: {artifact}")


def windows_path_to_wsl(path, cfg):
    return capture(["wsl", "-d", cfg["distro"], "--exec", "wslpath", "-a", "-u", str(path)])


def worker(action, cfg, extra=None, root=False):
    script = windows_path_to_wsl(TOOLS / "arkui.py", cfg)
    args = ["wsl", "-d", cfg["distro"]]
    if root:
        args += ["-u", "root"]
    args += ["--exec", "python3", script, "--linux-worker", action] + (extra or [])
    code = subprocess.call(args)
    if code:
        raise RuntimeError(f"WSL {action} failed with exit code {code}.")


def sdk_tool(cfg, relative):
    tool = Path(cfg["sdk_dir"]) / relative
    if not tool.is_file():
        raise RuntimeError(f"Android SDK tool missing: {tool}. Install the tool only, not a system image.")
    return str(tool)


def verify_artifact(cfg):
    pointer = STATE / "current-artifact.json"
    if not pointer.is_file():
        raise RuntimeError("No locally built ROM is registered. Run arkui.ps1 up or build then export.")
    relative = PurePosixPath(read_json(pointer)["path"])
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "artifacts":
        raise RuntimeError("Invalid local artifact path.")
    artifact = ROOT.joinpath(*relative.parts).resolve()
    if not artifact.is_relative_to((ROOT / "artifacts").resolve()):
        raise RuntimeError("ROM artifact must be inside this workspace.")
    proof = read_json(artifact / "build.json")
    if (proof.get("schema") != 1 or proof.get("local_build") is not True
            or proof.get("manifest_url") != cfg["manifest_url"]
            or proof.get("device") != cfg["device"] or proof.get("branch") != cfg["branch"]
            or proof.get("variant") != cfg["variant"] or proof.get("image_directory") != "image"
            or not proof.get("lineage_version")):
        raise RuntimeError("ROM provenance does not match a local ArkUI build.")
    image = artifact / "image"
    for name in REQUIRED_IMAGES + ("source.properties", "build.prop"):
        if name not in proof["files"]:
            raise RuntimeError(f"Unverified ROM file: {name}")
    for name, expected in proof["files"].items():
        relative_file = PurePosixPath(name)
        if relative_file.is_absolute() or ".." in relative_file.parts or "\\" in name:
            raise RuntimeError("Invalid file path in ROM receipt.")
        path = image.joinpath(*relative_file.parts).resolve()
        if not path.is_relative_to(image.resolve()) or sha256(path) != expected:
            raise RuntimeError(f"ROM file changed or escaped the image directory: {name}")
    if sha256(artifact / "source-lock.xml") != proof["source_lock_sha256"]:
        raise RuntimeError("ROM source lock does not match the build receipt.")
    if sha256(artifact / "source.diff") != proof["source_diff_sha256"]:
        raise RuntimeError("ROM source diff does not match the build receipt.")
    print(f"Verified local ROM: {proof['lineage_version']}")
    return artifact, image, proof


def avd_environment(cfg):
    env = os.environ.copy()
    env.update({"ANDROID_HOME": cfg["sdk_dir"], "ANDROID_SDK_ROOT": cfg["sdk_dir"],
                "ANDROID_AVD_HOME": str(ROOT / "emulator" / "avd")})
    return env


def create_avd(cfg, image, proof):
    home = ROOT / "emulator" / "avd"
    directory = home / (cfg["avd_name"] + ".avd")
    directory.mkdir(parents=True, exist_ok=True)
    previous = directory / "arkui-build.json"
    compatibility = {key: proof[key] for key in ("api_level", "branch", "device", "variant")}
    if previous.exists() and read_json(previous).get("compatibility") != compatibility:
        raise RuntimeError("The AVD contains data from an incompatible ROM configuration. Use a new "
                           "avd_name, or run start --wipe-data to explicitly reset its data.")
    entries = {"avd.ini.encoding": "UTF-8", "AvdId": cfg["avd_name"],
               "avd.ini.displayname": "ArkUI Local ROM", "abi.type": "x86_64",
               "hw.cpu.arch": "x86_64", "hw.cpu.ncore": str(cfg["emulator_cores"]),
               "hw.ramSize": str(cfg["emulator_memory_mb"]), "hw.keyboard": "yes",
               "hw.mainKeys": "no", "hw.dPad": "no",
               "hw.lcd.width": "1080", "hw.lcd.height": "2400", "hw.lcd.density": "420",
               "hw.gpu.enabled": "yes", "hw.gpu.mode": cfg["gpu"],
               "hw.gltransport": "asg",
               "hw.camera.back": "none", "hw.camera.front": "none",
               "disk.dataPartition.size": "8G", "image.sysdir.1": image.as_posix() + "/",
               "tag.id": "lineage", "tag.display": "ArkUI Local ROM",
               "target": "android-" + proof["api_level"], "PlayStore.enabled": "false",
               "showDeviceFrame": "no", "fastboot.forceColdBoot": "yes"}
    (directory / "config.ini").write_text("".join(f"{key}={value}\n" for key, value in entries.items()),
                                         encoding="utf-8")
    (home / (cfg["avd_name"] + ".ini")).write_text(
        f"avd.ini.encoding=UTF-8\npath={directory}\ntarget=android-{proof['api_level']}\n",
        encoding="utf-8")
    write_json(previous, {"image_zip_sha256": proof["image_zip_sha256"], "build_id": proof["build_id"],
                          "compatibility": compatibility})
    return directory


def adb_command(cfg, args, timeout=30, check=True):
    return capture([sdk_tool(cfg, "platform-tools/adb.exe"), "-s", f"emulator-{cfg['emulator_port']}"]
                   + args, timeout=timeout, check=check)


def managed_vm(cfg):
    path = STATE / "emulator.json"
    if not path.is_file():
        raise RuntimeError("No managed VM is recorded. Start the local ROM with arkui.ps1 start.")
    saved = read_json(path)
    if (saved.get("serial") != f"emulator-{cfg['emulator_port']}"
            or saved.get("avd_name") != cfg["avd_name"]):
        raise RuntimeError("VM config changed; restore its port/name before device synchronization.")
    return saved


def bridge_matches(bridge, saved):
    return (bridge.get("vm_pid") == saved["pid"]
            and bridge.get("vm_started_at") == saved["started_at"]
            and bridge.get("serial") == saved["serial"])


def vm_version(saved):
    path = STATE / "device-sync-success.json"
    if path.is_file():
        sync = read_json(path)
        if bridge_matches(sync, saved):
            return sync["lineage_version"]
    return saved["lineage_version"]


class AdbRelay(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


class AdbRelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if self.client_address[0] != "127.0.0.1":
            return
        process = None
        try:
            process = subprocess.Popen(self.server.tunnel_command, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=sys.stderr)
            sockets = (self.request, process.stdout)
            while not self.server.stopping.is_set():
                readable, _, _ = select.select(sockets, [], [], 1)
                for source in readable:
                    if source is self.request:
                        data = source.recv(65536)
                        if not data:
                            return
                        process.stdin.write(data)
                        process.stdin.flush()
                    else:
                        data = os.read(source.fileno(), 65536)
                        if not data:
                            return
                        self.request.sendall(data)
        except OSError as exc:
            print(f"ADB relay connection ended: {exc}", flush=True)
        finally:
            if process:
                process.stdin.close()
                process.stdout.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def windows_adb_tunnel():
    import msvcrt

    # Only transport bytes here; the Linux ADB client alone reads PRODUCT_OUT.
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    with socket.create_connection(("127.0.0.1", 5037), timeout=5) as upstream:
        upstream.settimeout(None)

        def forward_input():
            try:
                while data := os.read(sys.stdin.fileno(), 65536):
                    upstream.sendall(data)
                upstream.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        threading.Thread(target=forward_input, daemon=True).start()
        while data := upstream.recv(65536):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


def linux_adb_bridge(cfg, windows_python, windows_script, owner_pid):
    saved = managed_vm(cfg)
    record = STATE / "adb-bridge.json"
    with AdbRelay(("127.0.0.1", 0), AdbRelayHandler) as relay:
        relay.tunnel_command = [windows_python, windows_script, "--adb-tunnel"]
        relay.stopping = threading.Event()
        relay.timeout = 1

        def parent_closed():
            os.read(sys.stdin.fileno(), 1)
            relay.stopping.set()

        threading.Thread(target=parent_closed, daemon=True).start()
        write_json(record, {"pid": owner_pid, "linux_pid": os.getpid(), "vm_pid": saved["pid"],
                   "vm_started_at": saved["started_at"], "serial": saved["serial"],
                   "transport": "wsl-stdio", "host": "127.0.0.1", "port": relay.server_address[1]})
        print(f"WSL loopback ADB bridge: 127.0.0.1:{relay.server_address[1]} (WSL interop pipes)")
        try:
            while not relay.stopping.is_set():
                if (not (STATE / "emulator.json").is_file()
                        or not bridge_matches(read_json(record), read_json(STATE / "emulator.json"))):
                    break
                relay.handle_request()
        finally:
            relay.stopping.set()
            if record.is_file() and read_json(record).get("pid") == owner_pid:
                record.unlink()


def windows_adb_bridge(cfg, vm_pid):
    import ctypes
    from ctypes import wintypes

    saved = managed_vm(cfg)
    if saved["pid"] != vm_pid:
        raise RuntimeError("The managed VM changed before the ADB bridge started.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x00100000, False, vm_pid)
    if not handle:
        raise RuntimeError("The recorded emulator process is not running.")
    process = None
    try:
        python = windows_path_to_wsl(Path(sys.executable), cfg)
        script = windows_path_to_wsl(TOOLS / "arkui.py", cfg)
        process = subprocess.Popen(["wsl", "-d", cfg["distro"], "--exec", "python3", script,
                                   "--linux-adb-bridge", python, str(TOOLS / "arkui.py"), str(os.getpid())],
                                   stdin=subprocess.PIPE, stdout=sys.stdout, stderr=sys.stderr,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        while kernel.WaitForSingleObject(handle, 1000) == 0x00000102:
            if process.poll() is not None or not (STATE / "emulator.json").is_file():
                break
            if read_json(STATE / "emulator.json").get("started_at") != saved["started_at"]:
                break
    finally:
        kernel.CloseHandle(handle)
        if process:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait()


def ensure_adb_bridge(cfg):
    saved = managed_vm(cfg)
    if cfg["avd_name"] not in adb_command(cfg, ["emu", "avd", "name"]).splitlines():
        raise RuntimeError("The recorded ArkUI AVD is not online; refusing to create a device bridge.")
    capture([sdk_tool(cfg, "platform-tools/adb.exe"), "-H", "127.0.0.1", "-P", "5037", "start-server"])
    record = STATE / "adb-bridge.json"

    def ready():
        if not record.is_file():
            return False
        bridge = read_json(record)
        return bridge_matches(bridge, saved) and bridge.get("transport") == "wsl-stdio"

    if not ready():
        LOGS.mkdir(parents=True, exist_ok=True)
        with (LOGS / "adb-bridge.log").open("a", encoding="utf-8") as output:
            process = subprocess.Popen([sys.executable, str(TOOLS / "arkui.py"), "--adb-bridge",
                                        str(saved["pid"])], stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 30
        while not ready():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("WSL ADB bridge did not start. See logs/adb-bridge.log; "
                                   "the ROM and VM data were left unchanged.")
            time.sleep(0.25)
    bridge = read_json(record)
    print(f"WSL ADB bridge ready: {bridge['host']}:{bridge['port']} ({saved['serial']})")


def linux_device_env(cfg):
    saved = managed_vm(cfg)
    path = STATE / "adb-bridge.json"
    if not path.is_file() or not bridge_matches(read_json(path), saved):
        raise RuntimeError("The WSL ADB bridge is missing or stale. Restart the managed VM with "
                           "arkui.ps1 start, or run arkui.ps1 bridge once for an existing VM.")
    bridge = read_json(path)
    if (bridge.get("transport") != "wsl-stdio" or bridge["host"] != "127.0.0.1"
            or not isinstance(bridge["port"], int) or not 1 <= bridge["port"] <= 65535):
        raise RuntimeError("Invalid loopback ADB bridge endpoint.")
    executable = Path(cfg["source_dir"]) / "out/host/linux-x86/bin/adb"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError(f"Native ADB is missing: {executable}. Build the adb module in Ubuntu first.")
    env = linux_env(cfg)
    env.update({"ARKUI_ADB": str(executable), "ANDROID_SERIAL": saved["serial"],
                "ADB_SERVER_SOCKET": f"tcp:127.0.0.1:{bridge['port']}",
                "ARKUI_EXPECTED_VERSION": vm_version(saved),
                "ARKUI_BOOT_TIMEOUT": str(cfg["boot_timeout_seconds"])})
    return env


def wait_for_boot(cfg, proof, process=None):
    deadline = time.monotonic() + cfg["boot_timeout_seconds"]
    while time.monotonic() < deadline:
        if process is not None and process.poll() not in (None, 0):
            raise RuntimeError(f"Emulator exited with code {process.returncode}. See logs/emulator.log.")
        try:
            completed = adb_command(cfg, ["shell", "getprop", "sys.boot_completed"], check=False, timeout=10)
            if completed == "1":
                version = adb_command(cfg, ["shell", "getprop", "ro.lineage.version"])
                fingerprint = adb_command(cfg, ["shell", "getprop", "ro.build.fingerprint"])
                if version != proof["lineage_version"]:
                    raise RuntimeError(f"Booted ROM does not match the local build: {version}")
                if proof.get("fingerprint") and fingerprint != proof["fingerprint"]:
                    raise RuntimeError("Booted ROM fingerprint differs from the local image.")
                write_json(STATE / "boot-success.json", {"verified_at": now(),
                           "lineage_version": version, "fingerprint": fingerprint,
                           "build_id": proof["build_id"], "serial": f"emulator-{cfg['emulator_port']}"})
                print(f"Local ROM boot verified: {version}")
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(3)
    raise RuntimeError("ROM did not finish booting within the timeout. See logs/emulator.log and "
                       "arkui.ps1 adb logcat. The emulator is left running for diagnosis.")


def windows_start(cfg, *, wipe=False, headless=False, no_wait=False):
    artifact, image, proof = verify_artifact(cfg)
    emulator = sdk_tool(cfg, "emulator/emulator.exe")
    acceleration = capture([emulator, "-accel-check"])
    if not re.search(r"(?m)^0\s*$", acceleration):
        raise RuntimeError(f"Emulator hardware acceleration unavailable:\n{acceleration}")
    for port in (cfg["emulator_port"], cfg["emulator_port"] + 1):
        with socket.socket() as check_port:
            try:
                check_port.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"Emulator port {port} is occupied. Stop that VM or change emulator_port.") from exc
    directory = ROOT / "emulator/avd" / (cfg["avd_name"] + ".avd")
    previous = directory / "arkui-build.json"
    if wipe:
        previous.unlink(missing_ok=True)
    create_avd(cfg, image, proof)
    (STATE / "boot-success.json").unlink(missing_ok=True)
    args = [emulator, "-avd", cfg["avd_name"], "-sysdir", str(image),
            "-system", str(image / "system.img"), "-kernel", str(image / "kernel-ranchu"),
            "-ramdisk", str(image / "ramdisk.img"), "-initdata", str(image / "userdata.img"),
            "-port", str(cfg["emulator_port"]), "-accel", "on", "-gpu", cfg["gpu"],
            "-memory", str(cfg["emulator_memory_mb"]), "-writable-system", "-no-snapshot",
            "-no-audio", "-verbose"]
    if (image / "vendor.img").is_file():
        args += ["-vendor", str(image / "vendor.img")]
    if wipe:
        args.append("-wipe-data")
    if headless:
        args.append("-no-window")
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / "emulator.log").open("a", encoding="utf-8") as output:
        output.write(f"\n[{now()}] {shlex.join(args)}\n")
        output.flush()
        process = subprocess.Popen(args, env=avd_environment(cfg), stdout=output,
                                   stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    write_json(STATE / "emulator.json", {"pid": process.pid, "started_at": now(),
               "serial": f"emulator-{cfg['emulator_port']}", "port": cfg["emulator_port"],
               "avd_name": cfg["avd_name"], "artifact": str(artifact), "lineage_version": proof["lineage_version"]})
    print(f"Emulator started from local ROM; log: {LOGS / 'emulator.log'}")
    if not no_wait:
        wait_for_boot(cfg, proof, process)
        ensure_adb_bridge(cfg)
    else:
        print("Use arkui.ps1 bridge after the VM is online to enable WSL device synchronization.")


def windows_stop(cfg):
    path = STATE / "emulator.json"
    if not path.is_file():
        raise RuntimeError("No emulator managed by this workspace has been recorded.")
    saved = read_json(path)
    if saved["port"] != cfg["emulator_port"] or saved["avd_name"] != cfg["avd_name"]:
        raise RuntimeError("VM config changed. Restore its port/name before stopping it.")
    with socket.socket() as connection:
        connection.settimeout(1)
        if connection.connect_ex(("127.0.0.1", cfg["emulator_port"])):
            path.unlink()
            print("The recorded emulator is already stopped.")
            return
    version = adb_command(cfg, ["shell", "getprop", "ro.lineage.version"], check=False)
    if version and version != vm_version(saved):
        raise RuntimeError("The device on this port is a different ROM; refusing to stop it.")
    result = adb_command(cfg, ["emu", "avd", "name"], check=False)
    if cfg["avd_name"] not in result.splitlines():
        raise RuntimeError("The expected ArkUI AVD is not running on this port.")
    print(adb_command(cfg, ["emu", "kill"]))
    path.unlink()


def windows_setup(cfg):
    sdk_tool(cfg, "emulator/emulator.exe")
    sdk_tool(cfg, "platform-tools/adb.exe")
    worker("deps", cfg, root=True)
    worker("init", cfg)


def windows_doctor(cfg):
    print(f"Workspace: {ROOT}\nWSL distribution: {cfg['distro']}\nSource: {cfg['source_dir']}")
    print(f"Source in Explorer: \\\\wsl.localhost\\{cfg['distro']}" + cfg["source_dir"].replace("/", "\\"))
    print(f"ROM: {cfg['branch']} / {cfg['device']} / {cfg['variant']} / -j{cfg['build_jobs']}")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss") as distros:
            for index in range(winreg.QueryInfoKey(distros)[0]):
                with winreg.OpenKey(distros, winreg.EnumKey(distros, index)) as distro:
                    if winreg.QueryValueEx(distro, "DistributionName")[0] == cfg["distro"]:
                        base = winreg.QueryValueEx(distro, "BasePath")[0]
                        print(f"WSL backing disk: {base}; host free: {shutil.disk_usage(base).free / 2**30:.1f} GiB")
    except (OSError, ImportError):
        print("WSL backing disk could not be inspected; check host disk space separately.")
    failures = []
    try:
        worker("doctor", cfg)
    except RuntimeError as exc:
        failures.append(str(exc))
    try:
        emulator = sdk_tool(cfg, "emulator/emulator.exe")
        print(capture([emulator, "-accel-check"]))
        sdk_tool(cfg, "platform-tools/adb.exe")
    except RuntimeError as exc:
        failures.append(str(exc))
    for file in ("stage.json", "sync-success.json", "build-success.json", "current-artifact.json", "boot-success.json"):
        path = STATE / file
        if path.is_file():
            print(f"{file}: {json.dumps(read_json(path), ensure_ascii=True)}")
    if not (STATE / "current-artifact.json").is_file():
        print("No compiled ROM is registered. VM startup will refuse until build/export succeeds.")
    if failures:
        raise RuntimeError("\n".join(failures))


def linux_doctor(cfg):
    print(capture(["uname", "-srmo"]))
    print(capture(["free", "-h"]))
    print(capture(["df", "-h", str(Path(cfg["source_dir"]).parent)]))
    missing = [name for name in ("git", "git-lfs", "ccache", "curl", "make", "gcc", "g++", "ninja", "rg", "unzip")
               if not shutil.which(name)]
    if missing:
        raise RuntimeError("Missing build tools: " + ", ".join(missing))
    source = Path(cfg["source_dir"])
    if (source / ".repo/manifest.xml").exists():
        paths = capture([repo_command(), "list", "--all", "-p"], cwd=source, env=linux_env(cfg)).splitlines()
        checked_out = sum((source / name / ".git").exists() for name in paths)
        print(f"Source checkout progress: {checked_out}/{len(paths)} projects present")
    else:
        print("Source manifest has not been initialized.")
    print("WSL KVM access:", os.access("/dev/kvm", os.R_OK | os.W_OK), "(Windows WHPX runs this VM)")


def linux_worker(action, cfg, extra):
    if action == "deps":
        run(["bash", str(TOOLS / "bootstrap.sh")], log="setup.log")
    elif action == "init":
        linux_init(cfg)
    elif action == "sync":
        linux_sync(cfg, extra)
    elif action == "build":
        linux_build(cfg, extra)
    elif action == "check":
        linux_build(cfg, check_only=True)
    elif action == "export":
        linux_export(cfg)
    elif action == "doctor":
        linux_doctor(cfg)
    elif action in ("sync-device", "adb-sync"):
        arguments = (["--adb-sync"] if action == "adb-sync" else []) + extra
        env = linux_device_env(cfg)
        saved = managed_vm(cfg)
        run(["bash", str(TOOLS / "sync-device.sh")] + arguments,
            env=env, log="device-sync.log")
        dry_run = any(option in extra for option in ("--dry-run", "--help", "-h", "-l", "-n"))
        if not dry_run:
            if managed_vm(cfg)["started_at"] != saved["started_at"]:
                raise RuntimeError("The managed VM changed during synchronization; no success was recorded.")
            adb = [env["ARKUI_ADB"], "-s", saved["serial"]]
            write_json(STATE / "device-sync-success.json", {
                "vm_pid": saved["pid"], "vm_started_at": saved["started_at"], "serial": saved["serial"],
                "completed_at": now(), "source_dir": cfg["source_dir"], "arguments": extra,
                "lineage_version": capture(adb + ["shell", "getprop", "ro.lineage.version"], env=env),
                "fingerprint": capture(adb + ["shell", "getprop", "ro.build.fingerprint"], env=env),
                "boot_verified": action == "sync-device", "exported_rom_updated": False})
    elif action == "shell":
        env = linux_env(cfg)
        os.execvpe("bash", ["bash", "--init-file", str(TOOLS / "shell.sh"), "-i"], env)
    elif action == "branch":
        if len(extra) < 2:
            raise RuntimeError("Usage: arkui.ps1 branch NAME PROJECT [PROJECT ...]")
        run([repo_command(), "start"] + extra, cwd=cfg["source_dir"], env=linux_env(cfg), log="development.log")
    else:
        raise RuntimeError("Unknown WSL action: " + action)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "init", "sync", "check", "build", "export", "up", "dev",
                        "start", "stop", "verify", "doctor", "status", "shell", "code", "open", "branch", "adb", "logs",
                        "bridge", "sync-device"])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    cfg = config()
    extra = args.arguments
    if args.command in ("doctor", "status"):
        windows_doctor(cfg)
        return
    if args.command == "logs":
        log = extra[0] if extra else "sync"
        if log not in ("sync", "setup", "build", "check", "emulator", "development", "device-sync", "adb-bridge"):
            raise RuntimeError("Unknown log name.")
        path = LOGS / (log + ".log")
        if path.is_file():
            with path.open(encoding="utf-8", errors="replace") as stream:
                from collections import deque
                print("".join(deque(stream, maxlen=60)))
        else:
            print(f"No log yet: {path}")
        return
    if args.command == "open":
        os.startfile(f"\\\\wsl.localhost\\{cfg['distro']}" + cfg["source_dir"].replace("/", "\\"))
        return
    if args.command == "code":
        executable = shutil.which("code.cmd") or shutil.which("code")
        if not executable:
            raise RuntimeError("VS Code is not on PATH.")
        extensions = capture([executable, "--list-extensions"])
        if "ms-vscode-remote.remote-wsl" not in extensions.splitlines():
            run([executable, "--install-extension", "ms-vscode-remote.remote-wsl"])
        run([executable, "--remote", "wsl+" + cfg["distro"], cfg["source_dir"] + "/"])
        return
    if args.command == "shell":
        worker("shell", cfg)
        return
    if args.command == "adb":
        if extra and extra[0] == "sync":
            with operation_lock():
                ensure_adb_bridge(cfg)
                worker("adb-sync", cfg, extra[1:])
            return
        raise SystemExit(subprocess.call([sdk_tool(cfg, "platform-tools/adb.exe"), "-s",
                         f"emulator-{cfg['emulator_port']}"] + extra))
    if args.command in ("bridge", "sync-device"):
        with operation_lock():
            ensure_adb_bridge(cfg)
            if args.command == "sync-device":
                worker("sync-device", cfg, extra)
        return
    if args.command == "verify":
        verify_artifact(cfg)
        return
    with operation_lock():
        if args.command in ("up", "dev"):
            unknown = set(extra) - {"--wipe-data", "--headless", "--no-wait"}
            if unknown:
                raise RuntimeError("Unknown options: " + ", ".join(unknown))
            if (STATE / "emulator.json").is_file():
                stage("stop", lambda: windows_stop(cfg))
            if args.command == "up":
                stage("setup", lambda: windows_setup(cfg))
                stage("sync", lambda: worker("sync", cfg))
            stage("build", lambda: worker("build", cfg))
            stage("export", lambda: worker("export", cfg))
            stage("start", lambda: windows_start(cfg, wipe="--wipe-data" in extra,
                  headless="--headless" in extra, no_wait="--no-wait" in extra))
        elif args.command == "setup":
            stage("setup", lambda: windows_setup(cfg))
        elif args.command in ("init", "sync", "check", "build", "export", "branch"):
            stage(args.command, lambda: worker(args.command, cfg, extra))
        elif args.command == "start":
            unknown = set(extra) - {"--wipe-data", "--headless", "--no-wait"}
            if unknown:
                raise RuntimeError("Unknown start options: " + ", ".join(unknown))
            stage("start", lambda: windows_start(cfg, wipe="--wipe-data" in extra,
                  headless="--headless" in extra, no_wait="--no-wait" in extra))
        elif args.command == "stop":
            stage("stop", lambda: windows_stop(cfg))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "--adb-tunnel" and os.name == "nt":
            windows_adb_tunnel()
        elif len(sys.argv) == 5 and sys.argv[1] == "--linux-adb-bridge" and os.name != "nt":
            linux_adb_bridge(config(), sys.argv[2], sys.argv[3], int(sys.argv[4]))
        elif len(sys.argv) == 3 and sys.argv[1] == "--adb-bridge" and os.name == "nt":
            windows_adb_bridge(config(), int(sys.argv[2]))
        elif "--linux-worker" in sys.argv:
            position = sys.argv.index("--linux-worker")
            linux_worker(sys.argv[position + 1], config(), sys.argv[position + 2:])
        else:
            if os.name != "nt":
                raise RuntimeError("Run arkui.ps1 from PowerShell; it dispatches build work into WSL.")
            main()
    except KeyboardInterrupt:
        print("\nInterrupted. Repeat the same command to resume sync or incremental build.", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)

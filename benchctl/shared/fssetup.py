"""shared/fssetup.py — provision a filesystem for the SUT's data directory.

A container cannot format a filesystem. The filesystem under test therefore
has to live on the machine that runs the Docker daemon, and the container
bind-mounts a directory inside it. Nothing else changes between legs: same
image, same dataset, same envelope, same queries — only the filesystem under
the data directory changes.

Mechanism (Linux):

    <root>/fs/<type>.img          sparse file on the host's own filesystem
      └─ losetup --direct-io=on   -> /dev/loopN
           └─ mkfs.<type>         -> ext4 | xfs | btrfs | zpool
                └─ mount           -> <root>/mnt/<type>
                     └─ bind mount -> the container's data directory

--direct-io=on is not decoration. Without it the host page cache absorbs
every write before it reaches the device, and the filesystem under test
would never touch the disk: we would be measuring the host's cache. The flag
makes the loop device bypass that cache.

Two consequences to keep in mind when reading the numbers:

  - All filesystems sit on the same physical disk, one layer down. The
    comparison between them is meaningful; the absolute numbers describe
    the runner's disk, not a bare device.
  - ZFS sees its writes through a loop device and keeps its own ARC in host
    memory. Both are recorded in results/fs-<type>.json so the report can
    say so out loud.

Host backends, in the order they are tried:

  1. explicit  — BENCHCTL_HOST_CMD, e.g. "colima ssh --"
  2. container — we are already inside a container; use a privileged helper
                 that joins the host's mount namespace
  3. colima    — macOS dev box, the daemon lives inside the Colima VM
  4. local     — Linux CI (GitHub Actions): sudo on the runner
"""
import os
import shlex
import shutil
import subprocess
import sys

# Filesystems under test. Each entry is the command that formats the loop
# device; mount options are chosen to be equivalent across all four, so the
# comparison is about the filesystem and not about its knobs:
#   - no compression anywhere (btrfs and ZFS can compress, but then we would
#     be measuring the compressor too)
#   - noatime / atime=off everywhere (the default relatime would add a
#     metadata write on the first read of every file)
#   - no discard (the backing file is sparse; discarding would make the
#     reported free space a fiction)
FS_TYPES = ("ext4", "xfs", "btrfs", "zfs")

MOUNT_OPTS = {
    "ext4": ["noatime"],
    "xfs": ["noatime"],
    # nodiscard: the other three do not discard, and a discard on a sparse
    # backing file makes the reported free space a fiction.
    "btrfs": ["noatime", "nodiscard"],
    "zfs": [],          # ZFS gets atime=off on the dataset instead
}

DEFAULT_SIZE_GB = 12

# Space kept free next to the image for what the run itself writes: the
# dataset, the WAL (max_wal_size), the docker layers and the container's
# own copy of the data directory.
RESERVE_BYTES = 6 * 1024 ** 3


def parse_size(text):
    """Parse a docker-style size ('4G', '512M') into bytes.

    The envelope's memory limit is where the ZFS ARC cap comes from, and it
    is written the way Docker writes it.
    """
    if not text:
        return 0
    text = str(text).strip().upper()
    units = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
    if text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text)


class FsError(RuntimeError):
    """Raised when a filesystem cannot be provisioned. Never swallowed."""


# --------------------------------------------------------------------------
# host backends
# --------------------------------------------------------------------------

def _in_container():
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup") as f:
            cgroup = f.read()
    except OSError:
        return False
    return any(k in cgroup for k in ("docker", "kubepods", "containerd", "lxc"))


def _colima_running():
    if not shutil.which("colima"):
        return False
    p = subprocess.run(["colima", "status"], capture_output=True, text=True)
    return p.returncode == 0 and "running" in (p.stdout + p.stderr).lower()


def backend():
    """Which backend runs commands on the machine that runs the daemon."""
    if os.environ.get("BENCHCTL_HOST_CMD"):
        return "explicit"
    if _in_container():
        return "container"
    if _colima_running():
        return "colima"
    return "local"


def _wrap(script):
    """Turn a shell script into an argv that runs it on the daemon's machine."""
    kind = backend()
    if kind == "explicit":
        return shlex.split(os.environ["BENCHCTL_HOST_CMD"]) + ["sh", "-c", script]
    if kind == "colima":
        # The Colima user has passwordless sudo inside the VM.
        return ["colima", "ssh", "--", "sudo", "sh", "-c", script]
    if kind == "container":
        # Join the host's mount namespace AND its root, so paths mean what
        # they mean on the host. The image only has to provide nsenter; every
        # command runs from the host's own root filesystem.
        return [
            "docker", "run", "--rm", "--privileged", "--pid=host",
            "-v", "/:/host", "ubuntu:24.04",
            "nsenter", "--mount=/proc/1/ns/mnt", "--root=/proc/1/root",
            "--wd=/", "--", "/bin/sh", "-c", script,
        ]
    # local: Linux CI, passwordless sudo
    if os.geteuid() == 0:
        return ["sh", "-c", script]
    return ["sudo", "-n", "sh", "-c", script]


def host_sh(script, check=True):
    """Run a shell script on the machine that runs the Docker daemon."""
    p = subprocess.run(_wrap(script), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise FsError(
            f"host command failed (exit {p.returncode}) on backend "
            f"'{backend()}':\n  {script}\n{p.stderr.strip()}")
    return p


def host_out(script):
    """Run a script and return its stdout, stripped. Raises on failure."""
    return host_sh(script).stdout.strip()


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def _best_host_dir():
    """The machine's mount with the most free space, where we may write.

    Inside a VM the root filesystem is often the *smallest* one: Colima
    keeps docker's data on a separate volume, and a 12 GiB image plus a
    database on a 19 GiB root is how a run dies with an I/O error instead
    of an out-of-space error. Ask the machine which disk has room rather
    than guessing from the outside.

    Network shares are skipped on purpose: a loop device on virtiofs or 9p
    has no O_DIRECT support, and the loop driver refuses to attach one.
    """
    skip = ("tmpfs", "devtmpfs", "squashfs", "overlay", "virtiofs", "9p",
            "fuseblk", "fuse", "nfs", "cifs")
    out = host_out(
        "df -B1 --output=avail,fstype,target -x tmpfs -x devtmpfs -x squashfs "
        "-x overlay 2>/dev/null | awk 'NR>1 {print $1, $2, $3}' | sort -rn | head -8")
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            avail = int(parts[0])
        except ValueError:
            continue
        fstype, target = parts[1].strip(), parts[2].strip()
        if fstype.lower() in skip or avail < RESERVE_BYTES:
            continue
        if not target.startswith("/"):
            continue
        probe = host_sh(f"mkdir -p {target}/.benchctl-probe && "
                        f"rmdir {target}/.benchctl-probe", check=False)
        if probe.returncode == 0:
            return target
    return None


def resolve_root():
    """Where images and mount points live, on the daemon's machine.

    Must be visible to the Docker daemon, because the compose file bind
    mounts it by path. On macOS with Docker Desktop only /Users is shared
    with the VM, so there the root goes under $HOME.
    """
    env = os.environ.get("BENCHCTL_FS_ROOT")
    if env:
        return env
    if backend() == "container" and sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "benchctl-fs")
    best = _best_host_dir()
    if best:
        return f"{best}/benchctl-fs"
    for cand in ("/mnt/benchctl-fs", "/var/tmp/benchctl-fs"):
        parent = os.path.dirname(cand)
        if os.path.isdir(parent) and os.access(parent, os.W_OK):
            return cand
    return "/var/tmp/benchctl-fs"


# --------------------------------------------------------------------------
# capability probing
# --------------------------------------------------------------------------

def _has(cmd):
    return host_sh(f"command -v {cmd} >/dev/null 2>&1", check=False).returncode == 0


def _missing_tools_message(missing):
    """The exact command that installs what is missing."""
    packages = {
        "mkfs.ext4": "e2fsprogs",
        "mkfs.xfs": "xfsprogs",
        "mkfs.btrfs": "btrfs-progs",
        "zpool": "zfsutils-linux",
        "fio": "fio",
    }
    pkgs = sorted({packages[m] for m in missing if m in packages})
    if not pkgs:
        return ""
    return ("install them with: sudo apt-get update && "
            f"sudo apt-get install -y {' '.join(pkgs)}")


def availability(requested=None):
    """Which of the requested filesystems can actually be provisioned here.

    Returns {fs_type: {"available": bool, "reason": str}}. ZFS is the one
    that can be missing: its kernel module is not in every kernel, and on a
    runner it comes from linux-modules-extra rather than from the kernel
    image the runner booted.
    """
    out = {}
    for fs_type in (requested or FS_TYPES):
        if fs_type == "zfs":
            if not _has("zpool"):
                out[fs_type] = {"available": False,
                                 "reason": "zpool is not installed — "
                                           + _missing_tools_message(["zpool"])}
                continue
            mod = host_sh("modprobe zfs 2>&1", check=False)
            if mod.returncode != 0:
                out[fs_type] = {
                    "available": False,
                    "reason": "zfs kernel module unavailable: "
                              + (mod.stderr.strip() or mod.stdout.strip()
                                 or "modprobe zfs failed")
                              + " (on Ubuntu: sudo apt-get install -y "
                                "linux-modules-extra-$(uname -r))",
                }
                continue
            out[fs_type] = {"available": True, "reason": ""}
            continue
        mkfs = {"ext4": "mkfs.ext4", "xfs": "mkfs.xfs", "btrfs": "mkfs.btrfs"}[fs_type]
        if not _has(mkfs):
            out[fs_type] = {"available": False,
                             "reason": f"{mkfs} is not installed — "
                                       + _missing_tools_message([mkfs])}
            continue
        out[fs_type] = {"available": True, "reason": ""}
    return out


# --------------------------------------------------------------------------
# provision / teardown
# --------------------------------------------------------------------------

def _mkfs_script(fs_type, device):
    if fs_type == "ext4":
        # nodiscard: the backing file is sparse, and discarding would make
        # the free space we report a fiction.
        return f"mkfs.ext4 -F -q -E nodiscard {device}"
    if fs_type == "xfs":
        return f"mkfs.xfs -f -q {device}"
    if fs_type == "btrfs":
        return f"mkfs.btrfs -f -q --nodiscard {device}"
    if fs_type == "zfs":
        # ashift=12 (4K) to match a modern NVMe; a loop device reports 512B
        # logical sectors and ZFS would otherwise pick a 512-byte ashift and
        # warn about it.
        return (f"zpool create -f -o ashift=12 benchfs {device}")
    raise FsError(f"unknown filesystem: {fs_type}")


def free_bytes(path):
    """Bytes available on the filesystem that will hold the image.

    The path itself may not exist yet, so the nearest existing parent is
    measured instead.
    """
    probe = path
    for _ in range(4):
        if host_sh(f"test -d {probe}", check=False).returncode == 0:
            break
        probe = os.path.dirname(probe) or "/"
    out = host_out(f"df -B1 --output=avail {probe} 2>/dev/null | tail -1")
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return 0


def provision(fs_type, size_gb=DEFAULT_SIZE_GB, root=None, arc_max_bytes=None):
    """Format and mount a filesystem. Returns a state dict for the other calls.

    The state dict is what the driver passes around: it holds the host paths
    the compose file needs, plus the bits teardown() needs to undo this.
    """
    if fs_type not in FS_TYPES:
        raise FsError(f"unknown filesystem: {fs_type} (known: {', '.join(FS_TYPES)})")

    root = root or resolve_root()
    image = f"{root}/fs/{fs_type}.img"
    mount = f"{root}/mnt/{fs_type}"
    data_dir = f"{mount}/data"

    host_sh(f"mkdir -p {root}/fs {root}/mnt")

    # Space check before anything is created. The image file is sparse, so
    # it only costs what the filesystem actually writes into it — but the
    # data, the WAL and the docker layers all land on the same physical
    # disk. Running out of space mid-load does not fail cleanly: the
    # kernel returns EIO and the database dies with a signal, which looks
    # like a database bug and not like a full disk.
    have = free_bytes(root)
    if have and have < RESERVE_BYTES:
        raise FsError(
            f"not enough space at {root}: {have / 1024 ** 3:.1f} GiB available, "
            f"{RESERVE_BYTES / 1024 ** 3:.0f} GiB needed for the data, the WAL "
            f"and docker. Point BENCHCTL_FS_ROOT at a bigger disk, or give the "
            f"VM a bigger disk (colima start --disk N only applies when the VM "
            f"is created — an existing VM keeps the disk it was created with).")
    image_bytes = int(size_gb) * 1024 ** 3
    if have and have < image_bytes + RESERVE_BYTES:
        # Not fatal: the image is sparse and only grows with the data. It
        # becomes fatal inside the filesystem if the data outgrows it, and
        # that failure is legible ("No space left on device").
        print(f"[fssetup] warning: {have / 1024 ** 3:.1f} GiB free at {root}, "
              f"the image is {size_gb} GiB; the filesystem will fill up if the "
              f"data grows past it", flush=True)


    # A crashed run can leave a mount and a loop device behind. Mounting on
    # top of a stale mount would hide it but leave it alive, and the data
    # directory under it would still be the old filesystem's — which is how
    # a leg ends up loading into a database that is already full.
    host_sh(f"umount {mount} 2>/dev/null || true")
    for dev in host_out(
            f"losetup -a 2>/dev/null | grep ' {image}:' | cut -d: -f1").split():
        host_sh(f"losetup -d {dev} 2>/dev/null || true")

    # A fresh sparse file per leg. Sparse so the image costs nothing until
    # the filesystem writes to it.
    host_sh(f"rm -f {image}")
    host_sh(f"truncate -s {int(size_gb)}G {image}")

    dev = host_out(
        f"losetup --find --show --direct-io=on {image}").strip()
    if not dev:
        raise FsError("losetup returned no device")

    state = {
        "fs": fs_type,
        "backend": backend(),
        "root": root,
        "image": image,
        "device": dev,
        "mount": mount,
        "data_dir": data_dir,
        "pool": None,
        "dataset": None,
        "opts": MOUNT_OPTS.get(fs_type, []),
        "size_gb": size_gb,
    }

    try:
        host_sh(_mkfs_script(fs_type, dev))
        if fs_type == "zfs":
            # ZFS is not a filesystem you mount directly: the pool owns the
            # device and the dataset is the filesystem inside it.
            host_sh(f"zfs create -o mountpoint={mount} -o atime=off "
                    f"-o compression=off benchfs/data")
            host_sh(f"zpool set autotrim=off benchfs 2>/dev/null || true")
            if arc_max_bytes:
                # ZFS keeps its cache in host memory and will happily take
                # all of it. The other filesystems cache in the page cache,
                # which is also unbounded — but the envelope says 4G, so cap
                # the ARC at the same number instead of letting one
                # filesystem quietly get twice the memory.
                host_sh(f"echo {int(arc_max_bytes)} > "
                        f"/sys/module/zfs/parameters/zfs_arc_max 2>/dev/null || true")
            state["pool"] = "benchfs"
            state["dataset"] = "benchfs/data"
        else:
            opts = ",".join(state["opts"])
            host_sh(f"mkdir -p {mount}")
            host_sh(f"mount -o {opts} {dev} {mount}" if opts
                    else f"mount {dev} {mount}")

        host_sh(f"mkdir -p {data_dir}")
        # The container runs as its own uid (postgres is 999); the bind
        # mount has to be writable by it.
        host_sh(f"chmod 0777 {data_dir}")
    except Exception:
        teardown(state)
        raise

    return state


def teardown(state):
    """Unmount, detach the loop device, delete the image. Never raises."""
    if not state:
        return
    fs_type = state.get("fs")
    mount = state.get("mount")
    dev = state.get("device")
    image = state.get("image")

    if fs_type == "zfs" and state.get("pool"):
        host_sh(f"zpool destroy -f {state['pool']} 2>/dev/null || true")
    if mount:
        host_sh(f"umount {mount} 2>/dev/null || true")
    if dev:
        host_sh(f"losetup -d {dev} 2>/dev/null || true")
    if image:
        host_sh(f"rm -f {image} 2>/dev/null || true")
    if mount:
        host_sh(f"rm -rf {mount} 2>/dev/null || true")


def reset(state):
    """Empty the data directory, so the next leg starts from nothing.

    `docker compose down -v` deletes named volumes; it cannot delete the
    contents of a bind mount, so the driver calls this between legs.
    """
    data_dir = state.get("data_dir")
    if not data_dir:
        return
    host_sh(f"rm -rf {data_dir}/* {data_dir}/.[!.]* 2>/dev/null || true")
    host_sh(f"mkdir -p {data_dir} && chmod 0777 {data_dir}")


def settle(state):
    """Force everything written so far out to the device.

    Without this, the first read of each file can be served from the page
    cache and the filesystem's read path never gets measured. ZFS commits in
    transaction groups (default 5s), so it needs an explicit sync plus a
    wait for the group to land.
    """
    fs_type = state.get("fs")
    if fs_type == "zfs" and state.get("pool"):
        pool = state["pool"]
        before = host_out(f"zpool list -H -o txg {pool} 2>/dev/null || echo 0")
        host_sh(f"zpool sync {pool}")
        # Wait for the transaction group to advance past `before`.
        for _ in range(30):
            now = host_out(f"zpool list -H -o txg {pool} 2>/dev/null || echo 0")
            if now != before:
                return True
            host_sh("sleep 1")
        return False
    host_sh("sync")
    return True


def probe(state):
    """Collect the filesystem's own description of itself, for the record."""
    mount = state["mount"]
    fs_type = state["fs"]
    out = {
        "fs": fs_type,
        "backend": state.get("backend"),
        "device": state.get("device"),
        "mount": mount,
        "mount_options": "",
        "block_size": None,
        "fundamental_block_size": None,
        "total_bytes": None,
        "free_bytes": None,
        "zfs_properties": {},
    }
    try:
        # --first-only: a btrfs subvolume mount also matches the parent
        # filesystem's mount, and findmnt would print both.
        out["mount_options"] = host_out(
            f"findmnt -n -o OPTIONS --first-only {mount} 2>/dev/null | head -1").strip()
        stat = host_out(f"stat -f -c '%s %S %b %f' {mount} 2>/dev/null || true")
        if stat:
            parts = stat.split()
            if len(parts) == 4:
                out["block_size"] = int(parts[0])
                out["fundamental_block_size"] = int(parts[1])
                out["total_bytes"] = int(parts[2]) * int(parts[0])
                out["free_bytes"] = int(parts[3]) * int(parts[0])
    except Exception as e:  # noqa: BLE001 — probe is diagnostic, never fatal
        out["error"] = str(e)
    if fs_type == "zfs" and state.get("dataset"):
        try:
            props = {}
            for line in host_out(
                    f"zfs get -H -o property,value all {state['dataset']} "
                    f"2>/dev/null || true").splitlines():
                if "\t" in line:
                    k, _, v = line.partition("\t")
                    props[k] = v
            keep = ("recordsize", "compression", "atime", "mountpoint",
                    "blocksize", "ashift", "primary", "type")
            out["zfs_properties"] = {k: v for k, v in props.items() if k in keep}
        except Exception as e:  # noqa: BLE001
            out["zfs_error"] = str(e)
    return out


def fsync_bench_fio(mount_dir, seconds=5, size_mb=64):
    """fsync latency on a mounted filesystem, measured on the host.

    pg_test_fsync is PostgreSQL's own benchmark, and it uses O_DIRECT when
    the platform supports it. ZFS does not: a direct write has to be a
    multiple of the record size, so an 8 KiB O_DIRECT write fails with
    EINVAL and pg_test_fsync gives up ("write failed: Invalid argument").

    This is the fallback, and it is also the more relevant number: buffered
    writes with an fdatasync after each one, which is exactly what
    PostgreSQL's default WAL path does on Linux.
    """
    import json as _json
    if host_sh("command -v fio", check=False).returncode != 0:
        return {"available": False, "reason": "fio is not installed"}
    cmd = (
        f"fio --name=benchctl-fsync --directory={mount_dir} "
        f"--filename=fsync-bench.tmp --size={size_mb}M --rw=write --bs=4k "
        f"--direct=0 --fdatasync=1 --runtime={seconds} --time_based "
        f"--group_reporting=1 --eta=never --output-format=json"
    )
    p = host_sh(cmd)
    try:
        data = _json.loads(p.stdout)
        job = data["jobs"][0]
        write = job["write"]
        # With --fdatasync the flush is a separate operation, so its
        # latency lives in the "sync" section. The write itself is
        # buffered and fast, which is exactly the point: the cost of a
        # commit is the sync, not the write.
        sync = job.get("sync", {})
        lat = sync.get("lat_ns", {}).get("percentile") or \
            write.get("clat_ns", {}).get("percentile") or {}
        p50 = float(lat["50.000000"]) / 1000.0
        p99 = float(lat["99.000000"]) / 1000.0
        runtime = job.get("job_runtime") or seconds
        syncs = sync.get("total_ios") or write.get("total_ios")
        return {
            "available": True,
            "tool": "fio",
            "seconds": seconds,
            "bs": "4k",
            "direct": 0,
            "fdatasync": 1,
            "fdatasync_us_p50": round(p50, 1),
            "fdatasync_us_p99": round(p99, 1),
            "fdatasync_us_mean": round(sync.get("lat_ns", {}).get("mean", 0) / 1000.0, 1),
            "fsyncs": syncs,
            "fsyncs_per_s": round(syncs / runtime, 1) if syncs and runtime else None,
            "write_bytes": write.get("io_bytes"),
        }
    except (KeyError, ValueError, IndexError) as e:
        return {"available": False, "reason": f"unparseable fio output: {e}"}


def disk_usage(state):
    """Bytes used by the data directory, from the host's view.

    du on the host sees the filesystem's own allocation, including btrfs's
    metadata overhead and ZFS's slack — which is the point of the exercise.
    """
    data_dir = state["data_dir"]
    out = host_out(f"du -sb {data_dir} 2>/dev/null || true")
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return 0


if __name__ == "__main__":
    # Manual smoke test: python3 shared/fssetup.py xfs
    kind = sys.argv[1] if len(sys.argv) > 1 else "xfs"
    print("backend:", backend(), "root:", resolve_root())
    print("availability:", availability())
    st = provision(kind, size_gb=int(os.environ.get("FS_SIZE_GB", "2")))
    try:
        print("probe:", probe(st))
        print("usage:", disk_usage(st))
    finally:
        teardown(st)
    print("teardown ok")

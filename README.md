# my-benchmarks

Measurement harnesses. One directory per benchmark: a question, a reproducible
way to answer it, and the raw numbers from a real run.

These back blog posts. Two rules hold across everything here — every figure
published must be traceable to a file in this repo, and every harness must be
runnable by a reader who distrusts the number.

## Benchmarks

| Benchmark | Question | Headline |
| --- | --- | --- |
| [prom-vs-postgres](benchctl/scenarios/counter-rate.yaml) | Should metrics live in Prometheus or PostgreSQL? | Prometheus uses **59.9x** less disk; wins 9 of 11 dimensions |
| [fs-matrix](benchctl/scenarios/fs-matrix.yaml) | Which filesystem should a PostgreSQL data directory live on? | One database, four filesystems, everything else held constant |

Both scenarios run through the same harness, [benchctl](benchctl/): one driver,
one envelope, one set of rules about what a number has to be before it can be
published.

## Running a benchmark

```sh
cd benchctl
make run SCENARIO=scenarios/counter-rate.yaml
```

`make run` writes `results/report.md`. Scenarios are separately runnable
because they are separately diagnosable.

There is also a workflow: <kbd>Actions</kbd> → *benchmark* → *Run workflow*.
`scale=smoke` finishes in about two minutes and checks that the harness still
runs. `scale=full` takes roughly forty and regenerates `results/`.
`scale=fs` runs the filesystem matrix: four filesystems, one database.

The harness also ships an optional monitoring stack: cAdvisor, a host exporter
and both databases' own metrics feed a Grafana dashboard, so a run can be judged
on what it cost the machine as well as how fast it was. See
[Watching a run](benchctl/README.md#watching-a-run).

## The filesystem matrix

A container cannot format a filesystem, so the filesystem under test lives on
the machine that runs the Docker daemon and the container bind-mounts a
directory inside it:

```
/mnt/benchctl-fs/fs/xfs.img        sparse file on the runner's disk
  └─ losetup --direct-io=on        -> /dev/loop0
       └─ mkfs.xfs                 -> mounted at /mnt/benchctl-fs/mnt/xfs
            └─ bind mount          -> the container's data directory
```

`--direct-io=on` is not decoration. Without it the host page cache absorbs
every write before it reaches the device, and the filesystem under test would
never touch the disk — the benchmark would be measuring the host's cache.

The matrix covers ext4, XFS, btrfs and ZFS. Each leg runs the same image, the
same dataset, the same queries and the same envelope; only the filesystem
changes. `shared/fssetup.py` does the provisioning, and it works on a GitHub
Actions runner (`sudo` on the runner) and on a macOS box with Colima
(`colima ssh`). A filesystem that cannot be provisioned on the host — ZFS
without its kernel module, for instance — is recorded as a gap in
`results/fs-availability.json` and listed in the report, never silently
dropped.

### The host needs the tools

The filesystems are formatted by the machine that runs the Docker daemon, so
that machine needs `mkfs` and a ZFS userspace:

```sh
sudo apt-get update
sudo apt-get install -y e2fsprogs xfsprogs btrfs-progs zfsutils-linux fio
sudo modprobe zfs        # on Ubuntu: linux-modules-extra-$(uname -r)
```

With Colima, the packages go into the VM:

```sh
colima ssh -- sudo apt-get update
colima ssh -- sudo apt-get install -y e2fsprogs xfsprogs btrfs-progs zfsutils-linux fio
```

A VM's root filesystem is small — Colima's is 19 GiB — and a full disk does
not fail cleanly: the kernel returns EIO and the database dies with a
signal. `shared/fssetup.py` therefore picks the mount with the most room,
refuses to start without it, and says which disk and how much it needs.
`colima start --disk N` only applies when the VM is created.

## What every benchmark here has in common

- **Equal resource envelopes.** Whatever is under test gets the same CPU and
  memory budget. Otherwise "Prometheus used less memory" can quietly mean
  "Prometheus was allowed more memory".
- **Parity before performance.** Both sides hold the same data, verified by
  querying them and comparing, before any timing runs. A fast answer to the
  wrong question is worth nothing.
- **Raw results committed.** `results/*.json` and the rendered report are in the
  repo, not just a summary in a blog post.
- **Caveats recorded, not softened.** What a run could not measure is listed
  next to what it did.
- **`:latest` pinned by digest.** `results/environment.json` records what the tag
  resolved to on the day. Without it the numbers expire silently.
- **Results land as a PR, never a push to main.** A run that changes published
  figures should be a diff somebody can read.

## Why the harness is in the repo at all

Because numbers in blog posts rot. Every claim here is a file, and every file is
regenerable. If a number in a post stops matching `results/report.md`, one of
them is wrong and you can find out which.

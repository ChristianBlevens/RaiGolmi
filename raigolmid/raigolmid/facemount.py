"""What a face shows of the layers below it: the body read-only at
`/body`, the working copy at `/work`, and store paths read-only at `/nix/store`. Standalone:
it runs in the face-mount helper's own interpreter, which has nothing of raigolmid but this file.

    facemount.py sync <face-host-pid> [--body <body-host-pid>] [--work <dir>]
                      [--store <dir> [--closure <paths-file>]…]

Declarative: the face ends with exactly what the arguments name, and an omitted one is taken
away. `--work` and `--store` are directories in this helper, bound in from the host; each
`--closure` lists, one per line, the store paths of one closure (`closures.py`).

Needs host pids and CAP_SYS_ADMIN + CAP_SYS_CHROOT (setns into a mount namespace needs both),
plus CAP_SYS_PTRACE to open the namespace of a face running as another uid.

Every mount is cloned where its source is local and carried as a detached mount across the
setns into the face's namespace, where move_mount attaches it. Binding `/proc/<pid>/root` from
outside is refused by the kernel's check_mnt() with EINVAL, so the body's root is cloned inside the body's mount namespace.
Clones are non-recursive: the body's /proc, /dev, /sys and Docker's file binds do not come
across. The read-only flag is on the clone, so the body's own root stays writable.

The face's `/nix/store` is a tmpfs holding one bind per store path of the named closures. Two
closures can share a path, and a store path's name is its content's hash, so it is bound once.
What counts as present is what is *mounted* there, read from the face's mountinfo, never a
name in the listing: an empty mountpoint left by a run that died is replaced, not trusted.

Detaches are lazy: an editor holding a file open keeps that file readable, and the old body's
container can still be removed.
"""
import ctypes
import os
import resource
import sys

SYS_open_tree, SYS_move_mount, SYS_mount_setattr = 428, 429, 442   # asm/unistd_64.h
OPEN_TREE_CLONE = 1
AT_FDCWD, AT_EMPTY_PATH = -100, 0x1000
MOVE_MOUNT_F_EMPTY_PATH = 4
MOUNT_ATTR_RDONLY = 0x1
MNT_DETACH = 2
CLONE_NEWNS = 0x00020000
BODY, WORK, STORE = "/body", "/work", "/nix/store"

libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long

# Opened while /proc is still this helper's. After the setns, `/proc` resolves to the face's
# procfs, whose pid namespace does not contain this process, so `/proc/self` names nothing;
# `mountinfo` read through this fd reports whichever namespace the process is in by then.
SELF = os.open("/proc/self", os.O_RDONLY | os.O_DIRECTORY)


class MountAttr(ctypes.Structure):
    _fields_ = [("attr_set", ctypes.c_uint64), ("attr_clr", ctypes.c_uint64),
                ("propagation", ctypes.c_uint64), ("userns_fd", ctypes.c_uint64)]


def check(result: int, what: str) -> int:
    if result < 0:
        err = ctypes.get_errno()
        raise SystemExit(f"facemount: {what}: {os.strerror(err)} (errno {err})")
    return result


def clone(path: str, read_only: bool) -> int:
    tree = check(libc.syscall(SYS_open_tree, AT_FDCWD, path.encode(),
                              OPEN_TREE_CLONE | os.O_CLOEXEC), f"open_tree of {path}")
    if read_only:
        attr = MountAttr(attr_set=MOUNT_ATTR_RDONLY)
        check(libc.syscall(SYS_mount_setattr, tree, b"", AT_EMPTY_PATH, ctypes.byref(attr),
                           ctypes.sizeof(attr)), f"mount_setattr read-only on {path}")
    return tree


def attach(tree: int, target: str) -> None:
    check(libc.syscall(SYS_move_mount, tree, b"", AT_FDCWD, target.encode(),
                       MOVE_MOUNT_F_EMPTY_PATH), f"move_mount onto the face's {target}")
    os.close(tree)


def mount_points() -> list[str]:
    with open(os.open("mountinfo", os.O_RDONLY, dir_fd=SELF)) as f:
        return [line.split()[4] for line in f]


def set_mount(target: str, tree: int | None) -> str:
    """`target` holds `tree`, or only the tmpfs the face was created with when None."""
    stacked = mount_points().count(target)
    if stacked == 0:
        raise SystemExit(f"facemount: the face has no {target}; it is created with a tmpfs there")
    for _ in range(stacked - 1):
        check(libc.umount2(target.encode(), MNT_DETACH), f"detaching {target}")
    if tree is None:
        return f"{target} empty"
    attach(tree, target)
    return f"{target} attached"


def store_sources(store: str | None, closures: list[str]) -> dict[str, str]:
    """Store path name -> the source path serving it."""
    wanted: dict[str, str] = {}
    for listing in closures:
        with open(listing) as f:
            for name in f.read().split():
                wanted[name] = os.path.join(store, name)
    return wanted


def plan_store(wanted: dict[str, str], face_pid: str):
    """Clones what the face lacks, here; returns what finishes the job inside the face."""
    # Read in this helper's namespace, where the face's /proc entry is reachable: what is
    # mounted on the face's store now, so only the difference is cloned.
    with open(f"/proc/{face_pid}/mountinfo") as f:
        prefix = STORE + "/"
        mounted = {p[len(prefix):] for p in (line.split()[4] for line in f)
                   if p.startswith(prefix) and "/" not in p[len(prefix):]}
    links = {n: os.readlink(s) for n, s in wanted.items() if os.path.islink(s)}
    trees = {n: (clone(s, read_only=True), os.path.isdir(s))
             for n, s in wanted.items() if n not in links and n not in mounted}
    return lambda: _apply_store(wanted, mounted, links, trees)


def _apply_store(wanted, mounted, links, trees) -> str:
    removed = 0
    for name in os.listdir(STORE):
        path = os.path.join(STORE, name)
        keep = (name in mounted and name in wanted and name not in links) or (
            name in links and os.path.islink(path) and os.readlink(path) == links[name])
        if keep:
            continue
        if name in mounted:
            check(libc.umount2(path.encode(), MNT_DETACH), f"detaching {path}")
        if os.path.isdir(path) and not os.path.islink(path):
            os.rmdir(path)
        else:
            os.unlink(path)
        removed += 1
    added = 0
    for name, target in links.items():
        path = os.path.join(STORE, name)
        if not os.path.lexists(path):
            os.symlink(target, path)
            added += 1
    for name, (tree, is_dir) in trees.items():
        path = os.path.join(STORE, name)
        if is_dir:
            os.mkdir(path)
        else:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o444))
        attach(tree, path)
        added += 1
    return f"{STORE} {len(wanted)} paths, {added} added, {removed} removed"


def sync(face_pid: str, body_pid: str | None, work: str | None, store: str | None,
         closures: list[str], protect: list[tuple[str, bool]]) -> None:
    """`protect` is the .git paths bound over /work in order, each with whether it is
    read-only: a pinned directory is bound onto itself so it cannot be renamed, before the
    read-only paths under it (`git.ProtectedPaths`)."""
    # A closure is hundreds of store paths, each held as a detached mount until attached.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    # Every namespace is opened before any is entered: /proc is this helper's, and stays
    # readable only while it is this helper's root.
    face_ns = os.open(f"/proc/{face_pid}/ns/mnt", os.O_RDONLY)
    body_ns = os.open(f"/proc/{body_pid}/ns/mnt", os.O_RDONLY) if body_pid else None
    work_tree = clone(work, read_only=False) if work else None
    # The clone of /work is not recursive, so each of the .git paths nothing may write or
    # rename is cloned on its own and attached over it.
    protected = [(clone(f"{work}/{relative}", read_only=read_only), relative)
                 for relative, read_only in protect]
    apply_store = plan_store(store_sources(store, closures), face_pid)
    body_tree = None
    if body_ns is not None:
        check(libc.setns(body_ns, CLONE_NEWNS), "setns into the body's mount namespace")
        body_tree = clone("/", read_only=True)
    check(libc.setns(face_ns, CLONE_NEWNS), "setns into the face's mount namespace")
    done = [set_mount(BODY, body_tree), set_mount(WORK, work_tree)]
    for tree, relative in protected:
        attach(tree, f"{WORK}/{relative}")
    done += [f"{len(protected)} .git paths bound over /work", apply_store()]
    print(f"facemount: pid {face_pid}: " + "; ".join(done))


def main(argv: list[str]) -> None:
    usage = ("usage: facemount.py sync <face-pid> [--body <pid>] [--work <dir>] "
             "[--pin|--protect <path under work>]… [--store <dir> [--closure <paths-file>]…]")
    if len(argv) < 2 or argv[0] != "sync" or len(argv[2:]) % 2:
        raise SystemExit(usage)
    body = work = store = None
    closures: list[str] = []
    protect: list[tuple[str, bool]] = []
    for flag, value in zip(argv[2::2], argv[3::2]):
        if flag == "--body" and body is None:
            body = value
        elif flag == "--work" and work is None:
            work = value
        elif flag == "--store" and store is None:
            store = value
        elif flag == "--closure":
            closures.append(value)
        elif flag in ("--protect", "--pin"):
            protect.append((value, flag == "--protect"))
        else:
            raise SystemExit(usage)
    if (closures and store is None) or (protect and work is None):
        raise SystemExit(usage)
    sync(argv[1], body, work, store, closures, protect)


if __name__ == "__main__":
    main(sys.argv[1:])

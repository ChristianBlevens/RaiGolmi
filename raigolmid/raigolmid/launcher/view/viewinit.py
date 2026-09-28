"""Session-view entrypoint.

Runs privileged in a container that has joined the anchor's PID and network namespaces.
Enters the body's mount namespace, unshares a private one from it, builds the view's root
there, pivots into it, then drops every capability and execs the launcher.

SYS_ADMIN (mounts, pivot_root) and SYS_PTRACE (reading /proc/<body-pid>/root) are needed only
for the work below. Everything a user or an agent runs is a child of the launcher at the end,
and therefore unprivileged.

**Why this is a Python program and not a shell script.** The kernel refuses to bind from a
mount that is not in the caller's mount namespace, so the root cannot be assembled from the
view container's own namespace — it has to be assembled from inside the body's. From
the `setns` until the `pivot_root`, none of the toolbelt's files are reachable by path: not
`mount`, not `mkdir`, not `capsh`, and not this interpreter's own standard library. So that
span must be one resident process issuing syscalls, and it must **import nothing** inside it.
Every import in this file is therefore at the top, before the `setns`, and the only external
program is `capsh`, exec'd after the pivot where the toolbelt is reachable again.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import json
import os
import socket
import sys

MS_RDONLY, MS_REMOUNT, MS_BIND, MS_REC, MS_PRIVATE = 1, 32, 0x1000, 0x4000, 0x40000
MNT_DETACH = 2
CLONE_NEWNS = 0x00020000
# asm/unistd_64.h. open_tree and move_mount are the only way a tree from one mount namespace
# reaches another: captured as a detached mount before the setns, attached after it.
SYS_pivot_root, SYS_open_tree, SYS_move_mount = 155, 428, 429
OPEN_TREE_CLONE, AT_RECURSIVE, AT_FDCWD = 1, 0x8000, -100
MOVE_MOUNT_F_EMPTY_PATH = 4

# Kernel filesystems, the toolbelt closure, the working copy, and /.raigolmid are all provided
# below, so the body's own versions are skipped. /.raigolmid holds the launcher's code and its
# sockets precisely *because* it is not a path the body owns: putting them under /run would
# mean creating directories inside the body's live /run, which fails outright on a read-only
# body.
PROVIDED_BY_US = frozenset({"proc", "sys", "dev", "nix", "work", ".raigolmid"})

libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
libc.syscall.restype = ctypes.c_long

SOCK_DIR = os.environ.get("VIEW_SOCK_DIR", "/.raigolmid/sockets")
VIEW_PRIVATE = os.path.dirname(SOCK_DIR)
CODE_DIR = os.environ.get("VIEW_CODE_DIR", "/.raigolmid/code")
GENERATION = os.environ.get("VIEW_GENERATION", "0")
INSTANCE = os.environ.get("VIEW_INSTANCE")
# Named by raigolmid, not derived here: `Paths` folds `@` out of an instance id to make a
# filename, and a view that re-derived it would listen on a path raigolmid never opens.
SOCK_NAME = os.environ.get("VIEW_SOCK_NAME")


def log(message: str) -> None:
    print(f"[view] {message}", file=sys.stderr, flush=True)


def die(message: str) -> None:
    print(f"[view] FATAL: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def die_errno(what: str) -> None:
    code = ctypes.get_errno()
    die(f"{what}: errno {code} {errno.errorcode.get(code, '?')} — {os.strerror(code)}")


def mount(source: str | None, target: str, fstype: str | None, flags: int,
          data: str | None, what: str) -> None:
    rc = libc.mount(source.encode() if source else None, target.encode(),
                    fstype.encode() if fstype else None, flags,
                    data.encode() if data else None)
    if rc != 0:
        die_errno(what)


def capture(path: str, what: str) -> int:
    """A detached clone of `path`, which outlives the move into another mount namespace."""
    fd = libc.syscall(SYS_open_tree, AT_FDCWD, path.encode(),
                      OPEN_TREE_CLONE | AT_RECURSIVE)
    if fd < 0:
        die_errno(f"capturing {what} from {path}")
    return fd


def bin_siblings(root: str = "") -> list[str]:
    """The toolbelt image's top-level directories that its `/bin` links into relatively
    (Nixery's `/bin/go -> ../share/go/bin/go`). Each is placed beside `/.toolbelt/bin`, so a
    link resolves there as it does in the image."""
    names = set()
    for entry in os.listdir(f"{root}/bin"):
        if not os.path.islink(f"{root}/bin/{entry}"):
            continue
        target = os.readlink(f"{root}/bin/{entry}")
        if target.startswith("../"):
            name = target[3:].split("/", 1)[0]
            if name not in ("", "..", "bin") and os.path.isdir(f"{root}/{name}"):
                names.add(name)
    return sorted(names)


def find_body_pid() -> str:
    """The body is the process whose root filesystem is neither the anchor's nor our own.

    Discovery happens here rather than on the host because under Docker Desktop the daemon
    runs in its own WSL distribution: a container's host PID is not resolvable from the
    user's distribution, so there is no /proc/<hostpid>/status to read NSpid from.
    """
    def root_id(pid: str) -> tuple[int, int] | None:
        try:
            st = os.stat(f"/proc/{pid}/root")
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    mine = root_id("self")
    if mine is None:
        die("cannot stat own root")
    anchor = root_id("1")
    if anchor is None:
        die("cannot stat PID 1's root — the view needs SYS_PTRACE")
    me = str(os.getpid())
    for entry in sorted(os.listdir("/proc")):
        if not entry.isdigit() or entry in ("1", me):
            continue
        found = root_id(entry)
        if found is not None and found != mine and found != anchor:
            return entry
    die("no body process in the anchor's PID namespace; the body is not running")
    raise AssertionError("unreachable")


if not INSTANCE:
    die("VIEW_INSTANCE is required")
# `body`: the view's root is assembled on the body's filesystem (steps 1-8). `toolbelt`: an
# instance with no body, whose view keeps its own root and needs only steps 6 and 9-10.
VIEW_ROOT = os.environ.get("VIEW_ROOT")
if VIEW_ROOT not in ("body", "toolbelt"):
    die(f"VIEW_ROOT must be 'body' or 'toolbelt', not {VIEW_ROOT!r}")
if not SOCK_NAME:
    die("VIEW_SOCK_NAME is required: raigolmid names the socket, because only it knows the "
        "filename it will look for")

# --- 6. the user's own .git, protected wherever it is mounted ---------------------------
#
# raigolmid names the paths (`git.ProtectedPaths`, in VIEW_GIT_BINDS): hooks and config
# read-only over the writable mount, for the working copy and every submodule, so nothing in a
# container can make the host's git execute code; and each directory holding one bound onto
# itself first, so none can be renamed away with its binds. `at` maps a path relative to the
# view's root to one reachable now — inside the new root before the pivot, or the container's
# own root with no body — so these are same-namespace binds.
def protect_git(at) -> None:
    try:
        binds = json.loads(os.environ["VIEW_GIT_BINDS"])
    except (KeyError, ValueError) as exc:
        die(f"VIEW_GIT_BINDS must be raigolmid's list of .git paths to bind: {exc!r}")
    for relative, read_only in binds:
        target = at(f"work/{relative}")
        mount(target, target, None, MS_BIND, None, f"binding {relative} onto itself")
        if read_only:
            mount(None, target, None, MS_REMOUNT | MS_BIND | MS_RDONLY, None,
                  f"making {relative} read-only")


def assemble_on_the_body() -> None:
    """Steps 1-8: the toolbelt's trees on the body's own root, then the pivot into it."""
    body_pid = find_body_pid()
    if not os.path.isdir(f"/proc/{body_pid}/root"):
        die(f"/proc/{body_pid}/root is not readable")
    log(f"body pid {body_pid}, generation {GENERATION}")

    # --- 1. capture everything of ours, while it is still ours -------------------------------
    #
    # The toolbelt closure is bound at its real /nix/store path so the closure's absolute store
    # paths resolve — including this interpreter's own, which is what makes imports work again
    # after the pivot.
    captured: list[tuple[int, str]] = [
        (capture("/nix/store", "the toolbelt closure"), "nix/store"),
        (capture("/bin", "the toolbelt's bin"), ".toolbelt/bin"),
        *((capture(f"/{name}", f"the toolbelt's {name}"), f".toolbelt/{name}")
          for name in bin_siblings()),
        (capture("/work", "the working copy"), "work"),
        (capture(CODE_DIR, "the launcher's code"), CODE_DIR.lstrip("/")),
        (capture(SOCK_DIR, "the socket directory"), SOCK_DIR.lstrip("/")),
    ]

    # --- 2. into the body's mount namespace, then out into a private one ---------------------
    #
    # NOTHING MAY BE IMPORTED FROM HERE UNTIL THE PIVOT: this interpreter's standard library
    # lives in the toolbelt closure, which is not reachable by path again until then.
    ns_fd = os.open(f"/proc/{body_pid}/ns/mnt", os.O_RDONLY)
    if libc.setns(ns_fd, CLONE_NEWNS) != 0:
        die_errno("entering the body's mount namespace")
    os.close(ns_fd)
    if libc.unshare(CLONE_NEWNS) != 0:
        die_errno("unsharing a private mount namespace from the body's")
    mount(None, "/", None, MS_PRIVATE | MS_REC, None, "making / private")

    # --- 3. the new root ---------------------------------------------------------------------
    #
    # A tmpfs over a directory the body already has. Mounting over a directory writes nothing
    # beneath it, so a read-only body is fine, and the unshare above is what keeps the mount
    # invisible to the body. The directory need not be empty — a distroless body has no spare
    # one — so whatever is chosen is captured first and restored inside the new root under its
    # own name, and the view loses nothing.
    new_root = None
    shadowed = None
    for candidate in ("/mnt", "/media", "/srv", "/opt"):
        if os.path.isdir(candidate) and not os.path.islink(candidate) and not os.listdir(candidate):
            new_root = candidate
            break
    if new_root is None:
        for name in sorted(os.listdir("/")):
            path = f"/{name}"
            if name in PROVIDED_BY_US or os.path.islink(path) or not os.path.isdir(path):
                continue
            new_root = path
            break
        if new_root is None:
            die("the body has no directory to mount the view's root over")
        shadowed = capture(new_root, f"the body's {new_root}, before shadowing it")

    mount("tmpfs", new_root, "tmpfs", 0, "mode=0755", f"the tmpfs root over {new_root}")
    new_fd = os.open(new_root, os.O_RDONLY | os.O_DIRECTORY)
    shadowed_name = new_root.lstrip("/")


    def in_new(relative: str) -> str:
        """A path inside the new root, usable as a mount target before the pivot."""
        return f"/proc/self/fd/{new_fd}/{relative}"


    def mkdirs_in_new(relative: str) -> None:
        parts = relative.strip("/").split("/")
        for depth in range(1, len(parts) + 1):
            try:
                os.mkdir("/".join(parts[:depth]), 0o755, dir_fd=new_fd)
            except FileExistsError:
                pass
            except OSError:
                die(f"could not create {relative} inside the view's root")


    # --- 4. the body's own top level ---------------------------------------------------------
    bound = 0
    for name in sorted(os.listdir("/")):
        if name in PROVIDED_BY_US or name == shadowed_name:
            continue
        source = f"/{name}"
        if os.path.islink(source):
            # A top-level symlink (/bin -> usr/bin on merged-usr images) is recreated as a
            # symlink; binding it would resolve it and lose the indirection.
            try:
                os.symlink(os.readlink(source), name, dir_fd=new_fd)
            except OSError:
                die(f"could not recreate the body's symlink /{name}")
            continue
        if os.path.isdir(source):
            mkdirs_in_new(name)
            mount(source, in_new(name), None, MS_BIND | MS_REC, None,
                  f"rbind of the body's /{name}")
            bound += 1
            continue
        try:                                    # a stray top-level file, e.g. /.dockerenv
            with open(source, "rb") as src:
                payload = src.read()
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644, dir_fd=new_fd)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        except OSError:
            pass                                # never worth failing a view over

    if shadowed is not None:
        mkdirs_in_new(shadowed_name)
        if libc.syscall(SYS_move_mount, shadowed, b"", new_fd, shadowed_name.encode(),
                        MOVE_MOUNT_F_EMPTY_PATH) != 0:
            die_errno(f"restoring the body's {new_root} inside the view")
        os.close(shadowed)

    # --- 5. our own trees, moved back in -----------------------------------------------------
    for fd, relative in captured:
        mkdirs_in_new(relative)
        if libc.syscall(SYS_move_mount, fd, b"", new_fd, relative.encode(),
                        MOVE_MOUNT_F_EMPTY_PATH) != 0:
            die_errno(f"attaching {relative} inside the view's root")
        os.close(fd)
    log(f"root assembled: {bound} of the body's directories, {len(captured)} of ours")

    protect_git(in_new)

    # --- 7. the kernel filesystems ------------------------------------------------------------
    for relative in ("proc", "sys", "dev"):
        mkdirs_in_new(relative)
    # A fresh proc shows the anchor's PID namespace, which the view shares.
    mount("proc", in_new("proc"), "proc", 0, None, "mounting proc")
    if libc.mount(b"/sys", in_new("sys").encode(), None, MS_BIND | MS_REC, None) != 0:
        log("warn: /sys rbind failed; continuing without it")
    mount("/dev", in_new("dev"), None, MS_BIND | MS_REC, None, "rbind of /dev")

    # --- 8. pivot ------------------------------------------------------------------------------
    mkdirs_in_new(".oldroot")
    os.chdir(new_root)
    if libc.syscall(SYS_pivot_root, b".", b".oldroot") != 0:
        die_errno("pivot_root into the view's root")
    os.chdir("/")
    if libc.umount2(b"/.oldroot", MNT_DETACH) != 0:
        die_errno("detaching the old root")
    try:
        os.rmdir("/.oldroot")
    except OSError:
        pass
    log("pivoted: the root is the body's filesystem")


def assemble_on_the_toolbelt() -> None:
    """No body: the container's own root is the view's, with the working copy at /work.
    Only what steps 1-8 would have left behind is added — `/.toolbelt/bin`, which PATH, the
    shell below and raigolmid's probes all name, and what it links into — as binds of the
    image's own directories."""
    for name in ("bin", *bin_siblings()):
        try:
            os.makedirs(f"/.toolbelt/{name}", 0o755, exist_ok=True)
        except OSError as exc:
            die(f"could not create /.toolbelt/{name}: {exc}")
        mount(f"/{name}", f"/.toolbelt/{name}", None, MS_BIND | MS_REC, None,
              f"binding /{name} at /.toolbelt/{name}")
    protect_git(lambda relative: f"/{relative}")
    log("root is the toolbelt's own: no body")


if VIEW_ROOT == "body":
    assemble_on_the_body()
else:
    assemble_on_the_toolbelt()

# --- 9. hand over to the launcher, unprivileged -------------------------------------------
os.environ["PATH"] = "/.toolbelt/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Not /root: the launcher's children run as the user who owns the working copy (step 10),
# and a HOME they cannot write is a shell and a language server both failing at
# their first state file. This one is on the view's own tmpfs and dies with the view.
VIEW_HOME = f"{VIEW_PRIVATE}/home"
os.environ["HOME"] = VIEW_HOME
os.environ["PYTHONPATH"] = CODE_DIR
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

sock = f"{SOCK_DIR}/{SOCK_NAME}"

# The view's own writable corner, handed to the user who owns the working copy before
# anything is started (step 10 explains why that is who runs here). Every HOME-relative
# state file lands in it, and it dies with the view.
owner = os.stat(SOCK_DIR)
for directory in (VIEW_PRIVATE, VIEW_HOME):
    try:
        os.makedirs(directory, 0o700, exist_ok=True)
        os.chown(directory, owner.st_uid, owner.st_gid)
    except OSError as exc:
        die(f"could not give {directory} to uid {owner.st_uid}: {exc}")


def bind_for_the_launcher(path: str) -> int:
    """Bind, own and listen on the launcher's socket while there are still capabilities.

    The launcher cannot do this itself: the directory is detached from the view before it
    starts (step 9b), so the socket is bound here and inherited as `fd_number`.
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        die(f"could not clear the stale socket at {path}: {exc}")

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(path)
    except OSError as exc:
        die(f"could not bind {path}: {exc}")
    listener.listen(64)

    # Owned by whoever owns the directory — that is the user raigolmid runs as — and private
    # to them, so nothing else on the machine can reach a launcher.
    owner = os.stat(SOCK_DIR)
    os.chmod(path, 0o600)
    if (owner.st_uid, owner.st_gid) != (os.geteuid(), os.getegid()):
        try:
            os.chown(path, owner.st_uid, owner.st_gid)
        except OSError as exc:
            die(f"could not give {path} to uid {owner.st_uid}: {exc}")

    # detach() hands over the descriptor without closing it, under the number it has: dup2
    # to a chosen number is a no-op when the numbers match, and closing the socket object
    # would then close the descriptor itself.
    fd = listener.detach()
    os.set_inheritable(fd, True)
    return fd


ordinary_fd = bind_for_the_launcher(sock)

# capsh runs /bin/bash unless told otherwise, and a distroless or musl body has no
# /bin/bash. The shell comes from the toolbelt closure, which is the one thing present
# regardless of the body's libc.
shell = "/.toolbelt/bin/bash"
if not os.access(shell, os.X_OK):
    die(f"the toolbelt closure has no bash at {shell}; a session view cannot be built "
        "without a shell from the toolbelt")
capsh = "/.toolbelt/bin/capsh"
if not os.access(capsh, os.X_OK):
    die(f"the toolbelt closure has no capsh at {capsh}; capabilities could not be dropped")


def launcher_command(socket_path: str, fd_number: int) -> str:
    return (f"exec python3 -m raigolmid.launcher.server --socket '{socket_path}' "
            f"--socket-fd {fd_number} --instance '{INSTANCE}' --generation '{GENERATION}'")


# --- 9b. the socket directory leaves the view ---------------------------------------------
#
# The launcher holds its listening descriptor, so nothing in the view needs the
# directory by path again. Left mounted, it would be writable by everything step 10 starts,
# because that runs as the directory's owner. A view could then replace another view's
# socket or rewrite `focused`, and it could connect to another view's launcher.
# Detached while SYS_ADMIN is still held; step 10 leaves nothing that could mount it back.
if libc.umount2(SOCK_DIR.encode(), MNT_DETACH) != 0:
    die_errno(f"detaching {SOCK_DIR} from the view")

# --- 10. become the user who owns the working copy ----------------------------------------
#
# `/work` is the user's own repository, owned by the user raigolmid runs as. Root does not
# bypass file modes — that is CAP_DAC_OVERRIDE, and the view drops it — so a launcher left as
# uid 0 cannot write a file or commit. The agent
# container already solved this for the same bind mount by running as that uid
# (`agents/claude/Dockerfile`); this is the session view's half.
#
# The identity is read from the socket directory rather than passed in, because that
# directory *is* the daemon's, and `bind_for_the_launcher` already trusts it for exactly
# this fact. Two sources for one identity is how they drift apart.
#
# Being the body's uid is also what lets a debugger here ptrace the body's processes: same
# uid, with no capability.
if (os.geteuid(), os.getegid()) != (owner.st_uid, owner.st_gid):
    try:
        os.setgroups([])
        os.setgid(owner.st_gid)
        os.setuid(owner.st_uid)
    except OSError as exc:
        die(f"could not become uid {owner.st_uid}: {exc}")
    # setuid away from root drops the whole capability set with it. capsh below is what
    # sets no_new_privs and makes the empty set explicit rather than incidental.
    log(f"running as uid {owner.st_uid}, the owner of the working copy")

log(f"dropping all capabilities; the launcher takes over on {sock}")
# capsh and bash both exec straight through, and the fd is already inheritable.
os.execv(capsh, [capsh, "--no-new-privs", "--caps=", f"--shell={shell}",
                 "--", "-c", launcher_command(sock, ordinary_fd)])

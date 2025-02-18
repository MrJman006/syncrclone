"""
Most of the rclone interfacing
"""
import json
import os
from collections import deque, defaultdict
import subprocess, shlex
import lzma
import time
import re
from itertools import zip_longest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import debug, log, MINRCLONE
from .cli import ConfigError
from .dicttable import DictTable
from . import utils

FILTER_FLAGS = {
    "--include",
    "--exclude",
    "--include-from",
    "--exclude-from",
    "--filter",
    "--filter-from",
    "--files-from",
}


def mkdir(path, isdir=True):
    if not isdir:
        path = os.path.dirname(path)
    try:
        os.mkdir(path)
    except OSError:
        pass


class LockedRemoteError(ValueError):
    pass


class RcloneVersionError(ValueError):
    pass


class Rclone:
    def __init__(self, config):
        self.config = config
        self.add_args = []  # logging, etc
        self.tmpdir = config.tempdir

        self.rclonetime = 0.0

        try:
            os.makedirs(self.tmpdir)
        except OSError:
            pass

        self.validate()

        self.backup_path, self.backup_path0 = {}, {}
        for AB in "AB":
            self.backup_path0[
                AB
            ] = f"backups/{config.now}_{self.config.name}_{AB}"  # really only used for top level non-workdir backups with delete
            self.backup_path[AB] = utils.pathjoin(
                getattr(config, f"workdir{AB}"), self.backup_path0[AB]
            )

        self.version_check()

    def version_check(self):
        """
        Check the rclone version and raise an error if it doesn't match.
        I have been struggling with edge cases on this regex (e.g., #27 and #28)
        but it also isn't critical so wrap everything in a try block.
        """
        log("rclone version:")
        res = self.call(["--version"], stream=True)
        try:
            rever = re.search(r"^rclone v?(.*)$", res, flags=re.MULTILINE)
            ver = rever.group(1)  # Will raise attribute error if could not parse
            if tuple(map(int, ver.split("."))) < tuple(map(int, MINRCLONE.split("."))):
                raise RcloneVersionError(
                    f"Must use rclone >= {MINRCLONE}. Currently using {ver}"
                )
        except RcloneVersionError:
            raise
        except:
            log("WARNING: Could not parse rclone version number.")
            log(f"         Minimum version: {MINRCLONE}")

    def validate(self):
        config = self.config
        attrs = ["rclone_flags", "rclone_flagsA", "rclone_flagsB"]
        for attr in attrs:
            for v in getattr(config, attr):
                if v in FILTER_FLAGS:
                    raise ConfigError(
                        f"'{attr}' cannot have '{v}' or any other filtering flags"
                    )

    def call(
        self, cmd, stream=False, logstderr=True, display_error=True, fl_remote=None
    ):
        """
        Call rclone. If streaming, will write stdout & stderr to
        log. If logstderr, will always send stderr to log (default)
        """
        config = self.config
        cmd = shlex.split(self.config.rclone_exe) + cmd
        debug("rclone:call", cmd)

        env = os.environ.copy()
        k0 = set(env)

        env.update(self.config.rclone_env)
        env["RCLONE_ASK_PASSWORD"] = "false"  # so that it never prompts

        debug_env = {k: v for k, v in env.items() if k not in k0}
        if "RCLONE_CONFIG_PASS" in debug_env:
            debug_env["RCLONE_CONFIG_PASS"] = "**REDACTED**"

        debug(f"rclone: env {debug_env}")

        if stream:
            stdout = subprocess.PIPE
            stderr = subprocess.STDOUT
        else:  # Stream both stdout and stderr to files to prevent a deadlock
            tns = time.time_ns()
            stdout = open(f"{config.tempdir}/std.{tns}.out", mode="wb")
            stderr = open(f"{config.tempdir}/std.{tns}.err", mode="wb")

        t0 = time.time()
        proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, env=env)

        if stream:
            out = []
            with proc.stdout:
                for line in iter(proc.stdout.readline, b""):
                    line = line.decode(
                        errors="backslashreplace"
                    )  # Allow for bad decoding. See https://github.com/Jwink3101/syncrclone/issues/16
                    line = line.rstrip()
                    log("rclone:", line)
                    out.append(line)
            out = "\n".join(out)
            err = ""  # Piped to stderr

        ## Special for file listing. Not general purpose... Will count lines -1
        if fl_remote:
            with open(stdout.name, "rb") as fp:  # 'b' to avoid dealing with encoding
                _c = 0
                _t = time.time()
                lines = []

                while True:
                    line = fp.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        time.sleep(0.01)
                        continue
                    lines.append(line)
                    if b"\n" in line:
                        # normally would yield/return b'\n'.join(lines) but we don't
                        # really care
                        lines = []

                        _c += 1
                        if time.time() - _t > config.list_status_dt:
                            log(f"Reading from {fl_remote}: File count {_c - 1}")
                            _t = time.time()

        proc.wait()
        self.rclonetime += time.time() - t0

        if not stream:
            stdout.close()
            stderr.close()
            with open(stdout.name, "rt") as F:
                out = F.read()
            with open(stderr.name, "rt") as F:
                err = F.read()
            if err and logstderr:
                log(" rclone stderr:", err)

        if proc.returncode:
            if display_error:
                log("RCLONE ERROR")
                log("CMD", cmd)
                if stream:
                    log("STDOUT and STDERR", out)
                else:
                    log("STDOUT", out.strip())
                    log("STDERR", err.strip())
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, output=out, stderr=err
            )
        if not logstderr:
            out = out + "\n" + err
        return out

    def push_file_list(self, filelist, remote=None):
        config = self.config
        AB = remote
        remote = getattr(config, f"remote{AB}")
        workdir = getattr(config, f"workdir{AB}")

        dst = utils.pathjoin(workdir, f"{AB}-{self.config.name}_fl.json.xz")
        src = os.path.join(self.tmpdir, f"{AB}_curr")
        mkdir(src, isdir=False)

        filelist = list(filelist)
        with lzma.open(src, "wt") as file:
            json.dump(filelist, file, ensure_ascii=False)

        cmd = (
            config.rclone_flags
            + self.add_args
            + getattr(config, f"rclone_flags{AB}")
            + ["copyto", src, dst]
        )

        self.call(cmd)

    def pull_prev_list(self, *, remote=None):
        config = self.config
        AB = remote
        remote = getattr(config, f"remote{AB}")
        workdir = getattr(config, f"workdir{AB}")
        src = utils.pathjoin(workdir, f"{AB}-{self.config.name}_fl.json.xz")
        dst = os.path.join(self.tmpdir, f"{AB}_prev")
        mkdir(dst, isdir=False)

        cmd = (
            config.rclone_flags
            + self.add_args
            + getattr(config, f"rclone_flags{AB}")
            + ["--retries", "1", "copyto", src, dst]
        )
        try:
            self.call(cmd, display_error=False, logstderr=False)
        except subprocess.CalledProcessError as err:
            # Codes (https://rclone.org/docs/#exit-code) 3,4 are expected if there is no list
            if err.returncode in {3, 4}:
                log(f"No previous list on {AB}. Reset state")
                return []
            log(f"WARNING: Unexpected rclone return. Resetting state in {AB}")
            return []

        try:
            with lzma.open(dst) as file:
                return json.load(file)
        except FileNotFoundError:
            log(f"WARNING: Missing previous state in {AB}. Resetting")
            return []

    def file_list(self, *, prev_list=None, remote=None):
        """
        Get both current and previous file lists. If prev_list is
        set, then it is not pulled.

        Options:
        -------
        prev_list (list or DictTable)
            Previous file list. Specify if it is already known

        remote
            A or B


        It will decide if it needs hashes and whether to reuse them based
        on the config.
        """
        config = self.config

        AB = remote
        remote = getattr(config, f"remote{AB}")

        compute_hashes = "hash" in [config.compare, getattr(config, f"renames{AB}")]
        reuse = compute_hashes and getattr(config, f"reuse_hashes{AB}")

        # build the command including initial filters *before* any filters set
        # by the user
        cmd = [
            "lsjson",
            "--filter",
            "+ /.syncrclone/LOCK/*",
            "--filter",
            "- /.syncrclone/**",
        ]

        if compute_hashes and not reuse:
            cmd.append("--hash")

        if not config.always_get_mtime and not (
            config.compare == "mtime"
            or getattr(config, f"renames{AB}") == "mtime"
            or config.conflict_mode in ("newer", "older")
        ):
            cmd.append("--no-modtime")

        # Now that my above filters, add user flags
        cmd += (
            config.rclone_flags
            + self.add_args
            + getattr(config, f"rclone_flags{AB}")
            + config.filter_flags
        )

        cmd.extend(
            [
                "-R",
                "--no-mimetype",
                "--files-only",
            ]  # Not needed so will be faster
        )

        cmd.append(remote)

        files_raw = self.call(cmd, fl_remote="A")

        files = json.loads(files_raw)
        debug(f"{AB}: Read {len(files)}")
        for file in files:
            for key in [
                "IsDir",
                "Name",
                "ID",
                "Tier",
            ]:  # Things we do not need. There may be others but it doesn't hurt
                file.pop(key, None)
            mtime = file.pop("ModTime", None)
            file["mtime"] = utils.RFC3339_to_unix(mtime) if mtime else None

        # Make them DictTables
        files = DictTable(files, fixed_attributes=["Path", "Size", "mtime"])
        debug(f"{AB}: Read {len(files)}")

        if config.reset_state:
            debug(f"Reset state on {AB}")
            prev_list = []
        else:
            prev_list = self.pull_prev_list(remote=AB)

        if not isinstance(prev_list, DictTable):
            prev_list = DictTable(prev_list, fixed_attributes=["Path", "Size", "mtime"])

        if not compute_hashes or "--hash" in cmd:
            return files, prev_list

        # update with prev if possible and then get the rest
        not_hashed = []
        updated = 0
        for file in files:  # size,mtime,filename
            prev = prev_list[
                {k: file[k] for k in ["Size", "mtime", "Path"]}
            ]  # Will not find if no mtime not in remote
            if (
                not prev or "Hashes" not in prev or not prev.get("mtime", None)
            ):  # or '_copied' in prev: # Do not reuse a copied hash in case of incompatability
                not_hashed.append(file["Path"])
                continue
            updated += 1
            file["Hashes"] = prev["Hashes"]

        if len(not_hashed) == 0:
            debug(f"{AB}: Updated {updated}. No need to fetch more")
            return files, prev_list
        debug(f"{AB}: Updated {updated}. Fetching hashes for {len(not_hashed)}")

        tmpfile = self.tmpdir + f"/{AB}_update_hash"
        with open(tmpfile, "wt") as file:
            file.write("\n".join(f for f in not_hashed))

        cmd = ["lsjson", "--hash", "--files-from", tmpfile]
        cmd += (
            config.rclone_flags + self.add_args + getattr(config, f"rclone_flags{AB}")
        )

        cmd.extend(
            ["-R", "--no-mimetype", "--files-only"]  # Not needed so will be faster
        )

        cmd.append(remote)

        updated = json.loads(self.call(cmd))
        for file in updated:
            if "Hashes" in file:
                files[{"Path": file["Path"]}]["Hashes"] = file["Hashes"]

        debug(f"{AB}: Updated hash on {len(updated)} files")

        return files, prev_list

    def delete_backup_move(self, remote, dels, backups, moves):
        """
        Perform deletes, backups and moves. Same basic codes but with different
        reporting. If moves, files are (src,dest) tuples.
        """
        ## Optimization Notes
        #
        # Note: This was previously heavily optimized to avoid overlapping remotes.
        #       However, as of 1.59.0, this is no longer needed and these optimizations
        #       have been undone.
        #
        # rclone is faster if you can do many actions at once. For example, to delete
        # files, it is faster to do `delete --files-from <list-of-files>`.
        #
        # NOTE: The order here is important!
        #
        #     Delete w/ backup: Depends on the remote and the workdir settings
        #       Use `move --files-from` (ability added at 1.59.0)
        #
        #     Moves:
        #       When the file name itself (leaf) changes, we must just do `moveto` calls.
        #       Otherwise, we optimize moves when there there is more than one moved
        #       file at a base directory such as when a directory is moved.
        #       Note: we do NOT do directory moves but this is faster than moveto calls!
        #
        #       Consider:
        #
        #         "A/deep/sub/dir/file1.txt" --> "A/deeper/sub/dir/file1.txt"
        #         "A/deep/sub/dir/file2.txt" --> "A/deeper/sub/dir/file2.txt"
        #
        #       The names ('file1.txt' and 'file2.txt') are the same and there are two
        #       moves from "A/deep" to "A/deeper". Therefore, rather than call moveto
        #       twice, we do:
        #
        #         rclone move "A/deep" "A/deeper" --files-from files.txt
        #
        #       Where 'files.txt' is:
        #          sub/dir/file1.txt
        #          sub/dir/file2.txt"
        #
        #     Backups:
        #       Use the `copy/move --files-from`
        #
        #     Delete w/o backup
        #       Use `delete --files-from`
        #
        # References:
        #
        # https://github.com/rclone/rclone/issues/1319
        #   Explains the issue with the quote:
        #
        #   > For a remote which doesn't it has to move each individual file which might
        #   > fail and need a retry which is where the trouble starts...
        #
        # https://github.com/rclone/rclone/issues/1082
        #   Tracking issue. Also references https://forum.rclone.org/t/moving-the-contents-of-a-folder-to-the-root-directory/914/7
        #
        config = self.config
        AB = remote
        remote = getattr(config, f"remote{AB}")

        cmd0 = [None]  # Will get set later
        cmd0 += ["-v", "--stats-one-line", "--log-format", ""]
        # We know in all cases, the dest doesn't exists. For backups, it's totally new and
        # for moves, if it existed, it wouldn't show as a move. So never check dest,
        # always transfer, and do not traverse
        cmd0 += ["--no-check-dest", "--ignore-times", "--no-traverse"]
        cmd0 += (
            config.rclone_flags + self.add_args + getattr(config, f"rclone_flags{AB}")
        )

        dels = dels.copy()
        moves = moves.copy()
        backups = backups.copy()  # Will be appended so make a new copy

        if config.backup:
            dels_back = dels
            dels_noback = []
        else:
            dels_back = []
            dels_noback = dels

        debug(AB, "dels_back", dels_back)
        debug(AB, "dels_noback", dels_noback)
        debug(AB, "moves", moves)

        ## Delete with backups
        cmd = cmd0.copy()
        cmd[0] = "move"

        cmd += ["--retries", "4"]  # Extra safe

        tmpfile = self.tmpdir + f"/{AB}_movedel_del_nb"
        with open(tmpfile, "wt") as file:
            file.write("\n".join(dels_back))

        cmd += ["--files-from", tmpfile]
        cmd += [remote, self.backup_path[AB]]

        debug("Delete w/ backup", dels_back)
        for line in self.call(cmd, stream=False, logstderr=False).split("\n"):
            line = line.strip()
            if line:
                log("rclone:", line)

        ## Moves
        moveto = []  # src,dst
        move = defaultdict(list)

        for src, dst in moves:
            src, dst = Path(src), Path(dst)
            sparts = src.parts
            dparts = dst.parts

            # Need to zip_longest so that if one is shorter, you don't exhaust the
            # loop before ixdiv increments
            for ixdiv, (spart, dpart) in enumerate(
                zip_longest(sparts[::-1], dparts[::-1])
            ):
                if spart != dpart:
                    break

            if ixdiv == 0:  # different name. Must moveto
                moveto.append((str(src), str(dst)))
                continue

            srcdir = os.path.join(*sparts[:-ixdiv]) if sparts[:-ixdiv] else ""
            dstdir = os.path.join(*dparts[:-ixdiv]) if dparts[:-ixdiv] else ""
            file = os.path.join(*sparts[-ixdiv:])  # == dparts[-ixdiv:]
            # break
            move[srcdir, dstdir].append(file)

        # Now if only one item is being moved, we change it back to a moveto
        # copy so I can modify move in place
        for (srcdir, dstdir), files in move.copy().items():
            if len(files) > 1:
                continue
            src = os.path.join(srcdir, files[0])
            dst = os.path.join(dstdir, files[0])
            moveto.append((src, dst))
            del move[srcdir, dstdir]

        def _moveto(file):
            t = f"Move {repr(file[0])} --> {repr(file[1])}"
            src = utils.pathjoin(remote, file[0])
            dst = utils.pathjoin(remote, file[1])

            cmd = cmd0.copy()
            cmd[0] = "moveto"
            cmd += [src, dst]
            return t, self.call(cmd, stream=False, logstderr=False)

        with ThreadPoolExecutor(max_workers=int(config.action_threads)) as exe:
            for action, res in exe.map(_moveto, moveto):
                log(action)
                for line in res.split("\n"):
                    line = line.strip()
                    if line:
                        log("rclone:", line)

        for ii, ((srcdir, dstdir), files) in enumerate(move.items()):
            log(f"Grouped Move {repr(srcdir)} --> {repr(dstdir)}")
            for file in files:
                log(f"  {repr(file)}")

            flistpath = self.tmpdir + f"move_{ii}.txt"
            with open(flistpath, "wt") as fout:
                fout.write("\n".join(files))

            cmd = cmd0.copy()
            cmd[0] = "move"
            cmd += [
                utils.pathjoin(remote, srcdir),
                utils.pathjoin(remote, dstdir),
                "--files-from",
                flistpath,
            ]
            self.call(cmd, stream=True)

        ## Backups
        if backups:
            cmd = cmd0.copy()
            if config.backup_with_copy is None:
                cmd[0] = "copy" if self.copy_support(AB) else "move"
                debug(f"Automatic Copy Support: {cmd[0]}")
            elif config.backup_with_copy:
                cmd[0] = "copy"
                debug("Always using copy")
            else:
                cmd[0] = "move"
                debug("Always using move")

            cmd += ["--retries", "4"]  # Extra safe

            tmpfile = self.tmpdir + f"/{AB}_movedel_back"
            with open(tmpfile, "wt") as file:
                file.write("\n".join(backups))

            src = remote
            dst = self.backup_path[AB]

            cmd += ["--files-from", tmpfile, src, dst]
            debug("backing up", backups)
            for line in self.call(cmd, stream=False, logstderr=False).split("\n"):
                line = line.strip()
                if line:
                    log("rclone:", line)

        ## Deletes w/o backup
        if dels_noback:
            tmpfile = self.tmpdir + f"/{AB}_del"
            with open(tmpfile, "wt") as file:
                file.write("\n".join(dels))
            cmd = cmd0.copy()
            cmd += ["--files-from", tmpfile, remote]
            cmd[0] = "delete"
            log("deleting")
            for line in self.call(cmd, stream=False, logstderr=False).split("\n"):
                line = line.strip()
                if line:
                    log("rclone:", line)

    def transfer(self, mode, matched_size, diff_size):
        config = self.config
        if mode == "A2B":
            src, dst = config.remoteA, config.remoteB
        elif mode == "B2A":
            src, dst = config.remoteB, config.remoteA

        if not matched_size and not diff_size:
            return

        cmd = ["copy"]
        cmd += ["-v", "--stats-one-line", "--log-format", ""]
        cmd += (
            config.rclone_flags + self.add_args
        )  # + getattr(config,f'rclone_flags{AB}')
        # ^^^ Doesn't get used here.
        # TODO: Consider using *both* as opposed to just one
        # TODO: Make it more clear in the config

        # We need to be careful about flags for the transfer. Ideally, we would include
        # either --ignore-times or --no-check-dest to *always* transfer. The problem
        # is that if any of them need to retry, it will unconditionally transfer
        # *everything* again!
        #
        # The solution then is to let rclone decide for itself what to transfer from the
        # the file list. The problem here is we need to match `--size-only` for size
        # compare or `--checksum` for hash compare. If the compare is hash, we *already*
        # did it. And even for mtime, we don't want to request the ModTime on remotes
        # like S3. The solution is therefore as follows:
        #   - Decide what changes resulted in size changes (probably most). Run them with
        #     --size-only. Note that if `compare = 'size'`, this is implicit anyway
        #   - For those that should transfer but size has changed, run with nothing or
        #     --checksum. Do not need to consider --size-only since it will have been
        #     captured.
        #
        # This is still imperfect because of the additional rclone calls but it is safer!

        # This flags is not *really* needed but based on the docs (https://rclone.org/docs/#no-traverse),
        # it is likely the case that only a few files will be transfers. This number is a WAG. May change
        # the future or be settable.

        # This was an experiment. Keep it but comment out
        # if self.config.backup:
        #     cmd += ['--backup-dir',self.backup_path[{'B2A':'A','A2B':'B'}[mode]]]

        # diff_size first
        if diff_size:
            cmddiff = cmd + ["--size-only"]  # We KNOW they are different sized

            if len(diff_size) <= 100:
                cmddiff.append("--no-traverse")

            tmpfile = self.tmpdir + f"{mode}_transfer-diff_size"
            with open(tmpfile, "wt") as file:
                file.write("\n".join(diff_size))
            cmddiff += ["--files-from", tmpfile, src, dst]

            self.call(cmddiff, stream=True)

        if matched_size:
            cmdmatch = cmd.copy()

            if config.compare == "hash":
                cmdmatch.append("--checksum")
            elif config.compare == "size":
                raise ValueError("This should NOT HAPPEN")
            # else: pass # This just uses ModTime default of rclone

            if len(matched_size) <= 100:
                cmdmatch.append("--no-traverse")

            tmpfile = self.tmpdir + f"{mode}_transfer-matched_size"
            with open(tmpfile, "wt") as file:
                file.write("\n".join(matched_size))

            cmdmatch += ["--files-from", tmpfile, src, dst]
            self.call(cmdmatch, stream=True)

    def copylog(self, remote, srcfile, logname):
        config = self.config
        AB = remote

        dst = utils.pathjoin(getattr(config, f"workdir{AB}"), "logs", logname)

        cmd = ["copyto"]
        cmd += ["-v", "--stats-one-line", "--log-format", ""]
        cmd += (
            config.rclone_flags + self.add_args + getattr(config, f"rclone_flags{AB}")
        )

        cmd += ["--no-check-dest", "--ignore-times", "--no-traverse"]
        self.call(cmd + [srcfile, dst], stream=True)

    def lock(self, breaklock=False, remote="both"):
        """
        Sets or break the locks. Does *not* check for them first!
        """
        if remote == "both":
            self.lock(breaklock=breaklock, remote="A")
            self.lock(breaklock=breaklock, remote="B")
            return
        elif remote not in "AB":
            raise ValueError(
                f"Must specify remote as 'both', 'A', or 'B'. Specified {remote}"
            )

        config = self.config
        AB = remote
        remote = getattr(config, f"remote{AB}")
        workdir = getattr(config, f"workdir{AB}")

        cmd = [None]
        cmd += ["-v", "--stats-one-line", "--log-format", ""]
        cmd += (
            config.rclone_flags + self.add_args + getattr(config, f"rclone_flags{AB}")
        )

        cmd += ["--ignore-times", "--no-traverse"]

        lockdest = utils.pathjoin(workdir, f"LOCK/LOCK_{config.name}")

        log("")
        if not breaklock:
            log(f"Setting lock on {AB}")
            cmd[0] = "copyto"

            lockfile = utils.pathjoin(self.tmpdir, f"LOCK_{config.name}")
            with open(lockfile, "wt") as F:
                F.write(config.now)
            self.call(cmd + [lockfile, lockdest], stream=True)
        else:
            log(f"Breaking locks on {AB}. May return errors if {AB} is not locked")
            cmd[0] = "delete"
            try:
                self.call(
                    cmd + ["--retries", "1", lockdest], stream=True, display_error=False
                )
            except subprocess.CalledProcessError:
                log("No locks to break. Safely ignore rclone error")

    def check_lock(self, remote="both"):
        if remote == "both":
            self.check_lock("A")
            self.check_lock("B")
            return

        config = self.config
        AB = remote
        workdir = getattr(config, f"workdir{AB}")
        lockdest = utils.pathjoin(workdir, f"LOCK/LOCK_{config.name}")

        cmd = (
            config.rclone_flags
            + self.add_args
            + getattr(config, f"rclone_flags{AB}")
            + ["--retries", "1", "lsf", lockdest]
        )

        try:
            self.call(cmd, display_error=False, logstderr=False)
        except subprocess.CalledProcessError as err:
            # Codes (https://rclone.org/docs/#exit-code) 3,4 are expected if there is no file
            if err.returncode in {3, 4}:
                return True
            else:
                raise

        raise LockedRemoteError(f"Locked on {AB}, {lockdest}")

    def rmdirs(self, remote, dirlist):
        """
        Remove the directories in dirlist. dirlist is sorted so the deepest
        go first and then they are removed. Note that this is done this way
        since rclone will not delete if *anything* exists there; even files
        we've ignored.
        """
        config = self.config
        AB = remote
        remote = getattr(config, f"remote{AB}")

        # Originally, I sorted by length to get the deepest first but I can
        # actually get the root of them so that I can call rmdirs (with the `s`)
        # and let that go deep

        rmdirs = []
        for diritem in sorted(dirlist):
            # See if it's parent is already there. This can 100% be improved
            # since the list is sorted. See https://stackoverflow.com/q/7380629/3633154
            # for example. But it's not worth it here
            if any(diritem.startswith(f"{rmdir}/") for rmdir in rmdirs):
                continue  # ^^^ Add the / so it gets child dirs only
            rmdirs.append(diritem)

        cmd = config.rclone_flags + self.add_args + getattr(config, f"rclone_flags{AB}")
        cmd += [
            "rmdirs",
            "-v",
            "--stats-one-line",
            "--log-format",
            "",
            "--retries",
            "1",
        ]

        def _rmdir(rmdir):
            _cmd = cmd + [utils.pathjoin(remote, rmdir)]
            try:
                return rmdir, self.call(_cmd, stream=False, logstderr=False)
            except subprocess.CalledProcessError:
                # This is likely due to the file not existing. It is acceptable
                # for this error since even if it was something else, not
                # properly removing empty dirs is acceptable
                return rmdir, "<< could not delete >>"

        with ThreadPoolExecutor(max_workers=int(config.action_threads)) as exe:
            for rmdir, res in exe.map(_rmdir, rmdirs):
                log(f"rmdirs (if possible) on {AB}: {rmdir}")
                for line in res.split("\n"):
                    line = line.strip()
                    if line:
                        log("rclone:", line)

    @utils.memoize
    def features(self, remote):
        """Get remote features"""
        config = self.config
        AB = remote
        remote = getattr(config, f"remote{AB}")
        features = json.loads(
            self.call(
                ["backend", "features", remote]
                + config.rclone_flags
                + getattr(config, f"rclone_flags{AB}"),
                stream=False,
            )
        )
        return features.get("Features", {})

    def copy_support(self, remote):
        """
        Return whether or not the remote supports  server-side copy

        Defaults to False for safety
        """
        r = self.features(remote).get("Copy", False)
        debug(f"Copy Support {remote = }: {r}")
        return r

    def move_support(self, remote):
        """
        Return whether or not the remote supports  server-side move

        Defaults to False for safety
        """
        r = self.features(remote).get("Move", False)
        debug(f"Move Support {remote = }: {r}")
        return r

    def empty_dir_support(self, remote):
        """
        Return whether or not the remote supports empty-dirs

        Defaults to True since if it doesn't support them, calling rmdirs
        will just do nothing
        """
        r = self.features(remote).get("CanHaveEmptyDirectories", True)
        debug(f"EmptyDir Support {remote = }: {r}")
        return r

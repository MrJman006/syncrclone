#!/usr/bin/env python
import argparse
import sys
import os
import random
import warnings
import shutil

_showwarning = warnings.showwarning  # store this

import copy

from . import debug, set_debug, get_debug, log, __version__
from . import utils

_RETURN = False  # This gets reset by tests to make the cli return the object


class ConfigError(ValueError):
    pass


class NotAnSRCDirectoryError(ValueError):
    pass


class Config:
    def __init__(self, configpath=None):
        log(f"syncrclone ({__version__})")
        log(f"config path: '{configpath}'")
        self._configpath = configpath
        self._config = {"_configpath": self._configpath}

        templatepath = os.path.join(os.path.dirname(__file__), "config_example.py")

        try:
            with open(templatepath, "rt") as file:
                self._template = file.read()
        except:
            # This is a hack for when it is in an egg file. I need to figure
            # out a better way
            import zipfile

            with zipfile.ZipFile(__file__[: -len("/syncrclone/cli.py")]) as zf:
                self._template = zf.read("syncrclone/config_example.py").decode()

    def _write_template(self, outpath=None, localmode=False):
        if outpath is None:
            outpath = self._configpath

        txt = self._template.replace("__VERSION__", __version__)
        txt = txt.replace("__RANDOM__", utils.random_str(5))

        if localmode:
            txt = txt.replace(
                'remoteA = "<<MUST SPECIFY>>"', 'remoteA = "../" # set automatically'
            )

        if os.path.exists(outpath):
            raise ValueError(
                f"Path '{outpath}' exists. Specify a different path or move the existing file"
            )

        try:
            os.makedirs(os.path.dirname(outpath))
        except OSError:
            pass

        with open(outpath, "wt") as file:
            file.write(txt)

        debug(f"Wrote template config to {outpath}")

    def parse(self, skiplog=False, override=""):
        if self._configpath is None:
            raise ValueError("Must have a config path")

        self._config["log"] = self._config["print"] = log
        self._config["debug"] = debug
        self._config["__file__"] = os.path.abspath(self._configpath)
        self._config["__dir__"] = os.path.dirname(self._config["__file__"])
        self._config["__CPU_COUNT__"] = os.cpu_count()

        exec(self._template, self._config)  # Only reset if reading

        with open(self._configpath, "rt") as file:
            os.chdir(self._config["__dir__"])  # Globally set the program here
            text = file.read()

        # Add the override text before and after in case it sets functionality
        exec(override + "\n\n" + text + "\n\n" + override, self._config)

        # clean up all of the junk
        _tmp = {}
        exec("", _tmp)
        for key in _tmp:
            self._config.pop(key, None)
        for key in ["log", "print", "debug"]:
            self._config.pop(key, None)

        self.validate(skiplog=skiplog)

    def validate(self, skiplog=False):
        # versions. This can be changed in the future if things are broken
        config_ver = self._config["_syncrclone_version"].split(".")
        if config_ver != ["__VERSION__"]:
            config_ver = (int(config_ver[0]), int(config_ver[1])) + tuple(
                config_ver[2:]
            )
            if config_ver < (20210419, 0):
                warnings.warn(
                    "Previous behavior of conflict_mode changed. Please update your config"
                )
            # raise ConfigError(f"Version '{self._config['_syncrclone_version']}' is too old. Update config")

        for AB in "AB":
            if self._config[f"remote{AB}"] == "<<MUST SPECIFY>>":
                raise ConfigError(f"Must specify 'remote{AB}'")

        reqs = {
            "compare": ("size", "mtime", "hash"),
            "hash_fail_fallback": ("size", "mtime", None),
            "tag_conflict": (True, False),
        }
        for AB in "AB":
            reqs[f"reuse_hashes{AB}"] = True, False
            reqs[f"renames{AB}"] = "size", "mtime", "hash", None

        reqs["conflict_mode"] = ["tag", None]
        for mode in ("A", "B", "older", "newer", "smaller", "larger"):
            reqs["conflict_mode"].extend([mode, f"{mode}_tag"])

        for key, options in reqs.items():
            val = self._config[key]
            if val not in options:
                raise ConfigError(f"'{key}' must be in {options}. Specified '{val}'")

        self._config["action_threads"] = int(max([self._config["action_threads"], 1]))

        if self._config["tempdir"] is None:
            import tempfile

            self._config["tempdir"] = tmpdir = tempfile.TemporaryDirectory().name
        try:
            os.makedirs(self._config["tempdir"])
        except OSError:
            pass
        log(f"temp dir: {repr(self._config['tempdir'])}")

        # To be deprecated
        if self._config["conflict_mode"].endswith("_tag"):
            newmode = self._config["conflict_mode"][:-4]
            self._config["tag_conflict"] = True
            warnings.warn(
                (
                    f" conflict_mode '{self._config['conflict_mode']}' deprecated. "
                    f"Use `conflict_mode = {newmode}` and `tag_conflict = True`"
                )
            )
            self._config["conflict_mode"] = newmode

        if skiplog:
            return

        if not self._config["avoid_relist"]:
            log(
                (
                    "NOTE: 'avoid_relist' is set to False. For *most* use-cases, "
                    "it should be set to True to improve performance!"
                )
            )
        if self._config.get("log_dest", False):
            log("WARNING: log_dest is deprecated and ignored. See `save_logs`")

        # verify non-overlap of remotes. Not perfect
        for AB in "AB":
            workdir = self._config[f"workdir{AB}"]
            remote = self._config[f"remote{AB}"]

            if not workdir:
                continue

            workdir = os.path.abspath(workdir) if ":" not in workdir else workdir
            remote = os.path.abspath(remote) if ":" not in remote else remote

            if not os.path.relpath(
                workdir.replace(":", "/"), remote.replace(":", "/")
            ).startswith("../"):
                raise ConfigError("Cannot have overlapping workdir and remote")

        if any(self._config[f"workdir{AB}"] for AB in "AB"):
            if self._config["sync_backups"]:
                raise ConfigError("Cannot have sync_backups with specified workdirs")
            log(f"WARNING: specified workdirs is experimental. Use with caution.")

        log(f"A: '{self.remoteA}'")
        log(f"B: '{self.remoteB}'")

        if "--exclude-if-present" in self._config["filter_flags"]:
            warnings.warn("'--exclude-if-present' can cause issues. See readme")

    def __repr__(self):
        # Need to watch out for RCLONE_CONFIG_PASS in rclone_env
        # make a copy of the dict fixing that one but do not
        # just do a deepcopy in case the user imported modules
        cfg = self._config.copy()
        cfg["rclone_env"] = cfg["rclone_env"].copy()

        if "RCLONE_CONFIG_PASS" in cfg.get("rclone_env", {}):
            cfg["rclone_env"]["RCLONE_CONFIG_PASS"] = "**REDACTED**"

        return "".join(
            [
                "Config(",
                ", ".join(
                    f"{k}={repr(v)}" for k, v in cfg.items() if not k.startswith("_")
                ),
                ")",
            ]
        )

    def __getattr__(self, attr):
        return self._config[attr]

    def __setattr__(self, attr, value):
        if attr.startswith("_"):
            return super(Config, self).__setattr__(attr, value)

        self._config[attr] = value


DESCRIPTION = "Simple bi-directional sync using rclone"
EPILOG = """\
See syncrclone config file template for details and settings
"""


def cli(argv=None):
    from .main import SyncRClone

    parser = argparse.ArgumentParser(
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "configpath",
        nargs="?",
        default=".",
        help=(
            "Specify the path to the config file for this sync job. "
            "If `--new`, will be the path to write a new template. "
            "If specified as a directory, will search upwards for "
            "'.syncrclone/config.py' or create it here if `--new`."
        ),
    )

    parser.add_argument(
        "--break-lock",
        choices=["both", "A", "B"],
        help="Break locks on either A, B or both remotes",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Debug messages will be printed"
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode and do not change anything. See also --interactive",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Similar to --dry-run except it will show planned actions and prompt as to whether or not to proceed",
    )
    parser.add_argument(
        "--new", action="store_true", help="Path to save a new config file"
    )
    parser.add_argument(
        "--no-backup", action="store_true", help="Do not do any backups on this run"
    )
    parser.add_argument(
        "--override",
        action="append",
        default=list(),
        metavar="'OPTION = VALUE'",
        help=(
            "Override any config option for this call only. Must be specified as "
            "'OPTION = VALUE', where VALUE should be properly shell escaped. "
            "Can specify multiple times. There is no input validation of any sort."
        ),
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help=(
            "Will reset the state of the sync pairs. This will assume the two have "
            "not been synced and the end result is the UNION of the two remotes "
            "(i.e. no delete propogation, all modified files look like conflicts, etc). "
            "Also rehashes all files if applicable. It is best to run a regular sync "
            "and then perform a reset."
        ),
    )
    parser.add_argument(
        "--version", action="version", version="syncrclone-" + __version__
    )

    if argv is None:
        argv = sys.argv[1:]

    cliconfig = parser.parse_args(argv)

    if cliconfig.debug:
        set_debug(True)
        warnings.showwarning = _showwarning  # restore
    else:
        set_debug(False)
        warnings.showwarning = (
            showwarning  # Monkey patch warnings.showwarning for CLI usage
        )

    debug("argv:", argv)
    debug("CLI config:", cliconfig)

    try:
        if cliconfig.interactive and cliconfig.dry_run:
            raise ValueError("Cannot set `--dry-run` AND `--interactive`")

        # Decide if local mode or remote mode.
        localmode = os.path.isdir(cliconfig.configpath)
        debug(f"Localmode: {localmode}")
        if localmode:
            if cliconfig.new:
                cliconfig.configpath = os.path.join(
                    cliconfig.configpath, ".syncrclone/config.py"
                )
            else:
                cliconfig.configpath = utils.search_upwards(cliconfig.configpath)
                if not cliconfig.configpath:
                    raise NotAnSRCDirectoryError()
                debug(f"Found config: '{cliconfig.configpath}'")

        config = Config(cliconfig.configpath)

        if cliconfig.new:
            config._write_template(localmode=localmode)
            log(f"Config file written to '{cliconfig.configpath}'")
            return

        if not os.path.exists(cliconfig.configpath):
            raise ConfigError(f"config file '{cliconfig.configpath}' does not exist")

        if cliconfig.override:
            for item in cliconfig.override:
                log(f"CLI Override: {item}")

        config.parse(
            override="\n".join(cliconfig.override)
        )  # NOTE: This now changes where the entire program is executed to the path of that file!

        noback = cliconfig.no_backup
        del cliconfig.no_backup  # == to pop
        if noback:
            config.backup = False  # Override setting

        for key, val in vars(cliconfig).items():
            setattr(config, key, val)

        # Reset workdir
        for AB in "AB":
            workdir = getattr(config, f"workdir{AB}")
            setattr(config, f"workdir0{AB}", workdir)
            if not workdir:
                setattr(
                    config,
                    f"workdir{AB}",
                    utils.pathjoin(getattr(config, f"remote{AB}"), ".syncrclone"),
                )

        debug("config:", config)
        r = SyncRClone(config, break_lock=config.break_lock)
        if _RETURN:
            return r
        # Do this iff not returning
        try:
            shutil.rmtree(r.config.tempdir)
        except OSError:
            log(
                f"WARNING (unlogged): Could not remote tempdir {repr(r.config.tempdir)}"
            )
    except NotAnSRCDirectoryError:
        msg = (
            "ERROR: Could not find ''.syncrclone/config.py' in the specified or "
            "implied path.\nEXITING"
        )
        log(msg, file=sys.stdout, flush=True)
        sys.exit(2)

    except Exception as E:
        tmpdir = config.tempdir
        print(
            f"ERROR. Dumping logs (with debug) to '{tmpdir}/log.txt'", file=sys.stderr
        )
        with open(f"{tmpdir}/log.txt", "wt") as fout:
            fout.write("\n".join(line for _, line in log.hist))

        if get_debug():
            raise

        log("ERROR: " + str(E), file=sys.stderr)
        sys.exit(1)


def showwarning(*args, **kwargs):
    log("WARNING", str(args[0]), file=sys.stderr)

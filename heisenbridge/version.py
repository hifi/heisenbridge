import os
import shutil
import subprocess

module_dir = os.path.dirname(__file__)
root_dir = module_dir + "/../"

__version__ = "0.0.0"
__git_version__ = None

if os.path.exists(module_dir + "/version.txt"):
    __version__ = open(module_dir + "/version.txt").read().strip()

if os.path.exists(root_dir + ".git") and shutil.which("git"):
    try:
        git_env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "LANG": "C",
            "LC_ALL": "C",
        }
        git_bits = (
            subprocess.check_output(["git", "describe", "--tags"], stderr=subprocess.DEVNULL, cwd=root_dir, env=git_env)
            .strip()
            .decode("ascii")
            .split("-")
        )

        __git_version__ = git_bits[0][1:]

        if len(git_bits) > 1:
            __git_version__ += f".dev{git_bits[1]}"

        if len(git_bits) > 2:
            __git_version__ += f"+{git_bits[2]}"

        # always override version with git version if we have a valid version number
        __version__ = __git_version__
    except (subprocess.SubprocessError, OSError):
        pass

# Included to allow for editable installs
import importlib.util

from setuptools import Command
from setuptools import setup
from setuptools.command.build_py import build_py

# pull git or local version
spec = importlib.util.spec_from_file_location("version", "heisenbridge/version.py")
version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version)


class GenerateVersionCommand(Command):
    description = "Generate version.txt"
    user_options = []

    def run(self):
        with open("heisenbridge/version.txt", "w") as version_file:
            version_file.write(version.__version__)

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass


class BuildPyCommand(build_py):
    def run(self):
        GenerateVersionCommand.run(None)
        build_py.run(self)


setup(
    version=version.__version__,
    cmdclass={"gen_version": GenerateVersionCommand, "build_py": BuildPyCommand},
    packages=["heisenbridge"],
    package_data={"heisenbridge": ["version.txt"]},
)

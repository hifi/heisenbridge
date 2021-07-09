# Included to allow for editable installs
import importlib.util

from setuptools import setup
from setuptools.command.build_py import build_py

# pull git or local version
spec = importlib.util.spec_from_file_location("version", "heisenbridge/version.py")
version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version)


class BuildPyCommand(build_py):
    def run(self):

        with open("heisenbridge/version.txt", "w") as version_file:
            version_file.write(version.__version__)

        build_py.run(self)


setup(
    version=version.__version__,
    cmdclass={"build_py": BuildPyCommand},
    packages=["heisenbridge"],
    package_data={"heisenbridge": ["version.txt"]},
)

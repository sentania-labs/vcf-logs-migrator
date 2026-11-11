"""Headless migration services shared by the CLI and future GUI."""

import logging
from importlib.metadata import PackageNotFoundError, version

logging.getLogger(__name__).addHandler(logging.NullHandler())
logging.getLogger(__name__).propagate = False

try:
    __version__ = version("vcf-logs-migrator")
except PackageNotFoundError:
    __version__ = "0+unknown"

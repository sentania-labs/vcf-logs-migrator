"""Headless migration services shared by the CLI and future GUI."""

import logging

logging.getLogger(__name__).addHandler(logging.NullHandler())
logging.getLogger(__name__).propagate = False

__version__ = "0.1.1"

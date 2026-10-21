"""Hala: a local file converter for KDE Plasma."""

import logging
import sys

__version__ = "0.4.0"
# How hala starts itself again (the drop zone and shelf hand work to a new process). -I keeps
# the current folder off the import path: a folder there named "hala" mustn't stand in for us.
SELF = [sys.executable, "-I", "-m", "hala.cli"]

# Library-style logging: nothing is printed unless a front end (the desktop runner, the drop
# zone) sets up a handler. The CLI reports failures itself.
logging.getLogger("hala").addHandler(logging.NullHandler())

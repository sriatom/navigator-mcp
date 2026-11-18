"""Moved to the Cisco vendor pack: navigator_packs/cisco/cli.py. This module name stays as an alias of it."""
import sys

from navigator_packs.cisco import cli as _pack_module

sys.modules[__name__] = _pack_module

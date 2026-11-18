"""Moved to the Palo Alto vendor pack: navigator_packs/paloalto/writer.py. This module name stays as an alias of it."""
import sys

from navigator_packs.paloalto import writer as _pack_module

sys.modules[__name__] = _pack_module

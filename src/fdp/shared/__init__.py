"""Shared kernel — cross-cutting utilities used by every module.

This is the only module other contexts may import freely. Anything in here
must genuinely cross context boundaries; if a utility belongs to one context,
it stays in that context.

Submodules are imported by full dotted path (``from fdp.shared.errors import
NotFound``); this ``__init__`` deliberately does not re-export them, so the
import graph stays explicit.
"""

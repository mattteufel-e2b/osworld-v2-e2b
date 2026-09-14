"""Defer a heavy optional import until something actually uses it."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types


class _LazyModule(types.ModuleType):
    """Stand-in that loads the real module on the first attribute access.

    ``importlib.util.LazyLoader`` cannot do this job: a later plain ``import x``
    reads ``x.__spec__`` and that access alone forces the real import. Here
    ``__spec__``/``__path__`` are ordinary attributes, so ``import x`` binds the
    stand-in and only ``x.Something`` pays for the load.

    Importers keep their binding to this stand-in, so after the load its
    namespace is filled from the real module: later lookups hit ``__dict__``
    and never re-enter ``__getattr__``, so the package body runs exactly once.
    """

    def __getattr__(self, attribute: str):
        name = object.__getattribute__(self, "__name__")
        del sys.modules[name]
        try:
            module = importlib.import_module(name)
        except BaseException:
            # Leave the stand-in registered so the next access retries and
            # raises the real ImportError again instead of KeyError.
            sys.modules[name] = self
            raise
        self.__dict__.update(module.__dict__)
        return getattr(module, attribute)


def lazy_module(name: str) -> None:
    """Register ``name`` so ``import name`` is free until first use."""
    if name in sys.modules:
        return
    spec = importlib.util.find_spec(name)
    if spec is None:
        return
    module = _LazyModule(name)
    module.__spec__ = spec
    module.__file__ = spec.origin
    if spec.submodule_search_locations is not None:
        module.__path__ = list(spec.submodule_search_locations)
    sys.modules[name] = module

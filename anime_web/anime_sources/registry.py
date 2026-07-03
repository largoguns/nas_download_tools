from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass

from .base import BaseAnimeSource


@dataclass(frozen=True)
class SourceLoadError:
    module: str
    error: str


def load_sources() -> tuple[dict[str, BaseAnimeSource], list[SourceLoadError]]:
    sources: dict[str, BaseAnimeSource] = {}
    errors: list[SourceLoadError] = []
    package_name = __package__.rsplit(".", 1)[0] if __package__ else "anime_sources"
    package = importlib.import_module(package_name)

    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name.startswith("_") or module_info.name in {"base", "registry"}:
            continue

        module_name = f"{package_name}.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(SourceLoadError(module_name, str(exc)))
            continue

        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls is BaseAnimeSource or not issubclass(cls, BaseAnimeSource):
                continue
            if cls.__module__ != module.__name__:
                continue
            try:
                source = cls()
                if source.enabled:
                    sources[source.id] = source
            except Exception as exc:
                errors.append(SourceLoadError(f"{module_name}.{cls.__name__}", str(exc)))

    return sources, errors


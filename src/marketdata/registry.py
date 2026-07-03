"""
Registry de estado compartido entre servicios del runner.

data_capture publica aquí su instancia (libros + mercados observados) y el
motor 1 la consume en cada tick. Evita acoplar los servicios entre sí o pasar
por la DB para datos en vivo.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.strategies.data_capture import DataCaptureService

_capture: DataCaptureService | None = None


def set_capture(service: DataCaptureService | None) -> None:
    global _capture
    _capture = service


def get_capture() -> DataCaptureService | None:
    return _capture

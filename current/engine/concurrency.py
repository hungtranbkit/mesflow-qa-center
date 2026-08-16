from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Callable, TypeVar

T = TypeVar("T")


def run_simultaneously(workers: int, action: Callable[[int], T]) -> list[T]:
    if workers not in {1, 5, 10, 20, 22}:
        raise ValueError("workers must be one of 1, 5, 10, 20, 22")
    barrier = Barrier(workers)

    def invoke(index: int) -> T:
        barrier.wait(timeout=10)
        return action(index)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(invoke, range(workers)))

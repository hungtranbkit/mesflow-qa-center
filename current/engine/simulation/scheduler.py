"""Item 30's own scheduler design, literally:

    PriorityQueue: (actor_id, next_action_at)
    On action: execute; schedule actor's next realistic action.

No `while true: sleep(0.1)` polling loop (item 30 explicitly forbids it).
The run loop sleeps exactly until the next actor is due, in REAL seconds
(via SimClock.real_seconds_until), so an idle factory (e.g. overnight, no
night shift configured) produces near-zero CPU/API activity between
events -- realistic burstiness and idle periods, not a tight spin loop.

Persisted as one row per actor (sim_actors.next_action_at) rather than a
serialized heap blob: trivially resumable (item 33/78) by just re-reading
the table, and a human/debugger can read next_action_at directly in the
DB without deserializing anything.
"""
from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Callable


@dataclass(order=True)
class _Entry:
    next_action_at: float
    seq: int
    actor_id: str = field(compare=False)


class Scheduler:
    def __init__(self) -> None:
        self._heap: list[_Entry] = []
        self._counter = itertools.count()
        self._actor_ids: set[str] = set()

    def schedule(self, actor_id: str, next_action_at: float) -> None:
        heapq.heappush(self._heap, _Entry(next_action_at, next(self._counter), actor_id))
        self._actor_ids.add(actor_id)

    def is_empty(self) -> bool:
        return not self._heap

    def peek_time(self) -> float | None:
        return self._heap[0].next_action_at if self._heap else None

    def pop_due(self, now: float, max_batch: int = 50) -> list[str]:
        """Pop every actor whose next_action_at <= now, up to max_batch --
        the cap exists so a large backlog after a long pause (item 33)
        drains gradually across a few ticks rather than one giant burst of
        API calls in a single instant."""
        due: list[str] = []
        while self._heap and self._heap[0].next_action_at <= now and len(due) < max_batch:
            due.append(heapq.heappop(self._heap).actor_id)
        return due

    def remove_actor(self, actor_id: str) -> None:
        """Lazy removal: filters the heap once (rare -- only on actor
        retirement/quarantine, item 36's 'quarantine affected actor'),
        never on the hot path."""
        self._heap = [e for e in self._heap if e.actor_id != actor_id]
        heapq.heapify(self._heap)
        self._actor_ids.discard(actor_id)

    def __len__(self) -> int:
        return len(self._heap)


def run_until_empty_or_stopped(scheduler: Scheduler, clock, act: Callable[[str, float], float | None],
                                should_stop: Callable[[], bool], sleep: Callable[[float], None],
                                tick_hook: Callable[[], None] | None = None) -> None:
    """The actual event loop. `act(actor_id, sim_now) -> next_action_at or
    None` runs one actor's action and returns when it should run again
    (None retires the actor -- e.g. end of a finite-duration run for that
    actor). `tick_hook` runs once per real loop iteration (checkpointing,
    stop-condition checks) regardless of whether any actor was due."""
    while not should_stop():
        if scheduler.is_empty():
            if tick_hook:
                tick_hook()
            sleep(1.0)
            continue
        now = clock.now()
        due = scheduler.pop_due(now)
        if not due:
            wait_real = clock.real_seconds_until(scheduler.peek_time())
            sleep(min(wait_real, 5.0))  # re-check should_stop()/tick_hook at least every 5 real seconds
            if tick_hook:
                tick_hook()
            continue
        for actor_id in due:
            next_at = act(actor_id, now)
            if next_at is not None:
                scheduler.schedule(actor_id, next_at)
        if tick_hook:
            tick_hook()

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from threading import Event
from typing import TypeVar

T = TypeVar("T")


def _parallel(items: Iterable[T], action: Callable[[T, Event], None], workers: int) -> None:
    cancel = Event()
    pending: set[Future[None]] = set()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="transfer") as pool:
        try:
            for item in items:
                pending.add(pool.submit(action, item, cancel))
                if len(pending) == workers * 2:
                    completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        future.result()
            for future in as_completed(pending):
                future.result()
        except BaseException:
            cancel.set()
            for future in pending:
                future.cancel()
            raise

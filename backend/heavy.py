"""Глобальный шлюз тяжёлых фоновых сборок.

На Render free всего 512 МБ: когда WB-детали, Ozon, YM, отзывы, реклама и
продуктолог собираются одновременно, инстанс упирается в память и
перезапускается (все запросы в этот момент получают 502). Семафор пускает
тяжёлые сборки по одной, а после каждой принудительно собирает мусор.
"""
import asyncio
import gc
import logging

_log = logging.getLogger("heavy")
_sem = asyncio.Semaphore(1)


async def guard(coro, name: str = ""):
    """Выполняет корутину, удерживая глобальный слот тяжёлой работы."""
    label = name or getattr(coro, "__qualname__", "task")
    if _sem.locked():
        _log.info("heavy: %s ждёт слот", label)
    async with _sem:
        try:
            return await coro
        finally:
            gc.collect()


def rss_mb() -> float:
    """Текущий RSS процесса в МБ (Linux)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return 0.0

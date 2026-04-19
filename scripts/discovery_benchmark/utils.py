"""Reusable datalab ingestion primitives: logging, timing, idempotent ops."""

from __future__ import annotations

import contextvars
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from datalab_api import DatalabClient
from datalab_api._base import DuplicateItemError

logger = logging.getLogger("discovery_benchmark")

_current_cell: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_cell", default=None
)


def set_current_cell(cell_id: str | None):
    """Set the cell-id prefix used by log records in this context."""
    return _current_cell.set(cell_id)


def reset_current_cell(token) -> None:
    _current_cell.reset(token)


class _CellIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        cid = _current_cell.get()
        record.cell_id = cid or "-"
        return True


class _ColorFormatter(logging.Formatter):
    RESET = "\033[0m"
    LEVEL_COLORS = {
        logging.DEBUG: "\033[2;37m",
        logging.INFO: "\033[36m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    NAME_COLOR = "\033[1;35m"
    CELL_COLOR = "\033[1;32m"

    def __init__(self, use_color: bool):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        level = record.levelname
        name = record.name
        cell = getattr(record, "cell_id", "-")
        msg = record.getMessage()
        if self.use_color:
            level_c = self.LEVEL_COLORS.get(record.levelno, "")
            r = self.RESET
            head = f"{ts} {level_c}{level:<8}{r} {self.NAME_COLOR}{name}{r} [{self.CELL_COLOR}{cell}{r}]"
        else:
            head = f"{ts} {level:<8} {name} [{cell}]"
        if record.exc_info:
            msg = msg + "\n" + self.formatException(record.exc_info)
        return f"{head} {msg}"


def configure_logging(verbose: bool) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter(use_color=sys.stderr.isatty()))
    handler.addFilter(_CellIdFilter())
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@dataclass
class TimedOp:
    label: str
    start_ns: int = 0

    def __enter__(self):
        self.start_ns = time.monotonic_ns()
        return self

    def __exit__(self, *exc):
        elapsed = (time.monotonic_ns() - self.start_ns) / 1e9
        logger.info("%s done", self.label, extra={"elapsed_s": round(elapsed, 3)})


def upsert_item(
    client: DatalabClient,
    item_id: str | int,
    item_type: str,
    item_data: dict,
    collection_id: str | None = None,
) -> dict:
    try:
        item = client.create_item(
            item_id=item_id,
            item_type=item_type,
            item_data=item_data,
            collection_id=collection_id,
        )
        logger.info("created %s %s", item_type, item_id)
        return item
    except DuplicateItemError:
        pass
    try:
        existing = client.get_item(item_id=item_id)
    except Exception as e:
        logger.error("get_item %s failed: %s", item_id, e)
        raise
    client.update_item(item_id=item_id, item_data=item_data)
    logger.info("updated %s %s", item_type, item_id)
    return existing


def existing_file_names(item: dict) -> set[str]:
    return {str(f["name"]) for f in (item.get("files") or [])}


def existing_block_titles(item: dict) -> set[str]:
    """Return the set of block titles already present on the item, for idempotency."""
    titles: set[str] = set()
    blocks_obj = item.get("blocks_obj")
    if isinstance(blocks_obj, dict):
        for block in blocks_obj.values():
            t = block.get("title")
            if t:
                titles.add(str(t))
    if isinstance(item.get("blocks"), list):
        for b in item["blocks"]:
            if isinstance(b, dict) and b.get("title"):
                titles.add(str(b["title"]))
    return titles


def try_rename_block(
    client: DatalabClient,
    item_id: str,
    block_type: str,
    block_id: str,
    title: str,
) -> None:
    try:
        client.update_data_block(
            item_id=str(item_id),
            block_id=block_id,
            block_type=block_type,
            block_data={"title": title},
        )
    except Exception as e:
        logger.debug("rename block %s failed: %s", block_id, e)


def upload_if_new(
    client: DatalabClient,
    item_id: str | int,
    path: Path,
    existing_names: set[str],
    block_type: str | None,
    block_title: str | None = None,
) -> bool:
    canonical = path.name.replace(" ", "_")
    if canonical in existing_names or path.name in existing_names:
        logger.debug("skip existing file %s on %s", path.name, item_id)
        return False
    with TimedOp(f"upload {path.name} -> {item_id}"):
        uploaded = client.upload_file(item_id, str(path))
    existing_names.add(canonical)
    if block_type:
        with TimedOp(f"create_data_block {block_type} on {item_id}"):
            block = client.create_data_block(
                str(item_id), block_type, file_ids=uploaded["file_id"]
            )
        if block_title:
            try_rename_block(
                client, str(item_id), block_type, block.get("block_id", ""), block_title
            )
    return True


def ensure_block(
    client: DatalabClient,
    item_id: str,
    block_type: str,
    title: str,
    existing_titles: set[str],
) -> None:
    """Create a data block (no attached file) with a specific title if none exists."""
    if title in existing_titles:
        logger.debug("block %r already present on %s", title, item_id)
        return
    try:
        with TimedOp(f"create_data_block {block_type} on {item_id}"):
            block = client.create_data_block(str(item_id), block_type)
        try_rename_block(client, item_id, block_type, block.get("block_id", ""), title)
        existing_titles.add(title)
    except Exception as e:
        logger.error("create %s block on %s failed: %s", block_type, item_id, e)

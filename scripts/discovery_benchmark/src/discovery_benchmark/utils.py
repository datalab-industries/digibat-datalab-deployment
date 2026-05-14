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


def ensure_collection(
    client: DatalabClient,
    collection_id: str,
    title: str,
    description: str,
) -> None:
    """Create the collection if missing, else PATCH its title/description in place.

    ``create_item(collection_id=...)`` auto-creates empty collections, so by the
    time an ingestion script calls this, the collection often already exists
    with no description. There's no public ``update_collection`` on the client,
    so we PATCH the REST resource directly.
    """
    try:
        data, _ = client.get_collection(collection_id)
        immutable_id = data["immutable_id"]
    except Exception:
        client.create_collection(
            collection_id,
            collection_data={"title": title, "description": description},
        )
        logger.info("created collection %s", collection_id)
        return

    url = f"{client.datalab_api_url}/collections/{collection_id}"
    try:
        client._patch(
            url,
            json={"data": {"title": title, "description": description}},
        )
        logger.info("updated collection %s (title + description)", collection_id)
    except Exception as e:
        logger.warning("could not PATCH collection %s: %s", collection_id, e)


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
    existing_block_data: dict | None = None,
    file_id: str | None = None,
) -> None:
    """Rename a block. The ``/update-block/`` endpoint overwrites the server-side
    block_data with whatever we PATCH, so a title-only payload strips out
    ``file_id`` and any other fields. Pass ``existing_block_data`` (the dict
    returned by ``create_data_block``) so all fields survive the rename.
    """
    payload: dict = dict(existing_block_data) if existing_block_data else {}
    payload["title"] = title
    if file_id:
        payload["file_id"] = file_id
    try:
        client.update_data_block(
            item_id=str(item_id),
            block_id=block_id,
            block_type=block_type,
            block_data=payload,
        )
    except Exception as e:
        logger.debug("rename block %s failed: %s", block_id, e)


def _file_id_by_name(item: dict) -> dict[str, str]:
    """Map stored filename -> file immutable_id for files already on the item."""
    out: dict[str, str] = {}
    for f in item.get("files") or []:
        name = f.get("name")
        fid = f.get("immutable_id") or f.get("_id") or f.get("file_id")
        if name and fid:
            out[str(name)] = str(fid)
    return out


def _blocks_by_file_id(item: dict) -> dict[str, list[tuple[str, str]]]:
    """Map file_id -> list of (block_id, blocktype) for blocks on the item."""
    out: dict[str, list[tuple[str, str]]] = {}
    blocks = item.get("blocks_obj")
    if not isinstance(blocks, dict):
        return out
    for block_id, block in blocks.items():
        fid = block.get("file_id")
        btype = block.get("blocktype") or block.get("block_type")
        if fid and btype:
            out.setdefault(str(fid), []).append((str(block_id), str(btype)))
    return out


def _delete_block(client: DatalabClient, item_id: str, block_id: str) -> None:
    url = f"{client.datalab_api_url}/delete-block/"
    try:
        client._post(
            url,
            json={"item_id": str(item_id), "block_id": block_id},
        )
    except Exception as e:
        logger.warning("delete block %s on %s failed: %s", block_id, item_id, e)


def _file_less_blocks_of_type(item: dict, block_type: str) -> list[tuple[str, str | None]]:
    """Return (block_id, title) tuples for blocks on *item* that are the
    desired type but have no ``file_id`` attached (e.g. blocks whose file was
    stripped by a prior buggy rename)."""
    out: list[tuple[str, str | None]] = []
    blocks = item.get("blocks_obj")
    if not isinstance(blocks, dict):
        return out
    for block_id, block in blocks.items():
        btype = block.get("blocktype") or block.get("block_type")
        if btype == block_type and not block.get("file_id"):
            title = block.get("title")
            out.append((str(block_id), str(title) if title else None))
    return out


def reconcile_block_type(
    client: DatalabClient,
    item_id: str,
    item: dict,
    filename: str,
    desired_block_type: str,
    desired_title: str | None = None,
) -> bool:
    """Ensure *filename* is attached to exactly one block of *desired_block_type*
    on *item*. Handles three drift cases from earlier buggy runs:

    1. A block of the wrong type already references this file → delete and
       recreate as the desired type.
    2. The file has no block at all, but there is exactly one file-less block
       of the desired type (leftover from a rename that wiped ``file_id``) →
       re-attach the file to that block.
    3. The file has no block at all and no matching orphan → create a fresh
       block with the file.

    Returns True when anything changed.
    """
    file_ids = _file_id_by_name(item)
    candidates = [filename, filename.replace(" ", "_")]
    fid = next((file_ids[c] for c in candidates if c in file_ids), None)
    if fid is None:
        return False

    block_index = _blocks_by_file_id(item)
    matches = block_index.get(fid, [])

    # Case 1: wrong-type block(s) attached to this file.
    retyped = False
    for block_id, btype in matches:
        if btype == desired_block_type:
            continue
        logger.info(
            "retyping block %s on %s: %s -> %s",
            block_id, item_id, btype, desired_block_type,
        )
        _delete_block(client, str(item_id), block_id)
        try:
            with TimedOp(f"create_data_block {desired_block_type} on {item_id}"):
                client.create_data_block(
                    str(item_id), desired_block_type, file_ids=fid
                )
            retyped = True
        except Exception as e:
            logger.error("recreate %s block on %s failed: %s", desired_block_type, item_id, e)
    if matches:
        return retyped

    # Cases 2 & 3: the file has no block. Look for a file-less block to heal.
    orphans = _file_less_blocks_of_type(item, desired_block_type)

    chosen: str | None = None
    if desired_title:
        title_matches = [bid for bid, t in orphans if t == desired_title]
        if len(title_matches) == 1:
            chosen = title_matches[0]
        elif len(title_matches) > 1:
            logger.debug(
                "multiple orphan %s blocks share title %r on %s — skipping heal",
                desired_block_type, desired_title, item_id,
            )
    if chosen is None and len(orphans) == 1 and not desired_title:
        chosen = orphans[0][0]

    if chosen is not None:
        logger.info(
            "re-attaching file %s to orphan %s block %s on %s",
            filename, desired_block_type, chosen, item_id,
        )
        try:
            client.update_data_block(
                item_id=str(item_id),
                block_id=chosen,
                block_type=desired_block_type,
                block_data={"file_id": fid},
            )
            return True
        except Exception as e:
            logger.warning("re-attach %s on %s failed: %s", chosen, item_id, e)
            return False
    if orphans and desired_title is None:
        logger.debug(
            "ambiguous orphan %s blocks (%d) on %s for %s — leaving alone",
            desired_block_type, len(orphans), item_id, filename,
        )
        return False

    # No existing block for this file — create one.
    logger.info("creating missing %s block on %s for %s", desired_block_type, item_id, filename)
    try:
        with TimedOp(f"create_data_block {desired_block_type} on {item_id}"):
            client.create_data_block(str(item_id), desired_block_type, file_ids=fid)
        return True
    except Exception as e:
        logger.error("create %s block on %s failed: %s", desired_block_type, item_id, e)
        return False


def upload_file_only(
    client: DatalabClient,
    item_id: str | int,
    path: Path,
    existing_names: set[str],
) -> str | None:
    """Upload *path* to *item_id* if no file with that name is already present.

    Returns the new file's ``file_id`` (or ``None`` when skipped). Never
    creates a data block — useful for media (SEM/TEM/Cellerate) where the
    media block is broken upstream.
    """
    canonical = path.name.replace(" ", "_")
    if canonical in existing_names or path.name in existing_names:
        logger.debug("skip existing file %s on %s", path.name, item_id)
        return None
    with TimedOp(f"upload {path.name} -> {item_id}"):
        uploaded = client.upload_file(item_id, str(path))
    existing_names.add(canonical)
    return uploaded.get("file_id")


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
                client, str(item_id), block_type, block.get("block_id", ""), block_title,
                existing_block_data=block,
                file_id=uploaded["file_id"],
            )
    return True


def ensure_block(
    client: DatalabClient,
    item_id: str,
    block_type: str,
    title: str,
    existing_titles: set[str],
    file_id: str | None = None,
    item: dict | None = None,
) -> None:
    """Create a data block with the given title if none exists.

    If *file_id* is provided, it is attached on creation, and an already-present
    block of the same title with no ``file_id`` is healed by PATCHing in the
    file. *item* is needed only for the heal path; pass it when *file_id* is
    set so we can inspect existing blocks.
    """
    if title in existing_titles:
        if file_id and item is not None:
            _heal_file_less_block(client, item_id, item, block_type, title, file_id)
        else:
            logger.debug("block %r already present on %s", title, item_id)
        return
    try:
        kwargs = {"file_ids": file_id} if file_id else {}
        with TimedOp(f"create_data_block {block_type} on {item_id}"):
            block = client.create_data_block(str(item_id), block_type, **kwargs)
        try_rename_block(
            client, item_id, block_type, block.get("block_id", ""), title,
            existing_block_data=block,
            file_id=file_id,
        )
        existing_titles.add(title)
    except Exception as e:
        logger.error("create %s block on %s failed: %s", block_type, item_id, e)


def _heal_file_less_block(
    client: DatalabClient,
    item_id: str,
    item: dict,
    block_type: str,
    title: str,
    file_id: str,
) -> None:
    """If a block of *block_type* with matching *title* exists but has no
    ``file_id``, PATCH the file_id in."""
    blocks_obj = item.get("blocks_obj") or {}
    if not isinstance(blocks_obj, dict):
        return
    for bid, b in blocks_obj.items():
        btype = b.get("blocktype") or b.get("block_type")
        if btype != block_type or b.get("title") != title:
            continue
        if b.get("file_id"):
            return  # already healthy
        try:
            payload = dict(b)
            payload["file_id"] = file_id
            client.update_data_block(
                item_id=str(item_id), block_id=str(bid),
                block_type=block_type, block_data=payload,
            )
            logger.info("attached file %s to existing %s block on %s", file_id, block_type, item_id)
        except Exception as e:
            logger.warning("heal %s block on %s failed: %s", block_type, item_id, e)
        return

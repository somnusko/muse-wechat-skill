#!/usr/bin/env python3
"""File index for received WeChat files (file_index.py).

Every FILE item the bridge downloads in ``poll`` is recorded here:
who sent it, its original name, where it was saved, its type/size
and when it arrived, plus a short text snippet extracted from its
contents (when the format allows it). The worker can then find a
file again by name, sender, date or a word from its contents
(``ilink.py files-find <keyword>``) and resend it with the existing
``send-file`` / ``proactive-file`` commands — this module never sends
anything itself.

Storage: one JSON file, ``<data dir>/file_index.json``::

    {"version": 1, "entries": [entry, ...]}

with each entry::

    {"message_id": str, "sender": str, "file_name": str,
     "path": str, "size": int, "type": str,
     "snippet": str, "received_at": int, "date": "YYYY-MM-DD"}

Design notes (why it looks the way it does):

* JSON, not SQLite: the bridge keeps all of its other small state in
  JSON files (contacts, seen ids, send ledger), and the index is
  small (a few hundred entries at most). No new runtime dependency.
* Atomic writes: the whole file is written to a unique temp name
  (pid + thread id, so two writers never share a tmp file) and then
  ``os.replace``-d over the target — a crash mid-write leaves either
  the old file or the new file, never a torn one. This mirrors
  ``ilink._atomic_write``; it is re-implemented here so this module
  stays importable on its own.
* Concurrency: ``poll`` runs every ~5 s and the hook and the daemon
  share one data dir, so two processes can record at the same time.
  Every read-modify-write runs under an exclusive ``fcntl`` lock on
  ``file_index.lock`` (the same pattern as the send-ledger lock in
  ``ilink.py``). Without ``fcntl`` (Windows) the lock degrades to a
  no-op and the atomic replace is the only protection — last writer
  wins, the file itself never corrupts.
* Best-effort everywhere: ``record()`` never raises and never prints
  (``poll``'s stdout is a machine-readable JSON channel — one stray
  line corrupts the whole batch). A corrupt index file reads as
  empty; a failed write is swallowed by the caller-side ``try`` in
  ``poll`` anyway.
* Snippets are best-effort too: ``extract_snippet()`` returns ``""``
  for formats it cannot read instead of raising, so "search by
  description" degrades to "search by name/sender/date" rather than
  breaking the record path. Optional parsers (pypdf, openpyxl,
  python-docx) are imported lazily inside the extractor — none of
  them is a hard dependency of this module.
* Pruning: ``prune_missing()`` drops entries whose file no longer
  exists on disk. ``cmd_prune_media`` calls it after deleting old
  media, so the index never points at a deleted file for long.
  ``search()``/``list_recent()`` additionally annotate each returned
  entry with ``"exists": bool`` so the worker can see at a glance
  whether a hit is still resendable.

This module is deliberately independent of ``ilink.py``: it resolves
its own data dir from ``ILINK_DATA_DIR`` (same env var, same default
layout) and takes an explicit ``data_dir`` argument in every public
function, so unit tests can point it at a temp dir.
"""

import contextlib
import json
import os
import threading
import time

try:
    import fcntl
except ImportError:  # Windows has no fcntl: lock degrades to a no-op.
    fcntl = None

INDEX_FILENAME = "file_index.json"
LOCK_FILENAME = "file_index.lock"
INDEX_VERSION = 1
MAX_ENTRIES = 5000  # bound the file: oldest entries drop off first.
SNIPPET_MAX_CHARS = 500
SNIPPET_MAX_BYTES = 20 * 1024 * 1024  # never open a bigger file for text.

# Extensions treated as plain text (read the head, decode utf-8 loosely).
_TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".log", ".py", ".js",
              ".ts", ".html", ".xml", ".yaml", ".yml", ".ini", ".cfg"}


def _data_dir(data_dir=None):
    if data_dir:
        return data_dir
    env = os.environ.get("ILINK_DATA_DIR")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _index_path(data_dir=None):
    return os.path.join(_data_dir(data_dir), INDEX_FILENAME)


def _lock_path(data_dir=None):
    return os.path.join(_data_dir(data_dir), LOCK_FILENAME)


@contextlib.contextmanager
def _locked(data_dir=None):
    """Exclusive inter-process lock for one read-modify-write cycle."""
    if fcntl is None:
        yield
        return
    fd = os.open(_lock_path(data_dir),
                 os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write_json(path, data):
    """Write JSON atomically: unique tmp file + os.replace + chmod 0600."""
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load_unlocked(data_dir=None):
    """Read the index; missing/corrupt/wrong-shape reads as empty."""
    try:
        with open(_index_path(data_dir), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": INDEX_VERSION, "entries": []}
        entries = data.get("entries")
        if not isinstance(entries, list):
            return {"version": INDEX_VERSION, "entries": []}
        return {"version": INDEX_VERSION,
                "entries": [e for e in entries if isinstance(e, dict)]}
    except Exception:
        return {"version": INDEX_VERSION, "entries": []}


def _save_unlocked(state, data_dir=None):
    os.makedirs(_data_dir(data_dir), exist_ok=True)
    _atomic_write_json(_index_path(data_dir), state)


def _annotate(entry):
    """Return a copy with an ``exists`` flag (is the file still on disk?)."""
    out = dict(entry)
    try:
        out["exists"] = os.path.isfile(entry.get("path") or "")
    except Exception:
        out["exists"] = False
    return out


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record(message_id, sender, file_name, path, size, type,
           snippet="", received_at=None, data_dir=None):
    """Record one received file. Returns the stored entry, or None on
    failure. Never raises, never prints. Re-recording the same
    ``message_id`` updates the entry in place (poll redelivery after
    a crash must not create a duplicate row)."""
    try:
        if received_at is None:
            received_at = int(time.time())
        entry = {
            "message_id": str(message_id),
            "sender": str(sender or ""),
            "file_name": str(file_name or ""),
            "path": str(path or ""),
            "size": int(size or 0),
            "type": str(type or ""),
            "snippet": str(snippet or "")[:SNIPPET_MAX_CHARS],
            "received_at": int(received_at),
            "date": time.strftime("%Y-%m-%d", time.localtime(received_at)),
        }
        with _locked(data_dir):
            state = _load_unlocked(data_dir)
            entries = state["entries"]
            for i, old in enumerate(entries):
                if old.get("message_id") == entry["message_id"]:
                    entries[i] = entry
                    break
            else:
                entries.append(entry)
            # Newest last in storage; cap by dropping the oldest.
            if len(entries) > MAX_ENTRIES:
                entries[:] = entries[-MAX_ENTRIES:]
            _save_unlocked(state, data_dir)
        return entry
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def search(query, limit=10, data_dir=None):
    """Find entries by substring (case-insensitive) over file name,
    sender, date (``YYYY-MM-DD``) and the extracted text snippet.
    Newest first. Each result carries ``"exists"`` (file still on
    disk?). An empty query returns the most recent entries."""
    q = (query or "").strip().lower()
    with _locked(data_dir):
        state = _load_unlocked(data_dir)
    hits = []
    for entry in reversed(state["entries"]):
        if not q:
            hits.append(_annotate(entry))
        else:
            haystack = " ".join([
                str(entry.get("file_name", "")),
                str(entry.get("sender", "")),
                str(entry.get("date", "")),
                str(entry.get("snippet", "")),
            ]).lower()
            if q in haystack:
                hits.append(_annotate(entry))
        if len(hits) >= limit:
            break
    return hits


def list_recent(limit=10, data_dir=None):
    """Most recent entries, newest first, each with ``"exists"``."""
    return search("", limit=limit, data_dir=data_dir)


def get_by_message_id(message_id, data_dir=None):
    """One entry by its inbound message id, or None."""
    with _locked(data_dir):
        state = _load_unlocked(data_dir)
    for entry in state["entries"]:
        if str(entry.get("message_id")) == str(message_id):
            return _annotate(entry)
    return None



# ---------------------------------------------------------------------------
# Pruning consistency
# ---------------------------------------------------------------------------

def prune_missing(data_dir=None):
    """Drop index entries whose file no longer exists on disk (e.g.
    removed by ``prune-media``). Returns the number dropped. Called
    from ``cmd_prune_media`` after the media pass; also safe to run
    standalone. Never raises."""
    try:
        with _locked(data_dir):
            state = _load_unlocked(data_dir)
            kept = [e for e in state["entries"]
                    if os.path.isfile(e.get("path") or "")]
            dropped = len(state["entries"]) - len(kept)
            if dropped:
                state["entries"] = kept
                _save_unlocked(state, data_dir)
            return dropped
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Text-snippet extraction ("search by description")
# ---------------------------------------------------------------------------

def extract_snippet(path, max_chars=SNIPPET_MAX_CHARS):
    """Best-effort text excerpt from a file, for content search.

    * PDF: text of the first 3 pages (pypdf if installed, else the
      ``pdftotext`` CLI if present).
    * XLSX: sheet names + first 5 rows of the first sheet (openpyxl).
    * DOCX: first non-empty paragraphs (python-docx).
    * Plain text (.txt/.md/.csv/...): the head of the file.
    * Anything else (images, audio, video, archives, unknown):
      ``""``.

    Never raises; returns ``""`` when the format is unreadable, the
    parser is missing, or the file is over the size guard. Callers
    may pass a smaller ``max_chars``; the stored snippet is capped
    again in ``record()``."""
    try:
        if not path or not os.path.isfile(path):
            return ""
        if os.path.getsize(path) > SNIPPET_MAX_BYTES:
            return ""
        ext = os.path.splitext(str(path))[1].lower()
        if ext == ".pdf":
            text = _pdf_text(path)
        elif ext in (".xlsx", ".xlsm"):
            text = _xlsx_text(path)
        elif ext == ".docx":
            text = _docx_text(path)
        elif ext in _TEXT_EXTS:
            text = _plain_text(path)
        else:
            return ""
        return _clean(text)[:max_chars]
    except Exception:
        return ""


def _clean(text):
    """Collapse whitespace runs so a snippet stays one readable line-ish
    block and does not blow up the index with blank lines."""
    if not text:
        return ""
    return " ".join(str(text).split())


def _plain_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(64 * 1024)


def _pdf_text(path, max_pages=3):
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        parts = []
        for page in reader.pages[:max_pages]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        if any(p.strip() for p in parts):
            return "\n".join(parts)
    except Exception:
        pass
    # Fallback: poppler's pdftotext, when the binary exists. The
    # "-" argument writes to stdout; a missing binary raises and
    # the extractor degrades to "" below.
    try:
        import subprocess
        out = subprocess.run(
            ["pdftotext", "-f", "1", "-l", str(max_pages), "-layout",
             path, "-"],
            capture_output=True, timeout=30)
        if out.returncode == 0:
            return out.stdout.decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""


def _xlsx_text(path, max_rows=5):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"[{ws.title}]")
        rows = 0
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None and str(c) != ""]
            if cells:
                parts.append(" | ".join(cells))
                rows += 1
            if rows >= max_rows:
                break
        break  # first sheet only: the snippet is a fingerprint, not a dump
    try:
        wb.close()
    except Exception:
        pass
    return "\n".join(parts)


def _docx_text(path, max_paras=10):
    from docx import Document
    doc = Document(path)
    parts = []
    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if text:
            parts.append(text)
        if len(parts) >= max_paras:
            break
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI helpers (used by `ilink.py files` / `ilink.py files-find`)
# ---------------------------------------------------------------------------

def format_entry(entry, number=None):
    """One human-readable line: ``1. 报告.xlsx（12KB，2026-11-12，张三）``.
    Appends ``[已清理]`` when the file is gone from disk."""
    size = entry.get("size") or 0
    if size >= 1024 * 1024:
        size_s = f"{size / (1024 * 1024):.1f}MB"
    else:
        size_s = f"{max(size // 1024, 1) if size else 0}KB"
    prefix = f"{number}. " if number is not None else ""
    gone = " [已清理]" if not entry.get("exists", True) else ""
    return (f"{prefix}{entry.get('file_name', '')}（{size_s}，"
            f"{entry.get('date', '')}，{entry.get('sender', '')}）"
            f"{gone}\n  {entry.get('path', '')}")


def cmd_files(limit=10, data_dir=None):
    """`ilink.py files`: list the most recent received files."""
    entries = list_recent(limit=limit, data_dir=data_dir)
    if not entries:
        print("FILES_EMPTY")
        return 0
    for i, entry in enumerate(entries, 1):
        print(format_entry(entry, i))
    return 0


def cmd_files_find(query, limit=10, data_dir=None):
    """`ilink.py files-find <keyword>`: search by file name, sender,
    date substring or a word from the file's text snippet."""
    entries = search(query, limit=limit, data_dir=data_dir)
    if not entries:
        print("FILES_NOT_FOUND")
        return 1
    for i, entry in enumerate(entries, 1):
        print(format_entry(entry, i))
    return 0

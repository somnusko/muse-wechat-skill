#!/usr/bin/env python3
"""Tests for the received-file index (file_index.py).

Covers: record -> files-find round trip (name / sender / date /
content snippet), CLI entry points (`ilink.py files`,
`ilink.py files-find`), atomic writes under concurrent writers
(threads + processes), prune/index consistency, and the per-format
snippet extractors (PDF / XLSX / DOCX / TXT).

Run from the repo root:

    python3 -m unittest discover -s tests -v
    # or: python3 tests/test_file_index.py
"""
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-fileindex-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
os.environ["ILINK_HOOK_STATE_DIR"] = tempfile.mkdtemp(prefix="ilink-test-state-")
import file_index  # noqa: E402
import ilink  # noqa: E402


def _sample_pdf(path):
    """Minimal valid one-page PDF with extractable text (no generator
    dependency: hand-written content stream)."""
    stream = b"BT /F1 18 Tf 72 720 Td (Quarterly Report Q3) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    with open(path, "wb") as f:
        f.write(bytes(out))


def _sample_xlsx(path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "费用"
    ws.append(["项目", "金额", "备注"])
    ws.append(["差旅", 1200, "北京出差"])
    ws.append(["餐饮", 350, "团队聚餐"])
    wb.save(path)


def _sample_docx(path):
    from docx import Document
    doc = Document()
    doc.add_paragraph("会议纪要：季度复盘")
    doc.add_paragraph("决定：下季度重点推进华北市场。")
    doc.save(path)


def _record_worker(data_dir, start, count, out):
    for i in range(count):
        file_index.record(f"m{start + i}", "sender", f"f{start + i}.txt",
                          f"/tmp/nope-{start + i}.txt", 10, "file",
                          data_dir=data_dir)
    out.append(True)


class FileIndexTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ilink-fileindex-case-")
        self.samples = os.path.join(self.dir, "samples")
        os.makedirs(self.samples)
        self.pdf = os.path.join(self.samples, "季度报告.pdf")
        _sample_pdf(self.pdf)
        self.xlsx = os.path.join(self.samples, "费用表.xlsx")
        _sample_xlsx(self.xlsx)
        self.docx = os.path.join(self.samples, "会议纪要.docx")
        _sample_docx(self.docx)
        self.txt = os.path.join(self.samples, "notes.txt")
        with open(self.txt, "w", encoding="utf-8") as f:
            f.write("TODO：给王小明发合同扫描件，周五前完成。\n第二行。\n")

    # -- snippet extraction -------------------------------------------
    def test_extract_pdf(self):
        self.assertIn("Quarterly Report Q3",
                      file_index.extract_snippet(self.pdf))

    def test_extract_xlsx(self):
        snip = file_index.extract_snippet(self.xlsx)
        self.assertIn("费用", snip)
        self.assertIn("差旅", snip)
        self.assertIn("1200", snip)

    def test_extract_docx(self):
        snip = file_index.extract_snippet(self.docx)
        self.assertIn("会议纪要", snip)
        self.assertIn("华北市场", snip)

    def test_extract_txt(self):
        snip = file_index.extract_snippet(self.txt)
        self.assertIn("王小明", snip)
        self.assertIn("合同扫描件", snip)

    def test_extract_unknown_returns_empty(self):
        img = os.path.join(self.samples, "pic.jpg")
        with open(img, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"x" * 100)
        self.assertEqual("", file_index.extract_snippet(img))
        self.assertEqual("", file_index.extract_snippet("/nonexistent/x.pdf"))

    # -- record / search ----------------------------------------------
    def test_record_search_by_name_sender_date_snippet(self):
        for path, name in ((self.pdf, "季度报告.pdf"),
                           (self.xlsx, "费用表.xlsx"),
                           (self.docx, "会议纪要.docx"),
                           (self.txt, "notes.txt")):
            file_index.record("m-" + name, "张三", name, path,
                              os.path.getsize(path), "file",
                              snippet=file_index.extract_snippet(path),
                              data_dir=self.dir)
        self.assertEqual(1, len(file_index.search("费用表", data_dir=self.dir)))
        self.assertEqual(4, len(file_index.search("张三", data_dir=self.dir)))
        today = time.strftime("%Y-%m-%d")
        self.assertEqual(4, len(file_index.search(today, data_dir=self.dir)))
        # Content word ("description" search) hits via the snippet.
        self.assertEqual(1, len(file_index.search("华北市场", data_dir=self.dir)))
        self.assertEqual(1, len(file_index.search("王小明", data_dir=self.dir)))
        self.assertEqual(0, len(file_index.search("不存在的词", data_dir=self.dir)))

    def test_record_dedup_by_message_id(self):
        file_index.record("m1", "张三", "a.txt", self.txt, 10, "file",
                          data_dir=self.dir)
        file_index.record("m1", "张三", "b.txt", self.txt, 20, "file",
                          data_dir=self.dir)
        entries = file_index.list_recent(data_dir=self.dir)
        self.assertEqual(1, len(entries))
        self.assertEqual("b.txt", entries[0]["file_name"])

    def test_corrupt_index_reads_as_empty(self):
        with open(os.path.join(self.dir, "file_index.json"), "w") as f:
            f.write("{not json")
        self.assertEqual([], file_index.list_recent(data_dir=self.dir))
        # ... and a later record repairs it.
        file_index.record("m1", "张三", "a.txt", self.txt, 10, "file",
                          data_dir=self.dir)
        self.assertEqual(1, len(file_index.list_recent(data_dir=self.dir)))

    # -- concurrency ----------------------------------------------------
    def test_concurrent_thread_writes_stay_valid(self):
        threads = []
        for t in range(8):
            th = threading.Thread(
                target=_record_worker,
                args=(self.dir, t * 25, 25, []))
            threads.append(th)
            th.start()
        for th in threads:
            th.join()
        with open(os.path.join(self.dir, "file_index.json")) as f:
            data = json.load(f)
        self.assertEqual(200, len(data["entries"]))

    def test_concurrent_process_writes_stay_valid(self):
        with multiprocessing.Pool(4) as pool:
            pool.starmap(_record_worker,
                         [(self.dir, p * 10, 10, []) for p in range(4)])
        with open(os.path.join(self.dir, "file_index.json")) as f:
            data = json.load(f)
        self.assertEqual(40, len(data["entries"]))

    # -- prune consistency ----------------------------------------------
    def test_prune_missing_drops_gone_files_only(self):
        gone = os.path.join(self.dir, "gone.pdf")
        with open(gone, "wb") as f:
            f.write(b"x")
        os.remove(gone)
        file_index.record("m-gone", "张三", "gone.pdf", gone, 1, "file",
                          data_dir=self.dir)
        file_index.record("m-live", "张三", "notes.txt", self.txt, 10,
                          "file", data_dir=self.dir)
        self.assertEqual(1, file_index.prune_missing(data_dir=self.dir))
        remaining = file_index.list_recent(data_dir=self.dir)
        self.assertEqual(1, len(remaining))
        self.assertEqual("notes.txt", remaining[0]["file_name"])
        # search still shows the gone file's row as [已清理] before prune.
        file_index.record("m-gone2", "张三", "gone2.pdf", gone, 1, "file",
                          data_dir=self.dir)
        hits = file_index.search("gone2", data_dir=self.dir)
        self.assertEqual(1, len(hits))
        self.assertFalse(hits[0]["exists"])

    # -- CLI --------------------------------------------------------------
    def test_cli_files_and_files_find(self):
        import contextlib
        import io
        # NOTE: unittest discover imports every test module first, and
        # several of them overwrite os.environ["ILINK_DATA_DIR"] at
        # import time. The CLI resolves the data dir from the env at
        # call time, so pin it here for the duration of this test.
        old_env = os.environ.get("ILINK_DATA_DIR")
        os.environ["ILINK_DATA_DIR"] = DATA_DIR
        try:
            file_index.record("m1", "张三", "季度报告.pdf", self.pdf,
                          os.path.getsize(self.pdf), "file",
                          snippet=file_index.extract_snippet(self.pdf),
                          data_dir=DATA_DIR)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = ilink.main(["ilink.py", "files"])
            self.assertEqual(0, rc)
            self.assertIn("季度报告.pdf", buf.getvalue())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = ilink.main(["ilink.py", "files-find", "季度报告"])
            self.assertEqual(0, rc)
            self.assertIn("季度报告.pdf", buf.getvalue())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = ilink.main(["ilink.py", "files-find", "不存在的词"])
            self.assertEqual(1, rc)
            self.assertIn("FILES_NOT_FOUND", buf.getvalue())
        finally:
            if old_env is None:
                os.environ.pop("ILINK_DATA_DIR", None)
            else:
                os.environ["ILINK_DATA_DIR"] = old_env


if __name__ == "__main__":
    unittest.main()

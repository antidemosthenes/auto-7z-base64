import ctypes
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_7z_base64.filesystem import _source_paths
from auto_7z_base64.prefetch import MAX_FILE_BYTES, PrefetchPlan, _WindowsReader
from tests.helpers import WorkspaceTest


class PrefetchTests(WorkspaceTest):
    def test_planning_skips_empty_large_files_and_respects_budgets(self):
        plan = PrefetchPlan()
        for name, size in (("empty", 0), ("large", MAX_FILE_BYTES + 1), ("small", 3), ("extra", 3)):
            path = self.root / name
            path.write_bytes(b"a" * size)
            with patch("auto_7z_base64.prefetch.MAX_TOTAL_BYTES", 4):
                plan.add(path, path.stat())
        self.assertEqual([path.name for path in plan.files], ["small"])
        with patch("auto_7z_base64.prefetch.MAX_FILES", 1):
            plan.add(self.root / "extra", (self.root / "extra").stat())
        self.assertEqual(len(plan.files), 1)

    def test_small_single_cpu_and_non_windows_cases_skip_prefetch(self):
        plan = PrefetchPlan([Path("unused")])
        with patch("auto_7z_base64.prefetch._WindowsReader") as reader:
            plan.warm()
            plan.files *= 64
            with patch("auto_7z_base64.prefetch.os.cpu_count", return_value=1):
                plan.warm()
            with patch("auto_7z_base64.prefetch.sys.platform", "linux"):
                plan.warm()
            reader.assert_not_called()

    def test_network_inputs_skip_prefetch(self):
        plan = PrefetchPlan([self.root / str(index) for index in range(64)])
        with patch("auto_7z_base64.prefetch._WindowsReader") as reader:
            reader.return_value.local.return_value = False
            plan.warm()
            reader.return_value.read.assert_not_called()

    def test_fast_probe_skips_batch_and_slow_probe_warms_each_file_once(self):
        for elapsed, reads in ((0.001, 4), (0.1, 64)):
            with self.subTest(elapsed=elapsed):
                plan = PrefetchPlan([self.root / str(index) for index in range(64)])
                with (
                    patch("auto_7z_base64.prefetch._WindowsReader") as reader,
                    patch("auto_7z_base64.prefetch.time.perf_counter", side_effect=[0, elapsed]),
                    patch("auto_7z_base64.prefetch.os.cpu_count", return_value=8),
                ):
                    reader.return_value.local.return_value = True
                    plan.warm()
                    paths = [call.args[0] for call in reader.return_value.read.call_args_list]
                    self.assertEqual(len(paths), reads)
                    self.assertEqual(len(set(paths)), reads)

    @unittest.skipUnless(sys.platform == "win32", "Windows native file I/O is required")
    def test_native_reads_preserve_source_times_and_close_handles(self):
        plan = PrefetchPlan()
        before = {}
        for index in range(64):
            path = self.root / f"file-{index}.txt"
            path.write_bytes(b"exact file bytes")
            os.utime(path, ns=(1_500_000_000_000_000_000, 1_500_000_000_000_000_000))
            info = path.stat()
            before[path] = (info.st_atime_ns, info.st_mtime_ns, info.st_size)
        _source_paths([self.root], plan)
        reader = _WindowsReader()
        if not reader.local(self.root.anchor):
            self.skipTest("A local fixed drive is required")
        count_handles = ctypes.WinDLL("kernel32").GetProcessHandleCount
        count_handles.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        count_handles.restype = ctypes.c_int
        initial = ctypes.c_uint32()
        final = ctypes.c_uint32()
        self.assertTrue(count_handles(ctypes.c_void_p(-1), ctypes.byref(initial)))
        plan.warm()
        self.assertTrue(count_handles(ctypes.c_void_p(-1), ctypes.byref(final)))
        self.assertLessEqual(final.value, initial.value + 2)
        for path, expected in before.items():
            info = path.stat()
            self.assertEqual((info.st_atime_ns, info.st_mtime_ns, info.st_size), expected)

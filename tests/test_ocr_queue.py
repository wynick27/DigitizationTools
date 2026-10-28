import tempfile
import threading
import unittest
from unittest.mock import patch

from ocr.ocr_worker import OCRWorker


class OCRQueueTests(unittest.TestCase):
    def test_slow_page_does_not_block_refilling_other_slots(self):
        release = threading.Event()
        last_started = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0
        extraction_threads = []

        def image(*args):
            extraction_threads.append(threading.get_ident())
            return b"image"

        def engine(data, token, model, page):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                if page == 1:
                    release.wait(10)
                if page == 8:
                    last_started.set()
                return {"page": page}
            finally:
                with lock:
                    active -= 1

        with tempfile.TemporaryDirectory() as directory:
            worker = OCRWorker("batch", list(range(1, 9)), {"ocr_json_path": directory},
                               {"ocr_concurrent_tasks": 3, "ocr_retry_count": 1})
            with patch("ocr.ocr_worker.get_page_image", side_effect=image), patch.object(worker, "_run_engine", side_effect=engine):
                thread = threading.Thread(target=worker.run)
                thread.start()
                try:
                    self.assertTrue(last_started.wait(5), "queue waited for the blocked first page")
                    self.assertTrue(thread.is_alive())
                    self.assertLessEqual(peak, 3)
                    self.assertEqual(set(extraction_threads), {thread.ident})
                finally:
                    release.set()
                    thread.join(10)
                self.assertFalse(thread.is_alive())

    def test_stop_leaves_wait_without_waiting_for_engine(self):
        release = threading.Event()
        started = threading.Event()
        engine_done = threading.Event()

        def engine(*args):
            started.set()
            release.wait(10)
            engine_done.set()
            return {}

        with tempfile.TemporaryDirectory() as directory:
            worker = OCRWorker("batch", [1, 2], {"ocr_json_path": directory},
                               {"ocr_concurrent_tasks": 1, "ocr_retry_count": 1})
            with patch("ocr.ocr_worker.get_page_image", return_value=b"image"), patch.object(worker, "_run_engine", side_effect=engine):
                thread = threading.Thread(target=worker.run)
                thread.start()
                try:
                    self.assertTrue(started.wait(5))
                    worker.stop()
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                finally:
                    release.set()
                    engine_done.wait(5)
                    thread.join(10)

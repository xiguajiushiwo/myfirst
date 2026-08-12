import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from app.recognition.ocr_engine import _predict_array


def test_shared_ocr_predictor_is_serialized():
    active = 0
    maximum = 0
    state_lock = threading.Lock()

    class FakeEngine:
        def predict(self, input):
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with state_lock:
                active -= 1
            return []

    engine = FakeEngine()
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: _predict_array(engine, image), range(2)))

    assert maximum == 1

from __future__ import annotations

import os

from botex.config import _dotenv_is_disabled


def test_test_harness_disables_dotenv_loading():
    assert os.environ["BOTEX_DISABLE_DOTENV"] == "1"
    assert _dotenv_is_disabled()

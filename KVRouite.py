# -*- coding: utf-8 -*-
"""Backward-compatible launcher. Use `python VGSync.py` for the main entry point."""

import os
import runpy

if __name__ == "__main__":
    _here = os.path.dirname(os.path.abspath(__file__))
    runpy.run_path(os.path.join(_here, "VGSync.py"), run_name="__main__")

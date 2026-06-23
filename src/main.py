import os
import argparse
import logging
import streamlit.web.bootstrap
from typing import Literal
from pydantic import BaseModel, Field
from pathlib import Path

from .__version__ import __version__
from .test import run_mirror_test, run_loopback_test, run_play_test


def main():
    parser = argparse.ArgumentParser(description="MTConnect Player")
    parser.add_argument("--version", action="version", version=f"MTConnectExplorer {__version__}")
    parser.add_argument("--loopback-test", action="store_true", help="Run the loopback test")
    parser.add_argument("--mirror-test", action="store_true", help="Run the mirror test")
    parser.add_argument("--play-test", action="store_true", help="Run the play test")
    args = parser.parse_args()

    #_signal_register()
    if args.loopback_test:
        run_loopback_test()
    elif args.mirror_test:
        run_mirror_test()
    elif args.play_test:
        run_play_test()
    else:
        app_path = str(Path(__file__).parent / "app.py")
        flag_options = {
            "global.developmentMode": False,
            "server.port": 8501,
            "server.headless": True,
            "server.showEmailPrompt": False,
            "browser.gatherUsageStats": False,
            "client.toolbarMode": "minimal",
        }
        streamlit.web.bootstrap.load_config_options(flag_options=flag_options)
        flag_options["_is_running_with_streamlit"] = True
        streamlit.web.bootstrap.run(app_path, False, [], flag_options)

if __name__ == "__main__":
    main()

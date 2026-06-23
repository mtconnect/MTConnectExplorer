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
    parser.add_argument("--port", type=int, default=8501, help="Port to run the web server on (default: 8501)")
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
            "server.port": args.port,
            "server.headless": True,
            "server.showEmailPrompt": False,
            "server.maxUploadSize": 5120,  # 5 GB dataset uploads
            "browser.gatherUsageStats": False,
            "client.toolbarMode": "minimal",
            # Suppress Streamlit's "You can now view your Streamlit app..." banner
            # (and its URL list) so we can print our own startup message below.
            "logger.hideWelcomeMessage": True,
        }
        streamlit.web.bootstrap.load_config_options(flag_options=flag_options)
        flag_options["_is_running_with_streamlit"] = True

        # Silence urllib3's connection DEBUG logs (emitted by the external-IP
        # lookup below, which fetches the public IP over HTTP).
        logging.getLogger("urllib3").setLevel(logging.WARNING)

        # Print our own startup banner (Streamlit's is suppressed above), reusing
        # Streamlit's URL helpers so the Network/External URLs match its output.
        from streamlit import net_util
        from streamlit.web.server import server_util
        print(f"  Open MTConnectExplorer v{__version__} in your browser:")
        print(f"    Local URL: {server_util.get_url('localhost')}")
        internal_ip = net_util.get_internal_ip()
        if internal_ip:
            print(f"    Network URL: {server_util.get_url(internal_ip)}")
        external_ip = net_util.get_external_ip()
        if external_ip:
            print(f"    External URL: {server_util.get_url(external_ip)}")
        print()

        streamlit.web.bootstrap.run(app_path, False, [], flag_options)

if __name__ == "__main__":
    main()

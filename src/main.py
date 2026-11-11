import argparse
import logging
import sys
from pathlib import Path

from .__version__ import __version__

# Hosts considered local for the refuse-to-start check (docs/api.md §2).
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def main():
    parser = argparse.ArgumentParser(description="MTConnect Player")
    parser.add_argument("--version", action="version", version=f"MTConnectExplorer {__version__}")
    parser.add_argument("--loopback-test", action="store_true", help="Run the loopback test")
    parser.add_argument("--mirror-test", action="store_true", help="Run the mirror test")
    parser.add_argument("--play-test", action="store_true", help="Run the play test")
    # Headless SHDR-log -> dataset conversion (no web UI). Providing
    # --convert-shdr-log runs the converter and exits, mirroring the --*-test
    # flags above.
    parser.add_argument("--convert-shdr-log", metavar="LOGFILE",
                        help="Headless: convert an SHDR .log into a RawMTConnect dataset, then exit")
    parser.add_argument("--devices", metavar="DEVICESXML",
                        help="Devices.xml supplying the probe/device models (required with --convert-shdr-log)")
    parser.add_argument("--out", metavar="OUTFILE",
                        help="Output dataset path for --convert-shdr-log (default: datasets/<logstem>.jsonl)")
    parser.add_argument("--sample-interval-ms", type=int, default=250,
                        help="--convert-shdr-log: observation batching window in ms (default: 250)")
    parser.add_argument("--device", metavar="NAME",
                        help="--convert-shdr-log: restrict output to a single device")
    parser.add_argument("--port", type=int, default=8501, help="Port to run the web server on (default: 8501)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Address to bind the web server to (default: 127.0.0.1). "
                             "Binding a non-local address requires an api_key in config.json.")
    # Hidden: self-exec worker mode (spawned by services.hosts.SubprocessHost;
    # config on stdin, NDJSON events on stdout). Dispatched before any
    # streamlit import so worker processes start lean and PyInstaller
    # re-entry is safe.
    parser.add_argument("--worker", choices=["capture", "play", "simulate"],
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker:
        from .services.worker_main import run_worker
        sys.exit(run_worker(args.worker))
    if args.convert_shdr_log:
        if not args.devices:
            parser.error("--convert-shdr-log requires --devices")
        from .log_converter import convert_log
        convert_log(
            args.convert_shdr_log, args.devices, args.out,
            sample_interval_ms=args.sample_interval_ms, device=args.device,
        )
        return
    if args.loopback_test:
        from .test import run_loopback_test
        run_loopback_test()
    elif args.mirror_test:
        from .test import run_mirror_test
        run_mirror_test()
    elif args.play_test:
        from .test import run_play_test
        run_play_test()
    else:
        _run_server(host=args.host, port=args.port)


def _run_server(host: str, port: int) -> None:
    """Serve the Streamlit UI at / and the FastAPI backend at /api — one
    process, one port, via streamlit.starlette.App with the API mounted."""
    from starlette.routing import Mount

    import streamlit.web.bootstrap
    from streamlit.starlette import App

    from .api.app import create_api
    from .services import auth
    from .services.config import load_config, setup_logging

    config = load_config()
    setup_logging(config)

    exposed = host not in _LOCAL_HOSTS
    if exposed and not config.api_key:
        print(
            f"Error: refusing to bind '{host}' without an API key.\n"
            "Set an 'api_key' in config.json before binding beyond localhost. "
            "(It gates the /api endpoints; the UI itself has no authentication, "
            "so only ever run this on a trusted LAN, never the internet.)",
            file=sys.stderr,
        )
        sys.exit(1)

    # The in-process frontend authenticates with the runtime token; external
    # clients use the config.json api_key.
    auth.configure(api_key=config.api_key, server_port=port)

    flag_options = {
        "global.developmentMode": False,
        "server.port": port,
        "server.address": host,
        "server.headless": True,
        "server.showEmailPrompt": False,
        "server.maxUploadSize": 5120,  # 5 GB dataset uploads
        "browser.gatherUsageStats": False,
        "client.toolbarMode": "minimal",
        # Suppress Streamlit's "You can now view your Streamlit app..." banner
        # (and its URL list) so we can print our own startup message below.
        "logger.hideWelcomeMessage": True,
    }
    # Load options now so the URL helpers below read the right port/address;
    # App.run() re-applies the same mapping.
    streamlit.web.bootstrap.load_config_options(flag_options=flag_options)

    # Silence urllib3's connection DEBUG logs (emitted by the external-IP
    # lookup below, which fetches the public IP over HTTP).
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Print our own startup banner (Streamlit's is suppressed above), reusing
    # Streamlit's URL helpers so the URLs match its output. Only the LAN
    # (Network) URL is shown when exposed — the public-IP "External" URL is
    # deliberately omitted: this app has no UI authentication and is for
    # trusted-LAN use only, so advertising an internet-facing URL would invite
    # exactly the deployment the warning below tells you not to do.
    from streamlit import net_util
    from streamlit.web.server import server_util
    print(f"  Open MTConnectExplorer v{__version__} in your browser:")
    print(f"    Local URL: {server_util.get_url('localhost')}")
    if exposed:
        internal_ip = net_util.get_internal_ip()
        if internal_ip:
            print(f"    Network URL: {server_util.get_url(internal_ip)}")
    print(f"    API Docs:  {server_util.get_url('localhost')}/api/docs")
    print()
    if exposed:
        print(f"  WARNING: bound to {host} - reachable by other machines on the network,")
        print("  and the UI has no authentication. Use only on a trusted LAN; never")
        print("  expose this app to the internet.")
    else:
        print(f"  Bound to {host} (this machine only). To reach it from other machines on")
        print("  a trusted LAN, launch with --host 0.0.0.0 (requires an 'api_key' in")
        print("  config.json). This app has no UI authentication; never expose it to the")
        print("  internet.")
    print()

    app = App(
        str(Path(__file__).parent / "app.py"),
        routes=[Mount("/api", app=create_api())],
    )
    app.run(config=flag_options)


if __name__ == "__main__":
    main()

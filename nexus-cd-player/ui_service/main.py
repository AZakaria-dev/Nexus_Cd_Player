#!/usr/bin/env python3
"""Entry point for the Nexus CD player UI service.

    python3 -m ui_service.main

Runs OUTSIDE Click confinement, as the normal phablet user, because it needs
the optical drive via /dev/sg*. The Click/QML app is a frontend only and talks
to this over loopback HTTP.

Never starts playback on its own. It does perform one disc scan at startup so
the UI has something real to show immediately; pass --no-scan to skip it.
"""

import argparse
import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import controller as ctrl  # noqa: E402
from backend.log import get_logger  # noqa: E402
from ui_service import server as server_mod  # noqa: E402
from ui_service.worker import ControllerWorker  # noqa: E402

log = get_logger("ui_service")


def say(text=""):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Nexus CD Player UI service (loopback HTTP + JSON)")
    parser.add_argument("--host", default=server_mod.HOST,
                        help="loopback address to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=server_mod.PORT,
                        help="port to bind (default 8765)")
    parser.add_argument("--no-scan", action="store_true",
                        help="do not scan the disc at startup")
    parser.add_argument("--verbose", action="store_true",
                        help="also log to the console")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.verbose:
        console = logging.StreamHandler()
        console.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: "
                              "%(message)s"))
        logging.getLogger("nexus_cd").addHandler(console)

    say("Nexus CD Player UI service")

    controller = ctrl.CDController()
    worker = ControllerWorker(controller)
    worker.start()

    try:
        server = server_mod.create_server(worker, host=args.host,
                                          port=args.port)
    except ValueError as exc:
        say("Refusing to start: " + str(exc))
        worker.stop()
        return 2
    except OSError as exc:
        say("Cannot bind {0}:{1} -- {2}".format(args.host, args.port, exc))
        say("Is another copy of the service already running?")
        worker.stop()
        return 1

    host, port = server.server_address[0], server.server_address[1]
    say("Listening on http://{0}:{1}".format(host, port))
    say("Endpoints: GET /state, GET /health, POST /scan /play /pause "
        "/resume /stop /next /previous")
    if not args.no_scan:
        say("Scanning disc...")
        worker.submit("scan")
    say("Ctrl+C to shut down.")
    say("")

    stopping = {"now": False}

    def request_shutdown(signum, frame):
        if stopping["now"]:
            return
        stopping["now"] = True
        say("")
        say("Shutting down...")

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    thread = server_mod.serve_in_thread(server)
    try:
        while not stopping["now"] and worker.is_running():
            time.sleep(0.2)
    except KeyboardInterrupt:           # pragma: no cover - belt and braces
        stopping["now"] = True
        say("")
        say("Shutting down...")

    if not worker.is_running() and not stopping["now"]:
        say("Controller worker stopped unexpectedly; shutting down.")

    server.shutdown()
    server.server_close()
    thread.join(timeout=5.0)

    # The worker closes the controller on its own thread, preserving single
    # ownership even during shutdown.
    if worker.stop(timeout=15.0):
        say("Stopped cleanly.")
        return 0
    say("Worker did not stop cleanly; check logs.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

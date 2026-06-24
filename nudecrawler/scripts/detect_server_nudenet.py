#!/usr/bin/env python3

from flask import Flask, request
import os
import sys
import daemon
from daemon import pidfile
import tempfile

# import lockfile
import argparse
import signal
from PIL import UnidentifiedImageError
from rich.pretty import pprint

from ..config import read_config, get_config_path
from ..nudenet import nudenet_detect

app = Flask(__name__)

pidfile_path = "/tmp/.nudenet-server.pid"


@app.get("/ping")
def ping():
    return "ну вот pong, и что?"


@app.route("/detect", methods=["POST"])
def detect():
    temp_path = None
    page = None

    if request.files and "image" in request.files:
        file = request.files["image"]
        page = request.form.get("page")
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(file.read())
            temp_path = tmp.name
    elif request.json:
        path = request.json.get("path")
        page = request.json.get("page")
        if path:
            temp_path = path

    if not temp_path:
        return {"status": "ERROR", "error": "No image provided"}

    try:
        verdict = nudenet_detect(path=temp_path, page_url=page)
    except UnidentifiedImageError as e:
        print(f"Err: {page} {e}")
        return {"status": "ERROR", "error": str(e)}
    except Exception as e:
        print(f"Got uncaught exception {type(e)}: {e}")
        return {"status": "ERROR", "error": str(e)}
    finally:
        if request.files and "image" in request.files and temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception as e:
                print(f"Failed to delete temp file {temp_path}: {e}")

    pprint(f"{page}: {verdict}")
    return dict(verdict=verdict, page=page)


def get_args():
    config_path = get_config_path()
    parser = argparse.ArgumentParser("Daemonical REST API for NudeNet")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("-d", "--daemon", action="store_true", default=False)
    parser.add_argument(
        "-c",
        "--config",
        default=config_path,
        help=f"Path to nudecrawler.toml, ({config_path})",
    )
    parser.add_argument("--kill", action="store_true", default=False)
    parser.add_argument("--pidfile", default=pidfile_path)
    return parser.parse_args()


def main():
    global classifier
    global pidfile

    read_config()
    args = get_args()

    if args.kill:
        try:
            with open(pidfile_path) as fh:
                pid = int(fh.read())
                print("Killing nudenet server with pid", pid)
                os.kill(pid, signal.SIGINT)
            os.unlink(pidfile_path)
        except FileNotFoundError:
            print("no pidfile", pidfile_path, "not doing anything")
        sys.exit(0)

    if args.daemon:
        print("work as daemon...")
        with daemon.DaemonContext(
            # pidfile=lockfile.FileLock(args.pidfile)
            pidfile=pidfile.TimeoutPIDLockFile(pidfile_path)
        ):
            # pid = os.getpid()
            # with open(pidfile, "w+") as fh:
            #    print(pid, file=fh)
            print("daemon app.run")
            app.run(port=args.port)
            print("after app.run")
    else:
        app.run(port=args.port)

    print("done.")


if __name__ == "__main__":
    main()

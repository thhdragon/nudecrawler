#!/usr/bin/env python3

import argparse
import os
import signal
import sys

import daemon
from daemon import pidfile
from flask import Flask, request
from PIL import Image, UnidentifiedImageError
from rich.pretty import pprint
from transformers import pipeline

from ..config import get_config_path, read_config

app = Flask(__name__)
classifier = None

pidfile_path = "/tmp/.falconsai-server.pid"


@app.get("/ping")
def ping():
    return "ну вот pong, и что?"


@app.route("/detect", methods=["POST"])
def detect():
    path = request.json["path"]
    page = request.json.get("page")
    threshold = float(
        request.json.get("threshold", os.getenv("DETECTOR_THRESHOLD", "0.5"))
    )

    try:
        image = Image.open(path).convert("RGB")
        results = classifier(image)
    except UnidentifiedImageError as e:
        print(f"Err: {page} {e}")
        return {"status": "ERROR", "error": str(e)}
    except Exception as e:
        print(f"Got uncaught exception {type(e)}: {e}")
        return {"status": "ERROR", "error": str(e)}

    scores = {r["label"]: r["score"] for r in results}
    nsfw_score = scores.get("nsfw", 0.0)
    verdict = nsfw_score >= threshold

    pprint(f"{page}: {verdict} (nsfw={nsfw_score:.3f})")
    return dict(verdict=verdict, page=page, nsfw_score=nsfw_score)


def get_args():
    config_path = get_config_path()
    parser = argparse.ArgumentParser("Daemonical REST API for Falconsai NSFW detection")
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


def load_model():
    global classifier
    print("Loading Falconsai NSFW model...")
    classifier = pipeline(
        "image-classification", model="Falconsai/nsfw_image_detection"
    )
    print("Model ready.")


def main():
    read_config()
    args = get_args()

    if args.kill:
        try:
            with open(pidfile_path) as fh:
                pid = int(fh.read())
                print("Killing falconsai server with pid", pid)
                os.kill(pid, signal.SIGINT)
            os.unlink(pidfile_path)
        except FileNotFoundError:
            print("no pidfile", pidfile_path, "not doing anything")
        sys.exit(0)

    if args.daemon:
        print("work as daemon...")
        with daemon.DaemonContext(pidfile=pidfile.TimeoutPIDLockFile(pidfile_path)):
            load_model()
            app.run(port=args.port)
    else:
        load_model()
        app.run(port=args.port)

    print("done.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

import argparse
import os
import signal
import sys

if os.name != "nt":
    import daemon
    from daemon import pidfile
else:
    daemon = None
    pidfile = None
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
    image_file = None
    page = None
    threshold = float(os.getenv("DETECTOR_THRESHOLD", "0.5"))

    if request.files and "image" in request.files:
        image_file = request.files["image"]
        page = request.form.get("page")
        threshold = float(request.form.get("threshold", threshold))
    elif request.json:
        path = request.json.get("path")
        page = request.json.get("page")
        threshold = float(request.json.get("threshold", threshold))
        if path:
            image_file = path

    if not image_file:
        return {"status": "ERROR", "error": "No image provided"}

    try:
        if isinstance(image_file, str):
            image = Image.open(image_file).convert("RGB")
        else:
            image = Image.open(image_file.stream).convert("RGB")
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


@app.route("/detect_batch", methods=["POST"])
def detect_batch():
    threshold = float(os.getenv("DETECTOR_THRESHOLD", "0.5"))
    
    images = []
    valid_indices = []
    errors = {}
    paths_or_files = []
    pages = []

    if request.files and request.files.getlist("images"):
        uploaded_files = request.files.getlist("images")
        pages = request.form.getlist("pages")
        threshold = float(request.form.get("threshold", threshold))
        paths_or_files = uploaded_files
    elif request.json:
        paths = request.json.get("paths", [])
        pages = request.json.get("pages", [])
        threshold = float(request.json.get("threshold", threshold))
        paths_or_files = paths

    for idx, item in enumerate(paths_or_files):
        page = pages[idx] if idx < len(pages) else None
        try:
            if isinstance(item, str):
                image = Image.open(item).convert("RGB")
            else:
                image = Image.open(item.stream).convert("RGB")
            images.append(image)
            valid_indices.append(idx)
        except UnidentifiedImageError as e:
            print(f"Err opening image {item if isinstance(item, str) else 'uploaded_file'}: {page} {e}")
            errors[idx] = str(e)
        except Exception as e:
            print(f"Got exception opening image {item if isinstance(item, str) else 'uploaded_file'}: {page} {e}")
            errors[idx] = str(e)

    verdicts = [None] * len(paths_or_files)
    scores = [0.0] * len(paths_or_files)

    if images:
        try:
            results = classifier(images, batch_size=len(images))
            for i, result in zip(valid_indices, results):
                result_scores = {r["label"]: r["score"] for r in result}
                nsfw_score = result_scores.get("nsfw", 0.0)
                verdict = nsfw_score >= threshold
                verdicts[i] = bool(verdict)
                scores[i] = float(nsfw_score)
                page = pages[i] if i < len(pages) else None
                pprint(f"{page}: {verdict} (nsfw={nsfw_score:.3f})")
        except Exception as e:
            print(f"Got exception during classification: {e}")
            for idx in valid_indices:
                errors[idx] = f"Classification failed: {e}"

    response_items = []
    for idx in range(len(paths_or_files)):
        if idx in errors:
            response_items.append({
                "status": "ERROR",
                "error": errors[idx],
                "verdict": False,
                "nsfw_score": 0.0
            })
        else:
            response_items.append({
                "status": "OK",
                "verdict": verdicts[idx],
                "nsfw_score": scores[idx]
            })

    return {"results": response_items}


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
    device = -1
    try:
        import torch
        if torch.cuda.is_available():
            device = 0
            print("CUDA is available. Using GPU for classification.")
    except ImportError:
        pass
    classifier = pipeline(
        "image-classification", model="Falconsai/nsfw_image_detection", device=device
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
        if os.name == "nt":
            print("Daemon mode not supported on Windows.", file=sys.stderr)
            sys.exit(1)
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

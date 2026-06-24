#!/usr/bin/env python

"""script to check via Falconsai NSFW Image Detection.

https://huggingface.co/Falconsai/nsfw_image_detection.
"""

import os
import sys
from urllib.parse import urljoin

import requests

from nudecrawler.exceptions import NudeCrawlerException
from nudecrawler.localimage import basic_check

start_detector = "detect-server-falconsai.py"


def detect_nudity(path, address, threshold):
    endpoint = urljoin(address, "/detect")
    try:
        with open(path, "rb") as f:
            files = {"image": f}
            data = {"threshold": threshold}
            r = requests.post(endpoint, files=files, data=data)
            r.raise_for_status()
    except requests.RequestException as e:
        print(e)
        print("maybe detector not running?")
        print(f"start with: python {start_detector}")
        print("or add -a to skip filtering")
        sys.exit(100)

    rj = r.json()
    return int(rj["verdict"])


def main():
    image_path = sys.argv[1]

    detector_address = os.getenv("DETECTOR_ADDRESS", "http://localhost:5000/")
    detector_threshold = float(os.getenv("DETECTOR_THRESHOLD", "0.5"))
    min_w = int(os.getenv("DETECTOR_MIN_W", "0"))
    min_h = int(os.getenv("DETECTOR_MIN_H", "0"))

    try:
        basic_check(image_path, min_w, min_h)
    except NudeCrawlerException:
        sys.exit(0)

    c = detect_nudity(image_path, address=detector_address, threshold=detector_threshold)
    sys.exit(c)


if __name__ == "__main__":
    main()

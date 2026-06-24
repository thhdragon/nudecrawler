import json
import os
import sys
import threading

from .verbose import printv


class ImageCache(object):
    def __init__(self):
        self._sum2v = dict()
        self._url2sum = dict()
        self._lock = threading.Lock()

        self.miss_url = 0
        self.miss_sum = 0
        self.hit_url = 0
        self.hit_sum = 0
        self._new = 0

    def url2v(self, url):
        with self._lock:
            try:
                sum = self._url2sum[url]
                v = self._sum2v[sum]
                self.hit_url += 1
                return v
            except KeyError:
                self.miss_url += 1
                return None

    def sum2v(self, sum, url=None):
        with self._lock:
            try:
                v = self._sum2v[sum]
                self.hit_sum += 1
                # if we are here, url (most likely) not in _url2v
                if url:
                    self._url2sum[url] = sum
                    self._new += 1
                return v
            except KeyError:
                self.miss_sum += 1
                return None

    def register(self, url, sum, verdict):
        with self._lock:
            self._url2sum[url] = sum
            self._sum2v[sum] = verdict
            self._new += 1

    def load(self, path):
        with self._lock:
            with open(path) as fh:
                try:
                    cache = json.load(fh)
                except json.decoder.JSONDecodeError:
                    print("Invalid JSON in cache file", path)
                    print("Fix or delete file and restart")
                    sys.exit(1)

            self._url2sum = cache["_url2sum"]
            self._sum2v = cache["_sum2v"]
            print(f"Loaded {len(self._url2sum)} urls and {len(self._sum2v)} sums cache")

    def save_conditional(self, path, new=1):
        with self._lock:
            tmppath = path + ".tmp"
            if self._new >= new:
                printv(f"Save cache with {self._new} updates")
                self.save_unlocked(tmppath)
                os.replace(tmppath, path)

    def save_unlocked(self, path):
        data = dict(_url2sum=self._url2sum, _sum2v=self._sum2v)
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
        self._new = 0

    def save(self, path):
        with self._lock:
            self.save_unlocked(path)

    def status(self):
        with self._lock:
            return {
                "urls": len(self._url2sum),
                "sums": len(self._sum2v),
                "hit_url": self.hit_url,
                "hit_sum": self.hit_sum,
                "miss_url": self.miss_url,
                "miss_sum": self.miss_sum,
            }


cache = ImageCache()

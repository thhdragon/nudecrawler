import concurrent.futures
import hashlib
import http
import json
import os
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from evalidate import Expr

from .cache import cache
from .exceptions import *
from .remoteimage import RemoteImage
from .verbose import printv

processed_images = 0
processed_images_lock = threading.Lock()
context_fields = [
    "total_images",
    "nude_images",
    "nonnude_images",
    "new_nude_images",
    "new_nonnude_images",
    "new_total_images",
    "total_video",
]


def get_processed_images():
    return processed_images


def sha1sum(path):
    sum = hashlib.sha1()
    with open(path, "rb") as source:
        block = source.read(2**16)
        while len(block) != 0:
            sum.update(block)
            block = source.read(2**16)
    return sum.hexdigest()


class Page:
    def __init__(
        self,
        url: str,
        all_found=False,
        detect_image=None,
        min_total_images=0,
        min_images_size=10 * 1024,
        image_extensions=None,
        max_errors=None,
        max_pictures=None,
        detect_url=None,
        min_content_length=None,
        ignore_content_length=None,
        expr="True",
        workers=1,
        batch_manager=None,
    ):
        self.url = url
        self.workers = workers
        self._lock = threading.Lock()
        self.nban_links = 0
        self.nban_images = 0
        self.nude_images = 0
        self.nonnude_images = 0
        self.total_images = 0
        self.pending_images = 0
        self.batch_manager = batch_manager

        # images not found in cache
        self.new_nude_images = 0
        self.new_nonnude_images = 0
        self.new_total_images = 0

        self.total_video = 0
        self.text_found = list()
        self.links = list()
        self.images = list()
        self.videos = list()
        self.all_found = all_found
        self.ignore_content_length = ignore_content_length

        # minor errors
        self.error_counter = 0

        self.detect_image = detect_image
        self.detect_url = detect_url
        self.image_extensions = image_extensions or [".jpg", ".jpeg"]
        self.min_image_size = min_images_size
        self.min_total_images = min_total_images
        self.max_errors = max_errors
        self.max_pictures = max_pictures

        # expr to filter interesing
        self._code = None

        self.http_code = None

        self._ignore = False  # Ignore this page, we think it's spam, duplicate
        self._status = None
        self._status_detailed = None
        self._status_logged = False
        self._log = list()
        self.check_time = None
        self.content_length = None

        # can throw evalidate.EvalExpression here
        # node = Expr(expr).code
        # self._code = compile(node, '<user filter>', 'eval')
        self._code = Expr(expr).code

        printv("Processing:", self.url)

        try:
            page = urllib.request.urlopen(self.url)
            self.http_code = page.getcode()
            self.content_length = page.headers.get("content-length")
            if self.content_length is not None:
                self.content_length = int(self.content_length)

            if (
                self.content_length
                and min_content_length
                and self.content_length < min_content_length
            ):
                self.ignore(f"content-length {self.content_length} < minimal {min_content_length}")
                return
        except (
            urllib.error.URLError,
            ConnectionError,
            http.client.RemoteDisconnected,
        ) as e:
            if hasattr(e, "status") and e.status == 404:
                # print(e, type(e))
                # silent ignore most usual error (unless verbose)
                printv(url, 404)
                self._status = "IGNORED"
                self._status_detailed = "404"
                self._ignore = True
                self.http_code = e.status
            else:
                if hasattr(e, "status"):
                    self.http_code = e.status
                self.ignore(f"Exception {e} with {self.url}")
            return
        self.content_length = page.headers.get("content-length")

        if (
            self.ignore_content_length is not None
            and self.ignore_content_length == self.content_length
        ):
            self.ignore(f"Ignore because matches prev page content-length = {self.content_length}")
            return

        self.soup = BeautifulSoup(page, "html.parser")

    def ignore(self, reason):
        with self._lock:
            self._ignore = True
            self._status = "IGNORED"
            self._status_detailed = reason
        printv(f"IGNORE {self.url}, {self._status_detailed}")
        self.log(self._status_detailed)

    def log(self, msg, really=True):
        if not really:
            return
        with self._lock:
            self._log.append(msg)

    def check_all(self):
        started = time.time()

        if self._ignore:
            return

        self.total_images = len(self.soup.find_all("img"))
        self.total_links = len(self.soup.find_all("a"))

        self.check_images()
        self.check_video()

        self.check_time = round(time.time() - started, 2)
        self.log(f"Check time: {self.check_time}")
        # set status
        self.status()

    def check_video(self):
        self.total_video = len(self.soup.find_all("video"))
        for img in self.soup.find_all("video"):
            src = img.get("src")
            self.videos.append(src)

    def error(self, msg):
        with self._lock:
            self.error_counter += 1
            current_errors = self.error_counter
        self.log(f"minor error ({current_errors}): {msg}")
        if self.max_errors is not None and current_errors > self.max_errors:
            self.ignore(f"Too many minor errors: {self.max_errors}")

    def prefilter_image(self, url):
        """True is we should check, False if we can ignore this image"""

        with self._lock:
            if self._ignore:
                return False
        verdict = cache.url2v(url)

        if verdict is not None:
            # self.log(f'{url} passed prefilter because cached')
            return True

        path = urlparse(url).path
        ext = os.path.splitext(path)[1]

        if ext not in self.image_extensions:
            self.log(f"{url} bad extension, ignore")
            return False

        try:
            r = requests.head(url, timeout=1)
        except requests.exceptions.RequestException as e:
            self.error(f"{url} request exception: {e}")
            return False

        if r.status_code != 200:
            self.error(f"Bad status code: {url} {r.status_code}")
            return False
        try:
            cl = int(r.headers["Content-Length"])
        except KeyError:
            cl = None

        # self.log(f"{url} status:{r.status_code} content-length: {cl}")
        if cl is not None and cl < self.min_image_size:
            self.log(f"Too small image ({int(r.headers['Content-Length'])})")
            return False

        # self.log(f"{url} image passed prefilter")
        return True

    def do_detect_url(self, url):

        if self.detect_url == ":true":
            with self._lock:
                self.nude_images += 1
            return True

        if self.detect_url == ":false":
            return False

        env = os.environ.copy()
        env["NUDECRAWLER_PAGE_URL"] = self.url
        env["NUDECRAWLER_IMAGE_URL"] = url
        rc = subprocess.run([self.detect_url, url], env=env)
        if rc.returncode >= 100:
            print("FATAL ERROR")
            os._exit(1)
        if rc.returncode:
            self.log(f"{url} is nude")
            with self._lock:
                self.nude_images += 1
        else:
            self.log(f"{url} is NOT nude")
            with self._lock:
                self.nonnude_images += 1

        return

    def do_detect_image(self, url):
        try:
            ri = RemoteImage(url, page_url=self.url)
            sum = sha1sum(ri.path)
            verdict = cache.sum2v(sum, url=url)
            if verdict is not None:
                if verdict:
                    self.log(f"{url} is nude (cached sum)")
                    with self._lock:
                        self.nude_images += 1
                else:
                    self.log(f"{url} is NOT nude (cached sum)")
                    with self._lock:
                        self.nonnude_images += 1
                return verdict

            verdict = ri.detect_image(self.detect_image)
            with self._lock:
                self.new_total_images += 1

            if verdict:
                with self._lock:
                    self.new_nude_images += 1
            else:
                with self._lock:
                    self.new_nonnude_images += 1

            cache.register(url, sum, verdict)
            if verdict:
                self.log(f"{url} is nude")
                with self._lock:
                    self.nude_images += 1
            else:
                self.log(f"{url} is NOT nude")
                with self._lock:
                    self.nonnude_images += 1
            return
        except NudeCrawlerException as e:
            print(e)
            return

    def detect_cache_url(self, url):
        verdict = cache.url2v(url)
        if verdict is not None:
            if verdict:
                self.log(f"{url} is nude (cached url)")
                with self._lock:
                    self.nude_images += 1
            else:
                self.log(f"{url} is NOT nude (cached url)")
                with self._lock:
                    self.nonnude_images += 1
        return verdict

    def is_nude(self, url):
        if self.detect_cache_url(url) is not None:
            return

        if self.detect_url:
            self.do_detect_url(url)
            return

        if self.detect_image:
            try:
                self.do_detect_image(url)
            except Exception as e:
                printv("Broken image:", url)
                print(e)
            return

        raise (Exception("Need either detect_image or detect_url or all"))

    def check_images(self):
        global processed_images

        image_list = [urljoin(self.url, img.get("src")) for img in self.soup.find_all("img")]
        self.log(f"total images on page: {len(image_list)}")

        if len(image_list) < self.min_total_images:
            self.ignore(f"Skip because total images {len(image_list)} < {self.min_total_images}")
            return

        if self.workers > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
                results = list(executor.map(self.prefilter_image, image_list))
            image_list = [url for url, keep in zip(image_list, results) if keep]
        else:
            image_list = list(
                filter(
                    None,
                    [url if self.prefilter_image(url) else None for url in image_list],
                )
            )

        if self._ignore:
            return

        self.log(f"total prefiltered images on page: {len(image_list)}")
        if len(image_list) < self.min_total_images:
            self.ignore(
                f"Skip because total prefiltered images {len(image_list)} < {self.min_total_images}"
            )
            return

        if not self.all_found:
            targets = image_list[: self.max_pictures]

            def process_target(url):
                # 1. Check URL Cache
                verdict = cache.url2v(url)
                if verdict is not None:
                    with self._lock:
                        if verdict:
                            self.nude_images += 1
                            self.log(f"{url} is nude (cached url)")
                            if self.batch_manager and self.batch_manager.keep_dir:
                                from .batch import save_to_keep
                                save_to_keep(url, keep_dir=self.batch_manager.keep_dir)
                        else:
                            self.nonnude_images += 1
                            self.log(f"{url} is NOT nude (cached url)")
                    return None

                # 2. Download remote image
                try:
                    ri = RemoteImage(url, page_url=self.url)
                    sum_val = sha1sum(ri.path)
                except Exception as e:
                    printv("Broken image:", url)
                    print(e)
                    return None

                # 3. Check Sum Cache
                verdict = cache.sum2v(sum_val, url=url)
                if verdict is not None:
                    with self._lock:
                        if verdict:
                            self.nude_images += 1
                            self.log(f"{url} is nude (cached sum)")
                            if self.batch_manager and self.batch_manager.keep_dir:
                                from .batch import save_to_keep
                                save_to_keep(url, ri.path, sum_val, self.batch_manager.keep_dir)
                        else:
                            self.nonnude_images += 1
                            self.log(f"{url} is NOT nude (cached sum)")
                    return None

                # 4. Needs actual detection
                return (ri, sum_val)

            if self.workers > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
                    results = list(executor.map(process_target, targets))
            else:
                results = [process_target(url) for url in targets]

            with processed_images_lock:
                processed_images += len(targets)

            for res in results:
                if res is None:
                    continue
                ri, sum_val = res
                if self.batch_manager:
                    self.batch_manager.add_image(ri, sum_val, self)
                else:
                    try:
                        verdict = ri.detect_image(self.detect_image)
                        cache.register(ri.url, sum_val, verdict)
                        self.new_total_images += 1
                        if verdict:
                            self.new_nude_images += 1
                            self.nude_images += 1
                            self.log(f"{ri.url} is nude")
                        else:
                            self.new_nonnude_images += 1
                            self.nonnude_images += 1
                            self.log(f"{ri.url} is NOT nude")
                    except Exception as e:
                        print(f"Error detecting {ri.url}: {e}")
        else:
            if self.batch_manager and self.batch_manager.keep_dir:
                targets = image_list[: self.max_pictures]

                def process_target_keep(url):
                    try:
                        from .batch import save_to_keep
                        save_to_keep(url, keep_dir=self.batch_manager.keep_dir)
                    except Exception as e:
                        self.error(f"Error saving image {url} to keep: {e}")

                if self.workers > 1:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
                        list(executor.map(process_target_keep, targets))
                else:
                    for url in targets:
                        process_target_keep(url)

                with processed_images_lock:
                    processed_images += len(targets)

    def status(self):

        if not self._status_logged:
            self._status_logged = True
            makelog = True
        else:
            makelog = False

        if self._ignore:
            return f"{self._status} ({self._status_detailed})"

        self._status_detailed = (
            f"total: {self.total_images} (min: {self.min_total_images}) nude: {self.nude_images}"
        )

        self._status_detailed += f" Cache new/nude: {self.new_total_images}/{self.new_nude_images}"

        if self.total_video:
            self._status_detailed += f" video: {self.total_video}"
        self._status_detailed += f" ({self.check_time}s)"

        self.log(self._status_detailed, makelog)

        if self.all_found:
            self._status = "INTERESTING (ALL)"
            self.log(self._status, makelog)
            return self._status

        # Interesting or not? use evalidate
        ctx = dict()
        for field in context_fields:
            ctx[field] = getattr(self, field)
        r = eval(self._code, ctx.copy())
        if r:
            self._status = "INTERESTING"
            self.log(self._status, makelog)
            return self._status

        self._status = "???"
        self.log(self._status, makelog)
        return self._status

    def as_json(self):
        j = dict()
        j["status"] = self.status()

        for attr in [
            "url",
            "total_images",
            "nude_images",
            "new_nude_images",
            "nonnude_images",
            "new_nonnude_images",
            "total_video",
        ]:
            j[attr] = getattr(self, attr)

        return json.dumps(j)

    def __str__(self):

        text = ""

        text += f"{self.status()} {self.url}{f' ({self.check_time} sec)' if isinstance(self.check_time, float) else ''}\n"

        if self.all_found:
            text += f"  Total images: {self.total_images}\n"
        else:
            text += f"  Nude: {self.nude_images} ({self.new_nude_images} new) non-nude: {self.nonnude_images} ({self.new_nonnude_images} new)\n"

        # if self.new_nude_images or self.new_nonnude_images:
        #    text += f"  New nude {self.new_nude_images} non-nude {self.new_nonnude_images}\n"

        if self.total_video:
            text += f"  Total video: {self.total_video}\n"

        return text

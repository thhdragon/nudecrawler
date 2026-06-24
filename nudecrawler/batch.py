import os
import shutil
import hashlib
import urllib.parse
import concurrent.futures
from urllib.parse import urljoin
import requests

from .cache import cache
from .verbose import printv

def sha1sum(path):
    h = hashlib.sha1()
    with open(path, "rb") as source:
        block = source.read(2**16)
        while len(block) != 0:
            h.update(block)
            block = source.read(2**16)
    return h.hexdigest()

def save_to_keep(url, path=None, sum=None, keep_dir=None):
    if not keep_dir:
        return
    
    os.makedirs(keep_dir, exist_ok=True)
    
    pr = urllib.parse.urlparse(url)
    ext = os.path.splitext(pr.path)[1] or ".jpg"
    
    if not sum:
        with cache._lock:
            sum = cache._url2sum.get(url)
            
    if path and os.path.exists(path):
        if not sum:
            sum = sha1sum(path)
        dest = os.path.join(keep_dir, f"{sum}{ext}")
        if not os.path.exists(dest):
            try:
                shutil.copy(path, dest)
            except Exception as e:
                print(f"Error copying image to keep: {e}")
    else:
        from .remoteimage import RemoteImage
        try:
            ri = RemoteImage(url)
            if not sum:
                sum = sha1sum(ri.path)
            dest = os.path.join(keep_dir, f"{sum}{ext}")
            if not os.path.exists(dest):
                shutil.copy(ri.path, dest)
        except Exception as e:
            print(f"Error downloading image to keep: {e}")

class BatchManager:
    def __init__(self, batch_size, detect_image_script, keep_dir=None, on_page_finalized=None, workers=4):
        self.batch_size = batch_size
        self.script = detect_image_script
        self.keep_dir = keep_dir
        self.on_page_finalized = on_page_finalized
        self.workers = workers
        self.queue = []
        self.pending_pages = set()

    def add_image(self, ri, sha1sum_val, page):
        self.queue.append((ri, sha1sum_val, page))
        page.pending_images += 1
        self.pending_pages.add(page)
        if len(self.queue) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self.queue:
            return

        batch = self.queue
        self.queue = []

        is_falconsai = self.script == "detect-image-falconsai"

        if is_falconsai:
            self._flush_falconsai(batch)
        else:
            self._flush_fallback(batch)

        pages_to_check = set(p for _, _, p in batch)
        for p in pages_to_check:
            if p.pending_images == 0:
                self.pending_pages.discard(p)
                if self.on_page_finalized:
                    self.on_page_finalized(p)

    def _flush_falconsai(self, batch):
        detector_address = os.getenv("DETECTOR_ADDRESS", "http://localhost:5000/")
        detector_threshold = float(os.getenv("DETECTOR_THRESHOLD", "0.5"))
        endpoint = urljoin(detector_address, "/detect_batch")

        paths = [ri.path for ri, _, _ in batch]
        pages = [page.url for _, _, page in batch]

        files = []
        file_handles = []
        try:
            for path in paths:
                fh = open(path, "rb")
                file_handles.append(fh)
                files.append(("images", (os.path.basename(path), fh, "image/jpeg")))

            data = [("pages", page) for page in pages]
            data.append(("threshold", str(detector_threshold)))

            r = requests.post(endpoint, files=files, data=data, timeout=30)
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as e:
            print(f"Error calling detect_batch: {e}")
            results = [{"status": "ERROR", "error": str(e)}] * len(batch)
        finally:
            for fh in file_handles:
                fh.close()

        for (ri, sum_val, page), res in zip(batch, results):
            if res.get("status") == "OK":
                verdict = bool(res.get("verdict"))
                cache.register(ri.url, sum_val, verdict)
                page.new_total_images += 1
                if verdict:
                    page.new_nude_images += 1
                    page.nude_images += 1
                    page.log(f"{ri.url} is nude")
                    if self.keep_dir:
                        save_to_keep(ri.url, ri.path, sum_val, self.keep_dir)
                else:
                    page.new_nonnude_images += 1
                    page.nonnude_images += 1
                    page.log(f"{ri.url} is NOT nude")
            else:
                print(f"Error detecting {ri.url}: {res.get('error')}")
                page.error(f"Detection failed for {ri.url}: {res.get('error')}")

            page.pending_images -= 1

    def _flush_fallback(self, batch):
        def run_single(item):
            ri, sum_val, page = item
            try:
                verdict = ri.detect_image(self.script)
                cache.register(ri.url, sum_val, verdict)
                page.new_total_images += 1
                if verdict:
                    page.new_nude_images += 1
                    page.nude_images += 1
                    page.log(f"{ri.url} is nude")
                    if self.keep_dir:
                        save_to_keep(ri.url, ri.path, sum_val, self.keep_dir)
                else:
                    page.new_nonnude_images += 1
                    page.nonnude_images += 1
                    page.log(f"{ri.url} is NOT nude")
            except Exception as e:
                print(f"Error detecting {ri.url}: {e}")
                page.error(f"Detection failed for {ri.url}: {e}")
            finally:
                page.pending_images -= 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            list(executor.map(run_single, batch))

#!/usr/bin/env python

import argparse
import concurrent.futures
import datetime
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time

import transliterate
import transliterate.discover
from dotenv import load_dotenv
from rich.pretty import pprint

import nudecrawler
import nudecrawler.tgru

from .. import Page, Unbuffered
from ..cache import cache

# from ..verbose import printv
from ..config import get_args
from ..page import context_fields, get_processed_images
from ..version import __version__

transliterate.discover.autodiscover()

stats = {
    "cmd": None,
    "filter": {
        "expr": "True",
        "min_image_size": None,
        "min_total_images": 0,
        "min_content_length": None,
        "max_pictures": None,
        "image_extensions": None,
        "max_errors": None,
    },
    "uptime": 0,
    "urls": 0,
    "words": 0,
    "word": None,
    "last": {
        "url": None,
        "status": None,
        "detailed": None,
    },
    "last_interesting": {
        "url": None,
        "status": None,
        "detailed": None,
    },
    "now": None,
    "processed_images": 0,
    "ignored_pages": 0,
    "found_interesting_pages": 0,
    "found_nude_images": 0,
    "found_new_nude_images": 0,
    "found_new_total_images": 0,
    "resume": {},
    "gap_max": 0,
    "gap_url": None,
    "cache_path": None,
    "cache_save": 1,
}

previous_content_length = None

stats_file = None
stats_period = 60

stats_next_write = time.time() + stats_period

started = time.time()

logfile: str = None
stop_after = None
stop_each = None
refresh = None
detect_image = None
detect_url = None
workers = 1
lookahead = 16
batch_manager = None

#
# page_mintotal = 0

expr = "True"

nude = 1
video = 1
verbose = False
all_found = True

filter_methods = {
    "true": ("builtin", ":true"),
    "false": ("builtin", ":false"),
    "mudepy": ("builtin", ":nude"),
    "nudenetb": ("builtin", ":nudenet"),
    "aid": ("image", "detect-image-aid"),
    "falconsai": ("image", "detect-image-falconsai"),
    "nsfwapi": ("image", "detect-image-nsfw-api"),
    "nudenet": ("image", "detect-image-nudenet"),
}


def finalize_page(p):
    global stop_after, previous_content_length, logfile, verbose

    p.status()  # ensure status is compiled

    stats["last"]["url"] = p.url
    stats["last"]["status"] = p._status
    stats["last"]["detailed"] = p._status_detailed

    stats["found_new_total_images"] += p.new_total_images
    stats["found_new_nude_images"] += p.new_nude_images

    previous_content_length = p.content_length

    if p.status().startswith("INTERESTING"):
        stats["found_interesting_pages"] += 1
        stats["found_nude_images"] += p.nude_images
        stats["last_interesting"]["url"] = p.url
        stats["last_interesting"]["status"] = p._status
        stats["last_interesting"]["detailed"] = p._status_detailed

        if logfile:
            with open(logfile, "a") as fh:
                if logfile.endswith((".json", ".jsonl")):
                    print(p.as_json(), file=fh)
                else:
                    print(p, file=fh)

    if p.status().startswith("INTERESTING") or verbose:
        print(p)

    if p.status().startswith("IGNORED"):
        stats["ignored_pages"] += 1

    save_stats(force=False)

    if stats["cache_path"]:
        cache.save_conditional(stats["cache_path"], stats["cache_save"])

    if stop_after is not None and get_processed_images() > stop_after:
        print("Stop/refresh after processed", get_processed_images(), "images...")
        if refresh:
            subprocess.run(refresh)
            stop_after = get_processed_images() + stop_each
        else:
            print("No --refresh, exiting with code 2")
            sys.exit(2)


def analyse(url):

    global stop_after, previous_content_length, workers, batch_manager

    p = Page(
        url,
        all_found=all_found,
        detect_url=detect_url,
        detect_image=detect_image,
        ignore_content_length=previous_content_length,
        min_images_size=stats["filter"]["min_image_size"],
        image_extensions=stats["filter"]["image_extensions"],
        min_total_images=stats["filter"]["min_total_images"],
        max_errors=stats["filter"]["max_errors"],
        max_pictures=stats["filter"]["max_pictures"],
        expr=stats["filter"]["expr"],
        min_content_length=stats["filter"]["min_content_length"],
        workers=workers,
        batch_manager=batch_manager,
    )

    stats["urls"] += 1

    p.check_all()

    if p.pending_images == 0:
        finalize_page(p)

    return p


def save_stats(force=False):
    global stats_next_write

    if stats_file is None:
        return

    if time.time() > stats_next_write or force:
        stats["now"] = datetime.datetime.now().strftime("%m/%d/%Y %H:%M:%S")
        stats["uptime"] = int(time.time() - started)
        stats["processed_images"] = get_processed_images()

        stats["cache"] = cache.status()

        with open(stats_file, "w") as fh:
            json.dump(stats, fh, indent=4)
            stats_next_write = time.time() + stats_period


def check_word(word, day, fails, resumecount=None):

    global previous_content_length

    word = word.replace(" ", "-").translate({ord("ь"): "", ord("ъ"): ""})

    if word.startswith("https://"):
        baseurl = word
    else:
        trans_word = transliterate.translit(word, "tgru", reversed=True)
        baseurl = f"https://telegra.ph/{trans_word}"

    stats["word"] = word
    stats["words"] += 1

    url = f"{baseurl}-{day.month:02}-{day.day:02}"

    stats["resume"]["month"] = day.month
    stats["resume"]["day"] = day.day
    stats["resume"]["count"] = resumecount

    previous_content_length = None

    # r = requests.get(url)
    if not resumecount:
        p = analyse(url)
        # if p.ignore:
        #    return
        c = 2
        nfails = 1 if p.http_code == 404 else 0
    else:
        c = resumecount
        print(f"Resume from word {word} count {c}")
        nfails = 0

    while nfails < fails:
        if workers > 1:
            chunk_size = max(1, min(lookahead, fails - nfails))
        else:
            chunk_size = 1

        # Build a chunk of candidate URLs
        chunk_urls = []
        for i in range(chunk_size):
            url = f"{baseurl}-{day.month:02}-{day.day:02}-{c + i}"
            chunk_urls.append((c + i, url))

        if chunk_size == 1:
            idx, url = chunk_urls[0]
            p = analyse(url)
            if p.http_code == 404:
                nfails += 1
            else:
                if nfails > stats["gap_max"]:
                    stats["gap_max"] = nfails
                    stats["gap_url"] = url
                nfails = 0
            c += 1
            stats["resume"]["count"] = c
            continue

        # Instantiate Page objects in parallel
        def init_page(item):
            idx_val, url_val = item
            try:
                p_obj = Page(
                    url_val,
                    all_found=all_found,
                    detect_url=detect_url,
                    detect_image=detect_image,
                    ignore_content_length=None,
                    min_images_size=stats["filter"]["min_image_size"],
                    image_extensions=stats["filter"]["image_extensions"],
                    min_total_images=stats["filter"]["min_total_images"],
                    max_errors=stats["filter"]["max_errors"],
                    max_pictures=stats["filter"]["max_pictures"],
                    expr=stats["filter"]["expr"],
                    min_content_length=stats["filter"]["min_content_length"],
                    workers=workers,
                    batch_manager=batch_manager,
                )
                return idx_val, p_obj, None
            except Exception as ex:
                return idx_val, None, ex

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(init_page, chunk_urls))

        results.sort(key=lambda x: x[0])

        break_loop = False
        for idx, p, err in results:
            if err is not None:
                raise err

            url = p.url
            stats["urls"] += 1

            if p.http_code == 404:
                nfails += 1
                finalize_page(p)
                previous_content_length = None
            else:
                if (
                    previous_content_length is not None
                    and previous_content_length == p.content_length
                ):
                    p.ignore(f"Ignore because matches prev page content-length = {p.content_length}")

                p.check_all()
                if p.pending_images == 0:
                    finalize_page(p)

                if nfails > stats["gap_max"]:
                    stats["gap_max"] = nfails
                    stats["gap_url"] = url
                nfails = 0

            c += 1
            stats["resume"]["count"] = c

            if nfails >= fails:
                break_loop = True
                break

        if break_loop:
            break


def run_bulk_http_check(bulk_path, urls, workers=100):
    print(f"# Checking {len(urls)} candidate URLs using bulk-http-check...")

    # We run bulk-http-check with concurrency setting
    process = subprocess.Popen(
        [bulk_path, "-n", str(workers)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8"
    )

    try:
        # Write all URLs separated by newlines to stdin
        stdout, stderr = process.communicate(input="\n".join(urls), timeout=600)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        print("# Warning: bulk-http-check timed out", file=sys.stderr)

    valid_urls = []
    # bulk-http-check output lines look like:
    # https://telegra.ph/some-url OK 200
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 3:
            url_val = parts[0]
            status_code = parts[2]
            if status_code == "200":
                valid_urls.append(url_val)

    print(f"# bulk-http-check found {len(valid_urls)} active URLs out of {len(urls)} candidates.")
    return valid_urls


def process_urls(urls):
    global previous_content_length, workers, stats, all_found, detect_url, detect_image, batch_manager, lookahead

    chunk_size = lookahead if lookahead > 1 and workers > 1 else 1

    for chunk_start in range(0, len(urls), chunk_size):
        chunk = urls[chunk_start : chunk_start + chunk_size]
        chunk_urls = list(enumerate(chunk))

        if chunk_size == 1:
            _, url = chunk_urls[0]
            analyse(url)
            continue

        def init_page(item):
            idx_val, url_val = item
            try:
                p_obj = Page(
                    url_val,
                    all_found=all_found,
                    detect_url=detect_url,
                    detect_image=detect_image,
                    ignore_content_length=None,
                    min_images_size=stats["filter"]["min_image_size"],
                    image_extensions=stats["filter"]["image_extensions"],
                    min_total_images=stats["filter"]["min_total_images"],
                    max_errors=stats["filter"]["max_errors"],
                    max_pictures=stats["filter"]["max_pictures"],
                    expr=stats["filter"]["expr"],
                    min_content_length=stats["filter"]["min_content_length"],
                    workers=workers,
                    batch_manager=batch_manager,
                )
                return idx_val, p_obj, None
            except Exception as ex:
                return idx_val, None, ex

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(init_page, chunk_urls))

        results.sort(key=lambda x: x[0])

        for idx, p, err in results:
            if err is not None:
                print(f"Error processing {chunk[idx]}: {err}", file=sys.stderr)
                continue

            url = p.url
            stats["urls"] += 1

            if p.http_code == 404:
                finalize_page(p)
                previous_content_length = None
            else:
                if (
                    previous_content_length is not None
                    and previous_content_length == p.content_length
                ):
                    p.ignore(f"Ignore because matches prev page content-length = {p.content_length}")

                p.check_all()
                if p.pending_images == 0:
                    finalize_page(p)


def sanity_check(args):
    pass


def load_stats(path):
    global stats
    with open(path) as fh:
        loaded_stats = json.load(fh)

    for k in stats:
        if k not in loaded_stats:
            loaded_stats[k] = stats[k]

    stats = loaded_stats


def abort(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def main():
    global \
        nude, \
        video, \
        verbose, \
        all_found, \
        stats_file, \
        stats, \
        logfile, \
        stop_after, \
        stop_each, \
        detect_image, \
        detect_url, \
        refresh, \
        workers, \
        lookahead

    words = None
    args = get_args(
        argv=None, methods_list=", ".join(filter_methods.keys()), context_fields=context_fields
    )
    sanity_check(args)

    if args.verbose:
        pprint(args)

    # when fastforward, we go to specific word/day/count quickly
    fastforward = False

    # Disabled logger for nudenet
    logging.getLogger().disabled = True

    if args.unbuffered:
        sys.stdout = Unbuffered(sys.stdout)

    if args.resume:
        if args.workdir and not (os.path.isabs(args.resume) or args.resume.startswith(("/", "\\"))):
            args.resume = os.path.join(args.workdir, args.resume)

        print("Resume from", args.resume)
        try:
            load_stats(args.resume)
        except FileNotFoundError as e:
            abort(f"Missing status file {args.resume}")

        cmd = stats["cmd"]
        if not cmd or not isinstance(cmd, str):
            abort(f"Invalid or missing command ('cmd') in status file {args.resume}")
        args = get_args(
            argv=shlex.split(cmd)[1:],
            methods_list=", ".join(filter_methods.keys()),
            context_fields=context_fields,
        )
        fastforward = True
    else:
        stats["cmd"] = shlex.join(sys.argv)

    if args.workdir:
        for attr in ["cache", "wordlist", "log", "resume", "stats"]:
            old = getattr(args, attr)
            if old is not None and not (os.path.isabs(old) or old.startswith(("/", "\\"))):
                new = os.path.join(args.workdir, old)
                setattr(args, attr, new)

    # nude = args.nude
    # video = args.video
    verbose = args.verbose
    all_found = args.all
    matched_resume = False
    skipped_words = 0
    stop_after = args.stop
    stop_each = args.stop
    refresh = args.refresh
    detect_url = args.detect_url
    detect_image = args.detect_image
    stats["filter"]["expr"] = args.expr
    stats["filter"]["min_content_length"] = args.min_content_length
    stats["filter"]["max_errors"] = args.max_errors
    stats["filter"]["max_pictures"] = args.max_pictures
    stats["cache_path"] = args.cache
    stats["cache_save"] = args.cache_save
    workers = args.workers
    lookahead = args.lookahead

    if args.detect:
        try:
            kind, basename = filter_methods[args.detect]
        except KeyError:
            print(
                f"Do not know detector {args.detect!r}, use one of known detectors: ({', '.join(filter_methods.keys())}) or explicitly specify script with --detect-image or --detect-url"
            )
            sys.exit(1)

        if kind in ["image", "url"] and shutil.which(basename) is None:
            print(f"Cannot find {basename}, maybe not in $PATH?", file=sys.stderr)
            sys.exit(1)

        if kind == "builtin":
            if basename in [":nude", ":nudenet"]:
                detect_image = basename
            else:
                detect_url = basename
        elif kind == "image":
            detect_image = basename
            print(f"# Will use script {shutil.which(basename)} for filtering images")
        elif kind == "url":
            detect_url = basename
            print(f"# Will use script {shutil.which(basename)} for filtering images")

    # fix arguments
    if not any([detect_image, detect_url, all_found]):
        print(
            "# No nudity detector (--detect, --detect-url, --detect-image) given, using built-in --detect-image :nude by default"
        )
        detect_image = ":nude"

    nudecrawler.verbose.verbose = verbose

    if args.extensions:
        stats["filter"]["image_extensions"] = args.extensions

    if args.minsize:
        stats["filter"]["min_image_size"] = args.minsize * 1024

    if args.total:
        stats["filter"]["min_total_images"] = args.total

    if stats["cache_path"]:
        if os.path.exists(stats["cache_path"]):
            cache.load(stats["cache_path"])
        else:
            print(f"# No cache file {stats['cache_path']}, start with empty cache")

    # processing could start here
    global batch_manager
    from ..batch import BatchManager
    batch_manager = BatchManager(
        batch_size=args.batch_size,
        detect_image_script=detect_image,
        keep_dir=args.keep,
        on_page_finalized=finalize_page,
        workers=args.workers,
    )

    # --url
    if args.url:
        p = analyse(args.url)
        if batch_manager:
            batch_manager.flush()
        print(p.status())
        for msg in p._log:
            print(" ", msg)
        return

    ## wordlist
    if args.wordlist:
        with open(args.wordlist) as fh:
            words = [line.rstrip() for line in fh]

    if args.words:
        words = args.words

    if not words:
        print("Need either --url URL or words like 'nude' or -w wordlist.txt")
        sys.exit(1)

    logfile = args.log
    stats_file = args.stats

    use_bulk = False
    bulk_path = None
    if not args.no_bulk and not fastforward:
        cwd_bin = os.path.join(os.getcwd(), "bulk-http-check")
        cwd_bin_exe = os.path.join(os.getcwd(), "bulk-http-check.exe")
        if os.path.isfile(cwd_bin) and os.access(cwd_bin, os.X_OK):
            bulk_path = cwd_bin
        elif os.path.isfile(cwd_bin_exe) and (os.name == "nt" or os.access(cwd_bin_exe, os.X_OK)):
            bulk_path = cwd_bin_exe
        else:
            bulk_path = shutil.which("bulk-http-check") or shutil.which("bulk-http-check.exe")

        if bulk_path:
            use_bulk = True
        else:
            print("# bulk-http-check executable not found in PATH or CWD. Using standard sequential crawler.")

    if use_bulk:
        if args.day is None:
            start_day = datetime.datetime.now()
        else:
            start_day = datetime.datetime(2020, args.day[0], args.day[1])

        candidate_urls = []
        for w in words:
            w_cleaned = w.replace(" ", "-").translate({ord("ь"): "", ord("ъ"): ""})
            if w_cleaned.startswith("https://"):
                baseurl = w_cleaned
            else:
                trans_word = transliterate.translit(w_cleaned, "tgru", reversed=True)
                baseurl = f"https://telegra.ph/{trans_word}"

            day = start_day
            for _ in range(args.days):
                candidate_urls.append(f"{baseurl}-{day.month:02}-{day.day:02}")
                for c in range(2, args.lookahead + 1):
                    candidate_urls.append(f"{baseurl}-{day.month:02}-{day.day:02}-{c}")
                day = day - datetime.timedelta(days=1)

        valid_urls = run_bulk_http_check(bulk_path, candidate_urls, workers=args.bulk_workers)

        # Filter valid_urls to respect args.fails day-by-day
        valid_urls_set = set(valid_urls)
        filtered_valid_urls = []

        for w in words:
            w_cleaned = w.replace(" ", "-").translate({ord("ь"): "", ord("ъ"): ""})
            if w_cleaned.startswith("https://"):
                baseurl = w_cleaned
            else:
                trans_word = transliterate.translit(w_cleaned, "tgru", reversed=True)
                baseurl = f"https://telegra.ph/{trans_word}"

            day = start_day
            for _ in range(args.days):
                nfails = 0
                url_base = f"{baseurl}-{day.month:02}-{day.day:02}"
                if url_base in valid_urls_set:
                    filtered_valid_urls.append(url_base)
                    nfails = 0
                else:
                    nfails += 1

                if nfails < args.fails:
                    for c in range(2, args.lookahead + 1):
                        url_suffix = f"{baseurl}-{day.month:02}-{day.day:02}-{c}"
                        if url_suffix in valid_urls_set:
                            filtered_valid_urls.append(url_suffix)
                            nfails = 0
                        else:
                            nfails += 1
                        if nfails >= args.fails:
                            break

                day = day - datetime.timedelta(days=1)

        process_urls(filtered_valid_urls)
    else:
        for w in words:
            if fastforward and not matched_resume:
                if w == stats["resume"]["word"]:
                    matched_resume = True
                else:
                    skipped_words += 1
                    continue

            stats["resume"]["word"] = w

            if fastforward:
                day = datetime.datetime(2020, stats["resume"]["month"], stats["resume"]["day"])
            elif args.day is None:
                day = datetime.datetime.now()
            else:
                day = datetime.datetime(2020, args.day[0], args.day[1])

            days_tried = 0
            while days_tried < args.days:
                resumecount = stats["resume"]["count"] if fastforward else None
                # stop fastforward
                fastforward = False
                check_word(w, day, args.fails, resumecount=resumecount)

                days_tried += 1
                day = day - datetime.timedelta(days=1)

    if batch_manager:
        batch_manager.flush()

    print(
        f"Finished {len(words)} (skipped {skipped_words}) words in {time.time() - started:.2f} seconds, found {stats['found_interesting_pages']} pages"
    )
    if fastforward and not matched_resume:
        abort(f"Did not found word {stats['resume']['word']}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt as e:
        print("KEYBOARD INTERRUPT")
        print(e)
        save_stats(force=True)

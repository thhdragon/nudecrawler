import nudecrawler.verbose
from nudecrawler.page import Page

empty = "https://telegra.ph/empty-04-03"
belle_delphine = "https://telegra.ph/belle-delphine-01-16"
sasha_grey = "https://telegra.ph/sasha-grey-04-18"

nudecrawler.verbose.verbose = True


class TestBasic:
    def test_empty(self):
        p = Page(empty, detect_image=":nude")
        p.check_all()
        assert p.status().startswith("INTERESTING"), "Bad status!"
        print(p)

    def test_belle(self):
        p = Page(belle_delphine, detect_image=":true")
        p.check_all()
        assert p.status().startswith("INTERESTING"), "Bad status!"
        print(p)

    def test_belle_parallel(self):
        from nudecrawler.cache import cache

        cache._url2sum.clear()
        cache._sum2v.clear()
        p = Page(belle_delphine, detect_image=":true", workers=8)
        p.check_all()
        assert p.status().startswith("INTERESTING"), "Bad status!"
        print(p)

    def test_batch_manager(self, tmp_path):
        from nudecrawler.batch import BatchManager
        from nudecrawler.page import Page
        from nudecrawler.cache import cache
        import os

        cache._url2sum.clear()
        cache._sum2v.clear()

        keep_dir = str(tmp_path / "keep")
        
        finalized_pages = []
        def on_finalized(p):
            finalized_pages.append(p)

        bm = BatchManager(
            batch_size=2,
            detect_image_script=":true",
            keep_dir=keep_dir,
            on_page_finalized=on_finalized,
            workers=4
        )

        p = Page(belle_delphine, detect_image=":true", batch_manager=bm, max_pictures=5, min_total_images=0)
        p.check_all()
        bm.flush()

        assert p.pending_images == 0
        assert p in finalized_pages
        assert p.nude_images > 0
        
        files = os.listdir(keep_dir)
        assert len(files) > 0
        for f in files:
            assert f.endswith((".jpg", ".jpeg", ".png"))

    def test_check_word_parallel(self):
        import datetime
        from nudecrawler.scripts.nudecrawler import check_word, stats
        import nudecrawler.scripts.nudecrawler as nc_module

        # Configure settings for check_word
        nc_module.workers = 4
        nc_module.all_found = True
        nc_module.detect_image = ":true"

        word = "https://telegra.ph/empty"
        day = datetime.datetime(2024, 4, 3)
        
        stats["resume"] = {}
        stats["gap_max"] = 0
        stats["gap_url"] = None

        check_word(word, day, fails=2)

        # It should check empty-04-03 (c=1), then empty-04-03-2 (404), empty-04-03-3 (404), etc.
        # With fails=2, it will stop after c=3 (since 2 and 3 are 404s).
        # c will be incremented to 4.
        assert stats["resume"]["count"] >= 3


from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import io
import sqlite3
import tempfile
import unittest

from app import Catalog
from favorite_transfer import export_snapshot, merge_snapshot, parse_snapshot
from favorites import CollectionPaused, Collector, GalleryCounts, fetch_favorites, parse_gallery_counts
from maintenance import LibraryMaintenance


class FavoriteRatioTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "data" / "catalog.sqlite3"
        self.path.parent.mkdir()
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE gid_tid(gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
                INSERT INTO tag VALUES(1,'language:english');
            """)
            for gid in range(1, 9):
                db.execute("INSERT INTO gallery(gid,token,title,title_jpn,category,posted,filecount,rating) VALUES(?,'abcdef0123','Test book','','Non-H',?,10,'4.0')", (gid,gid))
                db.execute("INSERT INTO gid_tid VALUES(?,1)", (gid,))
            db.commit()
        self.catalog = Catalog(self.path)
        self.query = {"tag":["language:english"], "sort":["favorites_per_rating"]}

    def tearDown(self):
        self.directory.cleanup()

    def seed(self):
        for gid, count, ratings in ((1,100,30),(2,30,100),(3,0,50),(4,999,0),(5,20,None),(7,10,20),(8,20,40)):
            self.catalog.favorites.save(gid, count, "e-hentai.org", ratings)
        with self.catalog.favorites.connection() as db:
            db.execute("INSERT INTO rating_counts VALUES(6,10,1,'e-hentai.org')")

    def test_real_division_stable_pagination_and_unknowns_after_zero(self):
        self.seed()
        pages = [self.catalog.search({**self.query,"limit":["2"],"page":[str(page)]}) for page in range(1,5)]
        self.assertEqual([item["gid"] for page in pages for item in page["items"]], [1,8,7,2,3,6,5,4])
        self.assertAlmostEqual(pages[0]["items"][0]["favorite_rating_ratio"], 10/3)
        self.assertEqual(pages[0]["items"][1]["favorite_rating_ratio"], .5)
        self.assertEqual(pages[1]["items"][1]["favorite_rating_ratio"], .3)
        self.assertEqual(pages[2]["items"][0]["favorite_rating_ratio"], 0)
        self.assertIsNone(pages[2]["items"][1]["favorite_rating_ratio"])
        self.assertEqual(pages[0]["favorite_rating_ratio_coverage"], {"known":5,"total":8})
        self.assertEqual(pages[0]["favorite_coverage"]["known"], 7)
        filtered = self.catalog.search({**self.query,"title":["absent"]})
        self.assertEqual(filtered["favorite_rating_ratio_coverage"], {"known":0,"total":0})

    def test_records_keep_missing_metadata_and_failed_unknown_counts(self):
        self.seed()
        self.catalog.favorites.save(99,200,"e-hentai.org",100)
        with self.catalog.favorites.connection() as db:
            self.catalog.favorites._save_failure(db,6,"abcdef0123","e-hentai.org","Mock failure",1,0)
        query = {"sort":["favorites_per_rating"],"limit":["3"]}
        pages = [self.catalog.recorded({**query,"page":[str(page)]}) for page in range(1,4)]
        self.assertEqual([item["gid"] for page in pages for item in page["items"]], [1,99,8,7,2,3,6,5,4])
        self.assertFalse(pages[0]["items"][1]["metadata_available"])
        self.assertEqual(pages[0]["items"][1]["favorite_rating_ratio"],2)
        failed = self.catalog.recorded({**query,"collection":["failed"]})["items"][0]
        self.assertIsNone(failed["favorite_rating_ratio"])
        self.assertEqual(failed["rating_count"],10)

    def test_parser_uses_rating_count_element_and_rejects_ambiguous_values(self):
        for text, expected in (("0",0),("1",1),("1,234",1234),("<b>28</b>",28),("-1",None),("1,23",None),("1.5",None),("",None),("unknown",None),(str(2**63),None),("9"*5000,None)):
            with self.subTest(text=text):
                html = f'<span id="favcount">12</span><span id="rating_count">{text}</span>'
                self.assertEqual(parse_gallery_counts(html,1),GalleryCounts(12,expected))
        self.assertEqual(parse_gallery_counts('<span id="favcount">12</span><span id="rating_label">Average: 4.9</span>'),GalleryCounts(12))
        duplicate = '<span id="favcount">12</span><span id="rating_count">1</span><span id="rating_count">2</span>'
        self.assertIsNone(parse_gallery_counts(duplicate).rating_count)
        with self.assertRaises(CollectionPaused):
            parse_gallery_counts('<script>var gid = 2;</script><span id="favcount">12</span><span id="rating_count">3</span>',1)

    def test_one_request_fetches_both_counts_and_collector_saves_them(self):
        response = io.BytesIO(b'<span id="favcount">10</span><span id="rating_count">4</span>')
        response.headers = {"Content-Type":"text/html"}
        with patch("favorites.build_opener") as opener:
            opener.return_value.open.return_value = response
            counts = fetch_favorites({"host":"e-hentai.org"},1,"abcdef0123")
            self.assertEqual(counts,GalleryCounts(10,4))
            self.assertEqual(opener.return_value.open.call_count,1)
        collector = Collector(self.catalog,lambda settings,gid,token:GalleryCounts(gid*200,gid*20))
        try:
            collector.configure({"host":"e-hentai.org","interval":1})
            verified = collector.verify(self.query)
            self.assertEqual(verified["favorite_count"],verified["rating_count"]*10)
            collector.settings["interval"] = .001
            preview = collector.preview(self.query)
            collector.start(self.query,preview["preview_id"])
            collector.thread.join(timeout=5)
            self.assertFalse(collector.thread.is_alive())
            result = self.catalog.search(self.query)
            self.assertEqual(result["favorite_rating_ratio_coverage"]["known"],8)
            self.assertTrue(all(item["favorite_rating_ratio"]==10 for item in result["items"]))
        finally:
            collector.close()

    def test_missing_field_preserves_last_rating_count_and_share_format(self):
        self.catalog.favorites.save(1,100,"e-hentai.org",50)
        self.catalog.favorites.save(1,120,"e-hentai.org")
        self.assertEqual(self.catalog.search(self.query)["items"][0]["favorite_rating_ratio"],12/5)
        raw, count = export_snapshot(self.catalog.favorites.path)
        snapshot = parse_snapshot(raw)
        self.assertEqual((snapshot["version"],count),(2,1))
        self.assertNotIn("rating_count",snapshot["records"][0])
        snapshot["records"][0].update(favorite_count=200,checked_at=snapshot["records"][0]["checked_at"]+1)
        self.assertEqual(merge_snapshot(self.catalog.favorites.path,snapshot["records"])["updated"],1)
        reopened = Catalog(self.path)
        item = reopened.search(self.query)["items"][0]
        self.assertEqual((item["favorite_count"],item["rating_count"],item["favorite_rating_ratio"]),(200,50,4))
        for value in (-1,True,"5",2**63):
            with self.subTest(value=value),self.assertRaises(ValueError):
                reopened.favorites.save(1,99,"e-hentai.org",value)
        self.assertEqual(reopened.search(self.query)["items"][0]["favorite_count"],200)

    def test_old_records_remain_unknown_and_backup_contains_denominator(self):
        self.catalog.favorites.save(1,10,"e-hentai.org")
        item = self.catalog.search(self.query)["items"][0]
        self.assertIsNone(item["rating_count"])
        self.assertIsNone(item["favorite_rating_ratio"])
        self.assertEqual(item["favorite_rating"]["state"],"unknown")
        self.catalog.favorites.save(1,100,"e-hentai.org",20)
        engine = LibraryMaintenance(self.path,self.root)
        backup = engine.backup(False,"Mock ratio backup")
        with closing(sqlite3.connect(Path(backup["path"])/"data"/"favorites.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT rating_count FROM rating_counts WHERE gid=1").fetchone()[0],20)
        self.catalog.favorites.save(1,99,"e-hentai.org",5)
        ready = engine.prepare_restore(backup["path"])
        engine.restore(ready["id"],reload_catalog=lambda:Catalog(self.path))
        item = Catalog(self.path).search(self.query)["items"][0]
        self.assertEqual((item["favorite_count"],item["rating_count"],item["favorite_rating_ratio"]),(100,20,5))

    def test_original_levels_flooring_and_minimum_sample_on_both_pages(self):
        cases = ((0,20,0.0,"冷门"), (19,20,.9,"冷门"), (20,20,1.0,"一般"),
                 (79,20,3.9,"一般"), (80,20,4.0,"良好"), (139,20,6.9,"良好"),
                 (140,20,7.0,"优秀"), (199,20,9.9,"优秀"), (200,20,10.0,"杰作"),
                 (210,77,2.7,"一般"), (58,20,2.9,"一般"))
        for count,ratings,value,label in cases:
            with self.subTest(count=count,ratings=ratings):
                self.catalog.favorites.save(1,count,"e-hentai.org",ratings)
                for item in (self.catalog.search({"title":["Test book"],"sort":["newest"]})["items"][-1],
                             self.catalog.recorded({"sort":["recorded_desc"]})["items"][0]):
                    self.assertEqual(item["favorite_rating"], {"state":"rated","value":value,"label":label})
        self.catalog.favorites.save(1,99999,"e-hentai.org",19)
        self.catalog.favorites.save(2,0,"e-hentai.org",20)
        for result in (self.catalog.search(self.query),self.catalog.recorded({"sort":["favorites_per_rating"]})):
            self.assertEqual(result["items"][0]["gid"],2)
            item=next(item for item in result["items"] if item["gid"]==1)
            self.assertIsNone(item["favorite_rating_ratio"])
            self.assertEqual(item["favorite_rating"],{"state":"insufficient","value":None,"label":"样本不足"})
        self.assertEqual(self.catalog.search(self.query)["favorite_rating_ratio_coverage"],{"known":1,"total":8})
        self.catalog.favorites.save(1,99999,"e-hentai.org",0)
        self.assertEqual(self.catalog.recorded({})["items"][0]["favorite_rating"]["state"],"unrated")

    def test_sort_uses_untruncated_ratio_and_status_uses_same_rules(self):
        self.catalog.favorites.save(1,209,"e-hentai.org",77)
        self.catalog.favorites.save(2,208,"e-hentai.org",77)
        self.assertEqual([item["gid"] for item in self.catalog.search(self.query)["items"][:2]],[1,2])
        self.assertEqual([item["gid"] for item in self.catalog.recorded({"sort":["favorites_per_rating"]})["items"]],[1,2])
        self.assertEqual([item["favorite_rating"]["value"] for item in self.catalog.recorded({})["items"]],[2.7,2.7])
        rules=self.catalog.status()["favorite_rating_system"]
        self.assertEqual(rules["minimum_ratings"],20)
        self.assertEqual([level["threshold"] for level in rules["levels"]],[10,7,4,1,0])


if __name__ == "__main__":
    unittest.main()

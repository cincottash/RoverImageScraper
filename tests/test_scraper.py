import os

import pytest

from rover_scraper.api import ApiError, MarsVistaClient, Photo, compat_image_url
from rover_scraper.download import save_new_file
from rover_scraper.plan import Action, plan_camera, plan_sol
from rover_scraper.scraper import Scraper


def photo(i, camera="NAVCAM", sol=5, rover="Curiosity"):
    return Photo(id=i, rover=rover, sol=sol, camera=camera, url=f"https://img/{i}.jpg")


def content(p):
    return f"image-{p.id}".encode()


class FakeClient:
    def __init__(self, by_sol):
        self.by_sol = by_sol

    def photos_for_sol(self, rover, sol):
        return list(self.by_sol.get(sol, []))


def fake_fetch(_session, url):
    return f"image-{url.rsplit('/', 1)[1][:-4]}".encode()


def write_files(folder, contents):
    os.makedirs(folder, exist_ok=True)
    for i, data in enumerate(contents):
        with open(os.path.join(folder, str(i)), "wb") as f:
            f.write(data)


# ---- URL compatibility -----------------------------------------------------------------------------------

def test_perseverance_uses_1200_variant():
    assert compat_image_url("Perseverance", "https://x/ABC_800.jpg") == "https://x/ABC_1200.jpg"


def test_curiosity_url_unchanged():
    url = "https://mars.jpl.nasa.gov/msl-raw-images/msss/01000/mcam/1000MR0044631300503690E01_DXXX.jpg"
    assert compat_image_url("Curiosity", url) == url


# ---- planning --------------------------------------------------------------------------------------------

def test_plan_orders_by_id_and_groups_by_camera(tmp_path):
    photos = [photo(30, "MAST"), photo(10, "NAVCAM"), photo(20, "MAST"), photo(5, "NAVCAM")]
    plans = {p.camera: p for p in plan_sol(str(tmp_path), "Curiosity", 5, photos)}
    assert [p.id for p in plans["MAST"].photos] == [20, 30]
    assert [p.id for p in plans["NAVCAM"].photos] == [5, 10]
    assert plans["MAST"].folder == os.path.join(str(tmp_path), "Curiosity", "5", "MAST")
    assert all(p.action is Action.NEW for p in plans.values())


@pytest.mark.parametrize("on_disk, expected", [(3, Action.COMPLETE), (1, Action.TOPUP), (5, Action.SKIP_EXTRA),
                                               (0, Action.TOPUP)])
def test_plan_actions_by_file_count(tmp_path, on_disk, expected):
    folder = tmp_path / "cam"
    write_files(str(folder), [b"x"] * on_disk)
    assert plan_camera(str(folder), "Curiosity", 5, "NAVCAM", [photo(1), photo(2), photo(3)]).action is expected


def test_plan_conflict_on_gap_or_stray_file(tmp_path):
    folder = tmp_path / "cam"
    write_files(str(folder), [b"a", b"b"])
    os.remove(folder / "0")
    assert plan_camera(str(folder), "C", 5, "N", [photo(1), photo(2), photo(3)]).action is Action.CONFLICT
    (folder / "0").write_bytes(b"a")
    (folder / "notes.txt").write_text("hi")
    assert plan_camera(str(folder), "C", 5, "N", [photo(1), photo(2), photo(3)]).action is Action.CONFLICT


# ---- no-overwrite ----------------------------------------------------------------------------------------

def test_save_new_file_never_overwrites(tmp_path):
    path = str(tmp_path / "0")
    save_new_file(path, b"first")
    with pytest.raises(FileExistsError):
        save_new_file(path, b"second")
    assert open(path, "rb").read() == b"first"
    assert not os.path.exists(path + ".part")


# ---- end to end with fakes -------------------------------------------------------------------------------

def test_new_sol_downloads_all_in_id_order(tmp_path):
    client = FakeClient({5: [photo(12), photo(11), photo(40, "MAST")]})
    report = Scraper(client, str(tmp_path), log=lambda m: None, fetch=fake_fetch).run("Curiosity", 5, 6)
    nav = tmp_path / "Curiosity" / "5" / "NAVCAM"
    assert sorted(os.listdir(nav)) == ["0", "1"]
    assert (nav / "0").read_bytes() == b"image-11" and (nav / "1").read_bytes() == b"image-12"
    assert (tmp_path / "Curiosity" / "5" / "MAST" / "0").read_bytes() == b"image-40"
    assert not (tmp_path / "Curiosity" / "6").exists()  # no photos -> no folder (old behaviour)
    assert report.downloaded == 3


def test_topup_appends_when_prefix_matches(tmp_path):
    photos = [photo(1), photo(2), photo(3)]
    folder = tmp_path / "Curiosity" / "5" / "NAVCAM"
    write_files(str(folder), [content(photos[0])])
    report = Scraper(FakeClient({5: photos}), str(tmp_path), log=lambda m: None, fetch=fake_fetch) \
        .run("Curiosity", 5, 5)
    assert [(folder / str(i)).read_bytes() for i in range(3)] == [content(p) for p in photos]
    assert report.downloaded == 2 and not report.problems


def test_topup_refused_when_prefix_differs(tmp_path):
    photos = [photo(1), photo(2), photo(3)]
    folder = tmp_path / "Curiosity" / "5" / "NAVCAM"
    write_files(str(folder), [b"something else"])
    report = Scraper(FakeClient({5: photos}), str(tmp_path), log=lambda m: None, fetch=fake_fetch) \
        .run("Curiosity", 5, 5)
    assert os.listdir(folder) == ["0"] and (folder / "0").read_bytes() == b"something else"
    assert report.downloaded == 0 and "top-up refused" in report.problems[0]


def test_new_camera_added_to_existing_sol(tmp_path):
    existing = tmp_path / "Perseverance" / "5" / "NAVCAM_LEFT"
    write_files(str(existing), [b"keep"])
    photos = [photo(1, "NAVCAM_LEFT", rover="Perseverance"), photo(2, "PIXL_MCC", rover="Perseverance")]
    Scraper(FakeClient({5: photos}), str(tmp_path), log=lambda m: None, fetch=fake_fetch).run("Perseverance", 5, 5)
    assert (existing / "0").read_bytes() == b"keep"   # complete: untouched
    assert (tmp_path / "Perseverance" / "5" / "PIXL_MCC" / "0").read_bytes() == b"image-2"


def test_extra_files_left_alone(tmp_path):
    folder = tmp_path / "Curiosity" / "5" / "MAST"
    write_files(str(folder), [b"a", b"b", b"thumb"])
    report = Scraper(FakeClient({5: [photo(1, "MAST")]}), str(tmp_path), log=lambda m: None, fetch=fake_fetch) \
        .run("Curiosity", 5, 5)
    assert sorted(os.listdir(folder)) == ["0", "1", "2"] and report.actions["extra"] == 1


def test_dry_run_writes_nothing(tmp_path):
    report = Scraper(FakeClient({5: [photo(1)]}), str(tmp_path), dry_run=True, log=lambda m: None,
                     fetch=fake_fetch).run("Curiosity", 5, 5)
    assert os.listdir(tmp_path) == [] and report.would_download == 1


# ---- API client paging -----------------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.url, self.text, self.headers = payload, status, "u", "", {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, pages):
        self.pages, self.headers = pages, {}

    def get(self, url, params=None, timeout=None):
        return FakeResponse(self.pages[params["page"] - 1])


def api_photo(i, cam="NAVCAM_LEFT"):
    return {"id": i, "sol": 5, "camera": {"name": cam}, "img_src": f"https://x/{i}_800.jpg"}


def test_client_reads_all_pages_sorts_and_fixes_urls():
    pages = [{"photos": [api_photo(3), api_photo(1)], "pagination": {"total_count": 3, "total_pages": 2}},
             {"photos": [api_photo(2)], "pagination": {"total_count": 3, "total_pages": 2}}]
    photos = MarsVistaClient("k", session=FakeSession(pages)).photos_for_sol("Perseverance", 5)
    assert [p.id for p in photos] == [1, 2, 3]
    assert photos[0].url == "https://x/1_1200.jpg"


def test_client_rejects_short_listing():
    pages = [{"photos": [api_photo(1)], "pagination": {"total_count": 2, "total_pages": 1}}]
    with pytest.raises(ApiError):
        MarsVistaClient("k", session=FakeSession(pages), sleep=lambda s: None).photos_for_sol("Perseverance", 5)

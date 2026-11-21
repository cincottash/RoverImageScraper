import os

import pytest

from rover_scraper.api import ApiError, MarsVistaClient, Photo, SolSummary, compat_image_url
from rover_scraper.download import save_new_file
from rover_scraper.plan import Action, plan_sol, sol_looks_complete
from rover_scraper.scraper import Scraper


def photo(i, camera="NAVCAM", sol=5, rover="Curiosity"):
    return Photo(id=i, rover=rover, sol=sol, camera=camera, url=f"https://img/{i}.jpg")


def content(i):
    return f"image-{i}".encode()


class FakeClient:
    def __init__(self, by_sol):
        self.by_sol = by_sol
        self.listed = []

    def manifest(self, rover):
        out = {}
        for sol, photos in self.by_sol.items():
            out[sol] = SolSummary(len(photos), tuple(sorted({p.camera for p in photos})))
        return out

    def photos_for_sol(self, rover, sol):
        self.listed.append(sol)
        return list(self.by_sol.get(sol, []))


FAIL_IDS = set()


def fake_fetch(_session, url):
    i = int(url.rsplit("/", 1)[1][:-4])
    if i in FAIL_IDS:
        raise RuntimeError("HTTP 403")
    return content(i)


def write_files(folder, contents, start=0):
    os.makedirs(folder, exist_ok=True)
    for i, data in enumerate(contents, start):
        with open(os.path.join(folder, str(i)), "wb") as f:
            f.write(data)


def run(tmp_path, by_sol, rover="Curiosity", **kw):
    client = FakeClient(by_sol)
    report = Scraper(client, str(tmp_path), log=lambda m: None, fetch=fake_fetch, **kw) \
        .run(rover, min(by_sol), max(by_sol))
    return client, report


def files(folder):
    return {n: open(os.path.join(folder, n), "rb").read() for n in os.listdir(folder)}


# ---- URL compatibility -----------------------------------------------------------------------------------

def test_perseverance_uses_1200_variant():
    assert compat_image_url("Perseverance", "https://x/ABC_800.jpg") == "https://x/ABC_1200.jpg"


def test_curiosity_url_unchanged():
    url = "https://mars.jpl.nasa.gov/msl-raw-images/msss/01000/mcam/1000MR0044631300503690E01_DXXX.jpg"
    assert compat_image_url("Curiosity", url) == url


# ---- planning --------------------------------------------------------------------------------------------

def test_plan_groups_by_camera_in_id_order(tmp_path):
    plans = {p.camera: p for p in plan_sol(str(tmp_path), "Curiosity", 5,
                                           [photo(30, "MAST"), photo(10), photo(20, "MAST"), photo(5)])}
    assert [p.id for p in plans["MAST"].photos] == [20, 30]
    assert [p.id for p in plans["NAVCAM"].photos] == [5, 10]
    assert plans["MAST"].folder == os.path.join(str(tmp_path), "Curiosity", "5", "MAST")
    assert all(p.action is Action.NEW for p in plans.values())


@pytest.mark.parametrize("on_disk, expected", [(3, Action.COMPLETE), (5, Action.COMPLETE), (1, Action.FILL),
                                               (0, Action.FILL)])
def test_plan_actions_by_count(tmp_path, on_disk, expected):
    write_files(str(tmp_path / "Curiosity" / "5" / "NAVCAM"), [b"x"] * on_disk)
    [plan] = plan_sol(str(tmp_path), "Curiosity", 5, [photo(1), photo(2), photo(3)])
    assert plan.action is expected


def test_quick_check(tmp_path):
    sol = tmp_path / "Curiosity" / "5"
    assert not sol_looks_complete(str(tmp_path), "Curiosity", 5, 3, ["NAVCAM"])          # no folder
    write_files(str(sol / "NAVCAM"), [b"a", b"b"])
    assert not sol_looks_complete(str(tmp_path), "Curiosity", 5, 3, ["NAVCAM"])          # partial sol
    write_files(str(sol / "NAVCAM"), [b"c"], start=2)
    assert sol_looks_complete(str(tmp_path), "Curiosity", 5, 3, ["NAVCAM"])
    assert not sol_looks_complete(str(tmp_path), "Curiosity", 5, 3, ["NAVCAM", "MAST"])  # camera missing


# ---- no-overwrite ----------------------------------------------------------------------------------------

def test_save_new_file_never_overwrites(tmp_path):
    path = str(tmp_path / "0")
    save_new_file(path, b"first")
    with pytest.raises(FileExistsError):
        save_new_file(path, b"second")
    assert open(path, "rb").read() == b"first"
    assert not os.path.exists(path + ".part")


# ---- end to end with fakes -------------------------------------------------------------------------------

def test_new_sol_downloads_all(tmp_path):
    _, report = run(tmp_path, {5: [photo(12), photo(11), photo(40, "MAST")]})
    assert files(tmp_path / "Curiosity" / "5" / "NAVCAM") == {"0": content(11), "1": content(12)}
    assert files(tmp_path / "Curiosity" / "5" / "MAST") == {"0": content(40)}
    assert report.downloaded == 3


def test_complete_sol_skipped_without_listing(tmp_path):
    write_files(str(tmp_path / "Curiosity" / "5" / "NAVCAM"), [b"a", b"b", b"thumb"])
    client, report = run(tmp_path, {5: [photo(1), photo(2)]})
    assert client.listed == [] and report.sols_skipped_quick == 1 and report.downloaded == 0


def test_fill_adds_only_photos_not_already_present_any_order(tmp_path):
    folder = tmp_path / "Perseverance" / "9" / "NAVCAM"
    write_files(str(folder), [content(3), content(1)])           # old order differs: that's fine
    _, report = run(tmp_path, {9: [photo(i, sol=9, rover="Perseverance") for i in (1, 2, 3, 4)]},
                    rover="Perseverance")
    assert files(folder) == {"0": content(3), "1": content(1), "2": content(2), "3": content(4)}
    assert report.downloaded == 2 and report.already_present == 2


def test_fill_keeps_listed_duplicates(tmp_path):
    folder = tmp_path / "Curiosity" / "5" / "NAVCAM"
    write_files(str(folder), [content(1)])
    dup = Photo(id=2, rover="Curiosity", sol=5, camera="NAVCAM", url="https://img/1.jpg")  # same image again
    _, report = run(tmp_path, {5: [photo(1), dup]})
    assert files(folder) == {"0": content(1), "1": content(1)}


def test_fill_numbers_after_highest_existing_file(tmp_path):
    folder = tmp_path / "Curiosity" / "5" / "NAVCAM"
    write_files(str(folder), [content(1)], start=4)              # a gap: only file "4" exists
    run(tmp_path, {5: [photo(1), photo(2)]})
    assert files(folder) == {"4": content(1), "5": content(2)}


def test_new_camera_added_to_existing_sol(tmp_path):
    existing = tmp_path / "Perseverance" / "5" / "NAVCAM_LEFT"
    write_files(str(existing), [b"keep"])
    run(tmp_path, {5: [photo(1, "NAVCAM_LEFT", rover="Perseverance"), photo(2, "PIXL_MCC", rover="Perseverance")]},
        rover="Perseverance")
    assert files(existing) == {"0": b"keep"}
    assert files(tmp_path / "Perseverance" / "5" / "PIXL_MCC") == {"0": content(2)}


def test_failed_photo_skipped_rest_saved(tmp_path):
    FAIL_IDS.add(2)
    try:
        _, report = run(tmp_path, {5: [photo(1), photo(2), photo(3)]})
    finally:
        FAIL_IDS.clear()
    assert files(tmp_path / "Curiosity" / "5" / "NAVCAM") == {"0": content(1), "1": content(3)}
    assert len(report.problems) == 1
    # next run: count 2 < 3, so the sol is re-checked and the missing photo added
    _, report = run(tmp_path, {5: [photo(1), photo(2), photo(3)]})
    assert files(tmp_path / "Curiosity" / "5" / "NAVCAM")["2"] == content(2) and report.already_present == 2


def test_dry_run_writes_nothing(tmp_path):
    _, report = run(tmp_path, {5: [photo(1)]}, dry_run=True)
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

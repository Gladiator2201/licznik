#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Licznik zdjęć rodzinnych.
Przechodzi od strony głównej "Wszystko co nasze" przez strony roczne Google Sites,
zbiera linki do albumów Zdjęć Google, odczytuje liczbę zdjęć/filmów w każdym
albumie i zapisuje licznik.json (oraz opcjonalnie albumy.csv).

Wymaga tylko: Python 3.8+ i biblioteki requests.
Użycie:  python policz.py [--cache KATALOG] [--csv albumy.csv] [--out licznik.json]
"""
import argparse, csv, datetime, hashlib, json, os, re, sys, time, threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

START = "https://sites.google.com/view/wszystoconasze/strona-g%C5%82%C3%B3wna"
# Strony, które liczymy zawsze, nawet jeśli zniknęłyby ze strony głównej
EXTRA_SITES = [
    "https://sites.google.com/view/fotki2005/albumy",
    "https://sites.google.com/site/fotrodzinne2019/albumy",
    "https://sites.google.com/site/bagnscywszyscyrazem/",
]
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
WORKERS = 5

_tl = threading.local()
CACHE = None


def sess():
    s = getattr(_tl, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "pl,en;q=0.8"})
        _tl.s = s
    return s


def fetch(url, tries=4):
    """Zwraca (final_url, status, text). Z cache, jeśli włączony."""
    key = hashlib.sha1(url.encode()).hexdigest()
    if CACHE:
        p = os.path.join(CACHE, key + ".json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return d["final"], d["status"], d["text"]
    last = None
    for i in range(tries):
        try:
            r = sess().get(url, timeout=40, allow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                last = "HTTP %d" % r.status_code
                time.sleep(3 * (i + 1) ** 2)
                continue
            r.encoding = "utf-8"
            res = (r.url, r.status_code, r.text)
            if CACHE:
                with open(os.path.join(CACHE, key + ".json"), "w", encoding="utf-8") as f:
                    json.dump({"url": url, "final": res[0], "status": res[1], "text": res[2]}, f)
            return res
        except requests.RequestException as e:
            last = str(e)
            time.sleep(3 * (i + 1))
    return url, 0, "BLAD: %s" % last


def hrefs(html):
    out = []
    for m in re.findall(r'href="([^"]+)"', html):
        m = m.replace("&amp;", "&")
        if "google.com/url?" in m:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(m).query).get("q")
            if q:
                m = q[0]
        out.append(m)
    return out


PHOTO_RE = re.compile(r"^https?://(photos\.app\.goo\.gl|goo\.gl/photos|photos\.google\.com|"
                      r"get\.google\.com/albumarchive|picasaweb\.google\.com|"
                      r"lh\d\.googleusercontent\.com|plus\.google\.com/photos)", re.I)


def is_photo_link(u):
    return bool(PHOTO_RE.match(u)) and "googleusercontent.com" not in u


def site_root(u):
    m = re.match(r"https://sites\.google\.com/(site|view)/([^/?#]+)", u)
    return ("https://sites.google.com/%s/%s" % m.groups(), m.group(2)) if m else (None, None)


def year_of(name):
    m = re.search(r"(20\d\d)", name)
    return m.group(1) if m else name


def discover_sites():
    _, st, html = fetch(START)
    roots = {}
    for u in hrefs(html) + EXTRA_SITES:
        root, name = site_root(u)
        if not root or name == "wszystoconasze":
            continue
        roots[root] = name
    return roots


def subpages_of(root, name):
    """Strona /albumy + wszystkie jej podstrony (oraz strona główna witryny)."""
    path = urllib.parse.urlparse(root).path
    pages = [root + "/", root + "/albumy"]
    found = []
    for p in list(pages):
        _, st, html = fetch(p)
        if st != 200 or "accounts.google.com" in _:
            continue
        for h in hrefs(html):
            h2 = urllib.parse.unquote(h.split("#")[0].split("?")[0])
            if h2.startswith(path + "/"):
                full = "https://sites.google.com" + h2
                if full not in found:
                    found.append(full)
    # tylko strony z albumami (podstrony /albumy/...) – plus same strony startowe
    alb = [f for f in found if "/albumy/" in f]
    return pages, alb


def _is_video(it):
    ext = it[-1] if it and isinstance(it[-1], dict) else {}
    return "76647426" in ext


def parse_album(html):
    """Zwraca dict z danymi albumu udostępnionego albo None."""
    def ds(key):
        i = html.find("AF_initDataCallback({key: '%s'" % key)
        if i < 0:
            return None
        j = html.find("data:", i) + 5
        k = html.find(", sideChannel:", j)
        try:
            return json.loads(html[j:k])
        except Exception:
            return None
    d = ds("ds:1")
    if not d or not isinstance(d, list) or len(d) < 4 or not isinstance(d[3], list):
        return None
    meta, items, token = d[3], d[1] or [], d[2]
    count = None
    if len(meta) > 21 and isinstance(meta[21], int):
        count = meta[21]          # liczba elementów pokazywana przez Zdjęcia Google
    return {"id": meta[0], "title": meta[1], "count": count, "token": token or None,
            "listed": len(items), "videos": sum(map(_is_video, items))}


BATCH = "https://photos.google.com/_/PhotosUi/data/batchexecute"


def next_page(album_id, token, key):
    """Kolejna strona listy elementów albumu (RPC snAcKc). Zwraca (items, token)."""
    ck = "POST|%s|%s" % (album_id, token)
    h = hashlib.sha1(ck.encode()).hexdigest()
    if CACHE and os.path.exists(os.path.join(CACHE, h + ".json")):
        with open(os.path.join(CACHE, h + ".json"), encoding="utf-8") as f:
            inner = json.load(f)
        return inner[1] or [], inner[2]
    req = [[["snAcKc", json.dumps([album_id, token, None, key]), None, "generic"]]]
    for i in range(4):
        try:
            r = sess().post(BATCH, params={"rpcids": "snAcKc", "rt": "c"},
                            data={"f.req": json.dumps(req)}, timeout=60)
            if r.status_code == 200:
                for line in r.text.split("\n"):
                    line = line.strip()
                    if line.startswith('[["wrb.fr"'):
                        inner = json.loads(json.loads(line)[0][2])
                        if CACHE:
                            with open(os.path.join(CACHE, h + ".json"), "w", encoding="utf-8") as f:
                                json.dump(inner, f)
                        return inner[1] or [], inner[2]
        except (requests.RequestException, ValueError, IndexError, TypeError):
            pass
        time.sleep(3 * (i + 1) ** 2)
    raise RuntimeError("nie udało się pobrać kolejnej strony albumu")


def check_link(link):
    final, st, html = fetch(link)
    res = {"link": link, "final": final, "status": st}
    if st != 200:
        res["error"] = "HTTP %s" % st
        return res
    if "accounts.google.com" in final:
        res["error"] = "album prywatny (wymaga logowania)"
        return res
    a = parse_album(html)
    if not a or a["count"] is None:
        res["error"] = "nie udało się odczytać albumu"
        return res
    # dalsze strony (albumy > 300 elementów) – dla zliczenia filmów i weryfikacji
    m = re.search(r"[?&]key=([^&#]+)", final)
    tok = a.pop("token")
    try:
        guard = 0
        while tok and m and guard < 200:
            items, tok = next_page(a["id"], tok, m.group(1))
            a["listed"] += len(items)
            a["videos"] += sum(map(_is_video, items))
            guard += 1
        a["list_complete"] = not tok
    except RuntimeError:
        a["list_complete"] = False
    res.update(a)
    return res


def main():
    global CACHE
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache")
    ap.add_argument("--csv")
    ap.add_argument("--out", default="licznik.json")
    ap.add_argument("--details", help="pełny raport JSON (opcjonalnie)")
    a = ap.parse_args()
    if a.cache:
        os.makedirs(a.cache, exist_ok=True)
        CACHE = a.cache
    t0 = time.time()
    roots = discover_sites()
    unavailable = []
    print("Witryny:", len(roots), file=sys.stderr)
    pex = ThreadPoolExecutor(WORKERS)
    # 1) podstrony
    sub = {}
    for root, name, (starts, alb) in zip(roots, roots.values(),
                                         pex.map(lambda kv: subpages_of(*kv), roots.items())):
        sub[root] = (name, starts, alb)
        if not alb:
            unavailable.append(root)
        print("  %-60s %4d podstron" % (root, len(alb)), file=sys.stderr)
    # 2) linki do albumów z każdej podstrony
    pages = []
    for root, (name, starts, alb) in sub.items():
        for p in starts + alb:
            pages.append((root, name, p))
    def links_on(p):
        _, st, html = fetch(p[2])
        if st != 200:
            return p, None
        return p, sorted(set(h for h in hrefs(html) if is_photo_link(h)))
    refs = []      # (rok, witryna, podstrona, link)
    missing_pages = []
    for (root, name, p), lk in pex.map(links_on, pages):
        if lk is None:
            if "/albumy/" in p:
                missing_pages.append(p)
            continue
        for l in lk:
            refs.append((year_of(name), name, p, l))
    print("Linków do albumów:", len(refs), file=sys.stderr)
    # 3) albumy
    uniq = sorted(set(r[3] for r in refs))
    info = dict(zip(uniq, pex.map(check_link, uniq)))
    # 4) zliczanie (deduplikacja po ID albumu)
    seen = {}
    per_year = {}
    dead = []
    rows = []
    for rok, name, p, l in refs:
        r = info[l]
        cnt = r.get("count")
        rows.append([rok, p, l, cnt if cnt is not None else "", r.get("videos", ""), r.get("title", ""), r.get("id", ""), r.get("error", "")])
        if cnt is None:
            dead.append({"rok": rok, "podstrona": p, "link": l, "blad": r.get("error")})
            continue
        if r["id"] in seen:
            continue
        seen[r["id"]] = r
        y = per_year.setdefault(rok, {"albums": 0, "items": 0, "videos": 0, "listed": 0})
        y["albums"] += 1
        y["items"] += cnt
        y["videos"] += r.get("videos", 0)
        y["listed"] += r.get("listed", 0)
    total = sum(v["items"] for v in per_year.values())
    listed = sum(v["listed"] for v in per_year.values())
    for v in per_year.values():
        v["photos"] = v["items"] - v["videos"]
        del v["listed"]
    videos = sum(v["videos"] for v in per_year.values())
    out = {
        "total": total,
        "photos": total - videos,
        "videos": videos,
        "albums": len(seen),
        "listed_items": listed,
        "sites_unavailable": unavailable,
        "pages": sum(len(v[2]) for v in sub.values()),
        "dead_links": len(dead),
        "per_year": dict(sorted(per_year.items())),
        "updated": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
    }
    # Bezpiecznik: jeśli coś poszło nie tak (np. Google zablokował zapytania),
    # nie nadpisujemy dobrego licznika gorszym wynikiem.
    try:
        with open(a.out, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        prev = None
    if out["albums"] == 0 or (prev and prev.get("total") and out["total"] < 0.8 * prev["total"]):
        print("UWAGA: wynik podejrzany (%d zamiast %s) - licznik.json bez zmian"
              % (out["total"], prev and prev.get("total")), file=sys.stderr)
        sys.exit(2)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["rok", "podstrona", "link", "liczba", "w_tym_filmy", "tytul_albumu", "id_albumu", "blad"])
            w.writerows(rows)
    if a.details:
        with open(a.details, "w", encoding="utf-8") as f:
            json.dump({"albums": info, "dead": dead, "missing_pages": missing_pages,
                       "sites": {k: v[2] for k, v in sub.items()}}, f, ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("Czas: %.0f s" % (time.time() - t0), file=sys.stderr)


if __name__ == "__main__":
    main()

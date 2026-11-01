#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酷狗 gcid 歌单下载器（解决「只下到 3 首」问题）

背景
----
酷狗有两种歌单，下载难度天差地别：

| 歌单 ID 形态          | 例子                        | 上游能给出几首 |
|-----------------------|-----------------------------|----------------|
| `special/single/{数字}` | 8691023                    | **全量**（实测 106/106） |
| `gcid_xxx`            | gcid_3z1457hyaz3z0c5       | **只有 10 首预览** |

`gcid_` 是「用户自建歌单」。上游 `music-lib` 解析它时请求
`https://www.kugou.com/songlist/{gcid}/`，只从页面内嵌 JSON 里取
`info.songs` —— 而酷狗该页面**只内嵌前 10 首**（实测分页参数无效，
`page`/`pagesize` 一律返回同样 10 首）。真实总数在 `info.listinfo.count`。

于是网页端点「下载整个歌单」时只提交 3 个任务（实测 `added: 3`），
这就是「搜得到却只下到 3 首」的根因。

本脚本做什么
------------
1. 解析 gcid 歌单，拿到官方曲目总数（如 292 首）与内嵌的前 10 首；
2. 对这 10 首逐首换源（migu 等）拿直链，修正被污染的歌手字段；
3. **批量下载**：把这 10 首交给下载队列；
4. 若你填了酷狗 Cookie，则改走云歌单接口
   `gateway.kugou.com/v4/get_list_all_file`（pagesize=300）拿**全量**曲目。

用法
----
    # 需要先启动本地服务
    cd dist\\cli && ./music-dl.exe web --port 8080
    # 另开终端
    python 下载gcid歌单.py gcid_3z1457hyaz3z0c5
    python 下载gcid歌单.py "https://www.kugou.com/songlist/gcid_3z1457hyaz3z0c5/"

填 Cookie 拿全量（可选）
------------------------
在 `dist/cli/data/cookies.json` 里写入：

    {"kugou": "userid=2056178400; token=<你的token>; KUGOU_API_MID=<mid>"}

Cookie 从酷狗 App 登录后抓取（App 端才有 token / KUGOU_API_MID）。
没有 Cookie 时脚本退化为「下载可见的 10 首」。
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("MUSIC_DL_BASE", "http://127.0.0.1:8080/music")
TARGETS = ["migu", "qq", "netease", "kuwo", "joox"]
REQUEST_INTERVAL = 1.3
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# 云歌单接口（拿全量用）
KG_SIGN = "LnT6xpN3khm36zse0QzvmgTZ3waWdRSA"
KG_APPID = "3116"
KG_VER = "11440"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def http_get(url, headers=None, timeout=60, retries=3):
    """带重试的 GET。"""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=dict(UA, **(headers or {})))
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + attempt * 2)
    raise last


def api_get(path, params=None, timeout=60):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return json.loads(http_get(url, timeout=timeout))


def api_post(path, payload, timeout=90):
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE.rsplit("/", 1)[0],
    }
    req = urllib.request.Request(BASE + path, data=body, headers=dict(UA, **headers))
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def parse_playlist_id(raw):
    """从 URL 或纯 ID 里提取 gcid / 数字精选集 ID。"""
    raw = (raw or "").strip()
    m = re.search(r"gcid_[a-zA-Z0-9]+", raw)
    if m:
        return m.group(0)
    m = re.search(r"single/(\d+)", raw)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{5,})\b", raw)
    if m:
        return m.group(1)
    raise SystemExit("无法识别歌单 ID: %r" % raw)


# ---------------------------------------------------------------------------
# 歌名一致性（防止下错歌）
# ---------------------------------------------------------------------------

VERSION_WORDS = re.compile(
    r"\b(live|remix|version|edit|mix|instrumental|cover|demo|mono|stereo|"
    r"伴奏|伴奏版|现场版|录音室版|官方版|试听版)\b", re.I)


def normalize_title(text):
    """剥离括号内容与版本后缀，得到可比较的核心歌名。"""
    text = html.unescape(text or "")
    text = re.sub(r"[（(\[【].*?[)）\]】]", " ", text)
    text = VERSION_WORDS.sub(" ", text)
    text = re.sub(r"[\s\-_·・,，.。!！?？:：'\"“”/、~～]+", "", text)
    return text.lower()


def normalize_artist(text):
    """归一化歌手串：酷狗用「、」，migu 用「/」，不归一化会误判成不同歌手。"""
    text = html.unescape(text or "")
    text = re.sub(r"[\s\-_·・,，.。!！?？:：'\"“”~～]+", "", text)
    text = text.replace("/", "、")
    return text.strip("、").lower()


def same_song(orig, got):
    a, b = normalize_title(orig), normalize_title(got)
    return bool(a) and a == b


# ---------------------------------------------------------------------------
# 歌单解析
# ---------------------------------------------------------------------------

def extract_json_object(html_text):
    """从 `window.$output = {...};` 中用括号配平抠出 JSON。

    上游用的正则是 `({.*?})\\s*;\\s*</script>`，但真实页面里 `}</script>`
    前面还有内容，正则会匹配失败（报 "kugou songlist payload not found"）。
    这里改成括号配平，稳。
    """
    m = re.search(r"window\.\$output\s*=\s*", html_text)
    if not m:
        return None
    start = m.end()
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(html_text)):
        ch = html_text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html_text[start:i + 1])
                except ValueError:
                    return None
    return None


def fetch_songlist(gcid):
    """抓 gcid 歌单页面，返回 (元数据, 内嵌歌曲列表)。"""
    url = "https://m.kugou.com/songlist/%s/" % gcid
    text = http_get(url, headers={
        "Referer": "https://m.kugou.com/",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 Mobile/15E148",
    }, timeout=60).decode("utf-8", "replace")
    payload = extract_json_object(text)
    if not payload:
        raise SystemExit("songlist 页面结构异常，未能解析出 $output")
    info = payload.get("info") or {}
    listinfo = info.get("listinfo") or {}
    songs = []
    for item in info.get("songs") or []:
        names = [x.get("name") for x in (item.get("singerinfo") or []) if x.get("name")]
        artist = "、".join(names)
        title = item.get("name") or ""
        # 酷狗 songlist 的 name 字段是「歌手 - 歌名」拼接形式（如「喻言 - 蠢货」），
        # 真正的歌名要拆出来，否则换源时会拿整串去匹配必然失败。
        if not artist and " - " in title:
            head, tail = title.split(" - ", 1)
            artist, title = head.strip(), tail.strip()
        elif artist and title.startswith(artist + " - "):
            title = title[len(artist) + 3:].strip()
        songs.append({
            "name": title,
            "artist": artist,
            "duration": int(item.get("timelen") or 0) // 1000,
            "hash": item.get("hash") or "",
        })
    meta = {
        "name": listinfo.get("name") or "",
        "creator": listinfo.get("list_create_username") or "",
        "uid": listinfo.get("list_create_userid") or "",
        "count": int(listinfo.get("count") or 0),
        "gcid": payload.get("encode_gic") or gcid,
    }
    return meta, songs


def fetch_playlist_via_api(playlist_id, source="kugou"):
    """走本地服务的 /playlist 解析（数字精选集走这条能拿全量）。"""
    songs = api_get("/playlist", {"id": playlist_id, "source": source, "page_size": 500},
                    timeout=90)
    return songs


# ---------------------------------------------------------------------------
# 云歌单接口（填了 Cookie 才能用，可拿全量）
# ---------------------------------------------------------------------------

def kg_sign(params, data):
    import hashlib
    pairs = sorted("%s=%s" % (k, v) for k, v in params.items())
    return hashlib.md5((KG_SIGN + "".join(pairs) + data + KG_SIGN).encode()).hexdigest()


def load_kugou_cookie():
    """从 data/cookies.json 读酷狗 Cookie。"""
    for path in ("dist/cli/data/cookies.json", "data/cookies.json"):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        raw = data.get("kugou") or ""
        if isinstance(raw, dict):
            raw = "; ".join("%s=%s" % (k, v) for k, v in raw.items())
        if raw.strip():
            return raw
    return ""


def fetch_cloudlist_all(gcid):
    """用云歌单接口拿全量曲目。需要有效 token（App 登录态）。

    返回 list[dict]，失败返回 []。
    """
    cookie = load_kugou_cookie()
    if not cookie:
        return []
    fields = {}
    for pair in cookie.split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            fields[k.strip()] = v.strip()
    uid = fields.get("userid") or fields.get("KugooID") or ""
    token = fields.get("token") or ""
    mid = fields.get("KUGOU_API_MID") or ""
    if not (uid and token and mid):
        print("  [提示] Cookie 缺少 userid / token / KUGOU_API_MID，无法走全量接口")
        return []

    body = {
        "listid": gcid, "userid": uid, "area_code": 1, "show_relate_goods": 1,
        "pagesize": 300, "allplatform": 1, "show_cover": 1, "type": 0,
        "token": token, "page": 1,
    }
    data = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    params = {
        "dfid": fields.get("dfid", "-"), "mid": mid, "uuid": "-",
        "appid": KG_APPID, "clientver": KG_VER,
        "clienttime": str(int(time.time())), "token": token, "userid": uid,
    }
    query = urllib.parse.urlencode(params) + "&signature=" + kg_sign(params, data)
    url = "https://gateway.kugou.com/v4/get_list_all_file?" + query
    headers = {
        "Content-Type": "application/json",
        "x-router": "cloudlist.service.kugou.com",
        "dfid": params["dfid"], "mid": mid,
        "kg-rc": "1", "kg-thash": "5d816a0", "kg-rec": "1",
        "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",
        "User-Agent": "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi",
        "Cookie": cookie,
    }
    req = urllib.request.Request(url, data=data.encode("utf-8"), headers=headers)
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        print("  [提示] 云歌单接口调用失败: %s" % str(e)[:80])
        return []
    err = resp.get("error_code") or resp.get("errcode")
    if err:
        print("  [提示] 云歌单接口返回错误码 %s（token 可能无效或过期）" % err)
        return []
    info = (resp.get("data") or {}).get("info") or []
    songs = []
    for item in info:
        names = [x.get("name") for x in (item.get("singerinfo") or []) if x.get("name")]
        artist = "、".join(names)
        title = item.get("name") or ""
        # 同样是「歌手 - 歌名」拼接形式，需要拆出真歌名
        if not artist and " - " in title:
            head, tail = title.split(" - ", 1)
            artist, title = head.strip(), tail.strip()
        elif artist and title.startswith(artist + " - "):
            title = title[len(artist) + 3:].strip()
        songs.append({
            "name": title,
            "artist": artist,
            "duration": int(item.get("timelen") or 0) // 1000,
            "hash": item.get("hash") or "",
        })
    return songs


# ---------------------------------------------------------------------------
# 换源
# ---------------------------------------------------------------------------

def switch_source(song, targets=TARGETS):
    """主通道：换源接口。返回可用直链或 None。"""
    dur = str(song.get("duration") or "")
    for target in targets:
        try:
            data = api_get("/switch_source", {
                "name": song["name"], "artist": song["artist"],
                "source": "kugou", "target": target, "duration": dur,
            }, timeout=45)
        except Exception:
            continue
        if not data.get("id"):
            continue
        try:
            score = float(data.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if not same_song(song["name"], data.get("name") or ""):
            print("       [忽略] %s -> %s (score %.2f，歌名不符)"
                  % (song["name"][:20], str(data.get("name"))[:20], score))
            continue
        extra = data.get("extra")
        if not isinstance(extra, str):
            extra = json.dumps(extra, ensure_ascii=False) if extra else ""
        return {
            "id": data["id"], "source": data.get("source") or target,
            "name": data.get("name") or song["name"],
            # 关键：migu 匿名态的 artist 常是「🐷🐷」这类占位符，必须用原歌单歌手覆盖
            "artist": song.get("artist") or data.get("artist") or "",
            "album": data.get("album") or song.get("album") or "",
            "cover": data.get("cover") or song.get("cover") or "",
            "extra": extra,
            "score": score,
        }
    return None


def search_fallback(song, targets=TARGETS):
    """兜底通道：走 /search 按「歌名 + 歌手」精确筛选。

    switch_source 内部按「歌名+时长」模糊打分，源里有同名歌但打分把别的版本
    排在前面时，会返回 score=0.3 的错配；而 /search 返回完整候选列表，
    按双重硬判据筛能稳定命中。
    """
    want_artist = normalize_artist(song.get("artist") or "")
    try:
        want_dur = int(song.get("duration") or 0)
    except (TypeError, ValueError):
        want_dur = 0

    for target in targets:
        try:
            html_text = http_get(BASE + "/search?" + urllib.parse.urlencode({
                "q": song["name"], "sources": target, "type": "song",
            }), timeout=60).decode("utf-8", "replace")
        except Exception:
            continue
        cands = []
        for blk in re.findall(r'<li class="song-card"(.*?)>', html_text, re.S):
            g = lambda k: (re.findall(k + r'="([^"]*)"', blk) or [""])[0]
            name = html.unescape(g("data-name"))
            artist = html.unescape(g("data-artist"))
            if not same_song(song["name"], name):
                continue
            if want_artist and normalize_artist(artist) != want_artist:
                continue
            try:
                dur = int(g("data-duration") or 0)
            except (TypeError, ValueError):
                dur = 0
            diff = abs(dur - want_dur) if (dur and want_dur) else 0
            extra = g("data-extra")
            cands.append((diff, {
                "id": g("data-id"), "source": target,
                "name": name, "artist": song.get("artist") or artist,
                "album": html.unescape(g("data-album")),
                "cover": g("data-cover"),
                "extra": extra if isinstance(extra, str) else json.dumps(extra, ensure_ascii=False),
                "duration": dur,
            }))
        if cands:
            cands.sort(key=lambda x: x[0])
            diff, best = cands[0]
            print("       [直搜命中] %s -> %s (%s, 时长差 %ds)"
                  % (song["name"][:20], best["name"][:20], target, diff))
            return best
    return None


def resolve(song):
    """双通道换源：主通道 + 兜底通道（兜底重试几轮）。

    上游搜索的结果排序会波动 —— 同一首歌有时排第 1，有时排到第 5 之后，
    所以兜底通道必须重试，否则会漏掉本可命中的曲目。
    """
    alt = switch_source(song)
    if alt:
        return alt
    for attempt in range(3):
        alt = search_fallback(song)
        if alt:
            alt["score"] = 1.0
            return alt
        if attempt < 2:
            print("       [重试] %s 直搜未命中，再试一次" % song["name"][:20])
            time.sleep(3)
    return None


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

def submit_and_wait(songs, request_id):
    """提交队列并等它跑完。"""
    resp = api_post("/api/downloads/queue", {"request_id": request_id, "songs": songs})
    print("  提交队列: 新增 %s，已存在 %s" % (resp.get("added"), resp.get("existing")))
    total = resp.get("added", 0) + resp.get("existing", 0)
    if not total:
        return resp

    print("  轮询下载状态...")
    idle = 0
    while True:
        time.sleep(5)
        try:
            queue = api_get("/api/downloads/queue", {"page": 1, "status": "active"}, timeout=30)
        except Exception:
            continue
        active = len(queue.get("tasks") or [])
        counts = queue.get("counts") or {}
        print("    活跃 %d | 统计 %s" % (active, counts))
        if active == 0:
            idle += 1
            if idle >= 2:
                break
        else:
            idle = 0
    return resp


def main():
    if len(sys.argv) < 2:
        raise SystemExit(
            "用法: python 下载gcid歌单.py <歌单ID或链接>\n"
            "示例: python 下载gcid歌单.py gcid_3z1457hyaz3z0c5")

    playlist_id = parse_playlist_id(sys.argv[1])
    print("歌单 ID: %s" % playlist_id)
    print("服务地址: %s" % BASE)

    songs = []
    meta = {}
    if playlist_id.startswith("gcid_"):
        # gcid 用户自建歌单
        meta, visible = fetch_songlist(playlist_id)
        print("歌单名: %s（创建者 %s）" % (meta["name"], meta["creator"]))
        print("官方曲目总数: %d" % meta["count"])
        print("页面内嵌可见: %d 首" % len(visible))

        # 有 Cookie 时走云歌单接口拿全量
        full = fetch_cloudlist_all(playlist_id)
        if full:
            print("云歌单接口取到全量: %d 首" % len(full))
            songs = full
        else:
            print("  未配置酷狗 Cookie（或 token 无效），本次只能下页面可见的 %d 首" % len(visible))
            songs = visible
    else:
        # 数字精选集：本地服务能直接解析全量
        page = http_get(BASE + "/playlist?" + urllib.parse.urlencode({
            "id": playlist_id, "source": "kugou", "page_size": 500,
        }), timeout=90).decode("utf-8", "replace")
        for blk in re.findall(r'<li class="song-card"(.*?)>', page, re.S):
            g = lambda k: (re.findall(k + r'="([^"]*)"', blk) or [""])[0]
            raw = (re.findall(r"data-extra='([^']*)'", blk) or [""])[0]
            try:
                extra = json.loads(html.unescape(raw))
            except Exception:
                extra = {}
            songs.append({
                "name": html.unescape(g("data-name")),
                "artist": html.unescape(g("data-artist")),
                "duration": g("data-duration"),
                "hash": g("data-id"),
                "extra": json.dumps(extra, ensure_ascii=False),
                "source": "kugou",
            })
        print("解析到 %d 首" % len(songs))

    if not songs:
        raise SystemExit("没有可下载的曲目。")

    print("\n共 %d 首，开始换源...\n" % len(songs))
    ready = []
    failed = []
    for i, song in enumerate(songs, 1):
        alt = resolve(song)
        if alt:
            ready.append(alt)
            print("  [%3d/%d] %-24s -> %-6s (score %.2f)"
                  % (i, len(songs), song["name"][:24], alt["source"], alt["score"]))
        else:
            failed.append(song["name"])
            print("  [%3d/%d] %-24s -> 换源失败"
                  % (i, len(songs), song["name"][:24]))
        time.sleep(REQUEST_INTERVAL)

    print("\n换源成功 %d 首，失败 %d 首" % (len(ready), len(failed)))
    if failed:
        print("失败曲目: %s" % "、".join(failed[:10]))
    if not ready:
        raise SystemExit("全部换源失败。")

    print("\n提交下载队列...")
    submit_and_wait(ready, "gcid-%s-%d" % (playlist_id[:16], int(time.time())))
    print("\n完成。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键下载整张歌单（go-music-dl Web 端自动化）

用法:
    python 下载歌单.py <歌单链接或歌单ID> [输出目录]
    python 下载歌单.py 8691023
    python 下载歌单.py "https://www.kugou.com/yy/special/single/8691023.html"

流程: 解析歌单 -> 逐首换源(migu/qq/netease) -> 批量入队 -> 轮询结果
依赖: requests(标准库 urllib 亦可), 需先启动 music-dl.exe web
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

BASE = os.environ.get("MUSIC_DL_BASE", "http://127.0.0.1:8080")
PREFIX = "/music"
# 实测：migu 源对酷狗歌单覆盖率最高；netease/qq/kuwo/joox 的 switch_source
# 对本类歌单基本返回 404，放在后面仅作兜底。
TARGETS = ["migu", "qq", "netease", "kuwo", "joox"]
# score 仅作质量参考（会打印出来），真正的过滤判据是歌名一致性 same_song()。
# 实测：源缺歌时 score 恒为 0.3（错配），正确匹配通常 >= 0.9。
WARN_SCORE = 0.6
# 请求间隔（秒）。上游平台对高频请求会限流，表现为 HTTP 200 但歌曲列表为空。
REQUEST_INTERVAL = 1.2
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def get(path, params=None, timeout=60, retries=3):
    """带重试的 GET。上游音乐平台接口偶发 502/超时，重试即可恢复。"""
    url = BASE + PREFIX + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 403, 404):   # 业务性错误重试无意义
                raise
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def post_json(path, payload, timeout=60):
    url = BASE + PREFIX + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE,
        **UA,
    })
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def parse_playlist_id(raw):
    m = re.search(r"(?:single|playlist)/(\d+)", raw) or re.search(r"\b(\d{5,})\b", raw)
    if not m:
        sys.exit("无法识别歌单 ID: %s" % raw)
    return m.group(1)


# 版本后缀：换源后这些词会被保留或改写，不影响「同一首歌」的判定
VERSION_WORDS = re.compile(
    r"\b(live|remix|version|edit|mix|伴奏|instrumental|cover|demo|mono|stereo|"
    r"伴奏版|现场版|录音室版|官方版|高音质|试听版)\b", re.I)


def normalize_title(s):
    """去掉括号内容、版本后缀、标点，得到可比较的核心歌名。"""
    s = html.unescape(s or "")
    s = re.sub(r"[（(\[【].*?[)）\]】]", " ", s)     # 去掉括号及其内容
    s = VERSION_WORDS.sub(" ", s)
    s = re.sub(r"[\s\-_·・,，.。!！?？:：'\"“”/、~～]+", "", s)
    return s.lower()


def normalize_artist(s):
    """归一化歌手串，让不同来源的分隔符差异不影响匹配。

    酷狗歌单用「、」分隔（如「周杰伦、张惠妹」），migu 搜索结果用「/」
    （如「周杰伦 / 张惠妹」），不归一化会把同一组歌手误判成不同歌手。
    """
    s = html.unescape(s or "")
    s = re.sub(r"[\s\-_·・,，.。!！?？:：'\"“”~～]+", "", s)
    s = s.replace("/", "、").replace("、", "、")
    return s.strip("、").lower()


def same_song(orig, got):
    """判断换源返回的是不是同一首歌。

    以「核心歌名」比较：去掉 Live/Remix/伴奏 等版本后缀与括号说明后，
    两个歌名应完全一致。不一致说明换到了别的歌（如「七里香」→「乱舞春秋」），
    必须拒绝，否则用户下载到的会是错歌。
    """
    a, b = normalize_title(orig), normalize_title(got)
    if not a or not b:
        return False
    return a == b


def fetch_songs(pl_id, source="kugou"):
    """解析歌单，返回歌曲元数据列表。

    坑点记录（均为实测）：
    1. /playlist 路由虽然不直接读 page 参数，但 renderIndex 会统一做分页：
       page_size 默认取设置里的 WebPageSize（=50），超出会被截断到 500。
       所以取全量必须显式传 page_size=500，否则只拿到前 50 首。
       （server.go:412-421 读 page_size，server.go:431-451 做切片）
    2. 上游限流时不报错，而是返回 HTTP 200 + 空歌曲列表（页面回退成搜索视图）。
       必须用「解析到 0 首」判断限流并退避重试，不能只看 HTTP 状态码。
    """
    url_path = "/playlist"
    params = {"id": pl_id, "source": source, "page_size": "500"}

    html_text = None
    for attempt in range(5):
        try:
            html_text = get(url_path, params, timeout=90, retries=1)
        except Exception as e:
            print("  拉取失败(%s)，重试 %d/5" % (str(e)[:50], attempt + 1))
            time.sleep(5 * (attempt + 1))
            continue

        blocks = re.findall(r'<li class="song-card"(.*?)>', html_text, re.S)
        if blocks:
            break

        # 200 但没有歌曲卡片 —— 上游限流/降级，退避后重试
        print("  第 %d 次返回空列表（疑似上游限流），退避 10s 重试 %d/5" % (attempt + 1, attempt + 2))
        html_text = None
        time.sleep(10 * (attempt + 1))

    if html_text is None:
        sys.exit("歌单解析失败：上游平台限流或接口不可用，请稍后重试。")

    songs, seen = [], set()
    for blk in re.findall(r'<li class="song-card"(.*?)>', html_text, re.S):
        g = lambda k: (re.findall(k + r'="([^"]*)"', blk) or [""])[0]
        sid = g("data-id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        songs.append({
            "id": sid,
            "source": g("data-source") or source,
            "name": html.unescape(g("data-name")),
            "artist": html.unescape(g("data-artist")),
            "album": html.unescape(g("data-album")),
            "cover": g("data-cover"),
            "duration": g("data-duration"),
            "extra": (re.findall(r"data-extra='([^']*)'", blk) or [""])[0],
        })

    total_m = re.search(r'找到\s*<span class="count">(\d+)</span>', html_text)
    total = int(total_m.group(1)) if total_m else len(songs)
    print("  解析到 %d 首（页面声明 %d 首）" % (len(songs), total))

    # page_size 上限 500，超过则翻页补齐
    if total > len(songs) and len(songs) >= 500:
        page = 2
        while len(songs) < total and page <= 20:
            print("  翻页补齐: page=%d" % page)
            try:
                h2 = get(url_path, dict(params, page=str(page)), timeout=90, retries=2)
            except Exception as e:
                print("    第 %d 页失败: %s" % (page, str(e)[:50]))
                break
            added = 0
            for blk in re.findall(r'<li class="song-card"(.*?)>', h2, re.S):
                g = lambda k: (re.findall(k + r'="([^"]*)"', blk) or [""])[0]
                sid = g("data-id")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                added += 1
                songs.append({
                    "id": sid, "source": g("data-source") or source,
                    "name": html.unescape(g("data-name")),
                    "artist": html.unescape(g("data-artist")),
                    "album": html.unescape(g("data-album")),
                    "cover": g("data-cover"),
                    "duration": g("data-duration"),
                    "extra": (re.findall(r"data-extra='([^']*)'", blk) or [""])[0],
                })
            if added == 0:
                break
            page += 1
            time.sleep(REQUEST_INTERVAL)
        print("  补齐后共 %d 首" % len(songs))
    elif total and len(songs) < total:
        print("  提示：解析数(%d) 少于声明数(%d)，可能有同名曲目被折叠" % (len(songs), total))
    return songs


def switch_source(song, targets=TARGETS):
    """为单首找一个可下载的替代源。返回新元数据或 None。

    关键点（均为实测结论）：
    1. song['duration'] 直接传【秒】，不要换算成毫秒 —— 传毫秒接口会直接 404。
    2. score 不是可靠判据：源本身缺歌时，无论传什么参数都只会返回 0.3 的
       错误匹配（如「稻香」→「我的地盘」、「七里香」→「乱舞春秋」）。
       真正的判据是【返回的歌名与原歌名是否一致】。
    """
    dur = song.get("duration") or ""

    for t in targets:
        try:
            q = {"name": song["name"], "artist": song["artist"],
                 "source": song["source"], "target": t, "duration": dur}
            d = json.loads(get("/switch_source", q, timeout=45))
        except Exception:
            # 404 = 该目标源不支持此歌曲，直接试下一个
            continue
        if not d.get("id"):
            continue
        score = d.get("score")
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        # 歌名一致性是硬判据：换源后歌名被改写说明匹配到了别的歌。
        # 允许的差异仅限 Live/Remix/伴奏 等版本后缀与括号内的说明文字。
        if not same_song(song["name"], d.get("name") or ""):
            print("       [忽略] %s -> %s (score %.2f，歌名不符)"
                  % (song["name"][:20], str(d.get("name"))[:20], score))
            continue
        extra = d.get("extra")
        if not isinstance(extra, str):
            extra = json.dumps(extra, ensure_ascii=False) if extra else ""
        if score < WARN_SCORE:
            print("       [提示] %s 匹配分偏低 %.2f，已核对歌名一致"
                  % (song["name"][:20], score))
        return {
            "id": d["id"], "source": d["source"],
            "name": d.get("name") or song["name"],
            "artist": d.get("artist") or song["artist"],
            "album": d.get("album") or song["album"],
            "cover": d.get("cover") or song["cover"],
            "extra": extra,
            "score": score,
        }
    return None


def search_fallback(song, targets=TARGETS):
    """switch_source 全失败后的兜底：走搜索接口按【歌名+歌手】精确匹配。

    为什么需要这条通道（实测结论）：
    switch_source 内部按「歌名+时长」做模糊打分，源存在同名歌但打分算法
    把别的版本排在前面时，会返回 score=0.3 的错配（如「七里香」→「我的地盘」、
    「Mojito」→「东风破」）。而 /search 接口返回的是该源的完整候选列表，
    在里面按歌名一致 + 歌手匹配筛选，能稳定拿到正确曲目。

    实测：「七里香」「稻香」「Mojito」三首在 switch_source 全错配，
    经本通道全部命中（时长差 0~1 秒）。

    关于时长：时长只用来在多个同名候选里挑最接近的那一个，不作为硬性排除条件。
    因为 Live 版与专辑版是不同录音，时长本就会有差（实测「不该 (Live)」301 秒 vs
    「不该 (电视剧《幻城》主题曲)」290 秒，差 11 秒仍是同一首歌）。
    防错配由「歌名一致 + 歌手一致」双重硬判据保证，不依赖时长。
    """
    want = song.get("name") or ""
    want_artist = (song.get("artist") or "").strip()
    try:
        want_dur = int(song.get("duration") or 0)
    except (TypeError, ValueError):
        want_dur = 0

    for t in targets:
        try:
            html_text = get("/search", {"q": want, "sources": t, "type": "song"}, timeout=60)
        except Exception:
            continue

        cands = []
        for blk in re.findall(r'<li class="song-card"(.*?)>', html_text, re.S):
            g = lambda k: (re.findall(k + r'="([^"]*)"', blk) or [""])[0]
            name = html.unescape(g("data-name"))
            artist = html.unescape(g("data-artist"))
            if not same_song(want, name):
                continue
            dur = g("data-duration")
            try:
                dur = int(dur or 0)
            except (TypeError, ValueError):
                dur = 0
            # 歌手必须匹配（归一化分隔符差异），避免拿到同名别人的歌
            if want_artist:
                if normalize_artist(want_artist) != normalize_artist(artist):
                    continue
            diff = abs(dur - want_dur) if (dur and want_dur) else 0
            cands.append((diff, {
                "id": g("data-id"), "source": t,
                "name": name, "artist": artist,
                "album": html.unescape(g("data-album")),
                "cover": g("data-cover"),
                "extra": g("data-extra"),
                "duration": dur,
            }))

        if cands:
            cands.sort(key=lambda x: x[0])
            diff, best = cands[0]
            print("       [直搜命中] %s -> %s (%s, 时长差 %ds)"
                  % (want[:20], best["name"][:20], t, diff))
            return best
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    raw = sys.argv[1]
    pl_id = parse_playlist_id(raw)
    source = "kugou" if "kugou" in raw.lower() else "kugou"

    print("歌单 ID: %s" % pl_id)
    songs = fetch_songs(pl_id, source)
    if not songs:
        sys.exit("歌单解析为空，检查服务是否启动: %s%s/healthz" % (BASE, PREFIX))
    print("共 %d 首，开始换源...\n" % len(songs))

    ready, failed, fallback = [], [], []
    for i, s in enumerate(songs, 1):
        alt = switch_source(s)
        if alt:
            ready.append(alt)
            print("  [%3d/%d] %-24s -> %-6s (score %.2f)"
                  % (i, len(songs), s["name"][:24], alt["source"], alt["score"]))
        else:
            # 换源通道失败：再走直搜兜底通道（按歌名+歌手+时长精确匹配）
            alt = search_fallback(s)
            if alt:
                extra = alt.get("extra") or ""
                if not isinstance(extra, str):
                    extra = json.dumps(extra, ensure_ascii=False)
                alt["extra"] = extra
                alt["score"] = 1.0
                ready.append(alt)
                print("  [%3d/%d] %-24s -> %-6s (直搜兜底命中)"
                      % (i, len(songs), s["name"][:24], alt["source"]))
                time.sleep(REQUEST_INTERVAL)
                continue
            # 两条通道都找不到同一首歌。回退到原始源提交，
            # 若已在设置里填了酷狗 Cookie，这条仍能下到原音质。
            failed.append(s["name"])
            fallback.append({
                "id": s["id"], "source": s["source"], "name": s["name"],
                "artist": s["artist"], "album": s["album"],
                "cover": s["cover"], "extra": s["extra"],
            })
            print("  [%3d/%d] %-24s -> 换源无匹配，回退原源（需 Cookie）"
                  % (i, len(songs), s["name"][:24]))
        # 节流：连续高频请求会触发上游限流
        time.sleep(REQUEST_INTERVAL)

    if not ready and not fallback:
        sys.exit("全部换源失败。若已填 Cookie 可改用原 kugou 源直接下载。")

    req_id = "pl-%s-%d" % (pl_id, int(time.time()))
    batch = ready + fallback
    print("\n提交下载队列: %d 首（换源 %d + 原源回退 %d）..."
          % (len(batch), len(ready), len(fallback)))
    resp = json.loads(post_json("/api/downloads/queue", {"request_id": req_id, "songs": batch}))
    added = resp.get("added", 0)
    existing = resp.get("existing", 0)
    print("  新增任务 %d，已存在 %d" % (added, existing))

    # 注意：队列的 counts 是【全局累计】(所有历史批次混在一起)，不能用来判断本批次进度。
    # 正确判据：status=active 即 waiting+downloading 的总数，排空即本批次全部结束。
    print("轮询下载状态...")
    total = added + existing
    idle = 0
    while True:
        time.sleep(10)
        try:
            d = json.loads(get("/api/downloads/queue", {"page": 1, "status": "active"}, timeout=30))
            active = len(d.get("tasks", []))
            total_active = d.get("total", active)
            counts = d.get("counts", {})
            print("  活跃任务 %s | 全局统计 %s" % (total_active, counts))
            if int(total_active) == 0:
                idle += 1
                if idle >= 2:      # 连续两次为空，确认已排空
                    break
            else:
                idle = 0
        except Exception as e:
            print("  状态查询失败: %s" % e)
            continue

    # 汇总本批次结果
    try:
        d = json.loads(get("/api/downloads/queue", {"page": 1, "status": ""}, timeout=30))
        counts = d.get("counts", {})
        print("\n全局最终统计: %s" % counts)
    except Exception:
        pass
    print("本批次提交 %d 首（新增 %d）。" % (total, added))
    if failed:
        print("换源无匹配 %d 首（已回退原源提交，需配 Cookie 才能下到）: %s"
              % (len(failed), "、".join(failed[:10])))
    print("文件目录: %s\\dist\\cli\\data\\downloads\\" % os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    main()

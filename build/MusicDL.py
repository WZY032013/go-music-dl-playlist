#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MusicDL 音乐下载器 —— 自包含单文件 EXE

设计目标：把「go-music-dl 服务端」+「歌单批量下载逻辑」打包成一个 exe，
发给任何人双击即可用，无需安装 Go / Python / Docker。

运行模式:
    1) 双击运行（无参数）:
       启动本地 Web 服务（免鉴权、仅监听 127.0.0.1），自动打开浏览器。
       用户可在网页里搜索、播放、下载单曲/歌单。
    2) 命令行带歌单参数:
       MusicDL.exe <歌单链接或ID> [输出目录]
       自动解析歌单 -> 逐首换源 -> 批量下载到指定目录。

关键实现要点（均经源码与实测验证）:
    - music-dl.exe 的数据目录跟随【当前工作目录】，而非 exe 所在目录。
      所以必须 chdir 到用户可写目录再启动，否则 PyInstaller 临时目录
      在进程退出后被清理，下载的音乐会全部丢失。
    - `--desktop` 模式走 StartDesktop() -> DisableAuth=true，仅监听 127.0.0.1。
      免去首次运行的「初始化管理员账号 / setup token」流程，分享给他人最省事。
    - MUSIC_DL_CONFIG 环境变量可指定配置文件（settings.db）路径，
      用于把配置固定到用户目录，避免受 cwd 影响。
    - Web 端 download_dir 支持绝对路径（core/config_store.go:481 normalizeWebDownloadDir）。
"""

import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

APP_NAME = "MusicDL"
APP_VERSION = "1.0.0"

# 换源目标源优先级：实测 migu 对酷狗歌单覆盖率最高。
# 其余源（qq/netease/kuwo/joox）对本类歌单基本返回 404，且 joox 单次请求
# 会阻塞 6~7 秒（实测），放在前面会让整单换源多耗数分钟，因此只作兜底。
TARGETS = ["migu", "qq", "netease", "kuwo", "joox"]
# score 仅作质量参考（会打印出来），真正的过滤判据是歌名一致性 same_song()。
WARN_SCORE = 0.6
# 请求间隔（秒）。上游平台对高频请求会限流，表现为 HTTP 200 但歌曲列表为空。
REQUEST_INTERVAL = 1.2
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# --------------------------------------------------------------------------
# 路径与运行环境
# --------------------------------------------------------------------------

def is_frozen():
    return getattr(sys, "frozen", False)


def app_dir():
    """exe 真实所在目录（用户放这个 exe 的地方）。

    注意：PyInstaller --onefile 运行时，sys.executable 指向 %TEMP% 下的
    临时释放目录（_MEIPASS 的兄弟），不是用户看到的那个 exe。
    因此这里改用 sys.argv[0] 定位真实路径 —— 它在打包后仍是用户启动的
    那个 exe 的路径。
    """
    if is_frozen():
        # sys.argv[0] 在 onefile 模式下指向原始 exe（未被解压覆盖）
        argv0 = sys.argv[0] if sys.argv else ""
        if argv0:
            p = os.path.abspath(argv0)
            if os.path.isfile(p):
                return os.path.dirname(p)
            # argv0 可能是相对路径或已在 PATH 中，尝试解析
            try:
                from shutil import which
                found = which(argv0)
                if found:
                    return os.path.dirname(os.path.abspath(found))
            except Exception:
                pass
        # 兜底：sys.executable 的上级（临时目录的上级通常不是真实位置，
        # 但 _MEIPASS 的父目录在部分打包模式下可用）
        exe = os.path.abspath(sys.executable)
        parent = os.path.dirname(os.path.dirname(exe))
        if os.path.isfile(exe):
            return parent
        return os.path.dirname(exe)
    return os.path.dirname(os.path.abspath(__file__))


def user_data_dir():
    """用户数据目录：优先 exe 同级，其次 %LOCALAPPDATA%。

    下载的音乐最终落在这里的 downloads 子目录。
    """
    base = app_dir()
    try:
        # exe 同级可写就同级，方便用户直接找到文件
        probe = os.path.join(base, ".writetest")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return base
    except Exception:
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(local, APP_NAME)


def resource_path(*parts):
    """读取 PyInstaller 打包内的资源。"""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = app_dir()
    return os.path.join(base, *parts)


def find_music_dl():
    """定位 music-dl.exe。

    打包模式：资源在 _MEIPASS 下，需复制到用户数据目录再运行
    （music-dl.exe 运行时会创建 data/ 目录，而 _MEIPASS 是只读临时目录）。
    非打包模式：直接在项目目录里找。
    """
    candidates = []
    if is_frozen():
        candidates.append(resource_path("music-dl.exe"))
    else:
        candidates += [
            os.path.join(app_dir(), "music-dl.exe"),
            os.path.join(app_dir(), "dist", "cli", "music-dl.exe"),
        ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def prepare_music_dl(work_dir):
    """把 music-dl.exe 准备到 work_dir，返回其绝对路径。"""
    src = find_music_dl()
    if not src:
        return None
    dst = os.path.join(work_dir, "music-dl.exe")
    if os.path.isfile(src) and os.path.abspath(src) != os.path.abspath(dst):
        try:
            shutil.copy2(src, dst)
            return dst
        except Exception as e:
            print("复制 music-dl.exe 失败: %s" % e)
            return src
    return src


def pick_port(start=18200, tries=200):
    """找一个空闲端口。"""
    for p in range(start, start + tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            try:
                s.close()
            except Exception:
                pass
    return None


# --------------------------------------------------------------------------
# 服务进程管理
# --------------------------------------------------------------------------

class Service(object):
    """管理 music-dl.exe web 子进程。"""

    def __init__(self, work_dir, port, download_dir):
        self.work_dir = work_dir
        self.port = port
        self.download_dir = download_dir
        self.proc = None
        self.base = "http://127.0.0.1:%d" % port
        self.log_path = os.path.join(work_dir, "music-dl.log")
        self.pid_path = os.path.join(work_dir, "music-dl.pid")

    def kill_stale(self):
        """清理上一次运行遗留的 music-dl 进程。

        因为 music-dl 以独立进程组启动（用户直接关窗口也不会被杀），
        退出时可能留下「孤儿进程」占用端口。这里在启动前按 PID 文件
        把上次的实例干掉，保证同一时刻只有一个实例。
        """
        try:
            if not os.path.isfile(self.pid_path):
                return
            with open(self.pid_path, "r") as f:
                pid = int((f.read() or "0").strip())
        except Exception:
            return
        if pid <= 0:
            return
        try:
            os.kill(pid, 0)      # 探测存活，不实际发信号
        except OSError:
            self._clear_pid()
            return
        print("清理上次遗留的 music-dl 进程 (PID %d)" % pid)
        for killer in ("taskkill",):
            try:
                subprocess.run([killer, "/F", "/PID", str(pid)],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=20)
                break
            except Exception:
                continue
        time.sleep(1)
        self._clear_pid()

    def _clear_pid(self):
        try:
            if os.path.isfile(self.pid_path):
                os.remove(self.pid_path)
        except Exception:
            pass

    def _write_pid(self):
        try:
            with open(self.pid_path, "w") as f:
                f.write(str(self.proc.pid))
        except Exception:
            pass

    def start(self):
        exe = prepare_music_dl(self.work_dir)
        if not exe:
            raise RuntimeError(
                "未找到 music-dl.exe。\n"
                "请把 music-dl.exe 与 %s.exe 放在同一目录。" % APP_NAME)

        # 先清理上次遗留的实例，避免端口/进程冲突
        self.kill_stale()

        env = dict(os.environ)
        # 配置 DB 固定到用户目录，避免受 cwd / 临时目录影响
        env["MUSIC_DL_CONFIG"] = os.path.join(self.work_dir, "settings.db")
        env["MUSIC_DL_COOKIE_FILE"] = os.path.join(self.work_dir, "cookies.json")

        # 关键：music-dl.exe 的数据目录跟随 cwd，必须切到用户可写目录
        cwd = self.work_dir
        # 注意：不能用 with open(...) as f —— with 块退出时会关闭句柄，
        # subprocess 拿到已关闭的句柄会抛 "I/O operation on closed file"。
        try:
            self.log = open(os.path.join(cwd, "music-dl.log"), "ab")
        except Exception:
            self.log = subprocess.DEVNULL

        # 创建独立进程组：用户直接关掉控制台窗口（硬终止）时，
        # music-dl 子进程不会跟着被杀，下载队列能继续跑完。
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        self.proc = subprocess.Popen(
            [exe, "web", "--desktop", "--port", str(self.port), "--no-browser"],
            cwd=cwd, env=env, stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        self._write_pid()
        # 注意：atexit 只在正常退出时执行，用户双击关闭窗口（terminate）不走这里。
        return self.wait_ready()

    def wait_ready(self, timeout=45):
        """等待服务就绪。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(
                    "music-dl 进程已退出（退出码 %s）。日志：%s"
                    % (self.proc.returncode, self.log_path))
            try:
                req = urllib.request.Request(self.base + "/music/healthz", headers=UA)
                urllib.request.urlopen(req, timeout=5).read()
                return True
            except Exception:
                time.sleep(1)
        raise RuntimeError("服务启动超时，日志：%s" % self.log_path)

    def stop(self, wait_for_queue=False):
        """停止 music-dl 子进程。

        wait_for_queue=True 时会先等下载队列排空再停服务，
        避免用户关闭窗口时把正在下载的文件截断（半截的 mp3/flac 无法播放）。
        """
        if wait_for_queue and self.proc and self.proc.poll() is None:
            print("\n等待下载队列完成（最长 5 分钟）...")
            deadline = time.time() + 300
            while time.time() < deadline:
                try:
                    d = json.loads(http_get(self.base, "/api/downloads/queue",
                                            {"page": 1, "status": "active"},
                                            timeout=15, retries=1))
                    active = d.get("total", len(d.get("tasks", [])))
                    if int(active) == 0:
                        print("下载队列已排空。")
                        break
                    print("  剩余 %s 个任务..." % active)
                except Exception:
                    pass
                time.sleep(5)

        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        # 关闭日志句柄，避免文件句柄泄漏
        try:
            if self.log and self.log is not subprocess.DEVNULL:
                self.log.close()
        except Exception:
            pass
        # 只有进程确实结束时才清 PID 文件；
        # 若进程被孤儿化仍在运行，保留 PID 供下次启动时清理。
        if self.proc is None or self.proc.poll() is not None:
            self._clear_pid()

    def apply_download_dir(self, download_dir):
        """把 Web 端的下载目录设为绝对路径。

        注意：POST /music/settings 是【全量覆盖】语义
        （server.go:633 直接 ShouldBindJSON 到整个 core.WebSettings），
        只提交 {"downloadDir": ...} 会把 embedDownload 等字段重置为零值。
        因此必须先 GET 现有配置，改掉 downloadDir，再整体提交。
        """
        try:
            req = urllib.request.Request(
                self.base + "/music/settings",
                headers={"X-Requested-With": "XMLHttpRequest", **UA})
            cur = json.loads(urllib.request.urlopen(req, timeout=15).read())
            if not isinstance(cur, dict):
                cur = {}
        except Exception:
            cur = {}
        cur["downloadDir"] = download_dir

        try:
            body = json.dumps(cur).encode("utf-8")
            req = urllib.request.Request(
                self.base + "/music/settings", data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "X-Requested-With": "XMLHttpRequest",
                         "Origin": self.base, **UA})
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except Exception as e:
            print("设置下载目录失败（将使用默认 data/downloads）: %s" % str(e)[:80])
            return False


# --------------------------------------------------------------------------
# HTTP 辅助
# --------------------------------------------------------------------------

def http_get(base, path, params=None, timeout=60, retries=3):
    """带重试的 GET。上游音乐平台接口偶发 502/超时，重试即可恢复。"""
    url = base + "/music" + path
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


def http_post_json(base, path, payload, timeout=60):
    url = base + "/music" + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": base,
        **UA,
    })
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


# --------------------------------------------------------------------------
# 歌单解析与换源（逻辑来自已验证的 下载歌单.py）
# --------------------------------------------------------------------------

def parse_playlist_id(raw):
    """从 URL 或纯 ID 里提取歌单标识。

    支持两种形态：
    - 数字精选集：`8691023` 或 https://www.kugou.com/yy/special/single/8691023.html
    - gcid 用户自建歌单：`gcid_3z1457hyaz3z0c5` 或 .../songlist/gcid_xxx/
    """
    raw = (raw or "").strip()
    m = re.search(r"gcid_[a-zA-Z0-9]+", raw)
    if m:
        return m.group(0)
    m = re.search(r"(?:single|playlist)/(\d+)", raw) or re.search(r"\b(\d{5,})\b", raw)
    if not m:
        sys.exit("无法识别歌单 ID: %s" % raw)
    return m.group(1)


VERSION_WORDS = re.compile(
    r"\b(live|remix|version|edit|mix|伴奏|instrumental|cover|demo|mono|stereo|"
    r"伴奏版|现场版|录音室版|官方版|高音质|试听版)\b", re.I)


def normalize_title(s):
    """去掉括号内容、版本后缀、标点，得到可比较的核心歌名。"""
    import html as _html
    s = _html.unescape(s or "")
    s = re.sub(r"[（(\[【].*?[)）\]】]", " ", s)
    s = VERSION_WORDS.sub(" ", s)
    s = re.sub(r"[\s\-_·・,，.。!！?？:：'\"“”/、~～]+", "", s)
    return s.lower()


def normalize_artist(s):
    """归一化歌手串，让不同来源的分隔符差异不影响匹配。

    酷狗歌单用「、」分隔（如「周杰伦、张惠妹」），migu 搜索结果用「/」
    （如「周杰伦 / 张惠妹」），不归一化会把同一组歌手误判成不同歌手。
    """
    import html as _html
    s = _html.unescape(s or "")
    s = re.sub(r"[\s\-_·・,，.。!！?？:：'\"“”~～]+", "", s)
    s = s.replace("/", "、")
    return s.strip("、").lower()


def same_song(orig, got):
    """判断换源返回的是不是同一首歌（防止下错歌）。"""
    a, b = normalize_title(orig), normalize_title(got)
    if not a or not b:
        return False
    return a == b


def extract_kugou_payload(html_text):
    """从 `window.$output = {...};` 里用括号配平抠出 JSON。

    上游用的是 `({.*?})\\s*;\\s*</script>` 这种非贪婪正则，但真实页面里
    `}</script>` 前面还有别的内容，正则会匹配失败（报 "songlist payload not found"）。
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


def fetch_songlist_direct(gcid):
    """直采酷狗 gcid 用户歌单页面，返回歌曲列表。

    为什么要直采：服务端 /playlist 对 gcid 歌单只能解析出 3 首。
    根因是上游 music-lib 请求 https://www.kugou.com/songlist/{gcid}/ 后，
    只从页面内嵌 JSON 的 info.songs 里取曲目 —— 而酷狗该页面只内嵌前 10 首
    （实测 page/pagesize 参数一律无效）。真实总数在 info.listinfo.count。

    酷狗该页面的 song.name 是「歌手 - 歌名」拼接形式（如「喻言 - 蠢货」），
    真正的歌名必须拆出来，否则拿去换源会整串匹配必然失败。
    """
    import html as _html
    url = "https://m.kugou.com/songlist/%s/" % gcid
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 Mobile/15E148",
            "Referer": "https://m.kugou.com/",
        })
        text = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    except Exception as e:
        print("  [直采失败] %s" % str(e)[:60])
        return []

    payload = extract_kugou_payload(text)
    if not payload:
        print("  [直采失败] 页面结构异常，未能解析 $output")
        return []

    info = payload.get("info") or {}
    listinfo = info.get("listinfo") or {}
    total = int(listinfo.get("count") or 0)
    songs = []
    for item in info.get("songs") or []:
        names = [x.get("name") for x in (item.get("singerinfo") or []) if x.get("name")]
        artist = "、".join(names)
        title = item.get("name") or ""
        if not artist and " - " in title:
            head, tail = title.split(" - ", 1)
            artist, title = head.strip(), tail.strip()
        elif artist and title.startswith(artist + " - "):
            title = title[len(artist) + 3:].strip()
        songs.append({
            "id": item.get("hash") or "",
            "source": "kugou",
            "name": title,
            "artist": artist,
            "album": item.get("remark") or "",
            "cover": (item.get("cover") or "").replace("{size}", "240"),
            "duration": str(int(item.get("timelen") or 0) // 1000),
            "extra": json.dumps({
                "album_id": str(item.get("album_id") or ""),
                "hash": item.get("hash") or "",
                "file_hash": item.get("hash") or "",
            }, ensure_ascii=False),
        })

    print("  直采到 %d 首（官方声明 %d 首，页面仅内嵌 %d 首）"
          % (len(songs), total, len(songs)))
    if total > len(songs):
        print("  [提示] 酷狗对用户自建歌单只公开前 %d 首，其余 %d 首需填 Cookie 才能取"
              % (len(songs), total - len(songs)))
    return songs


def fetch_songs(base, pl_id, source="kugou"):
    """解析歌单，返回歌曲元数据列表。

    坑点记录（均为实测）：
    1. renderIndex 会统一做分页，page_size 默认取设置里的 WebPageSize(=50)，
       所以取全量必须显式传 page_size=500，否则只拿到前 50 首。
    2. 上游限流时不报错，而是返回 HTTP 200 + 空歌曲列表（页面回退成搜索视图）。
       必须用「解析到 0 首」判断限流并退避重试，不能只看 HTTP 状态码。
    3. gcid_ 开头的用户自建歌单，服务端 /playlist 只能解析出 3 首
       （上游只从 songlist 页面内嵌 JSON 取 info.songs，而酷狗该页只内嵌 10 首），
       所以这里改为直采酷狗页面，把可见曲目拿全。
    """
    if pl_id.startswith("gcid_"):
        direct = fetch_songlist_direct(pl_id)
        if direct:
            return direct

    params = {"id": pl_id, "source": source, "page_size": "500"}
    html_text = None
    for attempt in range(5):
        try:
            html_text = http_get(base, "/playlist", params, timeout=90, retries=1)
        except Exception as e:
            print("  拉取失败(%s)，重试 %d/5" % (str(e)[:50], attempt + 1))
            time.sleep(5 * (attempt + 1))
            continue
        if re.findall(r'<li class="song-card"(.*?)>', html_text, re.S):
            break
        print("  第 %d 次返回空列表（疑似上游限流），退避 10s 重试 %d/5"
              % (attempt + 1, attempt + 2))
        html_text = None
        time.sleep(10 * (attempt + 1))

    if html_text is None:
        sys.exit("歌单解析失败：上游平台限流或接口不可用，请稍后重试。")

    import html as _html
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
            "name": _html.unescape(g("data-name")),
            "artist": _html.unescape(g("data-artist")),
            "album": _html.unescape(g("data-album")),
            "cover": g("data-cover"),
            "duration": g("data-duration"),
            "extra": (re.findall(r"data-extra='([^']*)'", blk) or [""])[0],
        })

    total_m = re.search(r'找到\s*<span class="count">(\d+)</span>', html_text)
    total = int(total_m.group(1)) if total_m else len(songs)
    print("  解析到 %d 首（页面声明 %d 首）" % (len(songs), total))

    if total > len(songs) and len(songs) >= 500:
        page = 2
        while len(songs) < total and page <= 20:
            try:
                h2 = http_get(base, "/playlist", dict(params, page=str(page)),
                              timeout=90, retries=2)
            except Exception:
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
                    "name": _html.unescape(g("data-name")),
                    "artist": _html.unescape(g("data-artist")),
                    "album": _html.unescape(g("data-album")),
                    "cover": g("data-cover"),
                    "duration": g("data-duration"),
                    "extra": (re.findall(r"data-extra='([^']*)'", blk) or [""])[0],
                })
            if added == 0:
                break
            page += 1
            time.sleep(REQUEST_INTERVAL)
    return songs


def switch_source(base, song, targets=TARGETS):
    """为单首找一个可下载的替代源。返回新元数据或 None。

    关键点（均为实测结论）：
    1. song['duration'] 直接传【秒】，不要换算成毫秒 —— 传毫秒接口会直接 404。
    2. score 不是可靠判据：源本身缺歌时，无论传什么参数都只会返回 0.3 的
       错误匹配（如「稻香」→「我的地盘」）。真正的判据是【歌名是否一致】。
    """
    dur = song.get("duration") or ""
    for idx, t in enumerate(targets):
        # 首选源给足超时；兜底源上限 12s —— 实测 joox 单次请求会阻塞 6~7s，
        # 再加上 qq/netease/kuwo 的 404，每首缺歌会白白多耗十几秒。
        timeout = 45 if idx == 0 else 12
        try:
            q = {"name": song["name"], "artist": song["artist"],
                 "source": song["source"], "target": t, "duration": dur}
            d = json.loads(http_get(base, "/switch_source", q, timeout=timeout, retries=1))
        except Exception:
            continue
        if not d.get("id"):
            continue
        try:
            score = float(d.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
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
            # 关键：migu 匿名态返回的 artist 常是「🐷🐷」这类占位符，
            # 必须用原歌单的歌手名覆盖，否则文件会存成「🐷🐷 - 然后.mp3」。
            "artist": song.get("artist") or d.get("artist") or "",
            "album": d.get("album") or song["album"],
            "cover": d.get("cover") or song["cover"],
            "extra": extra,
            "score": score,
        }
    return None


def search_fallback(base, song, targets=TARGETS):
    """switch_source 全失败后的兜底：走搜索接口按【歌名+歌手】精确匹配。

    为什么需要这条通道（实测结论）：
    switch_source 内部按「歌名+时长」做模糊打分，源存在同名歌但打分算法
    把别的版本排在了前面时，会返回 score=0.3 的错配（如「七里香」→「我的地盘」、
    「Mojito」→「东风破」）。而 /search 接口返回该源的完整候选列表，
    在里面按「歌名一致 + 歌手一致」筛选就能稳定拿到正确曲目。

    实测：「七里香」「稻香」「Mojito」「不该 (Live)」四首在 switch_source 全错配，
    经本通道全部命中。

    时长只用于在多个同名候选中择优，不做硬排除：Live 版与专辑版是不同录音，
    时长本就会有差（「不该 (Live)」301s vs「不该 (幻城版)」290s，差 11s 仍是同一首）。
    防错配由「歌名一致 + 歌手一致」双重硬判据保证，不依赖时长。
    """
    import html as _html
    want = song.get("name") or ""
    want_artist = (song.get("artist") or "").strip()
    try:
        want_dur = int(song.get("duration") or 0)
    except (TypeError, ValueError):
        want_dur = 0

    for t in targets:
        try:
            html_text = http_get(base, "/search",
                                 {"q": want, "sources": t, "type": "song"},
                                 timeout=45, retries=1)
        except Exception:
            continue

        cands = []
        for blk in re.findall(r'<li class="song-card"(.*?)>', html_text, re.S):
            g = lambda k: (re.findall(k + r'="([^"]*)"', blk) or [""])[0]
            name = _html.unescape(g("data-name"))
            artist = _html.unescape(g("data-artist"))
            if not same_song(want, name):
                continue
            if want_artist and normalize_artist(want_artist) != normalize_artist(artist):
                continue
            try:
                dur = int(g("data-duration") or 0)
            except (TypeError, ValueError):
                dur = 0
            diff = abs(dur - want_dur) if (dur and want_dur) else 0
            cands.append((diff, {
                "id": g("data-id"), "source": t,
                "name": name, "artist": artist,
                "album": _html.unescape(g("data-album")),
                "cover": g("data-cover"),
                "extra": (re.findall(r"data-extra='([^']*)'", blk) or [""])[0],
                "duration": dur,
            }))

        if cands:
            cands.sort(key=lambda x: x[0])
            diff, best = cands[0]
            # 同样用原歌单歌手覆盖，避免 migu 匿名态返回「🐷🐷」占位符
            best["artist"] = song.get("artist") or best["artist"]
            print("       [直搜命中] %s -> %s (%s, 时长差 %ds)"
                  % (want[:20], best["name"][:20], t, diff))
            return best
    return None


def cache_path(work_dir, pl_id):
    return os.path.join(work_dir, "switch-cache-%s.json" % pl_id)


def load_switch_cache(work_dir, pl_id):
    """读取换源结果缓存。

    换源阶段 106 首约需 2 分钟（每次换源请求都要等上游响应，还带 1.2s 节流）。
    中途 Ctrl+C 或关窗口会丢掉全部进度，因此每首换源成功后立即落盘，
    重跑时直接复用，避免重复等待。
    """
    p = cache_path(work_dir, pl_id)
    try:
        if not os.path.isfile(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_switch_cache(work_dir, pl_id, cache):
    try:
        p = cache_path(work_dir, pl_id)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:
        pass


def download_playlist(base, pl_id, raw_input, work_dir=None):
    """完整歌单下载流程。"""
    print("歌单 ID: %s" % pl_id)
    songs = fetch_songs(base, pl_id, "kugou")
    if not songs:
        sys.exit("歌单解析为空。")
    print("共 %d 首，开始换源...\n" % len(songs))

    # 换源阶段较慢，用缓存让中断后可续跑
    cache = load_switch_cache(work_dir, pl_id) if work_dir else {}
    if cache:
        print("已载入换源缓存 %d 条（跳过重复请求）\n" % len(cache))

    ready, failed, fallback = [], [], []
    for i, s in enumerate(songs, 1):
        key = "%s|%s" % (s["name"], s["artist"])
        cached = cache.get(key)
        if cached:
            ready.append(cached)
            print("  [%3d/%d] %-24s -> %-6s (缓存 score %.2f)"
                  % (i, len(songs), s["name"][:24], cached["source"], cached["score"]))
            continue

        alt = switch_source(base, s)
        if alt:
            ready.append(alt)
            cache[key] = alt
            if work_dir:
                save_switch_cache(work_dir, pl_id, cache)
            print("  [%3d/%d] %-24s -> %-6s (score %.2f)"
                  % (i, len(songs), s["name"][:24], alt["source"], alt["score"]))
        else:
            # 换源通道失败：再走直搜兜底通道（按歌名+歌手精确匹配）
            alt = search_fallback(base, s)
            if alt:
                extra = alt.get("extra") or ""
                if not isinstance(extra, str):
                    extra = json.dumps(extra, ensure_ascii=False)
                alt["extra"] = extra
                alt["score"] = 1.0
                ready.append(alt)
                cache[key] = alt
                if work_dir:
                    save_switch_cache(work_dir, pl_id, cache)
                print("  [%3d/%d] %-24s -> %-6s (直搜兜底命中)"
                      % (i, len(songs), s["name"][:24], alt["source"]))
                time.sleep(REQUEST_INTERVAL)
                continue
            failed.append(s["name"])
            fallback.append({
                "id": s["id"], "source": s["source"], "name": s["name"],
                "artist": s["artist"], "album": s["album"],
                "cover": s["cover"], "extra": s["extra"],
            })
            print("  [%3d/%d] %-24s -> 换源无匹配，回退原源（需 Cookie）"
                  % (i, len(songs), s["name"][:24]))
        time.sleep(REQUEST_INTERVAL)

    if not ready and not fallback:
        sys.exit("全部换源失败。")

    req_id = "pl-%s-%d" % (pl_id, int(time.time()))
    batch = ready + fallback
    print("\n提交下载队列: %d 首（换源 %d + 原源回退 %d）..."
          % (len(batch), len(ready), len(fallback)))
    resp = json.loads(http_post_json(base, "/api/downloads/queue",
                                      {"request_id": req_id, "songs": batch}))
    print("  新增任务 %d，已存在 %d" % (resp.get("added", 0), resp.get("existing", 0)))

    # 队列 counts 是【全局累计】，不能用于判断本批次进度。
    # 正确判据：status=active 即 waiting+downloading 的总数，排空即本批次结束。
    print("轮询下载状态...")
    idle = 0
    while True:
        time.sleep(10)
        try:
            d = json.loads(http_get(base, "/api/downloads/queue",
                                    {"page": 1, "status": "active"}, timeout=30))
            active = d.get("total", len(d.get("tasks", [])))
            print("  活跃任务 %s | 全局统计 %s" % (active, d.get("counts", {})))
            if int(active) == 0:
                idle += 1
                if idle >= 2:
                    break
            else:
                idle = 0
        except Exception as e:
            print("  状态查询失败: %s" % str(e)[:60])
    try:
        d = json.loads(http_get(base, "/api/downloads/queue",
                                {"page": 1, "status": ""}, timeout=30))
        print("\n全局最终统计: %s" % d.get("counts", {}))
    except Exception:
        pass
    if failed:
        print("换源无匹配 %d 首（已回退原源提交，需配 Cookie 才能下到）: %s"
              % (len(failed), "、".join(failed[:10])))


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def print_banner():
    print("=" * 58)
    print("  %s v%s   音乐下载器" % (APP_NAME, APP_VERSION))
    print("=" * 58)


def setup_console_encoding():
    """强制 UTF-8 输出。

    Windows 控制台默认代码页是 GBK(936)，直接 print 中文会抛
    UnicodeEncodeError 或显示乱码。这里统一把 stdout/stderr 换成 UTF-8，
    并同步设置控制台代码页（Windows 10+ 有效）。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            # line_buffering=True：输出重定向到文件时也能实时看到进度，
            # 否则 Python 会用 8KB 块缓冲，把整个下载过程的日志攒到最后才吐。
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        except Exception:
            try:
                import io
                setattr(sys, stream_name,
                        io.TextIOWrapper(stream.buffer, encoding="utf-8",
                                         errors="replace", line_buffering=True))
            except Exception:
                pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def run_web_mode(svc):
    """双击模式：启动服务并打开浏览器。"""
    url = svc.base + "/music"
    print("\n服务已启动: %s" % url)
    print("下载目录: %s" % svc.download_dir)
    print("音乐文件会实时出现在上面的目录里。")
    print("\n[提示] 批量下整张歌单可用命令行：")
    print("       %s.exe <歌单链接或ID>" % APP_NAME)
    print("\n关闭本窗口会停止服务；若还有下载在跑，会先等它完成。\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        svc.proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        # 退出前等下载队列排空，避免文件被截断成无法播放的半截
        svc.stop(wait_for_queue=True)


def main():
    setup_console_encoding()
    print_banner()
    work_dir = user_data_dir()
    download_dir = os.path.join(work_dir, "downloads")
    try:
        os.makedirs(download_dir, exist_ok=True)
    except Exception:
        download_dir = os.path.join(tempfile.gettempdir(), "MusicDL", "downloads")
        os.makedirs(download_dir, exist_ok=True)

    port = pick_port()
    if not port:
        sys.exit("找不到可用端口。")

    svc = Service(work_dir, port, download_dir)
    print("工作目录: %s" % work_dir)
    print("启动本地服务（端口 %d）..." % port)
    try:
        svc.start()
    except Exception as e:
        print("\n启动失败: %s" % e)
        sys.exit(1)

    svc.apply_download_dir(download_dir)

    if len(sys.argv) >= 2:
        raw = sys.argv[1]
        pl_id = parse_playlist_id(raw)
        out = sys.argv[2] if len(sys.argv) >= 3 else download_dir
        if os.path.abspath(out) != os.path.abspath(download_dir):
            try:
                os.makedirs(out, exist_ok=True)
                svc.apply_download_dir(out)
                print("下载目录改为: %s" % out)
            except Exception as e:
                print("设置输出目录失败: %s" % str(e)[:60])
        try:
            download_playlist(svc.base, pl_id, raw, work_dir=svc.work_dir)
        finally:
            svc.stop()
        print("\n完成。文件目录: %s" % svc.download_dir)
    else:
        run_web_mode(svc)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("\n发生未预期的错误: %s" % str(e)[:200])
        sys.exit(1)

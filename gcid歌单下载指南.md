# 酷狗 gcid 歌单下载指南

## 一句话结论

**「用户0305喜欢的音乐」共 292 首**（酷狗官方 `listinfo.count`）。
网页版显示 10 首、下载只得到 3 首，**不是你的操作问题，是酷狗对「用户自建歌单」只公开 10 首预览**。

---

## 为什么只能下到 3 首

酷狗有两种歌单，解析难度完全不同：

| 歌单 ID 形态 | 例子 | 上游能拿到几首 |
|---|---|---|
| `special/single/{数字}` | `8691023` | **全量**（实测 106/106 首） |
| `gcid_xxx` | `gcid_3z1457hyaz3z0c5` | **只有 10 首预览** |

上游 `music-lib` 解析 `gcid_` 歌单时，请求：

```
https://www.kugou.com/songlist/{gcid}/
```

然后用正则从页面内嵌 JSON 里抠 `info.songs` 数组。而酷狗这个页面**只内嵌前 10 首**：

```
info.listinfo.count = 292    ← 真实总数（只用来显示"共292首"）
info.songs          = 10 首  ← 实际能拿到的
```

实测 `?page=2`、`?pagesize=300` 等分页参数**一律无效**，返回的还是同样 10 首。

于是网页端点「下载整个歌单」时：
- 内部走 `loadImportedCollectionSongs()` → 重新调 `GetPlaylistSongs(gcid_xxx)`
- 同样只解析到那点内容 → **队列只提交 3 个任务**（实测返回 `{"added":3}`）

而那 3 首（日不落 / 不死鸟 / 然后）恰好也是内嵌 10 首里的第 4、5、7 首。

---

## 怎么下载

### 方案 A：用我写的脚本（推荐，无需 Cookie，可下 10 首）

```bash
cd E:\WZY\format\go-music-dl
# 先启动本地服务（双击 build\dist\MusicDL.exe 也行，端口 18200）
cd dist\cli && ./music-dl.exe web --port 8080
# 另开终端
python 下载gcid歌单.py gcid_3z1457hyaz3z0c5
```

脚本做了三件网页端没做的事：
1. 绕开 `music-lib` 的 3 首限制，直接解析 songlist 页面拿到 **10 首**；
2. **修正被污染的歌手名** —— migu 匿名态返回的 `artist` 是「🐷🐷」这类占位符，
   脚本用原歌单歌手覆盖（否则文件会存成 `🐷🐷 - 然后.mp3`）；
3. 双通道换源（主通道 `switch_source` + 兜底 `/search` 精确筛选），实测 10/10 命中。

指定服务地址（如果用 EXE，端口是 18200）：
```bash
MUSIC_DL_BASE="http://127.0.0.1:18200/music" python 下载gcid歌单.py gcid_3z1457hyaz3z0c5
```

### 方案 B：填酷狗 Cookie → 下全量 292 首

云歌单接口 `gateway.kugou.com/v4/get_list_all_file` 支持 `pagesize=300`，
能拿全量曲目，但**必须有 App 登录态的 token**。

在 `dist/cli/data/cookies.json` 里写入：

```json
{
  "kugou": "userid=2056178400; token=你的token; KUGOU_API_MID=你的mid; dfid=-"
}
```

Cookie 从**酷狗 App** 登录后抓取（App 端才有 `token` / `KUGOU_API_MID`；
网页端 Cookie 没有这两个字段，会返回错误码 20017 = token 无效）。

配好后脚本会自动检测并改走全量接口：
```
官方曲目总数: 292
云歌单接口取到全量: 292 首
```

### 方案 C：换一种歌单形态

如果你能拿到这个歌单的**数字精选集 ID**（形如 `special/single/12345678`），
直接用现成的批量脚本就能全量下载：

```bash
python 下载歌单.py 12345678
```

数字精选集走 `mobilecdn/api/v3/special/song?pagesize=300`，实测 106/106 全量。

---

## 想拿到完整 292 首歌名清单

在填 Cookie 之前，可以先用浏览器辅助：

1. Chrome 登录酷狗 → 打开该歌单
2. F12 → Network → 筛选 `songlist` 或 `plist`
3. 看有没有带 `specialid` / `gid` / `pagesize` 的 XHR 请求

如果浏览器能拿到全量数据，说明该接口对登录态开放，把请求参数照着填进
`downloads.json` 的 cookie 段通常就能解决。

---

## 附：本次排查确认的技术细节

| 项 | 结论 |
|---|---|
| 官方曲目总数 | 292（`info.listinfo.count`） |
| 页面内嵌歌曲数 | 10（`info.songs`） |
| 内嵌 10 首 | 喻言-蠢货 / 陈慧娴-千千阕歌 / MJ-Whatever Happens / 蔡依林-日不落 / 霍尊-不死鸟 / 范玮琪-最初的梦想 / 萧亚轩-然后 / 萧亚轩-闪闪惹人爱 / 萧亚轩-最熟悉的陌生人 / 萧亚轩-爱的主打歌 |
| 创建者 | 用户0305（uid=2056178400） |
| songlist 页 `name` 字段 | 是「歌手 - 歌名」拼接，需拆分后换源才准 |
| 上游正则缺陷 | `window\.\$output\s*=\s*({.*?})\s*;\s*</script>` 在真实页面会失配，应用括号配平 |
| migu 匿名态 artist | 返回「🐷🐷」占位符，必须用原歌单歌手覆盖 |
| 云歌单接口错误码 | 20010 = 缺参数；20017 = token 无效 |
| 上游 `special/song` 接口 | `mobilecdn.kugou.com/api/v3/special/song?specialid=N&page=1&pagesize=300&version=9108&area_code=1` |

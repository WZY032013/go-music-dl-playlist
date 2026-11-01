# go-music-dl-playlist

酷狗 / 多平台**歌单批量下载**工具。基于 [guohuiyuan/go-music-dl](https://github.com/guohuiyuan/go-music-dl) v1.1.3（AGPL-3.0），
解决上游接口「能解析歌单但换源会下错歌」的问题。

## 这套工具解决什么

上游 `switch_source` 接口存在算法缺陷：**源里明明有歌，却会返回 score=0.3 的错配**。
实测（酷狗歌单 8691023，周杰伦热门歌曲故事，共 106 首）：

| 原歌名 | `switch_source` 返回 | score | migu 直搜真实结果 |
|---|---|---|---|
| 七里香 | 我的地盘 | 0.30 | **七里香 / 周杰伦 / 298s** |
| 稻香 | 稻香 (Remix摇滚版) | 0.44 | **稻香 / 周杰伦 / 223s** |
| Mojito | 东风破(Album Version) | 0.39 | **Mojito / 周杰伦 / 185s** |
| 不该 (Live) | 圣诞星（feat. 杨瑞代） | 0.30 | **不该(幻城版) / 周杰伦 张惠妹 / 290s** |

本工具用**双通道换源**解决：主通道失败后自动走 `/search` 兜底通道，按「歌名 + 歌手」精确筛选。

## 实测结果

| 指标 | 结果 |
|---|---|
| 歌单解析 | 106 / 106 |
| 主通道 `switch_source` 命中 | 102 / 106 |
| 兜底通道 `search_fallback` 救回 | **4 / 4** |
| **换源总成功率** | **106 / 106**（原源回退 0 首） |
| 落盘文件 | 105 个，2.2 GB |
| 音质分布 | FLAC 无损 45 首 + MP3 60 首 |
| 文件完整性 | 音频头逐字节校验，**0 损坏** |

## 快速开始

### 方式一：单文件 EXE（推荐给非技术用户）

`build/dist/MusicDL.exe`（20.6 MB），内嵌全部依赖，**对方电脑无需装任何东西**。

```bash
# 双击运行：自动启动本地服务并打开浏览器
MusicDL.exe

# 批量下载整张歌单
MusicDL.exe 8691023
MusicDL.exe "https://www.kugou.com/yy/special/single/8691023.html"
MusicDL.exe 8691023 "D:\我的音乐"
```

下载的文件在 `MusicDL.exe` 同级的 `downloads\` 目录。

### 方式二：Python 脚本

```bash
cd E:\WZY\format\go-music-dl
# 先启动服务
cd dist/cli && ./music-dl.exe web --port 8080
# 另开终端
python 下载歌单.py 8691023
```

## 核心设计

### 双通道换源

```
switch_source()  →  失败  →  search_fallback()  →  仍失败  →  回退原源（需 Cookie）
   (快, 102/106)      (兜底, 4/4)                  (VIP 歌, 需登录)
```

`search_fallback()` 走 `/search` 接口拿目标源的**完整候选列表**，按双重硬判据筛选：

- **歌名一致**：剥离 Live / Remix / 伴奏 等版本后缀与括号说明后完全相同
- **歌手一致**：归一化分隔符后相同（酷狗用「、」、migu 用「/」）

> 时长只用于在多个同名候选中择优，**不做硬排除**。Live 版与专辑版是不同录音，
> 时长本就会有差（「不该 (Live)」301s vs「不该 (幻城版)」290s，差 11s 仍是同一首）。

### 防下错歌

上游接口会返回完全无关的歌。因此所有换源结果都必须通过 `same_song()` 校验
（剥离版本后缀后的核心歌名比对），不通过直接拒绝。**实测从 104 首错歌中救回正确曲目。**

## 测试

```bash
python -m unittest test_下载歌单 -v     # 23 个用例
```

覆盖：
- `same_song()` 的错配反例（4 个）与版本后缀正例（8 个）
- `normalize_title()` / `normalize_artist()` 归一化
- 兜底通道候选筛选：同名他人歌排除、同名多候选按时长择优、歌手匹配
- `build/MusicDL.py` 与脚本版判据行为一致性（防止只改一处导致行为漂移）

## 已知限制

**酷狗 VIP 歌匿名下不到**：部分歌 `privilege=10`，匿名直取返回 `Failed to get URL`。
填入酷狗 Cookie 后可拿原音质；不填则走换源（免登录，但格式降级为 ZQ/MP3）。
填 Cookie 方法见 `使用说明.md`。

**上游接口依赖平台未公开接口**，平台改版后可能失效。

## 目录结构

```
go-music-dl/
├── 下载歌单.py              # 双通道换源批量下载脚本
├── test_下载歌单.py         # 单元测试（23 用例）
├── 使用说明.md              # 部署与使用说明（含全部踩坑记录）
└── build/
    ├── MusicDL.py           # 单文件 EXE 源码（与脚本共用换源逻辑）
    ├── build.py             # PyInstaller 打包脚本
    ├── 使用说明.md          # 面向最终用户的说明
    └── dist/MusicDL.exe     # 打包产物（20.6 MB）
```

## 注意事项

- 仅供个人学习与自用，遵守版权规定，**不要用于商业用途或二次分发下载内容**
- 下载的资源请在 24 小时内自行处理
- 底层引擎 AGPL-3.0，本封装同样遵循

## 致谢

- [guohuiyuan/go-music-dl](https://github.com/guohuiyuan/go-music-dl) v1.1.3（AGPL-3.0）

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用 PyInstaller 把 MusicDL.py 打成单文件 exe，并把 music-dl.exe 内嵌进去。

用法:
    python build/build.py
产物:
    build/dist/MusicDL.exe   （单文件，自包含 music-dl.exe）
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENTRY = os.path.join(HERE, "MusicDL.py")
MUSIC_DL = os.path.join(ROOT, "dist", "cli", "music-dl.exe")
OUT_DIR = os.path.join(HERE, "dist")
WORK_DIR = os.path.join(HERE, "work")
SPEC = os.path.join(HERE, "MusicDL.spec")


def main():
    if not os.path.isfile(ENTRY):
        sys.exit("找不到入口文件: %s" % ENTRY)
    if not os.path.isfile(MUSIC_DL):
        sys.exit("找不到 music-dl.exe: %s" % MUSIC_DL)

    for d in (OUT_DIR, WORK_DIR):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--console",
        "--name", "MusicDL",
        "--distpath", OUT_DIR,
        "--workpath", WORK_DIR,
        "--specpath", HERE,
        # 内嵌 music-dl.exe：运行时释放到用户目录再执行
        "--add-binary", "%s;." % MUSIC_DL,
        "--noupx",
        # 排除用不到的大依赖，减小体积
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "PIL",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "pytest",
        "--exclude-module", "setuptools",
        "--exclude-module", "pip",
        "--exclude-module", "tkinter",
        ENTRY,
    ]
    print("执行 PyInstaller ...")
    print(" ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        sys.exit("PyInstaller 失败，退出码 %d" % r.returncode)

    exe = os.path.join(OUT_DIR, "MusicDL.exe")
    if not os.path.isfile(exe):
        sys.exit("打包产物不存在: %s" % exe)
    size_mb = os.path.getsize(exe) / 1048576.0
    print("\n打包完成: %s  (%.1f MB)" % (exe, size_mb))


if __name__ == "__main__":
    main()

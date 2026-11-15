#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在桌面生成带图标的快捷方式（路径按脚本所在目录推算，换机器也能用）。"""
import os
import sys

import win32com.client

here = os.path.dirname(os.path.abspath(__file__))
script = os.path.join(here, "player_ui.py")
ico = os.path.join(here, "app.ico")

# 优先用 pythonw，免得每次启动弹一个黑框
pyw = sys.executable.replace("python.exe", "pythonw.exe")
if not os.path.exists(pyw):
    pyw = sys.executable

desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
lnk_path = os.path.join(desktop, "耳读.lnk")

shell = win32com.client.Dispatch("WScript.Shell")
lnk = shell.CreateShortcut(lnk_path)
lnk.TargetPath = pyw
lnk.Arguments = f'"{script}"'
lnk.WorkingDirectory = here
lnk.IconLocation = f"{ico},0"
lnk.Description = "耳读 · 把文字念出来"
lnk.Save()
print("已创建:", lnk_path)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交前密钥泄露自检。

用法：
    python tools/check_secrets.py            # 只报告，不失败
    python tools/check_secrets.py --strict   # 发现问题时退出码 1（CI 用）

它做四件事：
  1. 按文件名拦截敏感文件（密钥库 / 备份 / 运行数据）
  2. 按内容匹配常见密钥形态（sk-、user_、Bearer、apiKey=…）
  3. 找出硬编码的「某台机器专属」绝对路径（C:\\Users\\<某人>\\…）
  4. 列出将会被 git 跟踪的文件，便于人工复核

注意：这个脚本只做静态扫描，**不能**替代「密钥轮换」。
如果你的密钥曾经被推送过，唯一正确的处置是去各平台重新生成密钥。
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------- 1. 敏感文件名 ----------
SENSITIVE_NAMES = [
    "opencode_go_keys.json",
    "app_settings.json",
    "fx_cache.json",
    "grok_usage_archive.json",
    ".credentials.yaml",
    "settings.yaml",
]
SENSITIVE_SUFFIX = (".bak", ".tmp", ".corrupt", ".log")
# 允许存在的同名文件（仓库里本来就该有的）
ALLOWLIST_PATHS = set()

# ---------- 2. 密钥形态 ----------
SECRET_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "疑似 OpenAI 风格密钥 sk-…"),
    (re.compile(r"\bsk_[A-Za-z0-9_\-]{16,}"), "疑似密钥 sk_…"),
    (re.compile(r"\buser_[A-Za-z0-9]{16,}"), "疑似 Command Code 密钥 user_…"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}"), "硬编码 Bearer Token"),
    (re.compile(r"(?i)\b(api[_-]?key|apikey|access[_-]?token|secret)\b\s*[:=]\s*[\"'][^\"']{16,}[\"']"),
     "硬编码的 apiKey / token / secret"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "GitHub Personal Access Token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "GitHub PAT"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥内容"),
]

# ---------- 3. 机器专属绝对路径 ----------
MACHINE_PATH_PATTERNS = [
    (re.compile(r"(?<![%\w])[A-Za-z]:[\\/]Users[\\/](?!<|%|\$|\{|Public\b|Default\b)[A-Za-z0-9._\-]+"),
     "硬编码了某个 Windows 用户名路径"),
    (re.compile(r"(?<![%\w])[A-Za-z]:[\\/]opencode\b"), "硬编码了作者机器上的项目目录"),
    (re.compile(r"(?<![%\w])[A-Za-z]:[\\/]OpenCodeData\b"), "硬编码了某个盘符下的 OpenCodeData"),
    (re.compile(r"/home/(?!<|%|\$)[A-Za-z0-9._\-]+"), "硬编码了某个 Linux 家目录"),
]

SCAN_EXT = {".py", ".js", ".ts", ".json", ".yml", ".yaml", ".md", ".txt",
            ".ps1", ".sh", ".bat", ".cfg", ".ini", ".toml", ".spec", ".html"}
SKIP_DIRS = {".git", "build", "dist", "__pycache__", ".venv", "venv", "node_modules"}

# 这些文件里出现「路径」是正常说明文字，不做机器路径检查
PATH_CHECK_SKIP = {"README.md", "check_secrets.py"}


def walk_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


def scan():
    findings = []
    scanned = 0
    for path in walk_files():
        r = rel(path)
        if r in ALLOWLIST_PATHS:
            continue
        name = os.path.basename(path).lower()

        # 1. 文件名
        if name in SENSITIVE_NAMES or name.endswith(SENSITIVE_SUFFIX):
            findings.append(("文件名", r, "疑似密钥/运行数据文件，不应进入仓库"))
            continue

        ext = os.path.splitext(name)[1]
        if ext not in SCAN_EXT:
            continue
        try:
            if os.path.getsize(path) > 4 * 1024 * 1024:
                continue
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        scanned += 1

        for i, line in enumerate(lines, 1):
            if len(line) > 4000:
                continue
            for pat, desc in SECRET_PATTERNS:
                m = pat.search(line)
                if m:
                    shown = m.group(0)
                    shown = shown[:14] + "…" if len(shown) > 14 else shown
                    findings.append((f"密钥[{desc}]", f"{r}:{i}", f"命中片段 {shown!r}"))
            if name not in PATH_CHECK_SKIP:
                for pat, desc in MACHINE_PATH_PATTERNS:
                    m = pat.search(line)
                    if m:
                        findings.append((f"机器路径[{desc}]", f"{r}:{i}", m.group(0)))
    return findings, scanned


def git_tracked():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, timeout=20)
        if out.returncode == 0:
            return [x for x in out.stdout.splitlines() if x.strip()]
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="发现问题时以退出码 1 结束")
    args = ap.parse_args()

    print("仓库根目录：%s" % ROOT)
    findings, scanned = scan()
    print("已扫描 %d 个文本文件" % scanned)

    tracked = git_tracked()
    if tracked is None:
        print("（未检测到 git 仓库，跳过「将被跟踪的文件」清单）")
    else:
        print("git 已跟踪 %d 个文件" % len(tracked))
        bad = [t for t in tracked
               if os.path.basename(t).lower() in SENSITIVE_NAMES
               or os.path.basename(t).lower().endswith(SENSITIVE_SUFFIX)]
        for t in bad:
            findings.append(("git 跟踪", t, "敏感文件已被 git 跟踪，必须 git rm --cached"))

    if not findings:
        print("\n[OK] 未发现密钥或机器专属路径。")
        return 0

    print("\n发现 %d 处问题：\n" % len(findings))
    for kind, where, detail in findings:
        print("  [%s] %s\n        %s" % (kind, where, detail))
    print("\n提醒：静态扫描通过 ≠ 安全。"
          "\n如果这些密钥曾经被 push 过，请立刻到对应平台重新生成密钥（旧密钥视为已泄露）。")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())

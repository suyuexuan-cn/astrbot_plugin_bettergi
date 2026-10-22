#!/usr/bin/env python3
"""BetterGI 远程辅助程序启动器。

替代原来的 运行功能.bat，所有功能都由 Python 实现：
    - 交互式菜单
    - 前台 / 后台启动辅助程序
    - 注册 / 取消 / 查看开机自启（计划任务 + 注册表双通道）
    - 测试与 AstrBot 的连接
    - 修改 config.yaml（保留原注释）
    - 查看运行日志
    - 依赖检查与安装

也可以直接用命令行参数调用：
    python launcher.py --start
    python launcher.py --daemon
    python launcher.py --stop
    python launcher.py --install
    python launcher.py --uninstall
    python launcher.py --status
    python launcher.py --test
    python launcher.py --config
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
CONFIG = HERE / "config.yaml"
PID_FILE = HERE / "assistant.pid"
LOG_FILE = HERE / "assistant.log"
REQUIREMENTS = HERE / "requirements.txt"

TASK_NAME = "BetterGIAssistant"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

HEALTH_PATH = "/api/v1/plugins/extensions/bettergi/remote/health"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

LINE = "=" * 60


# --------------------------------------------------------------------------
# 控制台
# --------------------------------------------------------------------------


def setup_console() -> None:
    """统一切到 UTF-8，避免中文在 GBK 控制台里乱码或直接抛异常。"""
    if os.name == "nt":
        try:
            subprocess.run(
                "chcp 65001", shell=True, capture_output=True, timeout=10
            )
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def ok(msg: str) -> None:
    print(f"  [成功] {msg}")


def warn(msg: str) -> None:
    print(f"  [提示] {msg}")


def err(msg: str) -> None:
    print(f"  [错误] {msg}")


def info(msg: str) -> None:
    print(f"  {msg}")


def pause() -> None:
    try:
        input("\n按回车继续...")
    except (EOFError, KeyboardInterrupt):
        pass


def ask(prompt_text: str, default: str = "") -> str:
    """读取一行输入，空输入返回默认值（用于 y/N 确认）。"""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"  {prompt_text}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    if not value:
        return default
    return value.strip().strip('"').strip("'")


def prompt_value(prompt_text: str, current: str = "") -> str:
    """读取配置项。空输入返回空串，表示保留原值（不做校验）。"""
    suffix = f" [{current}]" if current else ""
    try:
        return input(f"  {prompt_text}{suffix}: ").strip().strip('"').strip("'")
    except (EOFError, KeyboardInterrupt):
        return ""


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def load_config() -> dict:
    """读取 config.yaml，失败返回空字典。"""
    if not CONFIG.exists():
        return {}
    try:
        import yaml
    except ImportError:
        err("缺少 pyyaml，无法读取配置，请先安装依赖")
        return {}
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        err(f"配置文件解析失败: {e}")
        return {}


def _yaml_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def set_config_value(key: str, value: str) -> bool:
    """写回单个配置项，保留文件里的注释和顺序。"""
    try:
        text = CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else ""
    except Exception as e:
        err(f"读取配置失败: {e}")
        return False

    line = f"{key}: {_yaml_literal(value)}"
    pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*:.*$", re.MULTILINE)

    if pattern.search(text):
        # 用 lambda 替换，避免路径里的反斜杠被 re 当作转义
        text = pattern.sub(lambda _m: line, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"

    try:
        CONFIG.write_text(text, encoding="utf-8")
        return True
    except Exception as e:
        err(f"写入配置失败: {e}")
        return False


def mask_key(key: str) -> str:
    if not key:
        return "未设置"
    if len(key) <= 8:
        return key[:2] + "****"
    return f"{key[:4]}****{key[-4:]}"


def config_problems(cfg: dict) -> list[str]:
    """检查必填项，返回问题列表。"""
    problems: list[str] = []
    if not cfg.get("astrbot_url"):
        problems.append("未填写 astrbot_url")
    elif not str(cfg["astrbot_url"]).startswith(("http://", "https://")):
        problems.append("astrbot_url 必须以 http:// 或 https:// 开头")
    if not cfg.get("bettergi_dir"):
        problems.append("未填写 bettergi_dir")
    elif not Path(str(cfg["bettergi_dir"])).is_dir():
        problems.append(f"bettergi_dir 目录不存在: {cfg['bettergi_dir']}")
    if not cfg.get("api_key"):
        problems.append("未填写 api_key")
    return problems


def edit_config_wizard() -> bool:
    """交互式修改配置。"""
    print(f"\n{LINE}\n  修改配置（直接回车保留原值）\n{LINE}")
    if not CONFIG.exists():
        warn(f"配置文件不存在，将新建: {CONFIG.name}")

    cfg = load_config()
    changed = False

    url = prompt_value("AstrBot 地址", str(cfg.get("astrbot_url", "")))
    if url:
        if not url.startswith(("http://", "https://")):
            err("地址必须以 http:// 或 https:// 开头，已跳过该项")
        else:
            url = url.rstrip("/")
            if url != cfg.get("astrbot_url"):
                set_config_value("astrbot_url", url)
                changed = True

    gi_dir = prompt_value("BetterGI 安装目录", str(cfg.get("bettergi_dir", "")))
    if gi_dir:
        p = Path(gi_dir)
        if not p.is_dir():
            err(f"目录不存在: {gi_dir}，已跳过该项")
        else:
            exe = next(
                (p / n for n in ("BetterGI.exe", "BetterGI.bat", "BetterGI.cmd") if (p / n).exists()),
                None,
            )
            if exe is None:
                warn(f"该目录下没找到 BetterGI.exe，仍会保存: {p}")
            if str(p) != cfg.get("bettergi_dir"):
                set_config_value("bettergi_dir", str(p))
                changed = True

    key = prompt_value("API 密钥（abk_ 开头）", str(cfg.get("api_key", "")))
    if key:
        if key.startswith("abk_") is False:
            warn("密钥通常以 abk_ 开头，请确认没有复制错")
        if key != cfg.get("api_key"):
            set_config_value("api_key", key)
            changed = True

    level = prompt_value("日志级别 DEBUG/INFO/WARNING/ERROR", str(cfg.get("log_level", "INFO")))
    if level:
        level = level.upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            err("日志级别无效，已跳过该项")
        elif level != cfg.get("log_level"):
            set_config_value("log_level", level)
            changed = True

    if changed:
        ok("配置已保存")
    else:
        warn("配置没有变化")
    return changed


def ensure_config() -> bool:
    """配置不完整时引导用户补全。"""
    cfg = load_config()
    problems = config_problems(cfg)
    if not problems:
        return True

    print(f"\n{LINE}\n  配置需要补全\n{LINE}")
    for p in problems:
        err(p)
    if not sys.stdin.isatty():
        return False

    answer = ask("现在修改配置? (y/N)", "n").lower()
    if answer not in ("y", "yes"):
        return False
    edit_config_wizard()
    return not config_problems(load_config())


# --------------------------------------------------------------------------
# 依赖
# --------------------------------------------------------------------------

REQUIRED_MODULES = (("httpx", "httpx"), ("psutil", "psutil"), ("yaml", "pyyaml"))


def missing_modules() -> list[str]:
    missing = []
    for module, package in REQUIRED_MODULES:
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


def check_deps(auto_install: bool = True) -> bool:
    """检查依赖，缺失时询问是否安装。"""
    missing = missing_modules()
    if not missing:
        return True

    err(f"缺少依赖: {', '.join(missing)}")
    if not auto_install or not sys.stdin.isatty():
        info(f"请执行: {sys.executable} -m pip install -r {REQUIREMENTS.name}")
        return False

    if ask("现在安装? (Y/n)", "y").lower() not in ("y", "yes"):
        return False

    cmd = [sys.executable, "-m", "pip", "install", *missing]
    if REQUIREMENTS.exists():
        cmd = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    info("执行: " + " ".join(cmd))
    try:
        code = subprocess.call(cmd)
    except Exception as e:
        err(f"安装失败: {e}")
        return False

    if code != 0:
        err("安装失败，请手动执行上面的命令")
        return False

    still = missing_modules()
    if still:
        err(f"安装后仍缺少: {', '.join(still)}（可能需要重启终端）")
        return False
    ok("依赖已安装")
    return True


# --------------------------------------------------------------------------
# 进程
# --------------------------------------------------------------------------


def find_pythonw() -> str:
    """优先用 pythonw.exe，实现无窗口后台运行。"""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if candidate.exists():
        return str(candidate)
    found = shutil.which("pythonw")
    return found or str(exe)


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
    except ImportError:
        return False
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False


def find_server_processes() -> list:
    """查找由本目录启动的辅助程序进程。"""
    try:
        import psutil
    except ImportError:
        return []

    here = str(HERE).lower()
    result = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            joined = " ".join(cmdline).lower()
            if "server.py" not in joined:
                continue
            matched = here in joined
            if not matched:
                try:
                    matched = str(proc.cwd()).lower() == here
                except Exception:
                    matched = False
            if matched:
                result.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    return result


def running_pid() -> int | None:
    pid = None
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            pid = None

    if pid and _pid_alive(pid):
        return pid

    procs = find_server_processes()
    if procs:
        pid = procs[0].pid
        PID_FILE.write_text(str(pid), encoding="utf-8")
        return pid

    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except Exception:
            pass
    return None


def start_foreground() -> int:
    """前台启动，日志直接打到控制台。"""
    if not ensure_config() or not check_deps():
        return 1

    if running_pid():
        warn("检测到辅助程序已在后台运行，前台再启动会重复连接")
        if ask("继续? (y/N)", "n").lower() not in ("y", "yes"):
            return 0

    print(f"\n  {LINE}\n  辅助程序启动中（Ctrl+C 停止）\n  {LINE}\n")
    try:
        return subprocess.call(
            [sys.executable, str(SERVER), "-c", str(CONFIG)], cwd=str(HERE)
        )
    except KeyboardInterrupt:
        print("\n  已停止")
        return 0
    except Exception as e:
        err(f"启动失败: {e}")
        return 1


def start_daemon() -> bool:
    """后台启动，无窗口运行。"""
    if not ensure_config() or not check_deps():
        return False

    pid = running_pid()
    if pid:
        warn(f"辅助程序已在运行 (PID {pid})")
        return True

    pythonw = find_pythonw()
    kwargs = {
        "cwd": str(HERE),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(
            [pythonw, str(SERVER), "-c", str(CONFIG)], **kwargs
        )
    except Exception as e:
        err(f"后台启动失败: {e}")
        return False

    time.sleep(1.5)
    if proc.poll() is not None:
        err(f"辅助程序启动后立即退出（返回码 {proc.returncode}），请用前台模式查看原因")
        return False

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    ok(f"已后台启动 (PID {proc.pid})")
    info(f"日志: {LOG_FILE.name}")
    return True


def stop_daemon() -> bool:
    """停止后台运行的辅助程序（只结束 server.py，不动 BetterGI 本体）。"""
    try:
        import psutil
    except ImportError:
        err("缺少 psutil，无法精确结束进程")
        return False

    procs = find_server_processes()
    pid = running_pid()
    targets = {p.pid for p in procs}
    if pid:
        targets.add(pid)

    if not targets:
        warn("未发现运行中的辅助程序")
        return False

    terminated = 0
    for target in targets:
        try:
            p = psutil.Process(target)
            p.terminate()
            terminated += 1
        except psutil.NoSuchProcess:
            continue
        except Exception as e:
            err(f"结束进程 {target} 失败: {e}")
            continue

    alive_pids = [t for t in targets if psutil.pid_exists(t)]
    if alive_pids:
        _, alive = psutil.wait_procs(
            [psutil.Process(t) for t in alive_pids], timeout=5
        )
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass

    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except Exception:
            pass

    if terminated:
        ok(f"已停止 {terminated} 个辅助程序进程")
        return True

    warn("进程已退出，已清理 PID 记录")
    return True


# --------------------------------------------------------------------------
# 开机自启
# --------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 20):
    """执行系统命令。失败（含命令被安全策略拦截）时返回 None，由调用方决定提示。"""
    kwargs: dict = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        kwargs.update({"text": True, "encoding": "mbcs", "errors": "replace"})
    else:
        kwargs.update({"text": True, "errors": "replace"})
    try:
        return subprocess.run(cmd, **kwargs)
    except Exception:
        return None


def _autostart_command() -> str:
    return f'"{find_pythonw()}" "{SERVER}" -c "{CONFIG}"'


def _task_exists() -> bool:
    if os.name != "nt":
        return False
    result = _run(["schtasks", "/Query", "/TN", TASK_NAME])
    return bool(result and result.returncode == 0)


def _registry_value() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, TASK_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _registry_set(value: str) -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, TASK_NAME, 0, winreg.REG_SZ, value)
        return True
    except Exception as e:
        err(f"写入注册表失败: {e}")
        return False


def _registry_delete() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, TASK_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def autostart_state() -> dict:
    return {"task": _task_exists(), "registry": _registry_value() is not None}


def install_autostart() -> bool:
    """注册开机自启：优先计划任务（可最高权限），失败回退注册表 Run。"""
    if os.name != "nt":
        err("开机自启仅支持 Windows")
        return False

    command = _autostart_command()
    info(f"启动命令: {command}")

    result = _run(
        [
            "schtasks", "/Create",
            "/TN", TASK_NAME,
            "/TR", command,
            "/SC", "ONLOGON",
            "/RL", "HIGHEST",
            "/F",
        ]
    )
    if result and result.returncode == 0:
        # 计划任务成功后清掉可能残留的注册表项，避免两处同时启动
        if _registry_value() is not None:
            _registry_delete()
        ok("已注册开机自启（计划任务），登录后自动后台运行")
        return True

    detail = (result.stderr or result.stdout or "").strip() if result else ""
    if detail:
        warn(f"计划任务创建失败: {detail.splitlines()[0]}")
    warn("改为写入当前用户注册表 Run 项（无需管理员权限）")

    if _registry_set(command):
        ok("已注册开机自启（注册表），登录后自动后台运行")
        return True

    err("注册开机自启失败")
    return False


def uninstall_autostart() -> bool:
    if os.name != "nt":
        err("开机自启仅支持 Windows")
        return False

    removed = False
    if _task_exists():
        result = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        if result and result.returncode == 0:
            removed = True
        else:
            err("删除计划任务失败，可能需要管理员权限")

    if _registry_value() is not None:
        if _registry_delete():
            removed = True

    if removed:
        ok("已取消开机自启")
    else:
        warn("没有找到已注册的开机自启")
    return removed


def print_autostart_status() -> None:
    state = autostart_state()
    print(f"\n{LINE}\n  开机自启状态\n{LINE}")
    info(f"计划任务 {TASK_NAME}: {'已注册' if state['task'] else '未注册'}")
    info(f"注册表 Run 项: {'已注册' if state['registry'] else '未注册'}")

    if state["task"]:
        result = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"])
        if result and result.returncode == 0:
            for line in result.stdout.splitlines():
                if re.search(r"(状态|上次运行|任务名|Status|Last Run)", line, re.I):
                    info(line.strip())
    if state["registry"]:
        info(f"注册表命令: {_registry_value()}")
    print()


# --------------------------------------------------------------------------
# 连接测试 / 日志
# --------------------------------------------------------------------------


def test_connection(cfg: dict | None = None) -> bool:
    """调用插件的健康检查接口，验证地址与密钥。"""
    cfg = cfg if cfg is not None else load_config()
    url = str(cfg.get("astrbot_url", "")).rstrip("/")
    key = str(cfg.get("api_key", ""))

    print(f"\n{LINE}\n  测试连接 AstrBot\n{LINE}")
    if not url:
        err("配置文件中 astrbot_url 为空")
        return False
    if not key:
        err("配置文件中 api_key 为空")
        info("请在 AstrBot 设置→OpenAPI 创建密钥（只勾选 plugin 权限，有效期永久）")
        return False

    endpoint = f"{url}{HEALTH_PATH}"
    info(f"地址: {endpoint}")
    info(f"密钥: {mask_key(key)}")

    try:
        import httpx
    except ImportError:
        err("缺少 httpx，请先安装依赖")
        return False

    headers = {"Authorization": f"ApiKey {key}"}
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.get(endpoint, headers=headers)
    except httpx.ConnectError:
        err("连接失败：无法建立 TCP 连接")
        info("1. AstrBot 是否已启动")
        info("2. astrbot_url 地址和端口是否正确")
        info("3. 防火墙 / 网络是否放行")
        return False
    except httpx.TimeoutException:
        err("连接超时：对端无响应")
        info("检查地址是否正确、AstrBot 是否卡住")
        return False
    except Exception as e:
        err(f"请求失败: {e}")
        return False

    if resp.status_code == 200:
        ok("连接成功")
        try:
            data = resp.json()
            info(f"插件状态: {data.get('status')}")
            info(f"辅助程序连接: {'已连接' if data.get('connected') else '未连接'}")
        except Exception:
            info(f"响应: {resp.text[:200]}")
        return True

    if resp.status_code in (401, 403):
        err(f"认证失败 (HTTP {resp.status_code})")
        info("1. api_key 是否复制完整（abk_ 开头）")
        info("2. 密钥是否勾选了 plugin 权限")
        info("3. 密钥是否已过期或被删除")
        return False

    if resp.status_code == 404:
        err("接口不存在 (HTTP 404)")
        info("1. 插件是否已加载并启用")
        info("2. 插件版本是否为 2.1.x（远程模式使用 SSE 接口）")
        return False

    err(f"响应异常 (HTTP {resp.status_code}): {resp.text[:200]}")
    return False


def show_log(lines: int = 40) -> None:
    print(f"\n{LINE}\n  最近 {lines} 行日志（{LOG_FILE.name}）\n{LINE}")
    if not LOG_FILE.exists():
        warn("日志文件不存在，辅助程序可能还没运行过")
        return
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        err(f"读取日志失败: {e}")
        return

    tail = content.splitlines()[-lines:]
    if not tail:
        warn("日志为空")
        return
    for line in tail:
        print(" ", line)
    print()


# --------------------------------------------------------------------------
# 菜单
# --------------------------------------------------------------------------


def print_summary() -> None:
    cfg = load_config()
    url = str(cfg.get("astrbot_url") or "未配置")
    gi_dir = str(cfg.get("bettergi_dir") or "未配置")
    key = mask_key(str(cfg.get("api_key") or ""))

    pid = running_pid()
    process = f"运行中 (PID {pid})" if pid else "未运行"

    state = autostart_state()
    marks = []
    if state["task"]:
        marks.append("计划任务")
    if state["registry"]:
        marks.append("注册表")
    autostart = " / ".join(marks) if marks else "未注册"

    print(f"\n{LINE}")
    print("  BetterGI 远程辅助程序（客户端模式）")
    print(LINE)
    print(f"  AstrBot 地址 : {url}")
    print(f"  BetterGI 目录: {gi_dir}")
    print(f"  API 密钥     : {key}")
    print(f"  后台进程     : {process}")
    print(f"  开机自启     : {autostart}")
    print(LINE)


MENU = [
    ("1", "启动辅助程序（前台，Ctrl+C 停止）"),
    ("2", "后台启动（无窗口）"),
    ("3", "停止后台运行的辅助程序"),
    ("4", "注册开机自启"),
    ("5", "取消开机自启"),
    ("6", "查看自启状态"),
    ("7", "测试连接 AstrBot"),
    ("8", "修改配置"),
    ("9", "查看运行日志"),
    ("0", "退出"),
]


def menu() -> int:
    while True:
        print_summary()
        for key, text in MENU:
            print(f"  {key}. {text}")
        print()

        try:
            choice = input("  请选择操作: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice in ("0", "q", "quit", "exit"):
            return 0
        elif choice == "1":
            start_foreground()
            pause()
        elif choice == "2":
            start_daemon()
            pause()
        elif choice == "3":
            stop_daemon()
            pause()
        elif choice == "4":
            install_autostart()
            pause()
        elif choice == "5":
            uninstall_autostart()
            pause()
        elif choice == "6":
            print_autostart_status()
            pause()
        elif choice == "7":
            test_connection()
            pause()
        elif choice == "8":
            edit_config_wizard()
            pause()
        elif choice == "9":
            show_log()
            pause()
        else:
            warn("无效的选项")


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def print_status() -> None:
    print_summary()
    print_autostart_status()
    cfg = load_config()
    problems = config_problems(cfg)
    print(f"{LINE}")
    if problems:
        print("  配置检查: 未通过")
        for p in problems:
            err(p)
    else:
        print("  配置检查: 通过")
    missing = missing_modules()
    print(f"  依赖检查: {'缺少 ' + ', '.join(missing) if missing else '通过'}")
    print(LINE)


def main(argv: list[str] | None = None) -> int:
    setup_console()

    parser = argparse.ArgumentParser(
        description="BetterGI 远程辅助程序启动器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "不带参数运行进入交互菜单。\n"
            "示例:\n"
            "  python launcher.py --start\n"
            "  python launcher.py --daemon\n"
            "  python launcher.py --test"
        ),
    )
    parser.add_argument("--start", action="store_true", help="前台启动辅助程序")
    parser.add_argument("--daemon", action="store_true", help="后台启动辅助程序")
    parser.add_argument("--stop", action="store_true", help="停止后台运行的辅助程序")
    parser.add_argument("--install", action="store_true", help="注册开机自启")
    parser.add_argument("--uninstall", action="store_true", help="取消开机自启")
    parser.add_argument("--status", action="store_true", help="查看配置、进程与自启状态")
    parser.add_argument("--test", action="store_true", help="测试连接 AstrBot")
    parser.add_argument("--config", action="store_true", help="交互式修改配置")
    parser.add_argument("--log", action="store_true", help="查看最近日志")
    args = parser.parse_args(argv)

    if args.start:
        return start_foreground()
    if args.daemon:
        return 0 if start_daemon() else 1
    if args.stop:
        return 0 if stop_daemon() else 1
    if args.install:
        return 0 if install_autostart() else 1
    if args.uninstall:
        return 0 if uninstall_autostart() else 1
    if args.test:
        return 0 if test_connection() else 1
    if args.status:
        print_status()
        return 0
    if args.config:
        return 0 if edit_config_wizard() else 1
    if args.log:
        show_log(80)
        return 0

    return menu()


if __name__ == "__main__":
    sys.exit(main())

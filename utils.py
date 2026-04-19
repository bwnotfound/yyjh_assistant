"""
utils.py - MuMu 模拟器自动化工具（归一化坐标系 + MuMuManager 数据源版本）

====== 坐标系约定 ======
对外接口一律使用**归一化坐标** (x, y)，范围 [0, 1]，参考系为
render_wnd 的客户区（= 纯游戏渲染区，不含 Windows 装饰、不含壁纸填充）。

    - Mumu.click(pos)         : pos ∈ [0,1]² → × 设备分辨率 → adb tap
    - Mumu.capture_window()   : 返回 render_wnd 的 PIL.Image
    - Mumu.is_color_similar() : pos × img.size → 像素色判
    - Mumu.crop_img()         : 同上
    - Mumu.norm_to_image()    : 归一化 → 图像像素

====== 数据分层 ======
    MumuInstall  : 磁盘安装位置（adb.exe, MuMuManager.exe 路径），一次解析
    MumuInfo     : 运行时状态（adb_port, render_wnd, is_android_started ...），随启停刷新
    Mumu         : 抽象自动化接口，组合上述两者

====== 未迁移代码 ======
OCR / Executor / wait_pos_change / move_seq_* 保留在文件末尾 LEGACY 区块，
内部硬编码像素坐标不适用于新接口，等后续整合到 config/common/ 时统一迁移。
"""

from __future__ import annotations

import ctypes
import json
import logging
import queue
import re
import subprocess
import threading
import time
import uuid
from ctypes import windll
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
import win32con
import win32gui
import win32ui
from PIL import Image
from skimage.metrics import structural_similarity


# =============================================================================
# 异常
# =============================================================================


class MumuError(RuntimeError):
    """MuMu 相关错误基类"""


class MumuNotInstalledError(MumuError):
    """找不到 MuMu 安装目录或关键可执行文件"""


class MumuNotRunningError(MumuError):
    """模拟器未启动 / 未就绪"""


class MumuInfoError(MumuError):
    """MuMuManager info 调用或解析失败"""


# =============================================================================
# Windows API helpers
# =============================================================================


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def get_dpi_scale(hwnd: int) -> float:
    """返回窗口所在显示器的 DPI 缩放系数（1.0=100%, 1.25=125%, 1.5=150% ...）"""
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def get_client_size_logical(hwnd: int) -> Tuple[int, int]:
    """客户区尺寸（逻辑像素，不乘 DPI）"""
    rect = RECT()
    ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
    return rect.right, rect.bottom


# =============================================================================
# SyncInteractiveSession（保留，用于长连 adb shell）
# =============================================================================


class SyncInteractiveSession:
    def __init__(self, cmd, encoding="gbk", read_interval=0.001):
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
            encoding=encoding,
            errors="replace",
        )
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop_reader = threading.Event()
        self._reader = threading.Thread(
            target=self._reader_thread, args=(read_interval,), daemon=True
        )
        self._reader.start()

    def _reader_thread(self, interval: float):
        while not self._stop_reader.is_set():
            line = self._proc.stdout.readline()
            if line == "" and self._proc.poll() is not None:
                break
            if line:
                self._queue.put(line)
            else:
                time.sleep(interval)

    def send_command(self, command: str, timeout: float = 999) -> str:
        marker = f"END_{uuid.uuid4().hex}"
        full_cmd = f"{command} && echo {marker} || echo {marker}"
        self._proc.stdin.write(full_cmd + "\n")
        self._proc.stdin.flush()

        lines: list[str] = []
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            try:
                line = self._queue.get(timeout=1)
            except queue.Empty:
                break
            stripped = line.rstrip("\r\n").strip()
            if stripped == marker:
                break
            lines.append(stripped)
        else:
            raise TimeoutError(f"等待命令“{command}”超时 {timeout} 秒")

        if lines and lines[0].strip() == command:
            return "\n".join(lines[1:])
        return "\n".join(lines)

    def close(self):
        self._stop_reader.set()
        try:
            self._proc.stdin.write("exit\nexit\n")
            self._proc.stdin.flush()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()


# =============================================================================
# 安装目录探测 + MumuInstall
# =============================================================================

# 默认相对路径（相对 MuMu 安装根）；新版 MuMu 把两个 exe 都放在 nx_main/ 下
DEFAULT_ADB_RELATIVE_PATH = "nx_main/adb.exe"
DEFAULT_MANAGER_RELATIVE_PATH = "nx_main/MuMuManager.exe"

MUMU_UNINSTALL_REG_SUBKEY = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MuMuPlayer"
)

# subprocess flag: 隐藏 MuMuManager 启动时的黑窗
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _find_mumu_install_root_from_registry() -> Optional[Path]:
    """
    读 HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\MuMuPlayer
    的 UninstallString，返回其所在目录。失败返回 None。
    同时尝试 64/32 位视图兼容 WOW64。
    """
    try:
        import winreg
    except ImportError:
        return None

    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY, 0):
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                MUMU_UNINSTALL_REG_SUBKEY,
                0,
                winreg.KEY_READ | view,
            ) as hkey:
                val, _ = winreg.QueryValueEx(hkey, "UninstallString")
        except OSError:
            continue
        val = (val or "").strip().strip('"').strip("'")
        if not val:
            continue
        root = Path(val).parent
        if root.is_dir():
            return root
    return None


@dataclass(frozen=True)
class MumuInstall:
    """MuMu 安装在本机的位置信息。一次解析、不可变。"""

    root: Path
    adb_exe: Path
    manager_exe: Path

    @classmethod
    def locate(
        cls,
        root: Optional[Path | str] = None,
        adb_rel: str = DEFAULT_ADB_RELATIVE_PATH,
        manager_rel: str = DEFAULT_MANAGER_RELATIVE_PATH,
        search_up_levels: int = 4,
    ) -> "MumuInstall":
        """
        解析 MuMu 安装位置。root 三种形态:
          - None      → 注册表自动探测
          - 目录路径   → 直接使用
          - .exe 路径  → 从文件所在目录向上最多 search_up_levels 级查找，
                        命中 <candidate>/<adb_rel> 的那一级即为根
        """
        if root is None:
            found = _find_mumu_install_root_from_registry()
            if found is None:
                raise MumuNotInstalledError(
                    "未显式指定 root，且从注册表 "
                    f"HKLM\\{MUMU_UNINSTALL_REG_SUBKEY} 读取 UninstallString 失败。"
                    "请手动传入 MuMu 安装根目录。"
                )
            logging.info("从注册表探测到 MuMu 安装根目录: %s", found)
            root_path = found
        else:
            root_path = Path(root)
            if root_path.is_file():
                cur = root_path.parent
                hit = False
                for _ in range(max(1, search_up_levels)):
                    if (cur / adb_rel).is_file():
                        root_path = cur
                        hit = True
                        break
                    if cur.parent == cur:
                        break
                    cur = cur.parent
                if not hit:
                    root_path = root_path.parent  # 让后续存在性检查给出明确报错
            elif not root_path.is_dir():
                raise MumuNotInstalledError(f"root 既非目录也非文件: {root_path!r}")

        adb_exe = (root_path / adb_rel).resolve()
        manager_exe = (root_path / manager_rel).resolve()
        if not adb_exe.is_file():
            raise MumuNotInstalledError(
                f"未找到 adb.exe: {adb_exe}\n"
                f"MuMu 安装根目录={root_path!r}，adb_rel={adb_rel!r}"
            )
        if not manager_exe.is_file():
            raise MumuNotInstalledError(
                f"未找到 MuMuManager.exe: {manager_exe}\n"
                f"manager_rel={manager_rel!r}"
            )
        return cls(root=root_path.resolve(), adb_exe=adb_exe, manager_exe=manager_exe)


# =============================================================================
# MuMuManager info 查询 + MumuInfo
# =============================================================================


def _parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _parse_hwnd(s) -> Optional[int]:
    if s is None or s == "":
        return None
    if isinstance(s, int):
        return s
    try:
        return int(str(s), 16)
    except ValueError:
        return None


def _parse_int(s) -> Optional[int]:
    if s is None or s == "":
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class MumuInfo:
    """MuMuManager info 返回的运行时状态切片。"""

    index: int
    is_process_started: bool
    is_android_started: bool
    adb_host_ip: Optional[str]
    adb_port: Optional[int]
    main_wnd: Optional[int]  # 解析后的 HWND 整数
    render_wnd: Optional[int]
    player_state: Optional[str]
    name: Optional[str]
    raw: dict = field(repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "MumuInfo":
        return cls(
            index=_parse_int(d.get("index")) or 0,
            is_process_started=_parse_bool(d.get("is_process_started", False)),
            is_android_started=_parse_bool(d.get("is_android_started", False)),
            adb_host_ip=(d.get("adb_host_ip") or None),
            adb_port=_parse_int(d.get("adb_port")),
            main_wnd=_parse_hwnd(d.get("main_wnd")),
            render_wnd=_parse_hwnd(d.get("render_wnd")),
            player_state=(d.get("player_state") or None),
            name=(d.get("name") or None),
            raw=d,
        )


def query_mumu_info(
    manager_exe: Path,
    vm_index: int,
    timeout: float = 10.0,
    encoding: str = "utf-8",
) -> MumuInfo:
    """调用 MuMuManager info -v <vm_index>，解析并返回 MumuInfo。"""
    cmd = [str(manager_exe), "info", "-v", str(vm_index)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as e:
        raise MumuInfoError(f"MuMuManager info 超时 ({timeout}s): {cmd}") from e
    except FileNotFoundError as e:
        raise MumuInfoError(f"MuMuManager.exe 不存在: {manager_exe}") from e

    stdout = result.stdout.decode(encoding, errors="replace")
    stderr = result.stderr.decode(encoding, errors="replace")

    if result.returncode != 0:
        raise MumuInfoError(
            f"MuMuManager info 失败 rc={result.returncode}\n"
            f"stdout: {stdout[:500]}\nstderr: {stderr[:500]}"
        )
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise MumuInfoError(f"MuMuManager info 输出非法 JSON: {stdout[:500]}") from e

    if not isinstance(data, dict):
        raise MumuInfoError(f"MuMuManager info 返回非 dict: {type(data).__name__}")

    # 兼容被 index 包裹的形态: {"0": {...}}
    if (
        "index" not in data
        and str(vm_index) in data
        and isinstance(data[str(vm_index)], dict)
    ):
        data = data[str(vm_index)]

    return MumuInfo.from_dict(data)


# =============================================================================
# Mumu 核心类
# =============================================================================


class Mumu:
    """
    MuMu 模拟器自动化入口。使用归一化坐标 [0,1]²。

    典型用法:
        with Mumu() as mumu:
            img = mumu.capture_window()
            mumu.click((0.5, 0.5))
    """

    def __init__(
        self,
        install: Optional[MumuInstall] = None,
        install_root: Optional[Path | str] = None,
        adb_rel: str = DEFAULT_ADB_RELATIVE_PATH,
        manager_rel: str = DEFAULT_MANAGER_RELATIVE_PATH,
        vm_index: int = 0,
        info_timeout: float = 10.0,
        device_resolution: Optional[Tuple[int, int]] = None,
        fallback_resolution: Tuple[int, int] = (1920, 1080),
        norm_out_of_range_policy: str = "warn_clip",
    ):
        self.install: MumuInstall = install or MumuInstall.locate(
            root=install_root, adb_rel=adb_rel, manager_rel=manager_rel
        )
        self.vm_index = vm_index
        self.info_timeout = info_timeout
        self.norm_policy = norm_out_of_range_policy
        self.fallback_resolution = fallback_resolution

        self.info: MumuInfo = self._query_info(require_running=True)

        self._adb_shell: Optional[SyncInteractiveSession] = None
        self._open_adb_shell()

        if device_resolution is not None:
            self.device_w, self.device_h = device_resolution
        else:
            self.device_w, self.device_h = self._probe_device_resolution()
        self.device_aspect = self.device_w / self.device_h

        logging.info(
            "Mumu 就绪: vm=%d, adb=%s:%d, render_wnd=0x%X, device=%dx%d",
            self.vm_index,
            self.info.adb_host_ip or "?",
            self.info.adb_port or 0,
            self.info.render_wnd or 0,
            self.device_w,
            self.device_h,
        )

    # ---------------- 数据源：MuMuManager info ----------------

    def _query_info(self, require_running: bool) -> MumuInfo:
        info = query_mumu_info(
            self.install.manager_exe, self.vm_index, self.info_timeout
        )
        if require_running:
            self._assert_running(info)
        return info

    @staticmethod
    def _assert_running(info: MumuInfo) -> None:
        if not info.is_process_started:
            raise MumuNotRunningError(
                f"vm={info.index} 外壳进程未启动 (player_state={info.player_state!r})"
            )
        if not info.is_android_started:
            raise MumuNotRunningError(
                f"vm={info.index} 安卓未启动 (player_state={info.player_state!r})"
            )
        if info.render_wnd is None:
            raise MumuNotRunningError(f"vm={info.index} 未获得 render_wnd")
        if info.adb_port is None:
            raise MumuNotRunningError(f"vm={info.index} 未获得 adb_port")

    def refresh_info(self, require_running: bool = True) -> MumuInfo:
        """重刷 MuMuManager info。adb endpoint 变化会自动重连 shell。"""
        logging.info("刷新 Mumu info (vm=%d)", self.vm_index)
        new_info = self._query_info(require_running=require_running)

        adb_changed = (
            new_info.adb_host_ip != self.info.adb_host_ip
            or new_info.adb_port != self.info.adb_port
        )
        self.info = new_info

        if adb_changed:
            logging.info("adb endpoint 变更 → 重连 adb shell")
            self._close_adb_shell()
            self._open_adb_shell()
        return new_info

    # ---------------- 便捷属性 ----------------

    @property
    def hwnd(self) -> int:
        assert self.info.render_wnd is not None
        return self.info.render_wnd

    @property
    def main_hwnd(self) -> Optional[int]:
        return self.info.main_wnd

    @property
    def adb_port(self) -> int:
        assert self.info.adb_port is not None
        return self.info.adb_port

    @property
    def adb_host(self) -> str:
        return self.info.adb_host_ip or "127.0.0.1"

    # ---------------- 自愈：窗口/adb 失效兜底 ----------------

    def _ensure_hwnd_alive(self, retry_once: bool = True) -> None:
        if win32gui.IsWindow(self.hwnd):
            return
        if not retry_once:
            raise MumuNotRunningError(
                f"render_wnd=0x{self.hwnd:X} 已失效且刷新后仍不可用"
            )
        logging.warning("render_wnd 失效，尝试 refresh_info 恢复")
        self.refresh_info()
        self._ensure_hwnd_alive(retry_once=False)

    # ---------------- adb shell ----------------

    def _open_adb_shell(self) -> None:
        self._adb_shell = SyncInteractiveSession(["cmd"], encoding="gbk")
        adb = f'"{self.install.adb_exe}"'
        endpoint = f"{self.adb_host}:{self.adb_port}"
        self._adb_shell.send_command(f"{adb} connect {endpoint}")
        self._adb_shell.send_command(f"{adb} -s {endpoint} shell")

    def _close_adb_shell(self) -> None:
        if self._adb_shell is not None:
            try:
                self._adb_shell.close()
            except Exception:
                pass
            self._adb_shell = None

    def init_adb_shell(self) -> None:
        """保留旧名：重建 adb shell 长连"""
        self._close_adb_shell()
        self._open_adb_shell()

    def run_command(self, command: list[str]) -> str:
        """在 adb shell 内执行命令（首元素为 'shell' 会被剥掉，兼容旧调用）"""
        if command and command[0] == "shell":
            command = command[1:]
        assert self._adb_shell is not None
        return self._adb_shell.send_command(" ".join(command))

    def _probe_device_resolution(self) -> Tuple[int, int]:
        """
        探测 adb tap 使用的坐标系尺寸：即 display 当前方向尺寸。

        步骤:
            1. `wm size` 拿自然方向 (nat_w, nat_h)
            2. `dumpsys` 拿当前 rotation，{1,3} 则交换得当前方向尺寸
            3. 用 render_wnd 客户区宽高比做 sanity check，和探测结果方向不一致则翻转
        """
        nat = self._probe_natural_size()
        if nat is None:
            logging.warning("wm size 失败，使用 fallback %s", self.fallback_resolution)
            return self.fallback_resolution
        nat_w, nat_h = nat

        rotation = self._probe_display_rotation()  # 0/1/2/3 或 None
        if rotation in (1, 3):
            cur_w, cur_h = nat_h, nat_w
        else:
            cur_w, cur_h = nat_w, nat_h
        if rotation is None:
            logging.info(
                "未能探测 display rotation，按自然方向 %dx%d 使用；若方向不对请在 MuMu 里检查设置",
                cur_w,
                cur_h,
            )

        # Sanity check: render_wnd 客户区方向应和 device 当前方向一致
        try:
            cw, ch = get_client_size_logical(self.hwnd)
            if cw > 0 and ch > 0:
                client_landscape = cw > ch
                device_landscape = cur_w > cur_h
                if client_landscape != device_landscape:
                    logging.warning(
                        "render_wnd %dx%d 与 device %dx%d 方向不一致（rotation=%s），"
                        "翻转 device w/h 作为 adb tap 坐标系",
                        cw,
                        ch,
                        cur_w,
                        cur_h,
                        rotation,
                    )
                    cur_w, cur_h = cur_h, cur_w
        except Exception as e:
            logging.warning("render_wnd sanity check 失败: %s", e)

        logging.info(
            "device 方向判定: 自然 %dx%d, rotation=%s, 当前 %dx%d",
            nat_w,
            nat_h,
            rotation,
            cur_w,
            cur_h,
        )
        return cur_w, cur_h

    def _probe_natural_size(self) -> Optional[Tuple[int, int]]:
        """wm size 返回的自然方向尺寸"""
        try:
            out = self.run_command(["shell", "wm", "size"])
            override = re.search(r"Override size:\s*(\d+)\s*x\s*(\d+)", out)
            physical = re.search(r"Physical size:\s*(\d+)\s*x\s*(\d+)", out)
            m = override or physical or re.search(r"(\d+)\s*x\s*(\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
            logging.warning("wm size 返回解析失败: %r", out)
        except Exception as e:
            logging.warning("wm size 执行失败: %s", e)
        return None

    def _probe_display_rotation(self) -> Optional[int]:
        """当前 display 旋转，返回 0/1/2/3 或 None"""
        # 优先 dumpsys input (格式稳定)
        try:
            out = self.run_command(["shell", "dumpsys", "input"])
            m = re.search(r"SurfaceOrientation:\s*(\d)", out)
            if m:
                return int(m.group(1))
        except Exception as e:
            logging.debug("dumpsys input 失败: %s", e)
        # 备选 dumpsys display
        try:
            out = self.run_command(["shell", "dumpsys", "display"])
            m = re.search(r"mRotation=(\d)", out)
            if m:
                return int(m.group(1))
        except Exception as e:
            logging.debug("dumpsys display 失败: %s", e)
        return None

    # ---------------- 归一化坐标 ----------------

    def _check_norm(
        self, pos: Tuple[float, float], name: str = "pos"
    ) -> Tuple[float, float]:
        x, y = pos
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            return x, y
        msg = f"{name}={pos} 超出归一化范围 [0,1]²"
        if self.norm_policy == "raise":
            raise ValueError(msg)
        if self.norm_policy == "warn_clip":
            logging.warning(msg)
        return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))

    def norm_to_image(
        self, pos: Tuple[float, float], img: Image.Image
    ) -> Tuple[int, int]:
        nx, ny = self._check_norm(pos, "norm_to_image.pos")
        w, h = img.size
        return int(nx * w), int(ny * h)

    def global_pos_to_game_window_pos(self, pos, img):
        """旧别名，语义同 norm_to_image"""
        return self.norm_to_image(pos, img)

    # ---------------- 点击 ----------------

    def click(self, pos: Tuple[float, float], delay: float = 0) -> bool:
        t0 = time.perf_counter()
        nx, ny = self._check_norm(pos, "click.pos")
        x = max(0, min(self.device_w - 1, int(nx * self.device_w)))
        y = max(0, min(self.device_h - 1, int(ny * self.device_h)))
        self.run_command(["shell", "input", "tap", str(x), str(y)])
        if delay > 0:
            remaining = delay - (time.perf_counter() - t0)
            if remaining > 0:
                time.sleep(remaining)
        return True

    # ---------------- 窗口 ----------------

    def bring_window_back(self) -> None:
        """若主窗口最小化则恢复（不抢焦点）。render_wnd 是子窗口无需单独处理。"""
        main = self.main_hwnd
        if not main or not win32gui.IsWindow(main):
            return
        while win32gui.IsIconic(main):
            win32gui.ShowWindow(main, win32con.SW_RESTORE)
            win32gui.SetWindowPos(
                main,
                win32con.HWND_BOTTOM,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
            time.sleep(0.2)

    # ---------------- 截图 ----------------

    def capture_window(self, delay: float = 0) -> Image.Image:
        """对 render_wnd 做 PrintWindow，得到纯游戏区截图。"""
        t0 = time.perf_counter()
        self.bring_window_back()
        self._ensure_hwnd_alive()

        hwnd = self.hwnd
        w_log, h_log = get_client_size_logical(hwnd)
        if w_log <= 0 or h_log <= 0:
            logging.warning(
                "render_wnd 客户区尺寸 %dx%d 异常，刷新 info 重试", w_log, h_log
            )
            self.refresh_info()
            hwnd = self.hwnd
            w_log, h_log = get_client_size_logical(hwnd)
            if w_log <= 0 or h_log <= 0:
                raise MumuNotRunningError(f"render_wnd 客户区尺寸非法: {w_log}x{h_log}")

        dpi = get_dpi_scale(hwnd)
        phys_w = max(1, int(round(w_log * dpi)))
        phys_h = max(1, int(round(h_log * dpi)))

        # PW_CLIENTONLY=1 | PW_RENDERFULLCONTENT=2
        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        saveBitMap = win32ui.CreateBitmap()
        try:
            saveBitMap.CreateCompatibleBitmap(mfcDC, phys_w, phys_h)
            saveDC.SelectObject(saveBitMap)
            windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 3)
            bmpinfo = saveBitMap.GetInfo()
            bmpstr = saveBitMap.GetBitmapBits(True)
            im = Image.frombuffer(
                "RGB",
                (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
                bmpstr,
                "raw",
                "BGRX",
                0,
                1,
            )
        finally:
            win32gui.DeleteObject(saveBitMap.GetHandle())
            saveDC.DeleteDC()
            mfcDC.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwndDC)

        if delay > 0:
            remaining = delay - (time.perf_counter() - t0)
            if remaining > 0:
                time.sleep(remaining)
        return im

    # ---------------- 图像操作 ----------------

    def is_color_similar(
        self,
        img: Image.Image,
        pos: Tuple[float, float],
        target_color: Tuple[int, int, int],
        threshold: int = 40,
    ) -> bool:
        x, y = self.norm_to_image(pos, img)
        color = img.getpixel((x, y))
        diff = sum(abs(color[i] - target_color[i]) for i in range(3))
        return diff <= threshold

    def crop_img(
        self,
        img: Image.Image,
        left_top: Tuple[float, float],
        right_bottom: Tuple[float, float],
    ) -> Image.Image:
        l, t = self.norm_to_image(left_top, img)
        r, b = self.norm_to_image(right_bottom, img)
        return img.crop((l, t, r, b))

    def diff_img(self, img1: Image.Image, img2: Image.Image) -> Optional[float]:
        if img1.size != img2.size:
            logging.warning("diff_img 尺寸不一致: %s vs %s", img1.size, img2.size)
            return None

        def _down(im: Image.Image) -> Image.Image:
            return im.resize(
                (int(im.size[0] * 0.25), int(im.size[1] * 0.25)),
                Image.Resampling.LANCZOS,
            )

        if img1.size[0] * img1.size[1] > 100 * 100:
            img1 = _down(img1)
            img2 = _down(img2)
        a = cv2.cvtColor(np.array(img1), cv2.COLOR_RGB2GRAY)
        b = cv2.cvtColor(np.array(img2), cv2.COLOR_RGB2GRAY)
        score, _ = structural_similarity(a, b, full=True)
        return 1 - score

    # ---------------- 资源清理 ----------------

    def close(self) -> None:
        self._close_adb_shell()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# =============================================================================
# ======================== LEGACY（待迁移） ====================================
# 保留结构便于 import，但基于旧像素坐标，新 Mumu 下会点偏 / 裁错。
# 等整合到 config/common/ 后统一迁移为归一化坐标。
# =============================================================================


class OCR:
    """TODO(migrate): chat_box_pos 仍为旧像素坐标"""

    def __init__(self, mumu: Mumu, ocr_mode: str = "cnocr"):
        assert ocr_mode in ("cnocr", "paddleocr")
        self.ocr_mode = ocr_mode
        if ocr_mode == "cnocr":
            from cnocr import CnOcr

            self.ocr = CnOcr(
                context="cuda",
                rec_model_name="scene-densenet_lite_136-gru",
            )
        else:
            from paddleocr import PaddleOCR

            self.ocr = PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device="gpu",
            )
        self.mumu = mumu
        self.chat_box_pos = (371, 219, 1291, 384)  # TODO(migrate): 迁为归一化

    def get_chat_image(self):
        img = self.mumu.capture_window()
        tl = self.mumu.global_pos_to_game_window_pos(
            (self.chat_box_pos[0], self.chat_box_pos[1]), img
        )
        br = self.mumu.global_pos_to_game_window_pos(
            (self.chat_box_pos[2], self.chat_box_pos[3]), img
        )
        return img.crop((tl[0], tl[1], br[0], br[1]))

    def get_text(self, img=None, join_text=True, threshold=0.4):
        if img is None:
            img = self.get_chat_image()
        if self.ocr_mode == "cnocr":
            result = self.ocr.ocr(img)
            if join_text:
                return "".join(d["text"] for d in result if d["score"] > threshold)
            return result
        cv2_img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        result = self.ocr.predict(cv2_img)[0]
        result = {"text": result["rec_texts"], "score": result["rec_scores"]}
        if join_text:
            return "".join(
                result["text"][i]
                for i in range(len(result["text"]))
                if result["score"][i] > threshold
            )
        return result


class Executor:
    """TODO(migrate): character_pos / block_* 为旧像素坐标"""

    def __init__(self, mumu: Mumu, vision_size: str = "小"):
        assert vision_size in ("小", "中", "大")
        self.character_pos = (848, 534)
        if vision_size == "小":
            self.block_width, self.block_height = 160, 81
            self.move_max_num = 8
        elif vision_size == "中":
            self.block_width, self.block_height = 202, 101
            self.move_max_num = 10
        else:
            raise NotImplementedError("大视野暂未实现")
        self.mumu = mumu
        self.cur_pos = None

    def set_cur_pos(self, pos):
        self.cur_pos = pos


# 模块级游戏逻辑参数（旧像素，待迁移）
LEGACY_CHARACTER_POS = (848, 534)
LEGACY_BLOCK_WIDTH = 160
LEGACY_BLOCK_HEIGHT = 80
LEGACY_MOVE_MAX_NUM = 8
LEGACY_VISION_DELTA_LIMIT = 8


def wait_screen_change(
    mumu: Mumu,
    reverse=False,
    threshold=0.03,
    max_wait_time=1,
    fps=5,
    raw_diff=False,
    crop_ratio=(0.03185, 0.08028, 0.8413, 0.9024),
):
    enter_time = time.perf_counter()
    last_img = None
    delay = 1 / fps
    last_time = time.perf_counter()
    while True:
        while True:
            d = delay - (time.perf_counter() - last_time)
            if d <= 0:
                break
            time.sleep(d)
        last_time = time.perf_counter()
        if not reverse and last_time - enter_time > max_wait_time:
            break
        img = mumu.capture_window()
        cs = (
            int(img.size[0] * crop_ratio[0]),
            int(img.size[1] * crop_ratio[1]),
            int(img.size[0] * crop_ratio[2]),
            int(img.size[1] * crop_ratio[3]),
        )
        img = img.crop(cs)
        if last_img is None:
            last_img = img
            continue
        if not raw_diff:
            diff_rate = mumu.diff_img(img, last_img)
        else:
            diff_cnt = 0
            step = 3
            for i in range(0, img.size[0], step):
                for j in range(0, img.size[1], step):
                    if (
                        not abs(
                            sum(img.getpixel((i, j))) - sum(last_img.getpixel((i, j)))
                        )
                        < 5
                    ):
                        diff_cnt += 1
            diff_cnt = diff_cnt * step**2
            diff_rate = diff_cnt / (img.size[0] * img.size[1])
        last_img = img
        if diff_rate is None:
            continue
        if reverse:
            if diff_rate > threshold:
                continue
            if diff_rate < 0.0001:
                continue
        else:
            if diff_rate < threshold:
                continue
        break


def wait_pos_change(
    mumu: Mumu,
    reverse=False,
    threshold=0.02,
    max_wait_time=1,
    fps=5,
    img=None,
    pos_region=None,
):
    """TODO(migrate): pos_region 未传时使用旧 1920×1030 参考的近似值兜底"""
    if pos_region is None:
        pos_region = (1719 / 1920, 349 / 1030, 1805 / 1920, 380 / 1030)
    enter_time = time.perf_counter()
    last_img = None
    if img is not None:
        last_img = mumu.crop_img(img, pos_region[:2], pos_region[2:])
    delay = 1 / fps
    last_time = time.perf_counter()
    while True:
        while time.perf_counter() - last_time < delay:
            time.sleep(delay - (time.perf_counter() - last_time))
        last_time = time.perf_counter()
        if not reverse and last_time - enter_time > max_wait_time:
            break
        img = mumu.capture_window()
        img = mumu.crop_img(img, pos_region[:2], pos_region[2:])
        if last_img is None:
            last_img = img
            continue
        diff_rate = mumu.diff_img(img, last_img)
        last_img = img
        if diff_rate is None:
            continue
        if reverse:
            if diff_rate > threshold:
                continue
        else:
            if diff_rate < threshold:
                continue
        break


def get_next_btn_pos(pos):
    """TODO(migrate): 像素偏移"""
    new_pos = (pos[0] - 250, min(pos[1] + 250, 976))
    if any(i < 0 for i in new_pos):
        raise ValueError(f"新位置{new_pos}不能为负数")
    return new_pos


def move_seq_parse(action_list, move_max_num=LEGACY_MOVE_MAX_NUM):
    start_pos = action_list[0]
    action_list = action_list[1:]
    result = [start_pos]
    last_x, last_y = start_pos
    last_tgt_x, last_tgt_y = None, None
    is_fly = False
    for tgt_x, tgt_y in action_list:
        if tgt_x == -1:
            if last_tgt_x is not None:
                result.append((last_tgt_x, last_tgt_y))
                last_x, last_y = last_tgt_x, last_tgt_y
                last_tgt_x, last_tgt_y = None, None
            result.append((-1, -1))
            is_fly = True
            continue
        if is_fly:
            is_fly = False
            result.append((tgt_x, tgt_y))
            last_x, last_y = tgt_x, tgt_y
            assert last_tgt_x is None
            continue
        if abs(tgt_x - last_x) + abs(tgt_y - last_y) > move_max_num:
            if last_tgt_x is not None:
                result.append((last_tgt_x, last_tgt_y))
                last_x, last_y = last_tgt_x, last_tgt_y
                last_tgt_x, last_tgt_y = None, None
            if abs(tgt_x - last_x) + abs(tgt_y - last_y) > move_max_num:
                if not (tgt_x == last_x or tgt_y == last_y):
                    return (
                        f"行走路径不合法，action_list: {action_list}中的[{tgt_x},{tgt_y}]不合法\n"
                        "过长运动必须保证相邻点有一维相同"
                    )
                if tgt_x == last_x:
                    while abs(tgt_y - last_y) > move_max_num:
                        last_y += move_max_num if tgt_y > last_y else -move_max_num
                        result.append((last_x, last_y))
                else:
                    while abs(tgt_x - last_x) > move_max_num:
                        last_x += move_max_num if tgt_x > last_x else -move_max_num
                        result.append((last_x, last_y))
                if last_x == tgt_x and last_y == tgt_y:
                    last_tgt_x, last_tgt_y = None, None
                else:
                    last_tgt_x, last_tgt_y = tgt_x, tgt_y
            else:
                last_tgt_x, last_tgt_y = tgt_x, tgt_y
        else:
            last_tgt_x, last_tgt_y = tgt_x, tgt_y
    if last_tgt_x is not None and (last_x != last_tgt_x or last_y != last_tgt_y):
        result.append((last_tgt_x, last_tgt_y))
    return result


def move_seq_exec(action_list, mumu: Mumu, map_size=None):
    """TODO(migrate): 内部 character_pos / block_* 为旧像素"""
    if map_size is None:
        map_size = (999, 999)
    map_endpoint_list = [
        (0, 0),
        (map_size[0], 0),
        (0, map_size[1]),
        (map_size[0] - 1, map_size[1] - 1),
    ]
    limit_case_pos_calibrate_list = [(0, -0.5), (0.5, 0), (-0.5, 0), (0, 0.5)]
    character_pos = LEGACY_CHARACTER_POS
    block_width = LEGACY_BLOCK_WIDTH
    block_height = LEGACY_BLOCK_HEIGHT
    vision_delta_limit = LEGACY_VISION_DELTA_LIMIT

    is_fly = False
    for i in range(1, len(action_list)):
        tgt_x, tgt_y = action_list[i]
        pre_pos = (
            action_list[i - 1] if action_list[i - 1][0] != -1 else action_list[i - 2]
        )
        min_delta = 999
        min_j = None
        for j, pos in enumerate(map_endpoint_list):
            delta = abs(pos[0] - pre_pos[0]) + abs(pos[1] - pre_pos[1])
            if delta > vision_delta_limit:
                continue
            if delta < min_delta:
                min_delta = delta
                min_j = j
        if min_j is not None:
            if min_j == 3:
                min_delta = abs(pre_pos[0] - (map_endpoint_list[3][0] + 1)) + abs(
                    pre_pos[1] - (map_endpoint_list[3][1] + 1)
                )
            offset = vision_delta_limit - min_delta
            if min_j == 3:
                offset += 2
            new_character_pos = (
                character_pos[0]
                + int(limit_case_pos_calibrate_list[min_j][0] * block_width * offset),
                character_pos[1]
                + int(limit_case_pos_calibrate_list[min_j][1] * block_height * offset),
            )
        else:
            new_character_pos = character_pos

        if tgt_x == -1 and tgt_y == -1:
            mumu.click(new_character_pos, 0.35)
            is_fly = True
            continue

        x_block_cnt, y_block_cnt = (
            tgt_x - action_list[i - 1 if not is_fly else i - 2][0],
            tgt_y - action_list[i - 1 if not is_fly else i - 2][1],
        )
        x, y = (
            new_character_pos[0]
            + x_block_cnt * block_width // 2
            - y_block_cnt * block_width // 2,
            new_character_pos[1]
            + x_block_cnt * block_height // 2
            + y_block_cnt * block_height // 2,
        )
        img = mumu.capture_window()
        mumu.click((x, y), 0.2)
        wait_pos_change(mumu, threshold=0.01, fps=10, img=img, max_wait_time=3)
        if is_fly:
            is_fly = False
            time.sleep(0.8)
        wait_screen_change(mumu, reverse=True, threshold=0.1, fps=10, raw_diff=True)


# =============================================================================
# 自测
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    with Mumu() as mumu:
        print(f"install.root   = {mumu.install.root}")
        print(f"install.adb    = {mumu.install.adb_exe}")
        print(f"install.mgr    = {mumu.install.manager_exe}")
        print(f"vm_index       = {mumu.vm_index}")
        print(f"adb endpoint   = {mumu.adb_host}:{mumu.adb_port}")
        print(f"main_wnd       = 0x{(mumu.main_hwnd or 0):X}")
        print(f"render_wnd     = 0x{mumu.hwnd:X}")
        print(f"player_state   = {mumu.info.player_state}")
        print(f"name           = {mumu.info.name}")
        print(
            f"device         = {mumu.device_w}x{mumu.device_h}, "
            f"aspect = {mumu.device_aspect:.4f}"
        )

        img = mumu.capture_window()
        print(f"capture size   = {img.size}, aspect = {img.size[0]/img.size[1]:.4f}")
        img.save("capture_test.png")

        center = img.getpixel((img.size[0] // 2, img.size[1] // 2))
        print(f"center pixel   = {center}")
        print(
            f"color match    = "
            f"{mumu.is_color_similar(img, (0.5, 0.5), center, threshold=0)}"
        )

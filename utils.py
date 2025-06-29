import os
import subprocess
import threading
import queue
import uuid
import logging
import time
import win32gui, win32ui, win32con, win32api
from ctypes import windll, wintypes
import ctypes
from PIL import Image
import cv2
from skimage.metrics import structural_similarity
import numpy as np

# DWM API 常量
DWMWA_EXTENDED_FRAME_BOUNDS = 9


# 定义 RECT 结构体
class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def get_window_shadow_bounds(hwnd):
    # 加载 dwmapi.dll
    dwmapi = ctypes.windll.dwmapi

    rect = RECT()
    result = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )

    if result != 0:
        raise ctypes.WinError(result)

    width = rect.right - rect.left
    height = rect.bottom - rect.top
    return (rect.left, rect.top, rect.right, rect.bottom, width, height)


class SyncInteractiveSession:
    def __init__(self, cmd: list, encoding: str = "gbk", read_interval: float = 0.001):
        """
        :param cmd: 要启动的会话命令列表，如 ["powershell"] 或 ["cmd.exe"]
        :param encoding: 子进程 stdout/stderr 解码用的编码
        :param read_interval: 后台线程读队列的轮询间隔，秒
        """
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
        self._queue = queue.Queue()
        self._stop_reader = threading.Event()
        self._reader = threading.Thread(
            target=self._reader_thread, args=(read_interval,), daemon=True
        )
        self._reader.start()

    def _reader_thread(self, interval: float):
        """后台线程，不停读 stdout 放到队列"""
        while not self._stop_reader.is_set():
            line = self._proc.stdout.readline()
            if line == "" and self._proc.poll() is not None:
                break  # 进程结束
            if line:
                self._queue.put(line)
            else:
                time.sleep(interval)

    def send_command(self, command: str, timeout: float = 999) -> str:
        """
        发送一行命令，并阻塞直到该行执行完毕（通过唯一 END_MARKER 识别），
        返回输出（不含标记本身）。
        :param command: 要执行的命令，不包括换行
        :param timeout: 等待标记的最长秒数
        """
        # 生成唯一标记
        marker = f"END_{uuid.uuid4().hex}"
        full_cmd = f"{command} && echo {marker} || echo {marker}"
        # 发命令
        self._proc.stdin.write(full_cmd + "\n")
        self._proc.stdin.flush()

        # 收集输出直到标记出现
        lines = []
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            try:
                line = self._queue.get(timeout=1)
            except queue.Empty:
                break
            stripped = line.rstrip("\r\n").strip()
            if stripped == marker:
                # 结束标记，退出循环
                break
            lines.append(stripped)
        else:
            raise TimeoutError(f"等待命令“{command}”超时 {timeout} 秒")

        # 通常第一行是命令回显，可以去掉
        if lines and lines[0].strip() == command:
            return "\n".join(lines[1:])
        return "\n".join(lines)

    def close(self):
        """优雅退出子进程"""
        # 停 reader 线程
        self._stop_reader.set()
        try:
            # 发送 exit 两次：先退出子 shell，再退出 powershell/cmd
            self._proc.stdin.write("exit\nexit\n")
            self._proc.stdin.flush()
        except Exception:
            pass
        # 等待进程结束
        self._proc.wait(timeout=5)


class Mumu:
    def __init__(
        self,
        mumu_dir_path,
        vm_index=0,
        window_size=(1758, 984),
        full_app_window_size=(1920, 1030),
        window_name="MuMu模拟器",
    ):
        self.window_name = window_name
        if mumu_dir_path.endswith(".exe"):
            mumu_dir_path = os.path.dirname(os.path.dirname(mumu_dir_path))
        self.mumu_dir_path = mumu_dir_path
        self.vm_index = vm_index
        self.window_size = window_size
        self.full_app_window_size = full_app_window_size
        self.full_screen_rate = full_app_window_size[0] / full_app_window_size[1]
        self.window_rate = window_size[0] / window_size[1]
        self.scale_ratio = 1.25
        self.ratio_threshold = 0.02
        self.hwnd = self.get_window_hwnd()
        self.init_adb_shell()

    def click(self, pos, delay=0):
        start = time.perf_counter()
        x, y = pos
        x, y = x - int((1920 - self.window_size[0]) / 2), y - 46
        r = self.full_app_window_size[0] / self.window_size[0]
        x, y = int(x * r), int(y * r)

        result = self.run_command(["shell", "input", "tap", str(x), str(y)])

        while True:
            end = time.perf_counter()
            if delay > end - start:
                time.sleep(delay - (end - start))
            else:
                break
        return True

    # def run_command(self, command):
    #     if not getattr(self, "_adb_init", False):
    #         self._adb_shell = subprocess.Popen(
    #             ["powershell"],  # 或使用 ['cmd'] on Windows
    #             stdin=subprocess.PIPE,
    #             stdout=subprocess.PIPE,
    #             stderr=subprocess.PIPE,
    #             text=True,  # Python 3.7+，启用文本模式
    #             bufsize=1,  # 行缓冲，方便交互
    #             # encoding="utf-8",
    #             encoding="gbk",  # ✅ 改为 gbk 更兼容中文 Windows
    #             errors="replace",  # ✅ 替换非法字符，防止 decode 崩溃
    #         )
    #         port = str(self.vm_index * 32 + 16384)
    #         self._adb_shell.stdin.write(
    #             'cd "' + os.path.join(self.mumu_dir_path, "shell") + '"\n'
    #         )
    #         self._adb_shell.stdin.write(f".\\adb connect 127.0.0.1:{port}\n")
    #         self._adb_shell.stdin.write(f".\\adb -s 127.0.0.1:{port} shell\n")
    #         self._adb_shell.stdin.write(f"echo endmarker\n")
    #         self._adb_shell.stdin.flush()
    #         output_lines = []
    #         while True:
    #             line = self._adb_shell.stdout.readline()
    #             if not line:
    #                 break
    #             output_lines.append(line)
    #             if line.strip() == "endmarker":  # 结束标记
    #                 break
    #         self._adb_init = True
    #     if command[0] == "shell":
    #         command = command[1:]
    #     command = " ".join(command)
    #     self._adb_shell.stdin.write(command + "\necho endmarker\n")  # 写入命令
    #     self._adb_shell.stdin.flush()

    #     output_lines = []
    #     while True:
    #         line = self._adb_shell.stdout.readline()
    #         if not line:
    #             break
    #         output_lines.append(line)
    #         if line.strip() == "endmarker":  # 结束标记
    #             break
    #     return "".join(output_lines[:-1])  # 不返回结束标记本身

    def init_adb_shell(self):
        self._adb_shell = SyncInteractiveSession(["cmd"], encoding="gbk")
        port = str(self.vm_index * 32 + 16384)
        adb_path = f'"{os.path.join(self.mumu_dir_path, "shell", "adb.exe")}"'
        self._adb_shell.send_command(f"{adb_path} connect 127.0.0.1:{port}")
        self._adb_shell.send_command(f"{adb_path} -s 127.0.0.1:{port} shell")

    def run_command(self, command):
        if command[0] == "shell":
            command = command[1:]
        command = " ".join(command)
        output = self._adb_shell.send_command(command)  # 写入命令
        return output  # 不返回结束标记本身

    def get_window_hwnd(self):
        titles = set()

        def foo(hwnd, mouse):
            if (
                win32gui.IsWindow(hwnd)
                and win32gui.IsWindowEnabled(hwnd)
                and win32gui.IsWindowVisible(hwnd)
            ):
                titles.add(win32gui.GetWindowText(hwnd))

        win32gui.EnumWindows(foo, 0)
        lt = [t for t in titles if t]
        lt.sort()
        hwnd = None
        for t in lt:
            if (t.find(self.window_name)) >= 0:
                # if (t.find("Edge")) >= 0:
                hwnd = win32gui.FindWindow(None, t)
                break
        # hwnd = win32gui.FindWindow(None, "MuMu模拟器12")
        if hwnd:
            return hwnd
        else:
            raise Exception("窗口未找到")

    def bring_window_back(self):
        hwnd = self.hwnd
        # 如果是最小化状态，恢复窗口（不激活）
        while win32gui.IsIconic(hwnd):
            # win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            # win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            # win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)  # 恢复窗口
            # 确保不会激活和前置
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_BOTTOM,  # 放到底部，不会抢占焦点
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
            time.sleep(0.2)

    def is_full_screen(self, img):
        width, height = img.size
        if abs(width / height - self.full_screen_rate) < self.ratio_threshold:
            return True
        elif (
            abs((width - 81 * 2) / (height - 46) - self.window_rate)
            < self.ratio_threshold
        ):
            return True
        elif abs(width / (height - 46) - self.window_rate) < self.ratio_threshold:
            return False
        elif abs(width / height - self.window_rate) < self.ratio_threshold:
            return False
        raise RuntimeError(
            "无法判断窗口是否全屏: {}x{}, 比例{:.3f}".format(
                width, height, width / height
            )
        )

    def global_pos_to_game_window_pos(self, pos, img):
        x, y = pos
        y -= 46
        x -= 81
        x_ratio, y_ratio = x / self.window_size[0], y / self.window_size[1]
        width, height = img.size
        if abs(width / height - self.window_rate) < self.ratio_threshold:
            return (
                int(x_ratio * width),
                int(y_ratio * height),
            )
        assert (
            False
        ), "图像比例不正确，需要确保传入的图像是游戏窗口截图，图像比例{:.3f}".format(
            width / height
        )
        height -= 46
        if not self.is_full_screen(img):
            width -= 81 * 2
        return (
            int(width * x_ratio),
            int(height * y_ratio),
        )

    def is_color_similar(self, img, pos, target_color, threshold=40):
        pos = self.global_pos_to_game_window_pos(pos, img)
        x, y = pos
        assert (
            abs(img.size[0] / img.size[1] - self.window_rate) < self.ratio_threshold
        ), "图像比例不正确，需要确保传入的图像是游戏窗口截图，图像比例{:.3f}".format(
            img.size[0] / img.size[1]
        )
        color = img.getpixel((x, y))
        cnt = 0
        for i in range(3):
            cnt += abs(color[i] - target_color[i])
        if cnt > threshold:
            return False
        return True

    def diff_img(self, img1, img2):
        if img1.size != img2.size:
            logging.warning(
                "图像大小不一致，无法计算差异: {} vs {}".format(img1.size, img2.size)
            )
            return None

        def resize(image):
            target_size = (int(image.size[0] * 0.25), int(image.size[1] * 0.25))
            return image.resize(target_size, Image.Resampling.LANCZOS)

        if img1.size[0] * img1.size[1] > 100 * 100:
            img1 = resize(img1)
            img2 = resize(img2)

        img1 = cv2.cvtColor(np.array(img1), cv2.COLOR_RGB2GRAY)
        img2 = cv2.cvtColor(np.array(img2), cv2.COLOR_RGB2GRAY)

        score, diff = structural_similarity(img1, img2, full=True)
        diff_rate = 1 - score  # 结构差异率
        return diff_rate

    def crop_img(self, img, left_top, right_bottom):
        left_top = self.global_pos_to_game_window_pos(left_top, img)
        right_bottom = self.global_pos_to_game_window_pos(right_bottom, img)
        return img.crop(
            (
                left_top[0],
                left_top[1],
                right_bottom[0],
                right_bottom[1],
            )
        )

    def capture_window(self, delay=0):
        now = time.perf_counter()

        user32 = ctypes.windll.user32
        screen_width = user32.GetSystemMetrics(0)
        screen_scale_ratio = int(screen_width * self.scale_ratio) / 1920

        self.bring_window_back()
        hwnd = self.hwnd
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)

        width = right - left
        height = bottom - top

        width = int(width * self.scale_ratio)
        height = int(height * self.scale_ratio)

        # 获取窗口的设备上下文（DC）
        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()

        # 创建位图对象
        saveBitMap = win32ui.CreateBitmap()
        saveBitMap.CreateCompatibleBitmap(
            mfcDC,
            width,
            height,
        )
        saveDC.SelectObject(saveBitMap)

        # 调用 PrintWindow API，捕获窗口图像
        result = windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 3)

        # 获取位图信息
        bmpinfo = saveBitMap.GetInfo()
        bmpstr = saveBitMap.GetBitmapBits(True)

        # 创建图像对象
        im = Image.frombuffer(
            "RGB",
            (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmpstr,
            "raw",
            "BGRX",
            0,
            1,
        )
        # im.show()

        x_offset, y_offset = 0, 0
        while True:
            color = im.getpixel((x_offset, y_offset))
            if not (color[0] == 0 and color[1] == 0 and color[2] == 0):
                break
            x_offset += 1
            y_offset += 1
        while True:
            is_changed = False
            color = im.getpixel((x_offset, y_offset - 1))
            if not (color[0] == 0 and color[1] == 0 and color[2] == 0):
                y_offset -= 1
                is_changed = True
            color = im.getpixel((x_offset - 1, y_offset))
            if not (color[0] == 0 and color[1] == 0 and color[2] == 0):
                x_offset -= 1
                is_changed = True
            if not is_changed:
                break
        if left < 0:
            x_offset -= int((left + 1) * self.scale_ratio)
        true_width, true_height = get_window_shadow_bounds(hwnd)[-2:]
        im = im.crop(
            (x_offset, y_offset, true_width + x_offset - 1, true_height + y_offset - 1)
        )
        # im.show()

        # 释放资源
        win32gui.DeleteObject(saveBitMap.GetHandle())
        saveDC.DeleteDC()
        mfcDC.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwndDC)

        if self.is_full_screen(im):
            im = im.crop((81, 46, im.size[0] - 81, im.size[1]))
        else:
            im = im.crop((0, 46, im.size[0], im.size[1]))

        if screen_scale_ratio != 1:
            im = im.resize(
                (
                    int(im.size[0] / screen_scale_ratio),
                    int(im.size[1] / screen_scale_ratio),
                ),
                Image.Resampling.NEAREST,
            )

        # if result == 1:
        #     im.show()
        # else:
        #     print("截图失败")
        while True:
            wait_time = delay - (time.perf_counter() - now)
            if wait_time > 0:
                time.sleep(wait_time)
            else:
                break
        return im


class OCR:
    def __init__(self, mumu: Mumu, ocr_mode="cnocr"):
        assert ocr_mode in ["cnocr", "paddleocr"]
        self.ocr_mode = ocr_mode
        if ocr_mode == "cnocr":
            from cnocr import CnOcr

            self.ocr = CnOcr(
                context="cuda",
                rec_model_name="scene-densenet_lite_136-gru",
            )
        elif ocr_mode == "paddleocr":
            from paddleocr import PaddleOCR

            self.ocr = PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device="gpu",
            )
        self.mumu = mumu
        self.chat_box_pos = (371, 219, 1291, 384)

    def get_chat_image(self):
        img = self.mumu.capture_window()
        top_left = self.mumu.global_pos_to_game_window_pos(
            (self.chat_box_pos[0], self.chat_box_pos[1]), img
        )
        bottom_right = self.mumu.global_pos_to_game_window_pos(
            (self.chat_box_pos[2], self.chat_box_pos[3]), img
        )
        return img.crop((top_left[0], top_left[1], bottom_right[0], bottom_right[1]))

    def get_text(self, img=None, join_text=True, threshold=0.4):
        if img is None:
            img = self.get_chat_image()
        if self.ocr_mode == "cnocr":
            from cnocr import CnOcr

            assert isinstance(self.ocr, CnOcr), "OCR模式不正确，应该是CnOcr"
            result = self.ocr.ocr(img)
            # print(result)
            if join_text:
                text = "".join(
                    [data["text"] for data in result if data["score"] > threshold]
                )
                return text
            return result
        elif self.ocr_mode == "paddleocr":
            from paddleocr import PaddleOCR
            import cv2

            assert isinstance(self.ocr, PaddleOCR), "OCR模式不正确，应该是PaddleOCR"
            cv2_img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
            result = self.ocr.predict(cv2_img)[0]
            result = {"text": result["rec_texts"], "score": result["rec_scores"]}
            if join_text:
                text = "".join(
                    [
                        result["text"][i]
                        for i in range(len(result["text"]))
                        if result["score"][i] > threshold
                    ]
                )
                return text
            return result


class Executor:
    def __init__(self, mumu: Mumu, vision_size="小"):
        assert vision_size in ["小", "中", "大"], "视野大小必须是 小、中、大 之一"
        self.character_pos = (848, 534)
        if vision_size == "小":
            self.block_width, self.block_height = 160, 81
            self.move_max_num = 8
        elif vision_size == "中":
            self.block_width, self.block_height = 202, 101
            self.move_max_num = 10
        elif vision_size == "大":
            raise NotImplementedError("大视野暂未实现")
        self.mumu = mumu
        self.cur_pos = None

    def set_cur_pos(self, pos):
        self.cur_pos = pos

    def move_to(self, pos, cur_pos=None, map_fix_vision_max_num=999):
        tgt_x, tgt_y = pos
        if tgt_x == -1 and tgt_y == -1:
            mumu.click(new_character_pos, 0.25)
            return
        if cur_pos is None:
            assert self.cur_pos is not None, "当前坐标未设置"
            cur_pos = self.cur_pos
        new_character_pos = (
            self.character_pos[0],
            self.character_pos[1]
            + max(0, sum(cur_pos) - map_fix_vision_max_num) * self.block_height // 2
            + min(0, sum(cur_pos) - 5) * self.block_height // 2,
        )

        x_block_cnt, y_block_cnt = (
            tgt_x - cur_pos[0],
            tgt_y - cur_pos[1],
        )
        x, y = (
            new_character_pos[0]
            + x_block_cnt * self.block_width // 2
            - y_block_cnt * self.block_width // 2,
            new_character_pos[1]
            + x_block_cnt * self.block_height // 2
            + y_block_cnt * self.block_height // 2,
        )
        mumu.click((x, y))

    def ocr_cur_pos(self, ocr: OCR, img=None):
        if img is None:
            img = self.mumu.capture_window()
        # left_top = self.mumu.global_pos_to_game_window_pos((1721,348), img)
        left_top = self.mumu.global_pos_to_game_window_pos((1619, 347), img)
        right_bottom = self.mumu.global_pos_to_game_window_pos((1809, 383), img)
        ocr_img = img.crop((left_top[0], left_top[1], right_bottom[0], right_bottom[1]))
        result = ocr.get_text(ocr_img, join_text=False)
        assert len(result) == 4, "OCR结果不正确，应该是4个结果：{}".format(result)
        self.cur_pos = (
            int(result[-3]["text"]),
            int(result[-1]["text"]),
        )
        return self.cur_pos


def wait_screen_change(
    mumu: Mumu, reverse=False, threshold=0.03, max_wait_time=1, fps=5, raw_diff=False
):
    enter_time = time.perf_counter()
    last_img = None
    delay = 1 / fps
    last_time = time.perf_counter()
    while True:
        while True:
            delta = delay - (time.perf_counter() - last_time)
            if delta <= 0:
                break
            time.sleep(delta)
        last_time = time.perf_counter()
        if not reverse and last_time - enter_time > max_wait_time:
            break
        img = mumu.capture_window()
        crop_ratio = (0.03185, 0.08028, 0.8413, 0.9024)
        crop_size = (
            int(img.size[0] * crop_ratio[0]),
            int(img.size[1] * crop_ratio[1]),
            int(img.size[0] * crop_ratio[2]),
            int(img.size[1] * crop_ratio[3]),
        )
        img = img.crop(crop_size)
        if last_img is None:
            last_img = img
            continue
        if not raw_diff:
            diff_rate = mumu.diff_img(img, last_img)
        else:
            # hero_offset = (803, 451, 881, 550)
            # hero_offset = mumu.global_pos_to_game_window_pos(
            #     (hero_offset[0], hero_offset[1]), img
            # ) + mumu.global_pos_to_game_window_pos(
            #     (hero_offset[2], hero_offset[3]), img
            # )
            # is_frame_drop = True
            # for i in range(hero_offset[0], hero_offset[2]):
            #     if not is_frame_drop:
            #         break
            #     for j in range(hero_offset[1], hero_offset[3]):
            #         if img.getpixel((i, j)) != last_img.getpixel((i, j)):
            #             is_frame_drop = False
            #             break
            diff_cnt = 0
            step = 3
            # step = 1
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
                # if diff_rate  == 0:
                continue
        else:
            if diff_rate < threshold:
                continue
        break


def wait_pos_change(
    mumu: Mumu, reverse=False, threshold=0.02, max_wait_time=1, fps=5, img=None
):
    enter_time = time.perf_counter()
    last_img = None
    if img is not None:
        last_img = mumu.crop_img(img, (1719, 349), (1805, 380))
    delay = 1 / fps
    last_time = time.perf_counter()
    while True:
        while time.perf_counter() - last_time < delay:
            time.sleep(delay - (time.perf_counter() - last_time))
        last_time = time.perf_counter()
        if not reverse and last_time - enter_time > max_wait_time:
            break
        img = mumu.capture_window()
        img = mumu.crop_img(img, (1719, 349), (1805, 380))
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


def move_to(mumu: Mumu, pos):
    mumu.click(*pos)
    wait_pos_change(mumu)
    wait_pos_change(mumu, reverse=True)


def get_next_btn_pos(pos):
    new_pos = (pos[0] - 250, min(pos[1] + 250, 976))
    if any([i < 0 for i in new_pos]):
        raise ValueError(f"新位置{new_pos}不能为负数")
    return new_pos


if __name__ == "__main__":
    mumu = Mumu("D:/MuMu Player 12/shell/MuMuManager.exe")
    # mumu = Mumu("D:/MuMu Player 12/shell/MuMuManager.exe", window_size=(554, 984))
    # mumu = Mumu("D:/MuMu Player 12/shell/MuMuManager.exe", window_size=(554, 984), window_name="画图")
    # mumu.click((1700, 321))
    # mumu.click((778, 573))
    # from bwtools.log import TimeCounter

    # with TimeCounter(""):
    #     for _ in range(10):
    #         mumu.click((778, 573))
    wait_screen_change(mumu, reverse=True, threshold=0.1, fps=10, raw_diff=True)
    # background_click(
    #     mumu.hwnd, 979, 466, click_type="left", delay=0.1
    # )  # 在窗口内点击
    # img = mumu.capture_window()
    # img.show()
    # img = mumu.capture_window()
    # img = mumu.capture_window()
    # img.show()
    # print(mumu.is_color_similar(
    #     img, (1684, 798), (149, 131, 103), threshold=30
    # ))
    # ocr = OCR(mumu)
    # print(ocr.get_text())
    # executor = Executor(mumu)
    # ocr = OCR(mumu)
    # ocr = OCR(mumu, ocr_mode="paddleocr")
    # ocr.get_text()
    # executor.ocr_cur_pos(ocr)
    # print(executor.cur_pos)

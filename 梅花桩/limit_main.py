import time
import time

from PIL import ImageGrab
import pyautogui

from bwtools.log import TimeCounter

t = 1012 / 1000


def during(x, y, time_delay=0):
    pyautogui.mouseDown(x, y)
    now = time.perf_counter()
    while time.perf_counter() - now < t:
        pass
    pyautogui.mouseUp(x, y)


if __name__ == "__main__":
    time.sleep(2)
    for _ in range(501):
        during(1050, 543, t)
        time.sleep(2)
    # pyautogui.mouseUp(1050, 543)
    # with TimeCounter("test"):
    #     for _ in range(100):
    #         pyautogui.mouseUp(1050, 543)

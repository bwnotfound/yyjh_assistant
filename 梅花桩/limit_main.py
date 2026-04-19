import time
import time

from PIL import ImageGrab
import pyautogui

# from bwtools.log import TimeCounter

# t = 1012 / 1000
# t = 1012 / 1000 * 0.9
t = 1012 / 1000 * 0.9 * 0.9


def during(x, y, time_delay=0):
    pyautogui.mouseDown(x, y)
    now = time.perf_counter()
    while time.perf_counter() - now < t:
        pass
    pyautogui.mouseUp(x, y)


if __name__ == "__main__":
    time.sleep(2)
    # during(2281, 1265, 200)
    # during(2281, 1265, 200)
    for _ in range(501):
        during(1050, 543, t - 50)
        time.sleep(1.3)
    # pyautogui.mouseUp(1050, 543)
    # with TimeCounter("test"):
    #     for _ in range(100):
    #         pyautogui.mouseUp(1050, 543)

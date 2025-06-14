import time
import sys, os

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
from utils import Mumu

symbol_pos = (1114, 574)
symbol_color = (129, 154, 25)

btn_pos = (1393, 386)

if __name__ == "__main__":
    mumu = Mumu("D:/MuMu Player 12/shell/MuMuManager.exe")
    while True:
        mumu.click(btn_pos)
        img = mumu.capture_window()
        # img.show()
        # exit()
        while not mumu.is_color_similar(img, symbol_pos, symbol_color, threshold=70):
            pass
        mumu.click(btn_pos)
        time.sleep(1)
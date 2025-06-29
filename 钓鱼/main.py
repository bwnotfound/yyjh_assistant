import time
import sys, os

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
from utils import Mumu

chat_btn_index = 0

symbol_pos = (1130, 611)
symbol_color = (102, 123, 18)

screen_pos = (406, 704)
screen_color = (203, 187, 159)

btn_pos = (1401, 388)

chat_height_space = 100
chat_first_btn_pos = (1140, 472)
chat_btn_pos_list = [
    (chat_first_btn_pos[0], chat_first_btn_pos[1] + i * chat_height_space)
    for i in range(5)
]


def enter_fising_screen(mumu: Mumu):
    mumu.click((1716, 510), delay=0.6)
    mumu.click(chat_btn_pos_list[chat_btn_index], delay=1)


if __name__ == "__main__":
    mumu = Mumu("D:/MuMu Player 12")
    while True:
        if not mumu.is_color_similar(
            mumu.capture_window(), screen_pos, screen_color, threshold=15
        ):
            enter_fising_screen(mumu)
        mumu.click(btn_pos)
        # img.show()
        # exit()
        while True:
            img = mumu.capture_window()
            if mumu.is_color_similar(img, symbol_pos, symbol_color, threshold=70):
                break
        mumu.click(btn_pos)
        time.sleep(1.5)

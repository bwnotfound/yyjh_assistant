import time
import sys, os

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
from utils import Mumu

package_pos = (456, 990)
piano_pos = (1096, 364)
play_btn_pos = (713, 856)
book_pos = (672, 405)
block_size = (150, 150)
table_offset_pos = (449, 334)

book_num = 4


def get_book_pos(num):
    x = int(table_offset_pos[0] + (num - 0.5) * block_size[0])
    y = table_offset_pos[1] + 0.5 * block_size[1]
    return (x, y)

mumu = Mumu("D:/MuMu Player 12/shell/MuMuManager.exe")

cur_num = 1
count = 0
while True:
    count += 1
    mumu.click(package_pos, 1)
    mumu.click(piano_pos, 1)
    mumu.click(play_btn_pos, 1)
    mumu.click(get_book_pos(cur_num), 1)
    cur_num += 1
    if cur_num > book_num:
        cur_num = 1
    print(f"第{count}次弹琴完毕")
    time.sleep(60 * 5 + 10)

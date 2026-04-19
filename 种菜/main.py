import time
import sys, os
import json

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
from utils import Mumu, OCR, wait_screen_change, move_to

symbol_pos = (875, 525)
symbol_color = (143, 5, 8)
symbol_color_similar_threshold = 50

start_pos = [15, 12]
field_pos_list = [[15, 12], [15, 13], [19, 15], [19, 16], [19, 17], [19, 20]]

state_text_list = ["周围杂草丛生", "叶子有点枯黄", "感觉叶子有些蔫", "茎叶有被虫咬的迹象"]
state_window_pos = (864, 289, 1266, 339)
growth_text_list = ["种子期", "幼苗期", "生长期", "采摘期"]
growth_time_list = [6, 10, 16]
sccess_window_pos = (615, 345, 730, 389)

center_pos = (17,15)

table_btn_pos_list = [[1735, 515],[1730,592]]
chat_btn_pos_list = [[1154,507],[1162,602],[1157,692],[1154,790]]

def crop(img, pos, mumu: Mumu):
    return img.crop(
        (
            *mumu.global_pos_to_game_window_pos(pos[:2], img),
            *mumu.global_pos_to_game_window_pos(pos[2:], img),
        )
    )


def growth_index(img, mumu: Mumu, ocr: OCR):
    img = crop(img, state_window_pos, mumu)
    text = ocr.get_text(img)
    return growth_text_list.index(text) if text in growth_text_list else None

def move_to(mumu: Mumu, pos):
    mumu.click(*pos)
    wait_pos_change(mumu)
    wait_pos_change(mumu, reverse=True)

if __name__ == "__main__":
    mumu = Mumu("D:/MuMuPlayer/nx_device/12.0/shell/MuMuManager.exe")
    ocr = OCR(mumu)

    cur_pos = start_pos
    for i, pos in enumerate(field_pos_list):
        if abs(cur_pos[0] - pos[0]) + abs(cur_pos[1] - pos[1]) > 6:
            move_to(center_pos, mumu)
        print(f"开始处理第{i+1}个田地: ")
        if cur_pos != pos:
            move_to(pos, mumu)
        if not has_error(mumu):
            continue

        img = mumu.capture_window()
        if has_error(mumu):
            print()
            continue

        growth_idx = growth_index(img, mumu, ocr)
        if growth_idx is None:
            print("无法识别植物状态，跳过当前田地")
            continue

        print(f"当前植物状态: {growth_text_list[growth_idx]}")
        if growth_idx == 3:
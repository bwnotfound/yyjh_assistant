import os
import time
import json

from pprint import pprint
from PIL import ImageGrab
import cv2
import numpy as np

strict = False
num_rows, num_cols = 9, 7
window_offset_x, window_offset_y = 80, 45
window_width, window_height = 1760, 980
table_offset_x, table_offset_y = 330, 90
table_width, table_height = 1093, 714
table_cell_width, table_cell_height = table_width / num_cols, table_height / num_rows

chess_select_x, chess_select_y = 959, 435
chess_select_height = 50
button_x, button_y = 1161, 437
main_btn_x, main_btn_y = 957, 957
is_selected = False

other_x, other_y = 1790, 66
other_color_in = (47, 47, 47)

in_btn_x, in_btn_y = 1721, 662
tuiyan_btn_x, tuiyan_btn_y = 1148, 472

root_dir = os.path.dirname(os.path.abspath(__file__))


def get_position_coordinates(x, y):
    """
    Convert the given x and y coordinates to the corresponding position on the screen.
    """
    return (
        int(window_offset_x + table_offset_x + (x - 0.5) * table_cell_width),
        int(window_offset_y + table_offset_y + (y - 0.5) * table_cell_height),
    )


def get_table_screenshot():
    """
    Capture a screenshot of the specified area of the screen.
    """
    return ImageGrab.grab(
        bbox=(
            window_offset_x + table_offset_x,
            window_offset_y + table_offset_y,
            window_offset_x + table_offset_x + table_width,
            window_offset_y + table_offset_y + table_height,
        )
    )


def is_main_screen():
    img = ImageGrab.grab()
    tgt_pixel = img.getpixel((other_x, other_y))
    if (
        abs(tgt_pixel[0] - other_color_in[0]) < 5
        and abs(tgt_pixel[1] - other_color_in[1]) < 5
        and abs(tgt_pixel[2] - other_color_in[2]) < 5
    ):
        return False
    return True


def is_tile_occupied(
    img, red_lower=(140, 0, 0), red_upper=(160, 35, 35), ratio_thresh=0.001
):
    # 971A1A
    mask = cv2.inRange(
        np.array(img),
        np.array(red_lower, dtype=np.uint8),
        np.array(red_upper, dtype=np.uint8),
    )
    red_ratio = np.sum(mask > 0) / mask.size
    return red_ratio > ratio_thresh, red_ratio


def get_enemy_info():
    img = get_table_screenshot()
    # 切割为num_rows*num_cols的格子
    is_enemy = [[False for _ in range(num_cols)] for _ in range(num_rows)]
    variance_matrix = [[0 for _ in range(num_cols)] for _ in range(num_rows)]
    for i in range(num_rows):
        for j in range(num_cols):
            # 计算每个格子的坐标
            x1 = int(j * table_cell_width)
            y1 = int(i * table_cell_height)
            x2 = int((j + 1) * table_cell_width)
            y2 = int((i + 1) * table_cell_height)
            # 截取每个格子的图片
            part_img = img.crop((x1, y1, x2, y2))
            # part_img.save(os.path.join(root_dir, f"output/part_img_{i}_{j}.png"))
            # 判断该格子是否有单位
            result, variance = is_tile_occupied(part_img)
            variance_matrix[i][j] = f"{variance:.4f}"
            if result:
                is_enemy[i][j] = True
    # 打印结果

    # pprint(is_enemy)
    # pprint(variance_matrix)
    return is_enemy


def click(x, y):
    import pyautogui

    pyautogui.click(x, y)


def place_unit(chess, x, y):
    sleep_time = 0.2
    global is_selected
    x, y = get_position_coordinates(x, y)
    click(x, y)
    time.sleep(sleep_time)
    if not is_selected:
        if chess == 1:
            click(chess_select_x, chess_select_y)
        elif chess == 2:
            click(chess_select_x, chess_select_y + chess_select_height)
        elif chess == 3:
            click(chess_select_x, chess_select_y + chess_select_height * 2)
        else:
            raise ValueError("Invalid chess type")
        is_selected = True
    time.sleep(sleep_time)
    if chess == 1:
        click(button_x, button_y)
    elif chess == 2:
        click(button_x, button_y + chess_select_height)
    elif chess == 3:
        click(button_x, button_y + chess_select_height * 2)
    else:
        raise ValueError("Invalid chess type")
    time.sleep(sleep_time)


if __name__ == "__main__":
    # get_table_screenshot().show()
    # get_enemy_info()
    with open(os.path.join(root_dir, "config.json"), "r", encoding="utf-8") as f:
        config = json.load(f)
    time.sleep(0.5)
    while True:
        is_selected = False
        enemy_info = get_enemy_info()
        for item in config:
            table = item["table"]
            pprint(table)
            pprint(enemy_info)
            is_match = True
            for i in range(num_rows):
                if not is_match:
                    break
                for j in range(num_cols):
                    if strict:
                        raise NotImplementedError("Strict mode is not implemented yet")
                    if (table[i][j] == 0 and not enemy_info[i][j]) or (
                        table[i][j] != 0 and enemy_info[i][j]
                    ):
                        continue
                    is_match = False
                    break
            if not is_match:
                continue
            break
        else:
            raise RuntimeError("无法匹配到配置文件中的任何棋盘")
        solution = item["solution"]
        for x, y, chess in solution:
            place_unit(chess, x, y)
        click(main_btn_x, main_btn_y)
        time.sleep(0.5)
        while not is_main_screen():
            time.sleep(0.5)
        time.sleep(0.5)
        click(in_btn_x, in_btn_y)
        time.sleep(0.5)
        click(in_btn_x, in_btn_y)
        time.sleep(0.5)
        click(in_btn_x, in_btn_y)
        time.sleep(0.5)
        click(in_btn_x, in_btn_y)
        time.sleep(0.5)
        click(tuiyan_btn_x, tuiyan_btn_y)
        time.sleep(0.5)

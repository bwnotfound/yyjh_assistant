import time

import sys, os

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
from utils import Mumu

available_count = 9

chat_btn_x, chat_btn_y = 1720, 586
doctor_submit_btn_x, doctor_submit_btn_y = 1156, 674
cook_symbol_x, cook_symbol_y = 865, 767
cook_symbol_color = (240, 92, 14)
doctor_x, doctor_y = 768, 576
cook_block_x, cook_block_y = 926, 495
cook_submit_btn_x, cook_submit_btn_y = 1149, 483
recipe_in_btn_x, recipe_in_btn_y = 1137, 381
recipe_choose_x, recipe_choosen_y = 1274, 779
cook_btn_x, cook_btn_y = 1399, 359
success_x, success_y = 729, 345
success_color = (255, 255, 0)
exit_x, exit_y = 1384, 744


chat_time_sleep = 0.4
chat_more_time_sleep = 0.7
move_time_sleep = 1.0


def click(x, y, time_delay=0):
    mumu.click((x, y), delay=time_delay)


def is_satisfied_color(x, y, color, threshold=60, img=None):
    if img is None:
        img = mumu.capture_window()
    return mumu.is_color_similar(img, (x, y), color, threshold=threshold)


if __name__ == "__main__":
    mumu = Mumu("D:/MuMuPlayer/nx_device/12.0/shell/MuMuManager.exe")
    time.sleep(1)
    for i in range(available_count):
        click(chat_btn_x, chat_btn_y, chat_time_sleep)
        click(chat_btn_x, chat_btn_y, chat_time_sleep)
        click(doctor_submit_btn_x, doctor_submit_btn_y, chat_time_sleep)
        click(chat_btn_x, chat_btn_y, chat_time_sleep)
        click(chat_btn_x, chat_btn_y, chat_more_time_sleep)
        click(cook_block_x, cook_block_y, move_time_sleep)
        click(chat_btn_x, chat_btn_y, chat_time_sleep)
        click(chat_btn_x, chat_btn_y, chat_time_sleep)
        click(cook_submit_btn_x, cook_submit_btn_y, chat_more_time_sleep)
        click(recipe_in_btn_x, recipe_in_btn_y, chat_more_time_sleep)
        click(recipe_choose_x, recipe_choosen_y, chat_more_time_sleep)
        while True:
            img = mumu.capture_window()
            if is_satisfied_color(
                success_x, success_y, success_color, img=img, threshold=300
            ):
                break
            if not is_satisfied_color(
                cook_symbol_x, cook_symbol_y, cook_symbol_color, img=img
            ):
                click(cook_btn_x, cook_btn_y, chat_time_sleep)
            time.sleep(0.2)
        time.sleep(1)
        click(chat_btn_x, chat_btn_y, chat_more_time_sleep)
        click(exit_x, exit_y, 1)
        click(doctor_x, doctor_y, move_time_sleep)
        click(chat_btn_x, chat_btn_y, chat_time_sleep)
        click(doctor_submit_btn_x, doctor_submit_btn_y, chat_time_sleep)
        click(doctor_submit_btn_x, doctor_submit_btn_y, chat_time_sleep)
        print(f"完成第{i + 1}次炼药")

import time
import sys, os
import json

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
from tqdm import tqdm
from utils import (
    Mumu,
    wait_screen_change,
    Executor,
    
    OCR,
    wait_pos_change,
    get_next_btn_pos,
)

start_index = 0
start_action_index = 0

character_pos = (848, 534)
block_width, block_height = 160, 80
move_max_num = 8

chat_btn_pos = (1737, 594)
collect_btn_pos = (1151, 508)
blank_btn_pos = (1717, 762)
package_pos = (451, 998)
ticket_btn_pos = (602, 856)
symbol_pos = (1287, 253)
symbol_color = (249, 240, 220)
symbol_color_diff_threshold = 50
window_origin_size = (1775, 985)

img_diff_threshold = 0.01


def action_parse(action_list, start_pos):
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
                    return """行走路径不合法，action_list: {}中的[{},{}]不合法
过长的运动必须保证当前坐标点和上一个坐标点有一个维度是相同的，保证程序能分批运动确保正确性""".format(
                        action_list, tgt_x, tgt_y
                    )
                if tgt_x == last_x:
                    while abs(tgt_y - last_y) > move_max_num:
                        if tgt_y > last_y:
                            last_y += move_max_num
                        else:
                            last_y -= move_max_num
                        result.append((last_x, last_y))
                else:
                    while abs(tgt_x - last_x) > move_max_num:
                        if tgt_x > last_x:
                            last_x += move_max_num
                        else:
                            last_x -= move_max_num
                        result.append((last_x, last_y))
                if last_x == tgt_x and last_y == tgt_y:
                    last_tgt_x, last_tgt_y = None, None
                else:
                    last_tgt_x, last_tgt_y = tgt_x, tgt_y
            else:
                # result.append((tgt_x, tgt_y))
                # last_x, last_y = tgt_x, tgt_y
                # last_tgt_x, last_tgt_y = None, None
                last_tgt_x, last_tgt_y = tgt_x, tgt_y
        else:
            last_tgt_x, last_tgt_y = tgt_x, tgt_y
    if last_tgt_x is not None and (last_x != last_tgt_x or last_y != last_tgt_y):
        result.append((last_tgt_x, last_tgt_y))
    return result


def action_exec(action_list, mumu: Mumu, ocr: OCR, executor: Executor, map_limit=999):
    is_fly = False
    for i in tqdm(range(1, len(action_list)), leave=False, desc="行走中"):
        tgt_x, tgt_y = action_list[i]
        pre_pos = (
            action_list[i - 1] if action_list[i - 1][0] != -1 else action_list[i - 2]
        )
        new_character_pos = (
            character_pos[0],
            character_pos[1]
            + max(0, sum(pre_pos) - map_limit) * block_height // 2
            + min(0, sum(pre_pos) - 8) * block_height // 2,
        )
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
        # (949, 688)
        img = mumu.capture_window()
        mumu.click((x, y), 0.2)
        wait_pos_change(
            mumu, threshold=img_diff_threshold, fps=10, img=img, max_wait_time=3
        )
        # move_cnt = abs(x_block_cnt) + abs(y_block_cnt) - 1
        if is_fly:
            is_fly = False
            # move_cnt = 0
            time.sleep(0.8)
        wait_screen_change(mumu, reverse=True, threshold=0.09, fps=10, raw_diff=True)
        # wait_pos_change(mumu, threshold=img_diff_threshold, reverse=True, fps=1.5)


def teleport(config, mumu: Mumu):
    mumu.click(package_pos, 1)
    mumu.click(ticket_btn_pos, 1)
    mumu.click(config["next_icon_pos"], 0.7)
    mumu.click(
        (
            config["next_btn_pos"]
            if "next_btn_pos" in config
            else get_next_btn_pos(config["next_icon_pos"])
        ),
        0.7,
    )


def collect(mumu: Mumu):
    mumu.click(chat_btn_pos, 0.4)
    mumu.click(collect_btn_pos, 0.25)
    cnt = 0
    while cnt < 5:
        now = time.perf_counter()
        mumu.click(blank_btn_pos)
        img = mumu.capture_window()
        if not mumu.is_color_similar(
            img, symbol_pos, symbol_color, symbol_color_diff_threshold
        ):
            cnt += 1
            continue
        cnt = 0
        if time.perf_counter() - now < 0.1:
            time.sleep(0.2)
    time.sleep(0.4)


if __name__ == "__main__":
    mumu = Mumu("D:/MuMuPlayer/nx_device/12.0/shell/MuMuManager.exe")
    # ocr = OCR(mumu)
    # executor = Executor(mumu)
    ocr = None
    executor = None
    with open(
        os.path.join(os.path.dirname(__file__), "config.json"), "r", encoding="utf-8"
    ) as f:
        config = json.load(f)

    for map_config in config:
        for i, action in enumerate(map_config["actions"]):
            parsed_action = action_parse(action[1:], action[0])
            if isinstance(parsed_action, str):
                print(parsed_action)
                exit()
        if "next_map_action" not in map_config:
            pass
        elif isinstance(map_config["next_map_action"], dict):
            pass
        else:
            parsed_action = action_parse(
                map_config["next_map_action"][1:],
                map_config["next_map_action"][0],
            )
            if isinstance(parsed_action, str):
                print(parsed_action)
                exit()

    for i, map_config in enumerate(config):
        if start_index is not None and i < start_index:
            continue
        print(f"开始处理第{i+1}张地图: {map_config['name']}")
        for j, action in enumerate(map_config["actions"]):
            if (
                start_action_index is not None
                and i == start_index
                and j < start_action_index
            ):
                continue
            parsed_action = action_parse(action[1:], action[0])
            action_exec(
                parsed_action,
                mumu,
                ocr,
                executor,
                map_limit=map_config.get("map_limit", 999),
            )
            print(f"第{j+1}次行走完成")
            collect(
                mumu,
            )
            print(f"第{j+1}次收集完成")
        if "next_map_action" not in map_config:
            break
        if isinstance(map_config["next_map_action"], dict):
            teleport(map_config["next_map_action"], mumu)
            time.sleep(4.5)
        else:
            parsed_action = action_parse(
                map_config["next_map_action"][1:],
                map_config["next_map_action"][0],
            )
            action_exec(
                parsed_action,
                mumu,
                ocr,
                executor,
                map_limit=map_config.get("map_limit", 999),
            )
            time.sleep(3.5)
    print("所有地图处理完成")
    mumu.click((1776, 994))
    print("保存完成")

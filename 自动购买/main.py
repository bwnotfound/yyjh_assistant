import time
import sys, os
import json
import argparse

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
from utils import (
    Mumu,
    wait_screen_change,
    wait_pos_change,
    get_next_btn_pos,
    move_seq_exec,
)

start_map_index = 0
start_action_index = 0
config_name = "config.json"
# config_name = "刷蛇.json"
# config_name = "杀牛.json"
# config_name = "十方集.json"

parser = argparse.ArgumentParser()
parser.add_argument("--config_name", type=str, default=None)
args = parser.parse_args()
if args.config_name is not None:
    config_name = args.config_name

character_pos = (848, 534)
block_width, block_height = 160, 80
vision_size = "小"
assert vision_size == "小", "目前只支持小视野"
if vision_size == "小":
    move_max_num = 8
    vision_max_delta_limit = 10
    vision_min_delta_limit = 8

table_height_space = 70
chat_height_space = 100
table_first_btn_pos = (1715, 509)
chat_first_btn_pos = (1140, 472)

table_btn_click_time_delay = 0.4
chat_btn_click_time_delay = 0.4
normal_btn_click_time_delay = 0.25
screen_change_time_delay = 1

table_btn_pos_list = [
    (table_first_btn_pos[0], table_first_btn_pos[1] + i * table_height_space)
    for i in range(5)
]
chat_btn_pos_list = [
    (chat_first_btn_pos[0], chat_first_btn_pos[1] + i * chat_height_space)
    for i in range(5)
]
buy_item_start_pos = (606, 338)
buy_space = (443, 147)
buy_item_pos_list = [
    (buy_item_start_pos[0] + j * buy_space[0], buy_item_start_pos[1] + i * buy_space[1])
    for i in range(5)
    for j in range(2)
]
buy_increase_btn_pos = (1600, 618)
buy_btn_pos = (1454, 868)
buy_exit_btn_pos = (1757, 919)

blank_btn_pos = (1717, 762)
package_pos = (451, 998)
ticket_btn_pos = (602, 856)

symbol_pos = (832, 374)
symbol_color = (251, 245, 234)
symbol_color_diff_threshold = 50


def move_seq_parse(action_list):
    start_pos = action_list[0]
    action_list = action_list[1:]
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
                last_tgt_x, last_tgt_y = tgt_x, tgt_y
        else:
            last_tgt_x, last_tgt_y = tgt_x, tgt_y
    if last_tgt_x is not None and (last_x != last_tgt_x or last_y != last_tgt_y):
        result.append((last_tgt_x, last_tgt_y))
    return result


# def move_seq_exec(action_list, mumu: Mumu, map_size=None):
#     if map_size is None:
#         map_size = (999, 999)
#     is_fly = False
#     for i in range(1, len(action_list)):
#         tgt_x, tgt_y = action_list[i]
#         pre_pos = (
#             action_list[i - 1] if action_list[i - 1][0] != -1 else action_list[i - 2]
#         )
#         if (
#             sum(pre_pos) < vision_min_delta_limit
#             or sum(map_size) - sum(pre_pos) < vision_max_delta_limit
#         ):
#             new_character_pos = (
#                 character_pos[0],
#                 character_pos[1]
#                 + max(0, sum(pre_pos) - (sum(map_size) - vision_max_delta_limit))
#                 * block_height
#                 // 2
#                 + min(0, sum(pre_pos) - vision_min_delta_limit) * block_height // 2,
#             )
#         elif (abs(pre_pos[0] - map_size[0]) + pre_pos[1]) < vision_min_delta_limit:
#             new_character_pos = (
#                 character_pos[0]
#                 - min(
#                     0,
#                     (abs(pre_pos[0] - map_size[0]) + pre_pos[1])
#                     - vision_min_delta_limit,
#                 )
#                 * block_width
#                 // 2,
#                 character_pos[1],
#             )
#         elif (pre_pos[0] + abs(pre_pos[1] - map_size[1])) < vision_max_delta_limit:
#             assert False, "西南方向的地图极点暂不支持"
#         else:
#             new_character_pos = character_pos
#         if tgt_x == -1 and tgt_y == -1:
#             mumu.click(new_character_pos, 0.35)
#             is_fly = True
#             continue
#         x_block_cnt, y_block_cnt = (
#             tgt_x - action_list[i - 1 if not is_fly else i - 2][0],
#             tgt_y - action_list[i - 1 if not is_fly else i - 2][1],
#         )

#         x, y = (
#             new_character_pos[0]
#             + x_block_cnt * block_width // 2
#             - y_block_cnt * block_width // 2,
#             new_character_pos[1]
#             + x_block_cnt * block_height // 2
#             + y_block_cnt * block_height // 2,
#         )
#         img = mumu.capture_window()
#         mumu.click((x, y), 0.2)
#         wait_pos_change(mumu, threshold=0.01, fps=10, img=img, max_wait_time=3)
#         if is_fly:
#             is_fly = False
#             time.sleep(0.8)
#         wait_screen_change(mumu, reverse=True, threshold=0.1, fps=10, raw_diff=True)


def teleport(config, mumu: Mumu):
    mumu.click(package_pos, screen_change_time_delay)
    mumu.click(ticket_btn_pos, screen_change_time_delay)
    mumu.click(config["next_icon_pos"], screen_change_time_delay)
    mumu.click(
        (
            config["next_btn_pos"]
            if "next_btn_pos" in config
            else get_next_btn_pos(config["next_icon_pos"])
        ),
        screen_change_time_delay,
    )
    time.sleep(3)


def buy_exec(buy_action_list, mumu: Mumu, auto_enter=True):
    if auto_enter:
        mumu.click(table_btn_pos_list[1], delay=table_btn_click_time_delay)
        mumu.click(chat_btn_pos_list[0], delay=screen_change_time_delay)
    for index, num in buy_action_list:
        index -= 1
        mumu.click(buy_item_pos_list[index], delay=normal_btn_click_time_delay)
        while num > 1:
            mumu.click(buy_increase_btn_pos, delay=normal_btn_click_time_delay)
            num -= 1
        mumu.click(buy_btn_pos)
        time.sleep(0.8)
    mumu.click(buy_exit_btn_pos, delay=screen_change_time_delay)


def kill_exec(mumu: Mumu):
    mumu.click(table_btn_pos_list[1], delay=table_btn_click_time_delay)
    mumu.click(blank_btn_pos, delay=screen_change_time_delay)
    start = time.perf_counter()
    mumu.click(chat_btn_pos_list[0], delay=screen_change_time_delay)
    mumu.click(blank_btn_pos, delay=screen_change_time_delay)
    while time.perf_counter() - start < 4.5:
        time.sleep(4.5 - (time.perf_counter() - start))


def custom_action_exec(custom_action_list, mumu: Mumu):
    for action in custom_action_list:
        if action["mode"] in ["button", "click"]:
            if action["mode"] == "button":
                if action["pos"].startswith("table"):
                    tgt_list = table_btn_pos_list
                else:
                    assert action["pos"].startswith(
                        "chat"
                    ), "自定义动作位置必须以table或chat开头"
                    tgt_list = chat_btn_pos_list
                index = int(action["pos"].split("_")[1])
                index -= 1  # 转换为0基索引
                if index < 0 or index >= len(tgt_list):
                    raise ValueError(f"自定义动作位置索引超出范围: {index}")
                pos = tgt_list[index]
            else:
                pos = action["pos"]
            start = time.perf_counter()
            mumu.click(pos)
            skip = action.get("skip", 0)
            while skip > 0:
                mumu.click(blank_btn_pos, delay=screen_change_time_delay)
                skip -= 1
            delay = action.get("delay", normal_btn_click_time_delay)
            while time.perf_counter() - start < delay:
                time.sleep(delay - (time.perf_counter() - start))
        elif action["mode"] == "buy":
            buy_exec(action["actions"], mumu, auto_enter=False)
        else:
            raise ValueError(f"未知的自定义动作模式: {action['mode']}")


if __name__ == "__main__":
    mumu = Mumu("D:/MuMuPlayer/nx_device/12.0")
    with open(
        os.path.join(os.path.dirname(__file__), config_name), "r", encoding="utf-8"
    ) as f:
        config = json.load(f)

    for map_config in config:
        for i, action in enumerate(map_config["actions"]):
            parsed_action = move_seq_parse(action["path"])
            if isinstance(parsed_action, str):
                print(f"### {map_config['name']}:" + parsed_action)
                exit()
        if not (
            "next_map_action" not in map_config
            or isinstance(map_config["next_map_action"], dict)
        ):
            parsed_action = move_seq_parse(map_config["next_map_action"])
            if isinstance(parsed_action, str):
                print(f"### {map_config['name']}:" + parsed_action)
                exit()

    for i, map_config in enumerate(config):
        if start_map_index is not None and i < start_map_index:
            continue
        print(f"---开始处理第{i+1}张地图: {map_config['name']}---")
        for j, action in enumerate(map_config["actions"]):
            if (
                start_action_index is not None
                and i == start_map_index
                and j < start_action_index
            ):
                continue
            parsed_action = move_seq_parse(action["path"])
            move_seq_exec(
                parsed_action,
                mumu,
                map_size=map_config.get("map_size", None),
            )
            print(f"    第{j+1}次行走完成")
            if action["mode"] == "buy":
                buy_exec(action["buy"], mumu)
                print(f"    第{j+1}次购买完成")
            elif action["mode"] == "kill":
                kill_exec(mumu)
                print(f"    第{j+1}次杀动物完成")
            elif action["mode"] == "custom":
                custom_action_exec(action["actions"], mumu)
                print(f"    第{j+1}次自定义动作完成")
            else:
                raise ValueError(f"未知的动作模式: {action['mode']}")
        if "next_map_action" not in map_config:
            break
        if isinstance(map_config["next_map_action"], dict):
            teleport(map_config["next_map_action"], mumu)
        else:
            parsed_action = move_seq_parse(map_config["next_map_action"])
            move_seq_exec(
                parsed_action,
                mumu,
                map_size=map_config.get("map_size", None),
            )
            time.sleep(3.5)
    print("### 所有地图处理完成 ###")

import time
import sys, os

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
from utils import Mumu, OCR

table_2_btn = (1719, 589)
blank_pos = (1714, 753)

chat_pos_list = []
for i in range(5):
    chat_pos_list.append((1150, 473 + i * 101))


def skip():
    mumu.click(blank_pos, delay=0.2)


def get_remains():
    mumu.click(table_2_btn, delay=0.2)
    skip()
    mumu.click(chat_pos_list[0], delay=0.2)
    skip()
    result = []
    for i in range(4):
        mumu.click(chat_pos_list[i], delay=0.2)
        skip()
        time.sleep(0.2)
        text = ocr.get_text()
        text = text.replace("：", ":").replace(";", ":").replace(" ", "")
        text = text.split(":")
        text = int(text[-1].split("块")[0])
        if text > 100:
            text = text % 100
        result.append(text)
        skip()
        skip()
    mumu.click(chat_pos_list[-1], delay=0.2)
    for i in range(4):
        remains[i] += result[i]
    print(result)


if __name__ == "__main__":
    mumu = Mumu("D:/MuMu Player 12/shell/MuMuManager.exe")
    ocr = OCR(mumu, ocr_mode="paddleocr")

    remains = [0, 0, 0, 0]

    mumu.click((1247, 607), delay=2)
    get_remains()
    print("仓库清点完成")
    time.sleep(0.5)
    mumu.click((861, 500))
    time.sleep(2)
    get_remains()
    print("火炉房清点完成")
    time.sleep(0.5)
    mumu.click((783, 576))
    time.sleep(2)
    get_remains()
    print("最终结果: {}".format(remains))

import time
import sys, os

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")
from utils import Mumu


mumu = Mumu("D:/MuMuPlayer/nx_device/12.0/shell/MuMuManager.exe")

while True:
    try:
        mumu.click((1369, 856))
    except:
        pass
    time.sleep(1)

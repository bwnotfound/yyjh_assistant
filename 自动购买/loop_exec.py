import os, time
from tqdm import tqdm

exec_time = 100000
delay = 12
# config_name = "config.json"
# config_name = "刷蛇.json"
# config_name = "杀牛.json"
config_name = "十方集.json"
# config_name = "玛尔部落.json"

if __name__ == "__main__":
    root_dir = os.path.dirname(os.path.abspath(__file__))
    for i in range(exec_time):
        os.system(
            f"python {os.path.join(root_dir, 'main.py')} --config_name {config_name}"
        )
        # time.sleep(1)
        print(f"# 第{i+1}次执行完成 #")
        t_bar = tqdm(total=int(delay), leave=False)
        cnt = 0
        start = time.time()
        while time.time() - start < delay:
            time.sleep(1)
            delta = int(time.time() - start) - cnt
            if delta > 0:
                cnt += delta
                t_bar.update(delta)
        t_bar.close()

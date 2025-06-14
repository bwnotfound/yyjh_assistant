action = [[2, 7], [3, 2], [2, 1], [3, 5], [2, 2], [3, 2]]

start_x, start_y = 0, 28

x_cnt, y_cnt = 0, 0
for direction, distance in action:
    if direction == 0:
        x_cnt, y_cnt = 0, 0
        continue
    if direction == 1:
        y_cnt -= distance
    elif direction == 2:
        x_cnt += distance
    elif direction == 3:
        y_cnt += distance
    elif direction == 4:
        x_cnt -= distance

print(f"end_x: {start_x + x_cnt}, end_y: {start_y + y_cnt}")

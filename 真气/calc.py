ratio = 1 - 0.6 - 0.03 - 0.06
base = 120 - 30
max_limit = 1300
now = 289

one_real = int(base * ratio)


def format_time(seconds):
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return "%d:%02d:%02d" % (h, m, s)


print("1真气需要{}s".format(one_real))
print("100真气需要{:.2f}min".format(one_real * 100 / 60))
print("满真气需要{}".format(format_time(one_real * (max_limit - now))))

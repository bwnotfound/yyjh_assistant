"""
OCR 文本 → 结构化数据的解析工具.

每个 parse_* 函数都是纯函数, 失败时返回 None, 不抛异常,
让调用方决定是 retry 还是 warn.

OCR 字符容错:
    cnocr 偶尔会把形近字识别错 (例如 "精炼" → "精练"). 在 ``_OCR_NORMALIZE_MAP``
    集中维护这种字符级误识别归一化. 所有 parser 入口都走 _normalize 一遍,
    未来发现新的形近字误识别只需要在 map 里加一对, 不用改各处正则.
"""

from __future__ import annotations

import re
from typing import Optional

from .data import Attribute


# =============================================================================
# OCR 字符容错
# =============================================================================
# 所有映射都是 "OCR 误识别 → 正确字符". 加新条目时:
#   1. 必须有具体诊断证据 (在某个版本的 cnocr 上观察到)
#   2. 不要做 "过度通用" 的字符替换 (如 'O'→'0'), 否则会误伤词条名等中文场景;
#      如果数字字段确实需要数字容错, 在该 parser 内部单独做.
_OCR_NORMALIZE_MAP = str.maketrans(
    {
        "练": "炼",  # 已观察: cnocr 把 "精炼" 识别成 "精练"
        "煉": "炼",  # 繁体兼容 (理论上不会出现, 顺手做)
    }
)


def _normalize(text: str) -> str:
    """对 OCR 输出做字符级归一化, 减少形近字误识别的影响."""
    if not text:
        return text
    return text.translate(_OCR_NORMALIZE_MAP)


# =============================================================================
# 属性词条: "攻击 126" / "免伤 2.2%" / "◆防御 3135" / "罡气61"
# =============================================================================

# 中文词条名 + 数字 + 可选百分号; 容忍前缀的菱形/圆点装饰符号和空格
_ATTR_RE = re.compile(
    r"^[\s\u25c6\u25c7\u2666\u25cf\u25cb\u2022\u00b7\u2b25◆◇•·]*"
    r"([\u4e00-\u9fff]{1,6})"  # 中文 1~6 字
    r"\s*"
    r"(\d+(?:\.\d+)?)"  # 数值
    r"\s*(%?)"
    r"\s*$"
)


def parse_attribute(text: str) -> Optional[Attribute]:
    """单行文本 → Attribute. 例如:
    "攻击 126"     → Attribute("攻击", 126.0, "")
    "免伤 2.2%"    → Attribute("免伤", 2.2, "%")
    "◆防御3135"   → Attribute("防御", 3135.0, "")
    """
    if not text:
        return None
    s = _normalize(text.strip())
    # 全角空格转半角
    s = s.replace("\u3000", " ")
    m = _ATTR_RE.match(s)
    if not m:
        return None
    name, value_s, unit = m.group(1), m.group(2), m.group(3)
    try:
        value = float(value_s)
    except ValueError:
        return None
    return Attribute(name=name, value=value, unit=unit)


# =============================================================================
# 已精炼次数: "已精炼:1次" / "已精炼: 12 次"
# OCR 把 "炼" 识别成 "练" 是已知误识别, 由 _normalize 处理.
# =============================================================================

_REFINE_COUNT_RE = re.compile(r"已精炼\s*[:：]?\s*(\d+)\s*次")


def parse_refine_count(text: str) -> Optional[int]:
    """从 '已精炼:1次' 这种文本提取数字."""
    if not text:
        return None
    m = _REFINE_COUNT_RE.search(_normalize(text))
    if not m:
        return None
    return int(m.group(1))


# =============================================================================
# 银两: "27两315文" / "7250两280文" / "27两" / "315文"
# 统一返回总文数 (1 两 = 1000 文).
# =============================================================================

_MONEY_FULL_RE = re.compile(r"(\d+)\s*两\s*(\d+)\s*文")
_MONEY_LIANG_RE = re.compile(r"(\d+)\s*两(?!\d)")
_MONEY_WEN_RE = re.compile(r"(\d+)\s*文")


def parse_money(text: str) -> Optional[int]:
    """返回总文数; 无法解析返回 None."""
    if not text:
        return None
    s = _normalize(text.strip())
    m = _MONEY_FULL_RE.search(s)
    if m:
        return int(m.group(1)) * 1000 + int(m.group(2))
    # 退化: 只有两 / 只有文 / 没单位
    has_liang = _MONEY_LIANG_RE.search(s)
    has_wen = _MONEY_WEN_RE.search(s)
    total = 0
    matched = False
    if has_liang:
        total += int(has_liang.group(1)) * 1000
        matched = True
    if has_wen:
        total += int(has_wen.group(1))
        matched = True
    if matched:
        return total
    return None


# =============================================================================
# 材料库存/消耗: "844/5" → (844, 5)
# 第一个数 = 库存, 第二个数 = 单次消耗
# =============================================================================

_MATERIAL_RE = re.compile(r"(\d+)\s*[/／:]\s*(\d+)")


def parse_material(text: str) -> Optional[tuple[int, int]]:
    """'844/5' → (库存=844, 单次消耗=5).

    注意: OCR 偶尔把 '/' 识别为 '1' 或别的字符; 调用方可以先 fall back 到
    'all digits 拼接' 再人工拆分, 这里只处理标准情况.
    """
    if not text:
        return None
    m = _MATERIAL_RE.search(_normalize(text))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

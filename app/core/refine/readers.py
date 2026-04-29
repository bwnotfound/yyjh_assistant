"""
两个面板的读取器 + 界面识别.

核心设计 (slot-based):
    - 旧词条 (extra_attrs) 拆成 3 个独立 ROI: extra_attr_1/2/3, 按从上到下排列.
      每个 slot 单独 OCR 解析, 失败的 slot 视为 "该位置无词条" (装备未满 3 条时).
    - 新词条 (准备界面) 拆成 3 个独立 ROI: new_attr_slot_1/2/3, 跟 extra_attr_i
      水平对齐. 新词条只会出现在被替换的那一行的右侧, 所以:
          replace_index = 唯一能解析出 Attribute 的那个 slot 索引
      这是直接、确定的判定, 不依赖箭头颜色检测.

OCR 还是只跑一次:
    - 整张截图扔给 cnocr, 拿到所有 OCRLine.
    - 各 slot 提取靠 OCRLine.bbox 中心点是否落入 ROI 像素范围, 几乎零成本.

ROI 归一化 → 像素:
    profile 里所有 ROI 都是 [0,1]² 归一化, 内部用 ``_norm_to_px`` 转成像素 ROI
    后再跟 ``OCRLine.bbox`` 做几何比较.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
from PIL import Image, ImageEnhance

from .data import (
    Attribute,
    ConfirmPanelState,
    MaterialState,
    Money,
    StatusPanelState,
)
from .ocr_backend import OCRBackend, OCRLine
from .parser import (
    parse_attribute,
    parse_material,
    parse_money,
    parse_refine_count,
)
from .profile import RefineProfile

log = logging.getLogger(__name__)


# 槽位 ROI key 顺序: 上 → 中 → 下
_EXTRA_SLOT_KEYS: tuple[str, ...] = ("extra_attr_1", "extra_attr_2", "extra_attr_3")
_NEW_ATTR_SLOT_KEYS: tuple[str, ...] = (
    "new_attr_slot_1",
    "new_attr_slot_2",
    "new_attr_slot_3",
)


# =============================================================================
# 几何工具
# =============================================================================


def _norm_to_px(
    roi_norm: tuple[float, float, float, float], img_w: int, img_h: int
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = roi_norm
    return (
        int(round(x1 * img_w)),
        int(round(y1 * img_h)),
        int(round(x2 * img_w)),
        int(round(y2 * img_h)),
    )


def _lines_in(
    roi_px: tuple[int, int, int, int], lines: Sequence[OCRLine]
) -> list[OCRLine]:
    """筛出 bbox 中心点落在 ROI 内的 OCR 行, 按 y 升序."""
    rx1, ry1, rx2, ry2 = roi_px
    out = [ln for ln in lines if rx1 <= ln.cx <= rx2 and ry1 <= ln.cy <= ry2]
    return sorted(out, key=lambda l: l.cy)


# =============================================================================
# 界面识别
# =============================================================================


def detect_panel(
    lines: Sequence[OCRLine],
    bottom_roi_px: tuple[int, int, int, int],
) -> Optional[str]:
    """根据底部按钮区 OCR 文字判定当前界面.

    返回:
        "status"  - 结束界面 (有"精炼"按钮)
        "confirm" - 准备界面 (有"接受"/"取消"按钮)
        None      - 都不是
    """
    bottom = _lines_in(bottom_roi_px, lines)
    text = "".join(l.text for l in bottom)
    has_accept = "接受" in text
    has_cancel = "取消" in text
    has_refine = "精炼" in text or "精煉" in text

    # confirm 界面同时有"接受/取消", 不会有"精炼"按钮
    if has_accept or has_cancel:
        return "confirm"
    if has_refine:
        return "status"
    return None


# =============================================================================
# 共享基类
# =============================================================================


@dataclass
class _OCRSnapshot:
    """一次 OCR 的产物: lines + 图像尺寸 + 原图(给裁切兜底用)."""

    lines: list[OCRLine]
    img_w: int
    img_h: int
    image_rgb: "np.ndarray"  # 原图 (RGB, H×W×3), 用于 ROI 裁切重试


class _BasePanelReader:
    def __init__(self, profile: RefineProfile, ocr: OCRBackend) -> None:
        self.profile = profile
        self.ocr = ocr

    # ---------- OCR 一次 ----------

    def ocr_full(self, image: Image.Image) -> _OCRSnapshot:
        arr = np.array(image.convert("RGB"))
        lines = self.ocr.recognize(arr)
        return _OCRSnapshot(
            lines=lines,
            img_w=arr.shape[1],
            img_h=arr.shape[0],
            image_rgb=arr,
        )

    def _ocr_roi_crop(self, roi_key: str, snap: _OCRSnapshot) -> list[OCRLine]:
        """裁切兜底: 把 ROI 区域裁出来作为小图单独跑 OCR.

        cnocr 在大图全局检测时, 对低对比度/小尺寸文字会漏检 (例如蓝色描边
        的新词条). 单独裁切后小图相对尺寸大, detector 更敏感. 代价: 多一次
        OCR 调用 (~200ms).

        关键细节: 直接把 ~177×40 的小图喂 PaddleOCR detector 也会失败 ——
        它内部对最小尺寸有要求, 输入太小时下采样几次后特征图就没了, 框不出
        任何文本框. 所以这里把裁切结果**放大 3 倍**再喂, 让 detector 看
        起来像在处理一张 ~530×120 的中等尺寸图, 通过率明显高很多.

        放大后的 bbox 需要先除以放大倍率回到子图坐标, 再加 ROI 偏移回到
        原图坐标系.
        """
        if roi_key not in self.profile.roi:
            return []
        x1, y1, x2, y2 = _norm_to_px(self.profile.roi[roi_key], snap.img_w, snap.img_h)
        if x2 <= x1 or y2 <= y1:
            return []
        crop = snap.image_rgb[y1:y2, x1:x2]
        if crop.size == 0:
            return []
        # 放大 N 倍: 让 PaddleOCR detector 不被 "图太小" 直接打回
        UPSCALE = 3
        try:
            crop_pil = Image.fromarray(crop)
            crop_pil = crop_pil.resize(
                (crop.shape[1] * UPSCALE, crop.shape[0] * UPSCALE),
                Image.BICUBIC,
            )
            crop_up = np.array(crop_pil)
        except Exception:
            log.exception("裁切放大失败 (roi=%s)", roi_key)
            return []
        try:
            sub_lines = self.ocr.recognize(crop_up)
        except Exception:
            log.exception("裁切兜底 OCR 失败 (roi=%s)", roi_key)
            return []
        if not sub_lines:
            log.info(
                "裁切兜底: %s 区域子图 OCR 仍为 0 行 "
                "(cnocr 在 %dx%d 放大后图上也没检测到任何文字)",
                roi_key,
                crop.shape[1] * UPSCALE,
                crop.shape[0] * UPSCALE,
            )
            return []
        # 把子图坐标平移回原图坐标系 (先除回放大倍率, 再加 ROI 左上偏移)
        out: list[OCRLine] = []
        for ln in sub_lines:
            bx1, by1, bx2, by2 = ln.bbox
            out.append(
                OCRLine(
                    text=ln.text,
                    bbox=(
                        bx1 // UPSCALE + x1,
                        by1 // UPSCALE + y1,
                        bx2 // UPSCALE + x1,
                        by2 // UPSCALE + y1,
                    ),
                    score=ln.score,
                )
            )
        log.info(
            "裁切兜底: %s 子图 OCR 拿到 %d 行: %s",
            roi_key,
            len(out),
            [(l.text, round(l.score, 2)) for l in out],
        )
        return out

    # ---------- 字段提取 ----------

    def _read_equipment_name(self, snap: _OCRSnapshot) -> Optional[str]:
        roi_px = _norm_to_px(self.profile.roi["equipment_name"], snap.img_w, snap.img_h)
        cands = _lines_in(roi_px, snap.lines)
        if not cands:
            return None
        # 装备名是中文短文本, 取置信度最高且全为中文的那条
        chinese_only = [
            l for l in cands if all("\u4e00" <= c <= "\u9fff" for c in l.text)
        ]
        pool = chinese_only if chinese_only else cands
        return max(pool, key=lambda l: l.score).text

    def _read_refine_count(self, snap: _OCRSnapshot) -> Optional[int]:
        roi_px = _norm_to_px(self.profile.roi["refine_count"], snap.img_w, snap.img_h)
        cands = _lines_in(roi_px, snap.lines)
        # OCR 把"已精炼:1次"识别成多段是常见情况, 拼一起再正则
        text = "".join(l.text for l in cands)
        return parse_refine_count(text)

    def _read_base_attrs(self, snap: _OCRSnapshot) -> dict[str, float]:
        """基础属性是一个块 (2 行: 防御 / 罡气), 拼接后逐行解析."""
        roi_px = _norm_to_px(self.profile.roi["base_attrs"], snap.img_w, snap.img_h)
        cands = _lines_in(roi_px, snap.lines)
        out: dict[str, float] = {}
        for ln in cands:
            attr = parse_attribute(ln.text)
            if attr is not None:
                out[attr.name] = attr.value
        return out

    def _read_attr_slot(self, roi_key: str, snap: _OCRSnapshot) -> Optional[Attribute]:
        """读单个属性槽位; 返回 None 表示该槽位为空 / 无法解析.

        OCR 偶尔把单行词条拆成多段 ("攻击" 跟 "126" 分开), 这里先尝试每行
        独立解析; 全失败再按 x 顺序拼接重试一次.
        """
        if roi_key not in self.profile.roi:
            return None
        roi_px = _norm_to_px(self.profile.roi[roi_key], snap.img_w, snap.img_h)
        cands = _lines_in(roi_px, snap.lines)
        if not cands:
            return None
        # 优先: 每一行单独解析
        for ln in cands:
            attr = parse_attribute(ln.text)
            if attr is not None:
                return attr
        # 容错: 按 x 顺序拼接全 ROI 内文字
        merged = "".join(ln.text for ln in sorted(cands, key=lambda l: l.cx))
        attr = parse_attribute(merged)
        if attr is None:
            log.debug("槽位 %s 解析失败. 内容: %s", roi_key, [l.text for l in cands])
        return attr

    def _read_extra_attr_slots(self, snap: _OCRSnapshot) -> list[Optional[Attribute]]:
        """返回 3 元素列表, 解析失败的槽位为 None.

        装备只有 1~2 条附加属性时, 后面的槽位自然是 None.
        """
        return [self._read_attr_slot(k, snap) for k in _EXTRA_SLOT_KEYS]

    def _read_attr_slot_via_crop(
        self, roi_key: str, snap: _OCRSnapshot
    ) -> Optional[Attribute]:
        """裁切兜底版的属性槽位读取 (多变体 retry).

        cnocr/PaddleOCR 在低对比度蓝色字上行为不稳定: 同一区域不同图像变体
        会得到不同识别结果. 这里跑多种图像变体, 任一变体能让 parse_attribute
        成功就用. 最坏情况会跑 ~5 次 OCR (~1s), 仅在主路径漏检时触发.

        刻意不做 'O→0' 等激进字符容错 — 那会让 ['O','防御','O'] 拼接成
        '防御00' 解析成 Attribute('防御', 0.0), 比识别失败更糟糕, 会污染
        yaml 数据. 宁可这次失败由 runner 跳过, 等下一次 OCR.
        """
        if roi_key not in self.profile.roi:
            return None
        x1, y1, x2, y2 = _norm_to_px(self.profile.roi[roi_key], snap.img_w, snap.img_h)
        if x2 <= x1 or y2 <= y1:
            return None

        # ROI 上下左右加 8px padding (在原图边界内), 给 detector 一点上下文.
        # PaddleOCR detector 对边缘紧贴文字的小图反应不稳定, 加 padding 经常
        # 能把识别率显著拉高.
        PAD = 8
        ex_x1 = max(0, x1 - PAD)
        ex_y1 = max(0, y1 - PAD)
        ex_x2 = min(snap.img_w, x2 + PAD)
        ex_y2 = min(snap.img_h, y2 + PAD)
        crop_arr = snap.image_rgb[ex_y1:ex_y2, ex_x1:ex_x2]
        if crop_arr.size == 0:
            return None

        UPSCALE = 3
        crop_pil = Image.fromarray(crop_arr)
        target_size = (crop_pil.width * UPSCALE, crop_pil.height * UPSCALE)

        # ---- 候选变体: 顺序 cheap → expensive, 快路径优先早停 ----
        bicubic = crop_pil.resize(target_size, Image.BICUBIC)
        nearest = crop_pil.resize(target_size, Image.NEAREST)
        enhanced = ImageEnhance.Contrast(bicubic).enhance(2.5)
        enhanced = ImageEnhance.Sharpness(enhanced).enhance(2.0)
        saturated = ImageEnhance.Color(bicubic).enhance(3.0)  # 饱和度 3 倍
        # 蓝色 mask: 把蓝色像素抽成黑色 / 其他像素变白色, 二值图
        # 用相对差异判蓝 (B 显著大于 R 和 G), 兼容不同饱和度的蓝色描边
        rgb = np.array(bicubic).astype(int)
        r_ch, g_ch, b_ch = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        blue_mask = (b_ch > 80) & (b_ch - r_ch > 20) & (b_ch - g_ch > 20)
        binary = np.full_like(rgb, 255)
        binary[blue_mask] = 0
        blue_binary = Image.fromarray(binary.astype(np.uint8))

        variants = [
            ("bicubic_3x", bicubic),
            ("nearest_3x", nearest),
            ("bicubic+enhance", enhanced),
            ("bicubic+saturate", saturated),
            ("blue_mask_binary", blue_binary),
        ]

        for variant_name, img_pil in variants:
            try:
                sub_lines_raw = self.ocr.recognize(np.array(img_pil))
            except Exception:
                log.exception(
                    "裁切兜底 OCR 异常 [variant=%s, roi=%s]",
                    variant_name,
                    roi_key,
                )
                continue
            if not sub_lines_raw:
                log.info("裁切兜底 [%s] %s: detector 输出 0 行", variant_name, roi_key)
                continue
            log.info(
                "裁切兜底 [%s] %s: %s",
                variant_name,
                roi_key,
                [(ln.text, round(ln.score, 2)) for ln in sub_lines_raw],
            )
            # 1) 逐行解析: cnocr 把整段属性识别成一行的情况
            for ln in sub_lines_raw:
                attr = parse_attribute(ln.text)
                if attr is not None:
                    log.info(
                        "裁切兜底命中 [variant=%s, roi=%s]: %s → %s",
                        variant_name,
                        roi_key,
                        ln.text,
                        attr.display,
                    )
                    return attr
            # 2) 按 x 排序拼接重试: cnocr 偶尔把 '免伤' '2.2%' 切成两段
            #    这种正常拆分用拼接能救回; 但 ['O', '防御', 'O'] 这种数字
            #    识别错的拼接也救不回 (parse_attribute 严格要求 "中文+数字"
            #    格式), 所以拼接对乱切的情况是安全的.
            merged = "".join(
                ln.text for ln in sorted(sub_lines_raw, key=lambda l: l.cx)
            )
            attr = parse_attribute(merged)
            if attr is not None:
                log.info(
                    "裁切兜底命中(拼接)[variant=%s, roi=%s]: %r → %s",
                    variant_name,
                    roi_key,
                    merged,
                    attr.display,
                )
                return attr
            log.debug(
                "variant %s 解析失败, 尝试下一个变体 (roi=%s)",
                variant_name,
                roi_key,
            )

        log.info("所有 variant 裁切兜底都解析失败 (roi=%s)", roi_key)
        return None

    def _read_money_at(self, roi_key: str, snap: _OCRSnapshot) -> Optional[Money]:
        roi_px = _norm_to_px(self.profile.roi[roi_key], snap.img_w, snap.img_h)
        cands = _lines_in(roi_px, snap.lines)
        text = "".join(l.text for l in cands)
        wen = parse_money(text)
        return Money(wen) if wen is not None else None

    def _read_material_at(
        self, roi_key: str, snap: _OCRSnapshot, name: str
    ) -> Optional[MaterialState]:
        roi_px = _norm_to_px(self.profile.roi[roi_key], snap.img_w, snap.img_h)
        cands = _lines_in(roi_px, snap.lines)
        text = " ".join(l.text for l in cands)
        result = parse_material(text)
        if not result:
            log.debug("材料解析失败: roi=%s text=%r", roi_key, text)
            return None
        stock, cost = result
        return MaterialState(name=name, stock=stock, cost_per_use=cost)


# =============================================================================
# StatusPanelReader (结束界面)
# =============================================================================


class StatusPanelReader(_BasePanelReader):
    def read(self, image: Image.Image) -> Optional[StatusPanelState]:
        """识别失败 (不在结束界面 / 关键字段缺失) 时返回 None."""
        snap = self.ocr_full(image)
        bottom_px = _norm_to_px(
            self.profile.roi["bottom_buttons"], snap.img_w, snap.img_h
        )
        if detect_panel(snap.lines, bottom_px) != "status":
            return None
        return self._build_status(snap)

    def _build_status(self, snap: _OCRSnapshot) -> Optional[StatusPanelState]:
        eq_name = self._read_equipment_name(snap)
        if not eq_name:
            log.warning("StatusPanel: 装备名识别失败")
            return None

        refine_count = self._read_refine_count(snap)
        if refine_count is None:
            log.warning("StatusPanel: '已精炼:N次' 识别失败, 用 0 占位")
            refine_count = 0

        base_dict = self._read_base_attrs(snap)
        extra_slots = self._read_extra_attr_slots(snap)
        # 跳过 None, 保留出现顺序
        extra_attrs = [a for a in extra_slots if a is not None]

        # 材料映射: 装备名 → 两个材料名
        materials_cfg = self.profile.equipment_material_map.get(eq_name)
        if not materials_cfg or len(materials_cfg) < 2:
            log.warning("装备 [%s] 没有配置材料映射 (或不足 2 个), 用占位名", eq_name)
            materials_cfg = ["材料1", "材料2"]
        m1 = self._read_material_at("material_1", snap, materials_cfg[0])
        m2 = self._read_material_at("material_2", snap, materials_cfg[1])
        materials = [m for m in (m1, m2) if m is not None]

        cost = self._read_money_at("cost_money", snap) or Money(0)
        balance = self._read_money_at("balance_money", snap) or Money(0)

        return StatusPanelState(
            equipment_name=eq_name,
            refine_count=refine_count,
            base_attrs=base_dict,
            extra_attrs=extra_attrs,
            materials=materials,
            cost=cost,
            balance=balance,
        )


# =============================================================================
# ConfirmPanelReader (准备界面)
# =============================================================================


class ConfirmPanelReader(_BasePanelReader):
    def read(self, image: Image.Image) -> Optional[ConfirmPanelState]:
        snap = self.ocr_full(image)
        bottom_px = _norm_to_px(
            self.profile.roi["bottom_buttons"], snap.img_w, snap.img_h
        )
        if detect_panel(snap.lines, bottom_px) != "confirm":
            return None
        return self._build_confirm(snap)

    def _build_confirm(self, snap: _OCRSnapshot) -> Optional[ConfirmPanelState]:
        eq_name = self._read_equipment_name(snap)
        if not eq_name:
            log.warning("ConfirmPanel: 装备名识别失败")
            return None

        refine_count = self._read_refine_count(snap)
        if refine_count is None:
            log.warning("ConfirmPanel: '已精炼:N次' 识别失败, 用 0 占位")
            refine_count = 0

        base_dict = self._read_base_attrs(snap)

        # 旧词条 3 槽位 (上→中→下)
        before_slots = self._read_extra_attr_slots(snap)
        attrs_before = [a for a in before_slots if a is not None]
        if not attrs_before:
            log.warning("ConfirmPanel: 三个旧词条 slot 都解析失败, 跳过本帧")
            return None

        # 新词条 3 槽位: 哪个 slot 有内容就是 replace_index
        new_slots = [self._read_attr_slot(k, snap) for k in _NEW_ATTR_SLOT_KEYS]
        non_empty_indices = [i for i, a in enumerate(new_slots) if a is not None]

        # 兜底: 三个 slot 整图 OCR 都空时, 触发裁切重试.
        # cnocr 整图模式对低对比度文字 (例如蓝色描边的新词条) 偶尔会漏检,
        # 单独裁切后小图相对尺寸大, detector 更敏感. 代价: 多 3 次 OCR (~600ms).
        if not non_empty_indices:
            log.info("ConfirmPanel: 三个新词条 slot 整图 OCR 都空, 触发裁切兜底")
            new_slots = [
                self._read_attr_slot_via_crop(k, snap) for k in _NEW_ATTR_SLOT_KEYS
            ]
            non_empty_indices = [i for i, a in enumerate(new_slots) if a is not None]

        if not non_empty_indices:
            log.warning(
                "ConfirmPanel: 整图 + 裁切兜底 都解析不到新词条. 槽位内容: %s",
                [self._slot_debug_text(k, snap) for k in _NEW_ATTR_SLOT_KEYS],
            )
            return None
        if len(non_empty_indices) > 1:
            # 多于 1 个 slot 有内容 → 异常 (理论上每次只替换 1 行).
            # 取第一个, 但记 warning, 让用户去看是不是 ROI 重叠或上一帧残影.
            log.warning(
                "ConfirmPanel: 多个新词条 slot 都识别到内容 %s, 取第一个",
                non_empty_indices,
            )
        slot_idx = non_empty_indices[0]
        new_attr = new_slots[slot_idx]

        # 把 slot 索引 (0~2) 映射到 attrs_before (压缩掉 None) 中的索引
        replace_index = self._slot_to_state_index(slot_idx, before_slots)
        if replace_index == -1:
            log.warning(
                "ConfirmPanel: 新词条出现在 slot %d, 但同位置的旧词条解析失败. "
                "这说明 ROI 标定可能歪了, replace_index 暂记 -1",
                slot_idx,
            )

        return ConfirmPanelState(
            equipment_name=eq_name,
            refine_count_inclusive=refine_count,
            base_attrs=base_dict,
            extra_attrs_before=attrs_before,
            new_attr=new_attr,
            replace_index=replace_index,
        )

    @staticmethod
    def _slot_to_state_index(
        slot_index: int, before_slots: list[Optional[Attribute]]
    ) -> int:
        """把 0~2 的 slot 索引转换成 attrs_before (压缩 None 后) 的索引.

        如果对应位置的旧词条 slot 是 None (本不该发生, 但 OCR 会偶尔失败),
        返回 -1.
        """
        if not (0 <= slot_index < len(before_slots)):
            return -1
        if before_slots[slot_index] is None:
            return -1
        # 数 slot_index 之前有几个非空
        return sum(1 for a in before_slots[:slot_index] if a is not None)

    def _slot_debug_text(self, roi_key: str, snap: _OCRSnapshot) -> str:
        """诊断辅助: 拿到该 slot 内 OCR 的原始文本, 用于 warning 日志."""
        if roi_key not in self.profile.roi:
            return f"{roi_key}=<未配置>"
        roi_px = _norm_to_px(self.profile.roi[roi_key], snap.img_w, snap.img_h)
        cands = _lines_in(roi_px, snap.lines)
        return f"{roi_key}={[l.text for l in cands]}"


# =============================================================================
# UnionPanelReader (一次 OCR 两边都试)
# =============================================================================


class UnionPanelReader:
    """同时持有两个 reader, 一次截图 + 一次 OCR, 判定后只走对应分支.

    用于 GUI 里"刷新一下当前状态"这种不确定在哪个界面的场景.
    """

    def __init__(self, profile: RefineProfile, ocr: OCRBackend) -> None:
        self._status = StatusPanelReader(profile, ocr)
        self._confirm = ConfirmPanelReader(profile, ocr)
        self.profile = profile

    def read(self, image: Image.Image):
        """返回 ('status', StatusPanelState) / ('confirm', ConfirmPanelState) / (None, None)."""
        snap = self._status.ocr_full(image)
        bottom_px = _norm_to_px(
            self.profile.roi["bottom_buttons"], snap.img_w, snap.img_h
        )
        kind = detect_panel(snap.lines, bottom_px)
        if kind == "status":
            return "status", self._status._build_status(snap)
        if kind == "confirm":
            return "confirm", self._confirm._build_confirm(snap)
        return None, None

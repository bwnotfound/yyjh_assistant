"""装备精炼序列采集 - 主对话框.

UI 结构:
    顶部: 配置文件路径 + 重载 + 编辑材料映射
    中部: 当前装备状态 (实时识别结果展示) + "刷新当前状态" 按钮
    控制: 采集次数 / 详细日志开关 / 锁定装备名 / 开始-停止
    底部: 进度条 + 日志区

线程模型:
    - GUI 线程: QDialog
    - 工作线程: QThread + _Worker, 跑 RefineCaptureRunner.run
    - 通信: QSignal (跨线程自动 QueuedConnection)
    - 中断: threading.Event (cancel_event)

注意:
    - 日志只通过 _log() 进入 log_view, _log() 既可以从 GUI 线程也可以从 worker 线程
      调用 (worker 线程通过 signal.log.emit 间接调到 GUI 线程的 _log).
"""

from __future__ import annotations

import logging
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from utils import Mumu

from app.core.refine import (
    ConfirmPanelState,
    RefineCaptureRunner,
    RefineProfile,
    RefineRecord,
    RefineRecorder,
    RunnerHooks,
    StatusPanelState,
    always_cancel,
    build_ocr_backend,
)
from app.core.refine.profile import DEFAULT_LOG_DIR, DEFAULT_PROFILE_PATH
from app.core.refine.readers import (
    ConfirmPanelReader,
    StatusPanelReader,
    UnionPanelReader,
)

from .material_editor import MaterialEditorDialog
from .timing_settings import _TimingSettingsDialog

log = logging.getLogger(__name__)


# =============================================================================
# 跨线程信号
# =============================================================================


class _Signals(QObject):
    log = Signal(str, str)
    status_state = Signal(object)
    confirm_state = Signal(object)
    record = Signal(object)
    progress = Signal(int, int)
    finished = Signal(int, str)  # (done, error_msg) error_msg 空表示正常


# =============================================================================
# Worker 线程
# =============================================================================


class _Worker(QThread):
    def __init__(
        self,
        runner: RefineCaptureRunner,
        target: int,
        expected_eq: Optional[str],
    ) -> None:
        super().__init__()
        self.runner = runner
        self.target = target
        self.expected_eq = expected_eq
        self.signals = _Signals()

    def run(self) -> None:
        try:
            done = self.runner.run(self.target, self.expected_eq)
            self.signals.finished.emit(done, "")
        except Exception as e:
            log.exception("worker 异常")
            tb = traceback.format_exc(limit=5)
            self.signals.finished.emit(0, f"{type(e).__name__}: {e}\n{tb}")


# =============================================================================
# 主对话框
# =============================================================================


class RefineCaptureDialog(QDialog):
    """装备精炼序列采集对话框."""

    def __init__(self, mumu: Mumu, parent=None) -> None:
        super().__init__(parent)
        self.mumu = mumu
        self.setWindowTitle("装备精炼序列采集")
        self.resize(880, 760)

        self._profile: Optional[RefineProfile] = None
        self._worker: Optional[_Worker] = None
        self._cancel_event: Optional[threading.Event] = None
        self._ocr_cache = None  # 复用 OCR backend 实例 (cnocr 加载慢)

        self._build_ui()
        self._try_load_profile()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 顶: profile
        top = QHBoxLayout()
        top.addWidget(QLabel("配置文件:"))
        self.profile_label = QLabel(str(DEFAULT_PROFILE_PATH))
        self.profile_label.setStyleSheet("color: #555;")
        top.addWidget(self.profile_label, 1)
        b1 = QPushButton("重新加载")
        b1.clicked.connect(self._try_load_profile)
        top.addWidget(b1)
        b_setup = QPushButton("配置 ROI / 按钮坐标")
        b_setup.clicked.connect(self._open_profile_setup)
        top.addWidget(b_setup)
        b2 = QPushButton("编辑材料映射")
        b2.clicked.connect(self._open_material_editor)
        top.addWidget(b2)
        b_timing = QPushButton("延时配置")
        b_timing.setToolTip(
            "调整精炼按钮点击 / 界面等待相关的延时参数 "
            "(对应 yaml 里的 delay_after_*, poll_interval, panel_wait_timeout)"
        )
        b_timing.clicked.connect(self._open_timing_settings)
        top.addWidget(b_timing)
        b_logs = QPushButton("查看采集结果")
        b_logs.setToolTip("查看 config/refine_logs 下各装备的精炼序列记录")
        b_logs.clicked.connect(self._open_log_viewer)
        top.addWidget(b_logs)
        layout.addLayout(top)

        # 当前状态区
        gb = QGroupBox("当前装备状态 (实时识别结果)")
        form = QFormLayout(gb)
        self.lbl_panel_kind = QLabel("-")
        self.lbl_eq = QLabel("-")
        self.lbl_refine_no = QLabel("-")
        self.lbl_base = QLabel("-")
        self.lbl_extra = QLabel("-")
        self.lbl_new_attr = QLabel("-")
        self.lbl_materials = QLabel("-")
        self.lbl_money = QLabel("-")
        self.lbl_remain = QLabel("-")
        self.lbl_extra.setWordWrap(True)
        for lbl in (
            self.lbl_panel_kind,
            self.lbl_eq,
            self.lbl_refine_no,
            self.lbl_base,
            self.lbl_extra,
            self.lbl_new_attr,
            self.lbl_materials,
            self.lbl_money,
            self.lbl_remain,
        ):
            lbl.setMinimumHeight(20)
        form.addRow("当前界面:", self.lbl_panel_kind)
        form.addRow("装备名:", self.lbl_eq)
        form.addRow("已精炼次数:", self.lbl_refine_no)
        form.addRow("基础属性:", self.lbl_base)
        form.addRow("附加属性:", self.lbl_extra)
        form.addRow("新词条 (准备界面):", self.lbl_new_attr)
        form.addRow("材料 (结束界面):", self.lbl_materials)
        form.addRow("银两 (结束界面):", self.lbl_money)
        form.addRow("预计剩余可精炼:", self.lbl_remain)
        layout.addWidget(gb)

        # 刷新 + 诊断 (一行两个按钮)
        refresh_row = QHBoxLayout()
        b_refresh = QPushButton("刷新当前状态 (从游戏截图重新识别)")
        b_refresh.clicked.connect(self._refresh_state_now)
        refresh_row.addWidget(b_refresh, 2)
        b_diag = QPushButton("OCR 诊断 (打印每个 ROI 的原文)")
        b_diag.setToolTip(
            "截图后对所有已配置 ROI 跑一次 OCR, 把每个 ROI 内识别到的原始文本"
            "和解析结果都打到日志区. 字段识别异常 (例如 refine_count 显示 0 但日志"
            "提示识别失败) 时用来定位是 ROI 偏了还是 OCR 把字符识别成奇怪的东西."
        )
        b_diag.clicked.connect(self._diagnose_ocr)
        refresh_row.addWidget(b_diag, 2)
        layout.addLayout(refresh_row)
        self._btn_refresh = b_refresh
        self._btn_diag = b_diag

        # 控制区
        ctl_box = QGroupBox("采集控制")
        ctl = QFormLayout(ctl_box)
        self.spin_target = QSpinBox()
        self.spin_target.setRange(1, 99999)
        self.spin_target.setValue(50)
        ctl.addRow("采集次数:", self.spin_target)

        self.expected_eq_combo = QComboBox()
        self.expected_eq_combo.setEditable(True)
        self.expected_eq_combo.setToolTip("启动前必须选定一件装备 (用于一致性校验)")
        ctl.addRow("锁定装备名:", self.expected_eq_combo)

        self.cb_verbose = QCheckBox("详细日志 (打印每帧识别状态)")
        ctl.addRow(self.cb_verbose)
        layout.addWidget(ctl_box)

        # 启动 / 停止
        btns = QHBoxLayout()
        self.btn_start = QPushButton("开始采集")
        self.btn_start.setStyleSheet("font-weight: bold;")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_stop.setEnabled(False)
        btns.addWidget(self.btn_start, 1)
        btns.addWidget(self.btn_stop, 1)
        layout.addLayout(btns)

        # 进度
        prog_row = QHBoxLayout()
        self.lbl_progress = QLabel("进度: 0 / 0")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        prog_row.addWidget(self.lbl_progress)
        prog_row.addWidget(self.bar, 1)
        layout.addLayout(prog_row)

        # 日志区
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("日志:"))
        log_header.addStretch(1)
        b_clear_log = QPushButton("清空日志")
        b_clear_log.setToolTip("清空下方日志区内容 (不影响 yaml/采集状态)")
        b_clear_log.clicked.connect(self._clear_log)
        log_header.addWidget(b_clear_log)
        layout.addLayout(log_header)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        layout.addWidget(self.log_view, 1)

    # ---------- profile ----------

    def _try_load_profile(self) -> None:
        path = DEFAULT_PROFILE_PATH
        if not path.exists():
            self._log("warning", f"配置文件不存在: {path}. 请按 INTEGRATION 文档创建.")
            self.profile_label.setText(f"{path} (缺失)")
            self._profile = None
            return
        try:
            self._profile = RefineProfile.load(path)
        except Exception as e:
            self._log("error", f"加载配置失败: {e}")
            self._profile = None
            return
        self.profile_label.setText(str(path))
        self.expected_eq_combo.clear()
        self.expected_eq_combo.addItems(
            list(self._profile.equipment_material_map.keys())
        )
        self._ocr_cache = None  # 配置变了, OCR backend 也得重建
        self._log(
            "info",
            f"已加载配置. 装备列表: {list(self._profile.equipment_material_map.keys())}",
        )

    def _open_material_editor(self) -> None:
        if self._profile is None:
            QMessageBox.warning(self, "提示", "请先确保 refine_profile.yaml 存在")
            return
        dlg = MaterialEditorDialog(self._profile, DEFAULT_PROFILE_PATH, self)
        if dlg.exec():
            self._try_load_profile()

    def _open_timing_settings(self) -> None:
        """打开延时配置子对话框. 改完后重载 profile."""
        if self._profile is None:
            QMessageBox.warning(self, "提示", "请先确保 refine_profile.yaml 存在")
            return
        dlg = _TimingSettingsDialog(self._profile, DEFAULT_PROFILE_PATH, self)
        if dlg.exec():
            self._try_load_profile()

    def _open_log_viewer(self) -> None:
        """打开精炼序列查看器. 不依赖 profile, 直接读 refine_logs 目录."""
        from app.views.refine_log_viewer_dialog import RefineLogViewerDialog

        dlg = RefineLogViewerDialog(DEFAULT_LOG_DIR, self)
        dlg.exec()

    def _open_profile_setup(self) -> None:
        """打开 ROI / 按钮坐标 配置工具.

        允许在 profile 完全不存在时也打开 (用户可以从零开始填). 关闭后无论
        用户保存与否都重新加载, 让主对话框看到最新状态.
        """
        from app.views.refine_profile_setup_dialog import RefineProfileSetupDialog

        # 配置文件可能还不存在; setup dialog 会按需创建
        DEFAULT_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        dlg = RefineProfileSetupDialog(self.mumu, DEFAULT_PROFILE_PATH, self)
        dlg.exec()
        # 不管用户是否真的保存, 都尝试重载, 让主面板状态最新
        self._try_load_profile()

    # ---------- OCR backend (惰性) ----------

    def _get_ocr(self):
        if self._ocr_cache is None:
            self._log("info", "首次构建 OCR 后端 (可能需要数秒下载/加载模型)")
            self._ocr_cache = build_ocr_backend(self._profile.ocr)
        return self._ocr_cache

    # ---------- 单帧刷新 ----------

    def _refresh_state_now(self) -> None:
        if self._profile is None:
            QMessageBox.warning(self, "提示", "未加载配置")
            return
        try:
            ocr = self._get_ocr()
            reader = UnionPanelReader(self._profile, ocr)
            img = self.mumu.capture_window()
            kind, state = reader.read(img)
        except Exception as e:
            log.exception("刷新失败")
            self._log("error", f"刷新失败: {e}")
            return
        if state is None:
            self.lbl_panel_kind.setText("(无法识别, 可能未在精炼界面)")
            return
        self.lbl_panel_kind.setText(
            "结束界面 (当前装备状态)" if kind == "status" else "准备界面 (待确认)"
        )
        if kind == "status":
            self._show_status(state)
        else:
            self._show_confirm(state)

    # ---------- OCR 诊断 ----------

    def _diagnose_ocr(self) -> None:
        """对每个 ROI 单独跑 OCR 几何过滤, 把原始文本和解析结果都打到日志.

        用法场景: 当主对话框显示 "已精炼 0 次" 但日志却 "WARNING: 已精炼:N次 识别
        失败" 时, 点这个按钮可以看到 refine_count ROI 内部到底 OCR 出了什么 ——
        - 如果输出 "已精炼:O次" → cnocr 把数字 0 识别成字母 O, 这是 OCR 模型本身
          的局限, 应该让 parser 容错
        - 如果输出 [] (空)        → ROI 框得不对, 没框到任何文字, 改 ROI
        - 如果输出 "已精炼:0次"    → 解析器有 bug (实际不会, 已经测过)

        这个方法主动跑一次 OCR (复用主对话框 _ocr_cache, 不重新加载模型).
        """
        if self._profile is None:
            QMessageBox.warning(self, "提示", "未加载配置")
            return
        from app.core.refine.parser import (
            parse_attribute,
            parse_material,
            parse_money,
            parse_refine_count,
        )
        from app.core.refine.readers import _lines_in, _norm_to_px, detect_panel

        try:
            ocr = self._get_ocr()
            img = self.mumu.capture_window()
        except Exception as e:
            log.exception("诊断: 截图/OCR 加载失败")
            self._log("error", f"诊断失败: {e}")
            return

        try:
            import numpy as np

            arr = np.array(img.convert("RGB"))
            lines = ocr.recognize(arr)
            img_w, img_h = arr.shape[1], arr.shape[0]
        except Exception as e:
            log.exception("诊断: OCR 失败")
            self._log("error", f"OCR 失败: {e}")
            return

        self._log(
            "info",
            f"━━━ OCR 诊断 ━━━ 图片 {img_w}×{img_h}, OCR 共识别 {len(lines)} 行",
        )

        # 1) 界面识别
        bottom_px = _norm_to_px(
            self._profile.roi.get("bottom_buttons", (0, 0, 0, 0)),
            img_w,
            img_h,
        )
        kind = detect_panel(lines, bottom_px)
        self._log(
            "info",
            f"界面识别: {kind or '(都不像)'}  "
            f"  bottom_buttons 内 OCR 文字: "
            f"{[l.text for l in _lines_in(bottom_px, lines)]}",
        )

        # 2) 每个 ROI 内 OCR 原文 + 字段解析
        # 字段解析用对应 parser 函数, 失败的话明确标 ✗
        parser_for_roi = {
            "equipment_name": ("equipment_name", lambda t: t if t else None),
            "refine_count": ("已精炼:N次", parse_refine_count),
            "base_attrs": ("属性 (整块)", None),  # 多行属性, 单独处理
            "extra_attr_1": ("属性", parse_attribute),
            "extra_attr_2": ("属性", parse_attribute),
            "extra_attr_3": ("属性", parse_attribute),
            "new_attr_slot_1": ("属性", parse_attribute),
            "new_attr_slot_2": ("属性", parse_attribute),
            "new_attr_slot_3": ("属性", parse_attribute),
            "material_1": ("库存/单次", parse_material),
            "material_2": ("库存/单次", parse_material),
            "cost_money": ("银两(文)", parse_money),
            "balance_money": ("银两(文)", parse_money),
            "bottom_buttons": ("(界面识别用)", None),
        }
        for roi_key in self._profile.roi.keys():
            roi_norm = self._profile.roi[roi_key]
            roi_px = _norm_to_px(roi_norm, img_w, img_h)
            cands = _lines_in(roi_px, lines)
            texts = [l.text for l in cands]
            scores = [f"{l.score:.2f}" for l in cands]

            field_label, parse_fn = parser_for_roi.get(roi_key, ("(未知)", None))

            # 解析
            if parse_fn is None:
                parsed = "(N/A)"
            elif roi_key == "equipment_name":
                # 装备名: 取置信度最高且全为中文的那条
                chinese_only = [
                    l for l in cands if all("\u4e00" <= c <= "\u9fff" for c in l.text)
                ]
                pool = chinese_only if chinese_only else cands
                parsed = (
                    repr(max(pool, key=lambda l: l.score).text)
                    if pool
                    else "✗ (没拿到候选)"
                )
            elif roi_key == "refine_count":
                # 拼接后再解析
                merged = "".join(texts)
                v = parse_fn(merged)
                parsed = f"{v}" if v is not None else f"✗ 拼接后='{merged}'"
            elif roi_key in ("cost_money", "balance_money"):
                merged = "".join(texts)
                v = parse_fn(merged)
                parsed = (
                    f"{v} 文 ≈ {v // 1000}两{v % 1000}文"
                    if v is not None
                    else f"✗ 拼接后='{merged}'"
                )
            elif roi_key in ("material_1", "material_2"):
                merged = " ".join(texts)
                v = parse_fn(merged)
                parsed = f"{v}" if v else f"✗ 拼接后='{merged}'"
            else:
                # 属性 slots: 先逐行, 全失败再 x 顺序拼接
                attr = None
                for t in texts:
                    attr = parse_fn(t)
                    if attr is not None:
                        break
                if attr is None and cands:
                    merged = "".join(l.text for l in sorted(cands, key=lambda l: l.cx))
                    attr = parse_fn(merged)
                if attr is not None:
                    parsed = f"{attr.name}={attr.value}{attr.unit or ''}"
                elif not cands:
                    parsed = "✗ ROI 内无 OCR 输出"
                else:
                    parsed = f"✗ 解析失败 (cands={texts})"

            # base_attrs 特殊: 块内多行
            if roi_key == "base_attrs":
                attrs = [parse_attribute(t) for t in texts]
                pairs = [
                    f"{a.name}={a.value}{a.unit or ''}" for a in attrs if a is not None
                ]
                parsed = ", ".join(pairs) if pairs else "✗ 无解析"

            self._log(
                "info",
                f"  {roi_key:18s} ({field_label}): "
                f"OCR={texts} 置信度={scores} → {parsed}",
            )

        # ============ 裁切兜底验证: 对 OCR=[] 的 ROI 单独裁切再 OCR ============
        # cnocr 整图模式对低对比度 / 小尺寸文字会漏检 (例如蓝色描边的新词条).
        # 把每个空 ROI 区域单独裁出来作为小图再跑 OCR — 小图模式下相对尺寸大,
        # detector 更敏感. 这一段就是给你看 "如果切小再 OCR, 能不能补救".
        # 真正的采集流程里, 这套兜底已经在 readers._build_confirm 里自动启用.
        empty_rois: list[str] = []
        for k in self._profile.roi.keys():
            kpx = _norm_to_px(self._profile.roi[k], img_w, img_h)
            if not _lines_in(kpx, lines):
                empty_rois.append(k)
        if empty_rois:
            self._log(
                "info",
                f"━━━ 裁切兜底验证 ({len(empty_rois)} 个 OCR=[] 的 ROI 单独裁切再跑 OCR, 5 变体 retry) ━━━",
            )
            UPSCALE = 3
            PAD = 8
            from PIL import Image as _PILImage
            from PIL import ImageEnhance as _PILImageEnhance

            def _build_variants(crop):
                """返回 [(variant_name, pil_image), ...]; 跟 readers 保持一致"""
                pil = _PILImage.fromarray(crop)
                target = (pil.width * UPSCALE, pil.height * UPSCALE)
                bicubic = pil.resize(target, _PILImage.BICUBIC)
                nearest = pil.resize(target, _PILImage.NEAREST)
                enh = _PILImageEnhance.Contrast(bicubic).enhance(2.5)
                enh = _PILImageEnhance.Sharpness(enh).enhance(2.0)
                sat = _PILImageEnhance.Color(bicubic).enhance(3.0)
                # 蓝色 mask 二值图
                rgb_arr = np.array(bicubic).astype(int)
                rc, gc, bc = rgb_arr[..., 0], rgb_arr[..., 1], rgb_arr[..., 2]
                bm = (bc > 80) & (bc - rc > 20) & (bc - gc > 20)
                binary = np.full_like(rgb_arr, 255)
                binary[bm] = 0
                blue_pil = _PILImage.fromarray(binary.astype(np.uint8))
                return [
                    ("bicubic_3x", bicubic),
                    ("nearest_3x", nearest),
                    ("bicubic+enhance", enh),
                    ("bicubic+saturate", sat),
                    ("blue_mask_binary", blue_pil),
                ]

            for k in empty_rois:
                x1, y1, x2, y2 = _norm_to_px(self._profile.roi[k], img_w, img_h)
                if x2 <= x1 or y2 <= y1:
                    self._log("info", f"  {k}: ROI 退化, 跳过")
                    continue
                # 跟 readers 保持一致: 加 padding
                ex_x1 = max(0, x1 - PAD)
                ex_y1 = max(0, y1 - PAD)
                ex_x2 = min(img_w, x2 + PAD)
                ex_y2 = min(img_h, y2 + PAD)
                crop = arr[ex_y1:ex_y2, ex_x1:ex_x2]
                try:
                    variants = _build_variants(crop)
                except Exception as e:
                    self._log("warning", f"  {k}: 构建变体失败 {type(e).__name__}: {e}")
                    continue
                any_hit = False
                for vname, vimg in variants:
                    try:
                        sub_lines = ocr.recognize(np.array(vimg))
                    except Exception as e:
                        self._log(
                            "warning",
                            f"  {k:18s} [{vname}]: 异常 {type(e).__name__}: {e}",
                        )
                        continue
                    if sub_lines:
                        any_hit = True
                        texts = [(l.text, round(l.score, 2)) for l in sub_lines]
                        self._log(
                            "info",
                            f"  ✓ {k:18s} [{vname:18s}]: {texts}",
                        )
                    else:
                        self._log(
                            "info",
                            f"  ✗ {k:18s} [{vname:18s}]: 0 行",
                        )
                if not any_hit:
                    self._log(
                        "info",
                        f"  ⚠ {k:18s}: 所有变体都 0 行 (这块区域 cnocr detector 真的看不见)",
                    )

        # ============ 增强诊断: 列出未落入任何 ROI 的 OCR 行 ============
        # 这一段专门排查 "字段对应文本被 cnocr 识别到了, 但被 ROI 几何过滤丢掉"
        # 的场景 — 例如新词条 ROI 框偏了, 文本中心点落在 ROI 之外, 这时主诊断
        # 段会显示 ROI 内 OCR=[], 但这里会显示该文本及其归一化坐标, 拿去和
        # yaml 里 ROI 范围一比就知道偏在哪.
        roi_dict = self._profile.roi
        orphans: list[tuple[float, float, str, float]] = []  # (ny, nx, text, score)
        for ln in lines:
            nx_c = ln.cx / img_w
            ny_c = ln.cy / img_h
            in_any = False
            for v in roi_dict.values():
                if v[0] <= nx_c <= v[2] and v[1] <= ny_c <= v[3]:
                    in_any = True
                    break
            if not in_any:
                orphans.append((ny_c, nx_c, ln.text, ln.score))
        if orphans:
            self._log(
                "info",
                f"━━━ 未落入任何 ROI 的 OCR 行 ({len(orphans)} 条, 按 y 排序) ━━━",
            )
            for ny, nx, text, score in sorted(orphans):
                self._log(
                    "info",
                    f"  '{text}' 置信={score:.2f} 中心=(nx={nx:.4f}, ny={ny:.4f})",
                )
        else:
            self._log("info", "(所有 OCR 行都落入了至少一个 ROI)")

        self._log("info", "━━━ 诊断完成 ━━━")

    def _show_status(self, s: StatusPanelState) -> None:
        self.lbl_panel_kind.setText("结束界面 (当前装备状态)")
        self.lbl_eq.setText(s.equipment_name)
        self.lbl_refine_no.setText(f"{s.refine_count} 次 (已成功)")
        self.lbl_base.setText(self._fmt_base(s.base_attrs))
        self.lbl_extra.setText(", ".join(a.display for a in s.extra_attrs))
        self.lbl_new_attr.setText("-")
        self.lbl_materials.setText(", ".join(m.display for m in s.materials) or "-")
        self.lbl_money.setText(f"花费 {s.cost.display}    持有 {s.balance.display}")
        self.lbl_remain.setText(f"{s.remaining_uses()} 次")

    def _show_confirm(self, s: ConfirmPanelState) -> None:
        self.lbl_panel_kind.setText("准备界面 (精炼结果待确认)")
        self.lbl_eq.setText(s.equipment_name)
        self.lbl_refine_no.setText(f"{s.refine_count_inclusive} 次 (含本次)")
        self.lbl_base.setText(self._fmt_base(s.base_attrs))
        before_strs = []
        for i, a in enumerate(s.extra_attrs_before):
            mark = "  ←本次替换" if i == s.replace_index else ""
            before_strs.append(f"{a.display}{mark}")
        self.lbl_extra.setText("\n".join(before_strs))
        self.lbl_new_attr.setText(s.new_attr.display if s.new_attr else "-")
        self.lbl_materials.setText("(准备界面无材料信息)")
        self.lbl_money.setText("(准备界面无银两信息)")
        self.lbl_remain.setText("-")

    @staticmethod
    def _fmt_base(d: dict) -> str:
        if not d:
            return "-"
        return ", ".join(f"{k} {v:g}" for k, v in d.items())

    # ---------- 启动 / 停止 ----------

    def _start(self) -> None:
        if self._profile is None:
            QMessageBox.warning(self, "提示", "未加载配置")
            return
        target = self.spin_target.value()
        eq_name = self.expected_eq_combo.currentText().strip()
        if not eq_name:
            QMessageBox.warning(self, "提示", "请选择或输入装备名")
            return
        if eq_name not in self._profile.equipment_material_map:
            ans = QMessageBox.question(
                self,
                "未配置材料",
                f"装备 [{eq_name}] 还未配置材料映射, 材料显示名将用占位符. 继续?",
            )
            if ans != QMessageBox.Yes:
                return

        log_path = DEFAULT_LOG_DIR / f"{eq_name}.yaml"
        try:
            ocr = self._get_ocr()
        except Exception as e:
            log.exception("OCR 初始化失败")
            QMessageBox.critical(self, "OCR 初始化失败", str(e))
            return

        sr = StatusPanelReader(self._profile, ocr)
        cr = ConfirmPanelReader(self._profile, ocr)
        recorder = RefineRecorder(log_path, equipment_name=eq_name)

        # 进度条
        self.bar.setRange(0, target)
        self.bar.setValue(0)
        self.lbl_progress.setText(f"进度: 0 / {target}")

        self._cancel_event = threading.Event()
        # hooks 通过 signals 桥接, 保证跨线程更新 GUI 安全
        signals = _Signals()
        hooks = RunnerHooks(
            on_log=lambda lv, msg: signals.log.emit(lv, msg),
            on_status_state=lambda s: signals.status_state.emit(s),
            on_confirm_state=lambda s: signals.confirm_state.emit(s),
            on_record=lambda r: signals.record.emit(r),
            on_progress=lambda d, t: signals.progress.emit(d, t),
        )
        runner = RefineCaptureRunner(
            mumu=self.mumu,
            profile=self._profile,
            recorder=recorder,
            status_reader=sr,
            confirm_reader=cr,
            policy=always_cancel,
            hooks=hooks,
            cancel_event=self._cancel_event,
            verbose_state_log=self.cb_verbose.isChecked(),
        )

        self._worker = _Worker(runner, target, eq_name)
        # _Worker 自己也有一组 signals (用于 finished), 这里用单独 signals 给 hooks
        self._worker.signals.finished.connect(self._on_finished)
        signals.log.connect(self._log)
        signals.status_state.connect(self._show_status)
        signals.confirm_state.connect(self._show_confirm)
        signals.record.connect(self._on_record)
        signals.progress.connect(self._on_progress)
        # 持有 signals 防止被 GC (workder 释放后再释放)
        self._hook_signals = signals

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._log(
            "info",
            f"开始采集. 装备={eq_name} 目标次数={target} 日志={log_path}",
        )
        self._worker.start()

    def _stop(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.btn_stop.setEnabled(False)
        self._log("info", "已请求中断, 等待当前一轮结束...")

    def _on_record(self, rec: RefineRecord) -> None:
        # 默认不再额外打印 (runner 已经打了一行简短的);
        # verbose 模式下 runner 会另外打详细字段, 这里不重复.
        pass

    def _on_progress(self, done: int, target: int) -> None:
        self.bar.setRange(0, max(target, 1))
        self.bar.setValue(done)
        self.lbl_progress.setText(f"进度: {done} / {target}")

    def _on_finished(self, done: int, err: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._cancel_event = None
        if err:
            self._log("error", f"采集结束 (异常). 已采集 {done} 次. 错误: {err}")
        else:
            self._log("info", f"采集结束. 共采集 {done} 次")

    # ---------- 日志 ----------

    def _log(self, level: str, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{ts}] [{level.upper()}]"
        self.log_view.appendPlainText(f"{prefix} {msg}")

    def _clear_log(self) -> None:
        self.log_view.clear()

    # ---------- 资源 ----------

    def closeEvent(self, ev) -> None:
        if self._worker is not None and self._worker.isRunning():
            if self._cancel_event:
                self._cancel_event.set()
            self._worker.wait(3000)
        super().closeEvent(ev)

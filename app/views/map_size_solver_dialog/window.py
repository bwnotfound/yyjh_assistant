"""
反解 map_size 工具

前提:
  · character_pos 准确 (movement_profile)
  · block_size 误差小 (movement_profile.vision_sizes[当前视野])
  · view_area 准确 (movement_profile.map_view_area)

工作流:
  1. 由 map_registry_dialog 在编辑某地图时点"反解…"按钮唤起;
     地图名 + 当前 vision_size 由调用方锁定传入 (单地图反解, 不允许混合)
  2. 在游戏里把角色走到能"贴边"的位置 —— 越靠近地图屏幕边越好:
       走到屏幕**左**端 (W 激活) → 解 mh
       走到屏幕**右**端 (E 激活) → 解 mw
       走到屏幕**下**端 (S 激活) → 解 mw+mh (参考校验)
       走到屏幕**上**端 (N 激活) → 无信息
  3. 点 [取一次观测]:
       a. 工具截一帧 + OCR 取角色当前格坐标 (gx, gy)
       b. 弹放大窗, 点击"角色脚下中心"得屏幕实际位置 (px, py)
       c. solve_map_size_observation 反解出对应维度
  4. 重复 3 直到 mw / mh 都解出 (或任一维度因路径不通无法观测).
  5. 点 [应用到主对话框]:
       未观测的维度填 999 (语义: 无穷大不限, 下游 compute_character_screen_pos
       自动不激活该方向修正); 已观测的填四舍五入整数.

跨多次观测同一维度的策略: 取最新值 (与 view_area_solver 一致).
S 观测的 sum 仅作参考显示, 不参与 mw/mh 候选 (无法独立分离).

返回值: dialog.exec() 返回 QDialog.Accepted 时, dialog.accepted_size 为
(mw_int, mh_int); 取消则为 None.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from utils import Mumu

from app.core.ocr import CoordReader, TemplateOCR
from app.core.profiles import (
    MapSizeSolveResult,
    MovementProfile,
    VisionSpec,
    solve_map_size_observation,
)
from app.views.position_picker.window import ZoomedSnapshotPicker

log = logging.getLogger(__name__)


MINIMAP_TEMPLATE_DIR = Path("config/templates/minimap_coord")


# =============================================================================
# UI 层观测记录 (反解逻辑本身在 app.core.profiles)
# =============================================================================


@dataclass
class _Observation:
    """一次观测的完整记录 (用于历史展示)"""

    pre_pos: tuple[int, int]
    screen_pos: tuple[float, float]
    result: MapSizeSolveResult


@dataclass
class _PendingPick:
    """主窗截图 + OCR 完成后的待反解上下文 (子窗每次点击都用这份反解)"""

    pre_pos: tuple[int, int]


# =============================================================================
# 主对话框
# =============================================================================


class MapSizeSolverDialog(QDialog):
    """
    反解 map_size 的交互工具.

    构造参数 (由 map_registry_dialog 传入, 锁定上下文):
      mumu             : Mumu 实例
      movement_profile : 当前分辨率的运动配置 (含 character_pos / view_area / vision_sizes)
      vision_name      : 当前地图选定的视野档位名 (从 LocationRecord.vision_size 读)
      map_label        : 地图名 (仅显示用)
      initial_size     : 主对话框 spinbox 当前值 (mw, mh) 或 None;
                         作为初始候选填充 (用户再观测可覆盖).

    使用方式:
      dlg = MapSizeSolverDialog(mumu, mp, "小", "黑水沟", initial_size=(0, 0))
      if dlg.exec() == QDialog.Accepted:
          mw, mh = dlg.accepted_size  # tuple[int, int], 未观测维度为 999
    """

    def __init__(
        self,
        mumu: Mumu,
        movement_profile: MovementProfile,
        vision_name: str,
        map_label: str,
        initial_size: Optional[tuple[int, int]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"反解 map_size — {map_label}")
        self.resize(620, 660)

        self._mumu = mumu
        self._movement_profile = movement_profile
        self._vision_name = vision_name
        self._map_label = map_label

        # 校验依赖参数 (调用方负责拦, 这里再保险一次)
        self._vision_spec: Optional[VisionSpec] = movement_profile.vision_sizes.get(
            vision_name
        )
        self._view_area = movement_profile.map_view_area

        # 候选累积 (取最新策略: 列表里追加, 显示用最后一项)
        self._mw_latest: Optional[float] = None
        self._mh_latest: Optional[float] = None
        self._mw_plus_mh_latest: Optional[float] = None
        self._observations: list[_Observation] = []

        # 初始 spinbox 值: 若 != 0 则视为已有值, 显示但不进 observations
        # (避免被用户当作"上次反解的结果")
        self._initial_mw: Optional[int] = None
        self._initial_mh: Optional[int] = None
        if initial_size is not None:
            mw0, mh0 = initial_size
            if mw0 > 0:
                self._initial_mw = int(mw0)
            if mh0 > 0:
                self._initial_mh = int(mh0)

        # 应用结果 (exec accept 后由调用方读)
        self.accepted_size: Optional[tuple[int, int]] = None

        # OCR 链路 lazy
        self._template_ocr: Optional[TemplateOCR] = None
        self._coord_reader: Optional[CoordReader] = None

        # 放大子窗 + 待反解上下文
        self._zoom_picker: Optional[ZoomedSnapshotPicker] = None
        self._pending: Optional[_PendingPick] = None

        self._build_ui()
        self._refresh_state_display()

    # -------------------------------------------------------------------------
    # OCR 链路
    # -------------------------------------------------------------------------

    def _get_coord_reader(self) -> CoordReader:
        if self._coord_reader is not None:
            return self._coord_reader
        roi = self._movement_profile.minimap_coord_roi
        if roi is None:
            raise RuntimeError(
                "运动配置里未录入 minimap_coord_roi (小地图坐标 ROI). "
                "请先用主界面「ROI 截取工具」录入."
            )
        if self._template_ocr is None:
            try:
                self._template_ocr = TemplateOCR.from_dir(MINIMAP_TEMPLATE_DIR)
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"OCR 字符模板目录无可用模板: {MINIMAP_TEMPLATE_DIR}\n原因: {e}"
                ) from e
            except Exception as e:
                raise RuntimeError(f"OCR 模板加载失败: {type(e).__name__}: {e}") from e
        self._coord_reader = CoordReader(
            mumu=self._mumu, ocr=self._template_ocr, roi_norm=roi
        )
        return self._coord_reader

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # 顶部说明
        tip = QLabel(
            "前提: character_pos / block_size / view_area 三者准确.\n"
            "几何: 地图相对屏幕顺时针 45°, 4 个屏幕方位各对应 1 个地图维度:\n"
            "  · 走到屏幕**左**端 (玩家偏左, dx<0) → 解 mh\n"
            "  · 走到屏幕**右**端 (玩家偏右, dx>0) → 解 mw\n"
            "  · 走到屏幕**下**端 (玩家偏下, dy>0) → 解 mw+mh (参考校验, 无法单独分离)\n"
            "  · 走到屏幕**上**端 (玩家偏上, dy<0) → 无信息 (公式不含 mw/mh)\n"
            "应用时未观测的维度填 999 (= 该方向无穷大, 不做贴边修正)."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #555; font-size: 11px;")
        root.addWidget(tip)

        # ---- 上下文展示 (锁定的地图 / 视野 / 几何参数) ----
        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 4)
        form.addRow("当前地图:", QLabel(self._map_label))
        form.addRow(
            "视野档位:",
            QLabel(self._vision_name if self._vision_spec else "(未配置)"),
        )

        cp = self._movement_profile.character_pos
        form.addRow("character_pos:", QLabel(f"({cp[0]:.4f}, {cp[1]:.4f})"))

        if self._vision_spec is not None:
            bw, bh = self._vision_spec.block_size
            form.addRow("block_size:", QLabel(f"({bw:.4f}, {bh:.4f})"))
        else:
            form.addRow("block_size:", QLabel("(视野档位未配置, 无法反解)"))

        if self._view_area is not None:
            vx0, vy0, vx1, vy1 = self._view_area
            form.addRow(
                "view_area:",
                QLabel(f"({vx0:.4f}, {vy0:.4f}, {vx1:.4f}, {vy1:.4f})"),
            )
        else:
            form.addRow("view_area:", QLabel("(未配置, 无法反解)"))
        root.addLayout(form)

        # ---- 反解状态 ----
        state_box = QLabel("")
        state_box.setStyleSheet(
            "background: #f5f5f5; border: 1px solid #ddd; "
            "padding: 8px; font-family: monospace;"
        )
        state_box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._state_label = state_box
        root.addWidget(self._state_label)

        # ---- 操作按钮 ----
        btn_row = QHBoxLayout()
        self._btn_obs = QPushButton("取一次观测")
        self._btn_obs.setMinimumHeight(36)
        self._btn_obs.clicked.connect(self._on_observe)
        btn_row.addWidget(self._btn_obs)

        btn_clear = QPushButton("清空所有")
        btn_clear.clicked.connect(self._on_clear)
        btn_row.addWidget(btn_clear)

        btn_apply = QPushButton("应用到主对话框")
        btn_apply.setMinimumHeight(36)
        btn_apply.clicked.connect(self._on_apply)
        btn_row.addWidget(btn_apply)

        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        root.addLayout(btn_row)

        # ---- 观测历史 ----
        root.addWidget(QLabel("观测历史:"))
        self._history_list = QListWidget()
        self._history_list.setStyleSheet("font-family: monospace; font-size: 11px;")
        root.addWidget(self._history_list, 1)

        # 依赖缺失则禁用观测按钮
        if self._vision_spec is None or self._view_area is None:
            self._btn_obs.setEnabled(False)
            tip2 = QLabel(
                "⚠ 依赖参数缺失: 请先在「运动配置」里补全 view_area + 当前视野的 block_size."
            )
            tip2.setStyleSheet("color: #c00; font-weight: bold;")
            root.addWidget(tip2)

    def _refresh_state_display(self) -> None:
        """
        显示规则:
          - 已观测维度: 显示浮点 + 四舍五入值 + "(本次反解)" 标记
          - sum 推算出的维度: 显示推算结果 + "(S 推算)" 标记
          - 仅有主对话框初值: 显示初值 + "(初值)" 标记
          - 都没有: 显示 999 + "(默认)" 标记
        额外行: 如果有 mw_plus_mh, 展示 sum 校验对比.
        """
        mw_int, mh_int, mw_src, mh_src = self._resolve_both()

        def _line(latest: Optional[float], int_v: int, src: str, label: str) -> str:
            if latest is not None:
                # 本次反解: 浮点 + 圆括号四舍五入
                return f"{label} = {latest:.3f} (round → {int_v})  [{src}]"
            return f"{label} = {int_v}  [{src}]"

        lines = [
            _line(self._mw_latest, mw_int, mw_src, "mw"),
            _line(self._mh_latest, mh_int, mh_src, "mh"),
        ]

        # sum 校验行: 拿"非推算 effective" (latest 优先, 其次非占位 initial) 与
        # sum 对比. 故意不用 _resolve_both 的输出, 因为 _resolve_both 里 mw 可能
        # 就是从 sum 推算来的, 拿它再去校验 sum 是循环论证.
        if self._mw_plus_mh_latest is not None:
            mw_eff = self._effective_value(self._mw_latest, self._initial_mw)
            mh_eff = self._effective_value(self._mh_latest, self._initial_mh)
            sum_check = ""
            if mw_eff is not None and mh_eff is not None:
                expected = mw_eff + mh_eff
                diff = self._mw_plus_mh_latest - expected
                tag = "✓ 一致" if abs(diff) <= 2 else "⚠ 偏差较大"
                sum_check = f"  vs 已知 mw+mh={expected:.1f} (Δ={diff:+.2f}) {tag}"
            lines.append(f"S 校验: mw+mh ≈ {self._mw_plus_mh_latest:.3f}{sum_check}")

        lines.append(f"观测次数: {len(self._observations)}")
        self._state_label.setText("\n".join(lines))

    # -------------------------------------------------------------------------
    # 回退链解析: 给一个维度, 按 [本次反解 > sum 推算 > 主对话框初值 > 999] 顺序
    # 解出最终整数值. 同时被 _refresh_state_display 和 _on_apply 复用, 保证
    # 屏上展示的预览值 == 应用时真正写入的值.
    # -------------------------------------------------------------------------

    def _effective_value(
        self, latest: Optional[float], initial: Optional[int]
    ) -> Optional[float]:
        """
        给定一维度, 返回"非推算"的有效估计 (本次反解 > 主对话框非占位初值).
        用于另一维度的 sum 推算输入. 999 视为占位 (用户来反解工具说明想覆盖 999),
        不参与推算约束.
        """
        if latest is not None:
            return float(latest)
        if initial is not None and initial < 999:
            return float(initial)
        return None

    def _resolve_dim(
        self,
        latest: Optional[float],
        initial: Optional[int],
        sum_v: Optional[float],
        other_eff: Optional[float],
        label: str,  # "mw" 或 "mh"
        other_label: str,  # 与 label 互补
    ) -> tuple[int, str]:
        """
        按回退链路解出 (int_value, src_label):
          1. latest      → "本次反解"
          2. sum 推算: sum_v - other_eff (前提两者都非 None, 推算结果合法)
                         → "S 推算 (...)"
          3. initial < 999 → "主对话框初值"
          4. initial == 999 → 视为占位但允许沿用 ("主对话框初值 (999 占位)")
          5. 默认 999     → "未观测 (默认 999)"
        """
        if latest is not None:
            return int(round(latest)), "本次反解"
        if sum_v is not None and other_eff is not None:
            inferred = sum_v - other_eff
            if 0 < inferred <= 999:
                return (
                    int(round(inferred)),
                    f"S 推算 ({label} = sum - {other_label} "
                    f"= {sum_v:.2f} - {other_eff:.2f} = {inferred:.2f})",
                )
        if initial is not None:
            if initial < 999:
                return initial, "主对话框初值"
            else:  # initial == 999
                return 999, "主对话框初值 (999 占位)"
        return 999, "未观测 (默认 999)"

    def _resolve_both(self) -> tuple[int, int, str, str]:
        """同时解出 mw 和 mh, 返回 (mw, mh, mw_src, mh_src)."""
        # Step 1: 算各自的 "非推算 effective" (用于另一维的 sum 推算输入).
        # 注意 effective 不能用 _resolve_dim 的输出 (那个已含推算, 会让两维互相
        # 推算造成循环), 所以单独计算.
        mw_eff = self._effective_value(self._mw_latest, self._initial_mw)
        mh_eff = self._effective_value(self._mh_latest, self._initial_mh)

        # Step 2: 各自走完整回退链
        mw_int, mw_src = self._resolve_dim(
            self._mw_latest,
            self._initial_mw,
            self._mw_plus_mh_latest,
            mh_eff,
            "mw",
            "mh",
        )
        mh_int, mh_src = self._resolve_dim(
            self._mh_latest,
            self._initial_mh,
            self._mw_plus_mh_latest,
            mw_eff,
            "mh",
            "mw",
        )
        return mw_int, mh_int, mw_src, mh_src

    # -------------------------------------------------------------------------
    # 取观测
    # -------------------------------------------------------------------------

    def _on_observe(self) -> None:
        if self._vision_spec is None or self._view_area is None:
            return  # 按钮已禁用, 兜底

        # 截一帧 + 同帧 OCR
        try:
            img = self._mumu.capture_window()
        except Exception as e:
            log.exception("截图失败")
            QMessageBox.critical(self, "截图失败", f"{type(e).__name__}: {e}")
            return
        try:
            reader = self._get_coord_reader()
        except RuntimeError as e:
            QMessageBox.warning(self, "OCR 不可用", str(e))
            return
        try:
            coord, raw_text, _ = reader.read_verbose(image=img)
        except Exception as e:
            log.exception("OCR 调用异常")
            QMessageBox.critical(self, "OCR 调用异常", f"{type(e).__name__}: {e}")
            return
        if coord is None:
            QMessageBox.warning(
                self,
                "OCR 读不到坐标",
                f"OCR 拼出的原始字符串: {raw_text!r}\n请确认游戏小地图正在显示坐标.",
            )
            return
        gx, gy = coord
        log.info("反解 map_size: OCR=(%d, %d) on %s", gx, gy, self._map_label)

        self._pending = _PendingPick(pre_pos=(gx, gy))

        prompt = (
            f"当前观测: {self._map_label}  OCR=({gx},{gy})  视野={self._vision_name}    "
            f"请精确点击「角色脚下中心」"
        )
        if self._zoom_picker is None:
            self._zoom_picker = ZoomedSnapshotPicker(
                img,
                self._mumu,
                parent=self,
                show_recapture=False,  # OCR 已锁帧, 子窗换图会让 OCR 过期
                prompt=prompt,
            )
            self._zoom_picker.point_picked.connect(self._on_pick_in_zoom)
        else:
            self._zoom_picker.set_image(img)
            self._zoom_picker.set_prompt(prompt)

        self._zoom_picker.set_record_count(len(self._observations), None)
        self._zoom_picker.show()
        self._zoom_picker.raise_()
        self._zoom_picker.activateWindow()

    def _on_pick_in_zoom(self, nx: float, ny: float) -> None:
        if self._pending is None:
            log.warning("_on_pick_in_zoom 触发但 _pending 为空, 忽略")
            return
        if self._vision_spec is None or self._view_area is None:
            return

        screen_pos = (nx, ny)
        result = solve_map_size_observation(
            pre_pos=self._pending.pre_pos,
            screen_pos=screen_pos,
            view_area=self._view_area,
            block_size=self._vision_spec.block_size,
            character_pos=self._movement_profile.character_pos,
        )

        # 累积 (取最新策略)
        if result.mw is not None:
            self._mw_latest = result.mw
        if result.mh is not None:
            self._mh_latest = result.mh
        if result.mw_plus_mh is not None:
            self._mw_plus_mh_latest = result.mw_plus_mh

        # 历史
        obs = _Observation(
            pre_pos=self._pending.pre_pos, screen_pos=screen_pos, result=result
        )
        self._observations.append(obs)
        self._append_history_item(obs)
        self._refresh_state_display()

    def _append_history_item(self, obs: _Observation) -> None:
        def _f(v: Optional[float]) -> str:
            return f"{v:.3f}" if v is not None else "—"

        head = (
            f"#{len(self._observations)} OCR=({obs.pre_pos[0]},{obs.pre_pos[1]}) "
            f"屏幕=({obs.screen_pos[0]:.4f},{obs.screen_pos[1]:.4f}) "
            f"方向={obs.result.direction}"
        )
        solved = (
            f"  解出: mw={_f(obs.result.mw)}  "
            f"mh={_f(obs.result.mh)}  "
            f"mw+mh={_f(obs.result.mw_plus_mh)}"
        )
        item_text = head + "\n" + solved
        for note in obs.result.notes:
            item_text += "\n  " + note
        item = QListWidgetItem(item_text)
        # 警告 (note 里有 ⚠) 标红; 此次观测什么都没解出 (none / 仅 N) 标灰.
        # has_any 检查涵盖了所有"无信息"情形, 包括 direction == "N" 或 "none".
        # X+Y 组合里只要有一个轴解出值 (例如 "W+N" 解出了 mh), 就算有信息, 不标灰.
        if any("⚠" in n for n in obs.result.notes):
            item.setForeground(QColor(180, 60, 60))
        elif not obs.result.has_any:
            item.setForeground(QColor(120, 120, 120))
        self._history_list.addItem(item)
        self._history_list.scrollToBottom()

    # -------------------------------------------------------------------------
    # 清空 / 应用
    # -------------------------------------------------------------------------

    def _on_clear(self) -> None:
        ans = QMessageBox.question(
            self,
            "确认清空",
            "清空当前累计候选 + 观测历史. 主对话框 spinbox 不会被改动.",
        )
        if ans != QMessageBox.Yes:
            return
        self._mw_latest = None
        self._mh_latest = None
        self._mw_plus_mh_latest = None
        self._observations.clear()
        self._history_list.clear()
        self._refresh_state_display()

    def _on_apply(self) -> None:
        """
        组装最终 (mw_int, mh_int) 写回主对话框 spinbox.
        回退链路统一走 _resolve_both: 本次反解 > sum 推算 > 主对话框初值 > 999.
        """
        mw_int, mh_int, mw_src, mh_src = self._resolve_both()

        # 判定本次工具用得有不有效:
        #   "本次反解" / "S 推算" 都算有效结果; 仅"主对话框初值"或"默认 999"
        #   说明用户什么也没观出来, 弹 warning.
        def _is_effective(src: str) -> bool:
            return src.startswith("本次反解") or src.startswith("S 推算")

        if not _is_effective(mw_src) and not _is_effective(mh_src):
            ans = QMessageBox.question(
                self,
                "无有效反解",
                f"本次未反解出任何 mw/mh 候选 (含 sum 推算).\n"
                f"将填入: mw={mw_int} ({mw_src}), mh={mh_int} ({mh_src}).\n\n"
                f"确认应用?",
            )
            if ans != QMessageBox.Yes:
                return

        # 一致性检查: spinbox 范围 0~999 (见 map_registry_dialog._build_detail_widget)
        if not (1 <= mw_int <= 999 and 1 <= mh_int <= 999):
            QMessageBox.warning(
                self,
                "数值越界",
                f"mw={mw_int}, mh={mh_int} 不在 [1, 999] 范围内, 拒绝应用. "
                f"请重新观测或检查依赖参数.",
            )
            return

        msg = (
            f"将填回主对话框 spinbox:\n"
            f"  mw = {mw_int}  ({mw_src})\n"
            f"  mh = {mh_int}  ({mh_src})\n\n"
            f"主对话框需要点「保存到 YAML」才会真正落盘. 确认?"
        )
        ans = QMessageBox.question(self, "确认应用", msg)
        if ans != QMessageBox.Yes:
            return

        self.accepted_size = (mw_int, mh_int)
        self.accept()

    # -------------------------------------------------------------------------
    # 关闭
    # -------------------------------------------------------------------------

    def closeEvent(self, ev) -> None:
        if self._zoom_picker is not None:
            self._zoom_picker.close()
        super().closeEvent(ev)

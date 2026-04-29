"""延时配置子对话框.

调整 yaml 里几个跟"点击节奏 / 等待超时"相关的延时参数:
    - delay_after_refine_click  : 点完 [精炼] 之后等多久再去识别准备界面
    - delay_after_decision_click: 点完 [接受]/[取消] 之后等多久再回结束界面识别
    - poll_interval             : 等待界面切换时的轮询间隔
    - panel_wait_timeout        : 等待界面切换的总超时

保存策略: 跟材料编辑器一样, 只更新这 4 个字段, 不动 ROI/按钮/材料映射等.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.core.refine.profile import RefineProfile

log = logging.getLogger(__name__)


# 默认值 (跟 RefineProfile.from_yaml_data 里的 fallback 保持一致)
_DEFAULTS = {
    "delay_after_refine_click": 1.5,
    "delay_after_decision_click": 1.5,
    "poll_interval": 0.3,
    "panel_wait_timeout": 8.0,
}

# 字段定义: (key, 显示名, tooltip, 取值范围 (min, max), 步长)
_FIELDS: tuple[tuple[str, str, str, tuple[float, float], float], ...] = (
    (
        "delay_after_refine_click",
        "点完 [精炼] 后等待 (秒)",
        "点击精炼按钮 → 等这么久 → 才开始截图识别准备界面.\n"
        "太短: 准备界面动画还没完成, OCR 漏检概率高 → 触发裁切兜底拖慢\n"
        "太长: 单次精炼总耗时变长. 建议 1.5 ~ 2.5",
        (0.0, 10.0),
        0.1,
    ),
    (
        "delay_after_decision_click",
        "点完 [接受/取消] 后等待 (秒)",
        "点击接受/取消按钮 → 等这么久 → 才开始截图识别下一轮的结束界面.\n"
        "建议跟 delay_after_refine_click 一致, 1.5 ~ 2.5",
        (0.0, 10.0),
        0.1,
    ),
    (
        "poll_interval",
        "等待界面切换的轮询间隔 (秒)",
        "等待界面切换时, 每隔这么久重新截图+识别一次.\n"
        "短: 切换响应快, 但 OCR 调用更频繁 (CPU 高)\n"
        "长: CPU 占用低, 但响应慢. 建议 0.2 ~ 0.4",
        (0.05, 2.0),
        0.05,
    ),
    (
        "panel_wait_timeout",
        "界面切换总超时 (秒)",
        "等待界面切换的总时间; 超时后 runner 抛异常停止采集.\n"
        "如果你装备识别慢, 适当调大避免误超时. 建议 8 ~ 15",
        (1.0, 60.0),
        1.0,
    ),
)


class _TimingSettingsDialog(QDialog):
    """RefineCaptureDialog 用的内嵌子对话框."""

    def __init__(
        self,
        profile: RefineProfile,
        profile_path: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("延时配置 (精炼采集相关)")
        self.resize(560, 360)
        self._profile_path = Path(profile_path)
        self._spins: dict[str, QDoubleSpinBox] = {}
        self._build_ui(profile)

    def _build_ui(self, profile: RefineProfile) -> None:
        root = QVBoxLayout(self)

        info = QLabel(
            "调整精炼采集流程里的延时. 改完保存会直接写回 yaml, "
            "不影响其他字段 (ROI / 按钮坐标 / 材料映射 / OCR 配置).\n"
            "改动会在主对话框点 [重新加载] 或重新打开后生效."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #555;")
        root.addWidget(info)

        form = QFormLayout()
        for key, label, tooltip, (lo, hi), step in _FIELDS:
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(step)
            spin.setDecimals(2)
            spin.setSuffix(" 秒")
            spin.setValue(getattr(profile, key))
            spin.setToolTip(tooltip)
            self._spins[key] = spin
            form.addRow(label, spin)
        root.addLayout(form)

        # 重置 + OK/Cancel
        bot = QHBoxLayout()
        b_reset = QPushButton("恢复默认值")
        b_reset.setToolTip(
            "把所有字段恢复到代码里的默认值, 但还需要点保存才会写入 yaml"
        )
        b_reset.clicked.connect(self._on_reset)
        bot.addWidget(b_reset)
        bot.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("保存到 yaml")
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        bot.addWidget(bb)
        root.addLayout(bot)

    def _on_reset(self) -> None:
        for key, spin in self._spins.items():
            spin.setValue(_DEFAULTS[key])

    def _on_save(self) -> None:
        # 读 yaml + 仅更新这 4 个字段 + 写回
        try:
            if self._profile_path.exists():
                data = (
                    yaml.safe_load(self._profile_path.read_text(encoding="utf-8")) or {}
                )
            else:
                data = {}
        except Exception as e:
            log.exception("读 profile 失败")
            QMessageBox.critical(self, "读取失败", f"{type(e).__name__}: {e}")
            return

        for key, spin in self._spins.items():
            data[key] = round(spin.value(), 2)

        # 备份
        if self._profile_path.exists():
            try:
                bak = self._profile_path.with_suffix(self._profile_path.suffix + ".bak")
                bak.write_bytes(self._profile_path.read_bytes())
            except OSError:
                log.exception("备份失败 (继续写主文件)")

        try:
            self._profile_path.write_text(
                yaml.safe_dump(
                    data,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            log.exception("写 profile 失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return

        QMessageBox.information(
            self,
            "已保存",
            "延时配置已写入 yaml.\n请在主对话框点 [重新加载], 或重新打开主对话框后生效.",
        )
        self.accept()

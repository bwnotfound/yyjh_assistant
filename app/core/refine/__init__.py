"""装备精炼序列采集模块.

子模块:
    data           数据类 (Attribute / Money / 两个 PanelState / RefineRecord)
    parser         OCR 文本 → 结构化解析工具
    arrow_detector 绿色箭头检测 (HSV mask + 连通域); 当前 readers 不使用,
                   保留作为未来辅助校验的备用.
    ocr_backend    OCR 后端抽象 + cnocr 实现 + 工厂
    profile        refine_profile.yaml 加载/保存
    readers        StatusPanelReader / ConfirmPanelReader / UnionPanelReader
                   (slot-based: 旧词条 3 槽位 + 新词条 3 槽位, 水平对齐)
    recorder       一件装备一个 yaml 的持久化
    runner         采集主循环 + 决策接口 + Hooks
"""

from .data import (
    Attribute,
    ConfirmPanelState,
    MaterialState,
    Money,
    RefineRecord,
    StatusPanelState,
)
from .ocr_backend import CnOcrBackend, OCRBackend, OCRLine, build_ocr_backend
from .profile import DEFAULT_LOG_DIR, DEFAULT_PROFILE_PATH, RefineProfile
from .readers import (
    ConfirmPanelReader,
    StatusPanelReader,
    UnionPanelReader,
    detect_panel,
)
from .recorder import RefineRecorder
from .runner import (
    RefineCancelled,
    RefineCaptureRunner,
    RefinePolicy,
    RunnerHooks,
    always_cancel,
)

__all__ = [
    # data
    "Attribute",
    "ConfirmPanelState",
    "MaterialState",
    "Money",
    "RefineRecord",
    "StatusPanelState",
    # ocr
    "CnOcrBackend",
    "OCRBackend",
    "OCRLine",
    "build_ocr_backend",
    # profile
    "DEFAULT_LOG_DIR",
    "DEFAULT_PROFILE_PATH",
    "RefineProfile",
    # readers
    "ConfirmPanelReader",
    "StatusPanelReader",
    "UnionPanelReader",
    "detect_panel",
    # recorder
    "RefineRecorder",
    # runner
    "RefineCancelled",
    "RefineCaptureRunner",
    "RefinePolicy",
    "RunnerHooks",
    "always_cancel",
]

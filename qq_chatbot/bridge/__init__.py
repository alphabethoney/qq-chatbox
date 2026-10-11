"""接入 QQ（NapCat OneBot11）的桥接层。"""

from .onebot import run_bridge
from .policy import BridgeRuntime, Outbound

__all__ = ["run_bridge", "BridgeRuntime", "Outbound"]

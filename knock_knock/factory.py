"""knock knock 邀约插件的应用工厂。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from one_dragon.base.operation.application.application_config import ApplicationConfig
from one_dragon.base.operation.application.application_factory import ApplicationFactory
from one_dragon.base.operation.application_base import Application

from . import const
from .app import KnockKnockApp
from .config import KnockKnockConfig

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


class KnockKnockAppFactory(ApplicationFactory):

    def __init__(self, ctx: ZContext) -> None:
        ApplicationFactory.__init__(self, const)
        self.ctx: ZContext = ctx

    def create_application(self, instance_idx: int, group_id: str) -> Application:
        return KnockKnockApp(self.ctx)

    def create_config(self, instance_idx: int, group_id: str) -> ApplicationConfig:
        return KnockKnockConfig(instance_idx=instance_idx, group_id=group_id)

    # 不覆写 create_run_record：游戏每天 3 次邀约是硬上限，跑完当天不用再跑。
    # 父类在 DEFAULT_GROUP=True 时会按区服刷新时间建每日记录，一条龙据此跳过
    # 当天已完成的应用，应用卡片也能显示「今天已运行」。

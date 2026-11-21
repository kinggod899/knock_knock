"""knock knock 邀约的设置弹窗。

只有两项，用 FLYOUT（轻量弹窗）就够：
- 一天尝试邀约几次（不是成功几次，见配置项说明）
- 信赖满级后要不要继续邀约
"""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout

from one_dragon_qt.utils.config_utils import get_prop_adapter
from one_dragon_qt.widgets.app_setting.app_setting_flyout import AppSettingFlyout
from one_dragon_qt.widgets.setting_card.spin_box_setting_card import (
    SpinBoxSettingCard,
)
from one_dragon_qt.widgets.setting_card.switch_setting_card import (
    SwitchSettingCard,
)

from . import const
from .config import KnockKnockConfig


class KnockKnockSettingFlyout(AppSettingFlyout):
    """knock knock 邀约的设置弹窗。"""

    def _setup_ui(self, layout: QVBoxLayout) -> None:
        self.attempts_opt = SpinBoxSettingCard(
            icon='', title='尝试邀约次数',
            content='一天最多点几次「邀约」。成功上限仍是 3 次；'
                    '这个数比 3 大是为了跨过信赖全满、卡升阶任务那些点不动的人',
            minimum=1, maximum=30,
            margins=self.card_margins,
        )
        layout.addWidget(self.attempts_opt)

        self.continue_opt = SwitchSettingCard(
            icon='', title='达到信赖后继续骚扰代理人',
            content='开着：信赖满级弹窗点「确认」，继续邀约（拿不到更多信赖值，'
                    '但邀约流程能走完）；关着：点「取消」，换下一个',
            margins=self.card_margins,
        )
        layout.addWidget(self.continue_opt)

    def init_config(self) -> None:
        config: KnockKnockConfig = self.ctx.run_context.get_config(
            app_id=const.APP_ID,
            instance_idx=self.ctx.current_instance_idx,
            group_id=self.group_id,
        )
        self.attempts_opt.init_with_adapter(
            get_prop_adapter(config, 'max_invite_attempts'),
        )
        self.continue_opt.init_with_adapter(
            get_prop_adapter(config, 'continue_after_trust_full'),
        )

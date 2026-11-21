"""kk约会代发的配置。

只放真正需要玩家改的东西：一天邀约几次、邀约到好感已满时要不要继续往下翻。
坐标和等待时间不在这里 —— 那些是实机标定值，归 screen 常量文件。
"""

from one_dragon.base.operation.application.application_config import ApplicationConfig

from . import const


class KnockKnockConfig(ApplicationConfig):

    def __init__(self, instance_idx: int, group_id: str) -> None:
        ApplicationConfig.__init__(
            self,
            app_id=const.APP_ID,
            instance_idx=instance_idx,
            group_id=group_id,
        )

    @property
    def daily_invite_limit(self) -> int:
        """一天要成功邀约几个代理人。游戏上限是 3，配置只允许 1-3。"""
        value = self.get('daily_invite_limit', 3)
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return 3
        return max(1, min(3, limit))

    @daily_invite_limit.setter
    def daily_invite_limit(self, new_value: int) -> None:
        self.update('daily_invite_limit', max(1, min(3, int(new_value))))

    @property
    def max_invite_attempts(self) -> int:
        """一轮最多尝试邀约几个代理人（点几次「邀约」）。

        为什么需要这个上限：一天只能成功邀约 3 次，但如果角色信赖全满、或者
        卡着升阶任务，那些人是**点不动**的 —— 脚本会一路往下把所有代理人都点
        一遍。用这个数收手，默认 5 次（比 3 次多两次余量，能跨过一两个点不动的）。

        和 daily_invite_limit 的区别：那个是"成功几次就停"，这个是"尝试几次就停"。
        """
        value = self.get('max_invite_attempts', 5)
        try:
            count = int(value)
        except (TypeError, ValueError):
            return 5
        return max(1, min(30, count))

    @max_invite_attempts.setter
    def max_invite_attempts(self, new_value: int) -> None:
        self.update('max_invite_attempts',
                    max(1, min(30, int(new_value))))

    @property
    def continue_after_trust_full(self) -> bool:
        """信赖等阶已满时，要不要继续发起邀约。

        **两种"点不动"要区分开**：
        - 这个开关关着（默认）：点「取消」，换下一个代理人。适合信赖已满就不想
          再浪费次数的情况。
        - 这个开关开着：点「确认」继续邀约。信赖满级只是拿不到更多信赖值，
          邀约本身还能正常完成 —— 想刷聊天内容/凑次数的人会开这个。
        """
        return bool(self.get('continue_after_trust_full', False))

    @continue_after_trust_full.setter
    def continue_after_trust_full(self, new_value: bool) -> None:
        self.update('continue_after_trust_full', bool(new_value))

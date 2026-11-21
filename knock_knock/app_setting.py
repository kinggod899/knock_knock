"""kk约会代发的设置声明。

框架按文件名约定（*_app_setting.py）在各插件目录里扫描这个文件，
自动在运行卡片上挂齿轮按钮。
"""

from one_dragon_qt.services.app_setting.app_setting_provider import (
    AppSettingProvider,
    SettingType,
)

from . import const


class KnockKnockAppSetting(AppSettingProvider):
    """kk约会代发的设置声明。"""

    app_id: str = const.APP_ID
    setting_type: SettingType = SettingType.FLYOUT

    @staticmethod
    def get_setting_cls() -> type:
        # 惰性导入：界面依赖 Qt，放到真正要显示时才加载
        from .setting_flyout import KnockKnockSettingFlyout

        return KnockKnockSettingFlyout

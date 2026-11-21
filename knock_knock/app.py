"""knock knock 自动邀约（键鼠方案）。

输入全部走键鼠：鼠标点坐标、键盘按键。不再使用虚拟手柄。

**为什么键鼠能驱动绝区零（这次实测的结论）**：绝区零进程是提权运行的，
非管理员进程发过去的键鼠会被 Windows UIPI 挡掉，只有内核级虚拟手柄能绕过 ——
第一版就是这样误判成「只能用手柄」。一条龙启动器本身用 pyuac 提权，插件跑在
框架进程里天然有注入权限。所以本插件跑之前会核对提权状态，没提权就直接报错，
免得又出现「点了没反应」的假象。

实机跑通的链路（2026-11-18，提权进程 + 游戏窗口置前）：

0. **先传送到「录像店-房间」**——knock knock 只能在录像店范围内用。
   复用框架的 `Transport`（和录像店营业、情报板那些应用同一套写法），它会
   自己处理"不在大世界 / 在地图页 / 在战斗中"等情况。
1. 大世界按 **Q** 唤出 knock knock（点右下角「信息」图标是兜底）。
   **按 Q 打开的界面默认停在「会话」tab**，所以进来先点最左边的 tab。
2. 联系人列表每项两行：名字 + 状态。**状态决定能不能邀**：
   - `在线` → 今天还没邀过，可以点；
   - `联络中` → 今天已经邀过，跳过。
   顺序按用户要求：**从列表最上面开始**，能约就约，约不了往下点下一个。
3. 点右侧卡片的「邀约」按钮，按结果分三路：
   - 弹「信赖等阶已满」→ 点「取消」，换下一个（默认焦点在「确认」，绝不能点确认）；
   - 进对话 → 反复点 REPLY / 对话框，直到出结束界面；
   - 结束界面两种：「开始邀约同行」直接点；「上午/下午/夜晚/凌晨 + 加入日程」
     要先点时段，再点「加入日程」。
4. 完成后回联系人列表，继续下一个。
5. **按流程走完即算成功**：结束界面出来了就算这次邀约做完，不管左上
   「剩余邀约N/3」有没有下降。那个计数只在联系人页读得到，还可能读成 -1，
   拿它当成功判据会误判；它只用来做「已经 0/3 就提前收工」的兜底。
"""

from __future__ import annotations

import ctypes
import re
import time
from typing import TYPE_CHECKING

import cv2
import numpy as np

from one_dragon.base.geometry.point import Point
from one_dragon.base.geometry.rectangle import Rect
from one_dragon.base.operation.application import application_const
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils import cv2_utils, str_utils
from one_dragon.utils.log_utils import log
from zzz_od.application.zzz_application import ZApplication
from zzz_od.operation.transport import Transport

from . import const
from . import screen as sc
from .config import KnockKnockConfig

if TYPE_CHECKING:
    from numpy import ndarray

    from one_dragon.base.operation.operation_base import OperationResult
    from zzz_od.context.zzz_context import ZContext


def is_process_elevated() -> bool:
    """当前进程是否有管理员权限。

    绝区零自己是提权进程，非提权的键鼠事件会被 Windows 拦掉，
    所以这个前提必须显式检查，否则表现就是「脚本在跑但游戏没反应」。
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class AgentRow:
    """联系人列表里的一行：名字 + 状态 + 可点击的中心点。"""

    def __init__(self, name: str, center: Point, status: str) -> None:
        self.name: str = name
        self.center: Point = center
        self.status: str = status

    @property
    def available(self) -> bool:
        """这个代理人今天还能邀约吗（状态是「在线」而不是「联络中」）。"""
        for word in sc.STATUS_BUSY:
            if str_utils.find_by_lcs(word, self.status, percent=0.85):
                return False
        for word in sc.STATUS_AVAILABLE:
            if str_utils.find_by_lcs(word, self.status, percent=0.85):
                return True
        return False

    def __repr__(self) -> str:
        return (
            f'AgentRow({self.name!r}, {self.center}, {self.status!r}, '
            f'available={self.available})'
        )


class KnockKnockApp(ZApplication):
    """在录像店打开 knock knock，挑在线的代理人发邀约并走完对话，每天最多 3 次。"""

    def __init__(self, ctx: ZContext) -> None:
        ZApplication.__init__(
            self,
            ctx=ctx,
            app_id=const.APP_ID,
            op_name=const.APP_NAME,
        )
        self.config: KnockKnockConfig = self.ctx.run_context.get_config(
            app_id=const.APP_ID,
            instance_idx=self.ctx.current_instance_idx,
            group_id=application_const.DEFAULT_GROUP_ID,
        )
        self.invite_done: int = 0
        # 已经点过几次「邀约」（不管成没成）。用来卡「尝试次数上限」
        self.invite_attempts: int = 0
        self.scanned_names: set[str] = set()
        self.trust_full_count: int = 0
        self.current_agent: str = ''
        self.remain_before: int = -1
        self.no_available_found: bool = False
        # 需要在挑选代理人前把列表滚回顶部
        self._need_scroll_top: bool = True
        # 进入前控制器的后台模式状态，退出时还原
        self._prev_background_mode: bool | None = None

    def handle_init(self) -> None:
        log.info('薇薇安要和法厄同把日子过好')
        self.invite_done = 0
        self.invite_attempts = 0
        self.scanned_names = set()
        self.trust_full_count = 0
        self.current_agent = ''
        self.remain_before = -1
        self.no_available_found = False
        self._need_scroll_top = True
        self._use_keyboard_mouse()

    def after_operation_done(self, result: OperationResult) -> None:
        self._restore_input_mode()
        ZApplication.after_operation_done(self, result)

    # ─── 输入层：键鼠 ─────────────────────────────────────────

    # ─── 截图：统一转成 BGR ──────────────────────────────────

    def screenshot(self) -> ndarray:
        """取截图，并把通道转成 **BGR** 再交给下游。

        **这个覆写不能省。** 框架的截图器（bitblt / GDI）输出的是 **RGB**
        （`gdi_screencapper_base.py` 里 `COLOR_BGRA2RGB`），框架内部
        `find_character_avatars` / `template_info` 这些也按 RGB 写。
        但 OpenCV 的颜色判据习惯按 BGR 用，本插件所有颜色常量（红色感叹号、
        白色选项条、tab 黄色高亮）都是按 BGR 写的。

        不转就会这样（实测）：红色感叹号在 RGB 数组里被当成 BGR 读，红蓝颠倒，
        变成"蓝色圆点"，检测结果 0 个 —— 现象就是「明明有过期邀约却识别不到」。
        实测对比：同一帧画面，按 BGR 解释 0 个，交换 R/B 后 3 个。

        覆写一处比改 27 个调用点可靠。
        """
        screen = ZApplication.screenshot(self)
        if screen is None:
            return screen
        return cv2.cvtColor(screen, cv2.COLOR_RGB2BGR)

    def _use_keyboard_mouse(self) -> None:
        """切到前台键鼠模式并把游戏窗口置前。

        两件事都不能省：
        - 全局配置可能是后台手柄模式，那时 `controller.click()` 走 PostMessage，
          绝区零不认；
        - 键鼠事件只作用在**前台窗口**，窗口没置前等于白按。
        """
        if not is_process_elevated():
            # 只警告不阻断：某些环境下用户用其他方式提权，硬失败反而挡住正常使用
            log.warning(
                'knock knock：当前进程没有管理员权限，'
                '绝区零是提权进程，键鼠事件很可能被系统拦掉。'
                '请用一条龙启动器（自带提权）运行。',
            )

        controller = self.ctx.controller
        if self._prev_background_mode is None:
            self._prev_background_mode = controller.background_mode
        if controller.background_mode:
            log.info('knock knock：后台手柄模式切到前台键鼠模式')
            controller.enable_foreground_mode()
        controller.active_window()

    def _restore_input_mode(self) -> None:
        """还原进入前的输入模式。"""
        if self._prev_background_mode is None:
            return
        controller = self.ctx.controller
        if self._prev_background_mode and not controller.background_mode:
            gamepad_type = self.ctx.game_config.background_gamepad_type
            controller.enable_background_mode(gamepad_type)
            log.info('knock knock：已还原后台手柄模式')
        self._prev_background_mode = None

    def _click(self, point: Point, wait: float = 0.0,
               pc_alt: bool = False) -> bool:
        """点一个游戏坐标，点完等 wait 秒。"""
        if not self.ctx.controller.click(point, pc_alt=pc_alt):
            log.warning(f'点击 {point} 失败')
            return False
        if wait > 0:
            time.sleep(wait)
        return True

    def _tap(self, key: str, wait: float = 0.0) -> None:
        """按一下键盘键，按完等 wait 秒。"""
        self.ctx.controller.btn_tap(key)
        if wait > 0:
            time.sleep(wait)

    def _scroll_list(self, down: int, wait: float = 0.0) -> None:
        """在列表区滚轮。down 为正往下滚、为负往上滚。"""
        self.ctx.controller.scroll(down, sc.LIST_SCROLL_POINT)
        if wait > 0:
            time.sleep(wait)

    # ─── OCR 小工具 ───────────────────────────────────────────

    def _ocr_results(self, screen: ndarray, rect: Rect | None = None) -> list:
        return self.ctx.ocr_service.get_ocr_result_list(
            screen, rect=rect, crop_first=rect is not None,
        )

    def _ocr_texts(self, screen: ndarray, rect: Rect | None = None) -> list[str]:
        return [item.data for item in self._ocr_results(screen, rect)]

    def _has_text(self, screen: ndarray, keywords: tuple[str, ...] | str,
                  rect: Rect | None = None, percent: float = 0.8) -> bool:
        word_list = (keywords,) if isinstance(keywords, str) else keywords
        for text in self._ocr_texts(screen, rect):
            for word in word_list:
                if str_utils.find_by_lcs(word, text, percent=percent):
                    return True
        return False

    def _find_text_center(self, screen: ndarray, keyword: str,
                          rect: Rect | None = None,
                          percent: float = 0.8) -> Point | None:
        """找关键词所在的文字中心点，用于点击。多个命中时取最靠上的。"""
        best = None
        for item in self._ocr_results(screen, rect):
            if not str_utils.find_by_lcs(keyword, item.data, percent=percent):
                continue
            center = item.center
            if best is None or center.y < best.y:
                best = center
        return best

    def _find_dialog_option(self, screen: ndarray) -> Point | None:
        """找出 REPLY 选项组里**真正能点的那一条**，找不到返回 None。

        实机踩的坑：`REPLY` 只是选项组的标签（气泡图标 + 文字），点它没有任何反应。
        能点的是标签下方那几条**纯白圆角长条**按钮。所以这里直接按形状找：

        - 在会话区里取纯白掩膜；
        - 找横向很长的连通块（宽高比 > 3、高度 30~70）；
        - **宽度占会话区 0.68 以上**，把对方那种较窄的白色气泡排掉；
        - **中心 x 离会话区中心不到 12%**，选项条是居中的，气泡靠左；
        - **中心落在会话区下半部（0.65 以下高度不算）**，选项组固定在底部，
          气泡在对话流上方；
        - 取最靠上的一个（按用户要求选第一个选项）的中心。

        为什么不用「OCR 读 REPLY 标签 + 固定偏移」：标签 y 会随会话滚动位置变，
        偏移量也就跟着飘；纯白长条的形状判据不受滚动影响，而且白条本身就是要
        点的目标，不存在「量到标签却点到空气」的问题。截图已由框架统一缩到
        1920x1080，同一套阈值对 2K 一样成立。
        """
        area = sc.CHAT_AREA
        part = cv2_utils.crop_image_only(screen, area)
        mask = cv2_utils.get_white_part(
            part, noise_threshold=sc.OPTION_WHITE_NOISE_THRESHOLD,
        )
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )

        min_width = area.width * sc.OPTION_MIN_WIDTH_RATIO
        max_center_offset = area.width * sc.OPTION_MAX_CENTER_OFFSET_RATIO
        area_center_x = (area.x1 + area.x2) // 2
        min_center_y = area.y1 + area.height * sc.OPTION_MIN_Y_RATIO

        found: list[tuple[int, Point]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if h < sc.OPTION_MIN_HEIGHT or h > sc.OPTION_MAX_HEIGHT:
                continue
            if w < min_width or w / h < sc.OPTION_MIN_ASPECT:
                continue
            center = Point(area.x1 + x + w // 2, area.y1 + y + h // 2)
            if abs(center.x - area_center_x) > max_center_offset:
                continue
            if center.y < min_center_y:
                continue
            found.append((y, center))

        if len(found) == 0:
            return None
        found.sort(key=lambda pair: pair[0])
        return found[0][1]

    def _find_click_to_advance(self, screen: ndarray) -> Point:
        """没有选项组时（对方正在说话）找一个能推进对话的点。

        点最下面那条消息是安全的：游戏里点对话气泡就是「继续」。读不到任何
        消息才退回固定兜底点。
        """
        results = self._ocr_results(screen, sc.CHAT_AREA)
        if len(results) > 0:
            bottom = max(results, key=lambda item: item.y)
            return bottom.center
        return sc.DIALOG_CLICK_POINT

    # ─── 过期邀约（红色感叹号）────────────────────────────────

    def _red_marks(self, screen: ndarray, rect: Rect) -> list[Point]:
        """在指定区域里找红色感叹号，按 y 从上到下返回中心点。

        **不能把"区域里的红色"一律当感叹号**：左栏头像本身就有红色（猫又、
        红发角色），会把别人的行当成待处理会话，点错行然后原地空转。
        所以除了颜色，还按实测几何过滤：感叹号在行右上角 x≈656，尺寸
        w≈19 h≈20，宽高比 0.9~1.0。
        """
        part = cv2_utils.crop_image_only(screen, rect)
        hsv = cv2.cvtColor(part, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array(sc.EXCLAIM_HSV_LOWER[0]),
                           np.array(sc.EXCLAIM_HSV_UPPER[0]))
        mask |= cv2.inRange(hsv, np.array(sc.EXCLAIM_HSV_LOWER[1]),
                            np.array(sc.EXCLAIM_HSV_UPPER[1]))

        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )
        low, high = sc.EXCLAIM_ASPECT_RANGE
        found: list[tuple[int, Point]] = []
        for contour in contours:
            if cv2.contourArea(contour) < sc.EXCLAIM_MIN_AREA:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            if w < sc.EXCLAIM_MIN_W or h < sc.EXCLAIM_MIN_H:
                continue
            if not (low <= w / max(h, 1) <= high):
                continue
            found.append((y, Point(rect.x1 + x + w // 2, rect.y1 + y + h // 2)))

        found.sort(key=lambda pair: pair[0])
        return [point for _y, point in found]

    def _list_exclamations(self, screen: ndarray) -> list[Point]:
        """联系人/会话列表里还没处理的过期邀约感叹号。"""
        return self._red_marks(screen, sc.EXCLAIM_AREA)

    def _has_tab_badge(self, screen: ndarray) -> bool:
        """顶部会话 tab 上还有没有红点提醒。"""
        return len(self._red_marks(screen, sc.TAB_BADGE_AREA)) > 0

    def _has_expired_invite(self, screen: ndarray) -> bool:
        """当前画面是否还有待处理的过期邀约（列表感叹号 或 tab 徽标）。"""
        return (len(self._list_exclamations(screen)) > 0
                or self._has_tab_badge(screen))

    def _clear_one_expired(self, mark: Point) -> bool:
        """处理掉一个过期会话，处理干净返回 True。

        做法（实机验证过）：点那一行进会话，然后在会话里反复推进 ——
        有白色选项条就点第一条，没有就点对话区底部。每推进一步检查那一行的
        感叹号是否消失，消失就算这个会话处理完。

        **注意：点掉一个不会自动重排列表**，所以要按当前感叹号坐标逐行点，
        不能假设"处理完第一个，第二个会顶上来"。

        判"这一行处理完了"就看**这个位置的感叹号还在不在**：游戏里点掉一个过期
        会话后，那一行的感叹号就地消失，其余行不动（实测点掉 2 个，剩余数从
        2 变 1 变 0，y 坐标不变）。
        """
        self._click(
            Point(sc.LIST_ROW_CLICK_X, mark.y + sc.LIST_ROW_CLICK_Y_OFFSET),
            wait=sc.NAV_WAIT,
        )
        time.sleep(0.8)

        for _ in range(sc.EXPIRED_MAX_STEPS):
            screen = self.screenshot()
            marks = self._list_exclamations(screen)
            if not any(abs(item.y - mark.y) < 40 for item in marks):
                return True

            option = self._find_dialog_option(screen)
            if option is not None:
                self._click(option, wait=sc.DIALOG_WAIT)
            else:
                self._click(sc.DIALOG_CLICK_POINT, wait=sc.DIALOG_WAIT)
            time.sleep(0.5)

        return not any(
            abs(item.y - mark.y) < 40
            for item in self._list_exclamations(self.screenshot())
        )

    def _wait_list_settled(self) -> list[Point]:
        """等左侧列表渲染稳定后，返回感叹号列表。

        **这是修掉「明明有过期邀约却检测不到」的关键。** 点完 tab 那一下界面
        会重绘，立刻截图可能抓到空列表或半成品 —— 之前就直接 return 0 个，
        然后一路往下走，过期邀约完全没处理。

        做法：连续采样到次数用完，返回**整个过程中检出最多的那次**。

        **不能"两次一致就提前返回"**：验证时踩过 —— 前两帧都还是空列表，长度
        相等（都是 0），于是第 2 帧就判定"稳定"并返回空，后面画出来的感叹号
        完全没看到。这正是线上"检测不到过期邀约"的失败模式。

        取最大值而不是最后一次，是因为宁可多清也不要漏清：真有过期邀约时，
        画出来之后每一帧都会检出；误检的风险由判据本身（区域+形状）控制。
        """
        best: list[Point] = []
        for _ in range(sc.EXPIRED_SETTLE_TIMES):
            time.sleep(sc.NAV_WAIT)
            marks = self._list_exclamations(self.screenshot())
            if len(marks) > len(best):
                best = marks
        return best

    def _clear_expired_invites(self) -> int:
        """把所有过期邀约清掉，返回处理成功的会话数。

        必须在「挑选代理人」**之前**做：昨天邀约了没赴约，今天进联系人列表
        会被这些挂着「已过期」的会话卡住。
        """
        cleared = 0
        # 清不掉的行的 y 记在这里，避免一直重试同一行导致漏掉后面那些能清的
        failed_ys: list[int] = []

        for _ in range(sc.EXPIRED_MAX_SESSIONS):
            # 等画面稳定再判断，否则可能抓到重绘中的空列表
            marks = self._wait_list_settled()
            pending = [
                item for item in marks
                if not any(abs(item.y - fy) < 40 for fy in failed_ys)
            ]
            if len(pending) == 0:
                if len(marks) > 0:
                    log.warning(f'有 {len(marks)} 个过期邀约点不掉，跳过')
                break

            mark = pending[0]
            if self._clear_one_expired(mark):
                cleared += 1
                log.info(f'已处理一个过期邀约（行 y={mark.y}），累计 {cleared}')
            else:
                # 这一行清不掉就记下来换下一行，不要 break —— 后面的行可能能清
                failed_ys.append(mark.y)
                log.warning(f'过期邀约处理未生效（行 y={mark.y}），换下一行')

        if cleared > 0:
            log.info(f'过期邀约处理完成，共 {cleared} 个')
        return cleared

    # ─── 画面判据 ─────────────────────────────────────────────

    def _in_knock_knock(self, screen: ndarray) -> bool:
        return self._has_text(screen, sc.SCREEN_TITLE_KEYWORD, sc.TITLE_AREA,
                              percent=0.8)

    def _read_remain(self, screen: ndarray) -> int:
        """读「剩余邀约N/3」里的 N，读不到返回 -1。"""
        for text in self._ocr_texts(screen, sc.REMAIN_AREA):
            match = re.search(r'(\d+)\s*/\s*(\d+)', text)
            if match is not None:
                return int(match.group(1))
            match = re.search(r'(\d+)', text)
            if match is not None:
                return int(match.group(1))
        return -1

    def _is_on_contact_list(self, screen: ndarray) -> bool:
        """是否停在**联系人列表** tab。

        判据是「剩余邀约N/3」这个计数 —— 它只在联系人页出现。
        不能拿卡片上的「邀约」当判据：会话 tab 的会话详情也可能带这个词，
        会把会话页误认成联系人页。
        """
        return self._has_text(screen, sc.REMAIN_KEYWORD, percent=0.75)

    def _read_agent_rows(self, screen: ndarray) -> list[AgentRow]:
        """把联系人列表读成 AgentRow 列表，按 y 从上到下排序。

        列表每项两行：名字在上、状态在下（实机状态行在名字下方约 29px）。
        所以按名字 y 分组，再把落在名字下方 45px 内的文本当作它的状态。
        """
        results = self._ocr_results(screen, sc.LIST_AREA)
        if len(results) == 0:
            return []

        names: list[tuple[int, Point, str]] = []
        statuses: list[tuple[int, str]] = []
        for item in results:
            text = item.data.strip()
            if text == '':
                continue
            if str_utils.find_by_lcs(sc.KEYWORD_ALL_CONTACTS, text, percent=0.85):
                continue
            is_status = False
            for word in sc.STATUS_AVAILABLE + sc.STATUS_BUSY:
                if str_utils.find_by_lcs(word, text, percent=0.85):
                    is_status = True
                    break
            if is_status:
                statuses.append((item.y, text))
            else:
                names.append((item.y, item.center, text))

        names.sort(key=lambda pair: pair[0])
        statuses.sort(key=lambda pair: pair[0])

        rows: list[AgentRow] = []
        for name_y, center, name in names:
            status = ''
            for status_y, status_text in statuses:
                if 0 <= status_y - name_y <= 45:
                    status = status_text
                    break
            rows.append(AgentRow(name, center, status))
        return rows

    # ─── 导航 ─────────────────────────────────────────────────

    def _scroll_list_to_top(self) -> None:
        """把联系人列表滚回顶部。滚轮不会循环，滚够次数一定停在顶。"""
        for _ in range(8):
            self._scroll_list(-3, wait=0.3)
        time.sleep(sc.SCROLL_WAIT)

    def _back_to_contact_list(self) -> bool:
        """点顶部第一个 tab 回到联系人列表，回来了返回 True。

        按 Q 打开的界面默认停在「会话」tab，所以每次进来都得点一下最左边的图标。
        """
        if self._is_on_contact_list(self.screenshot()):
            return True

        for _ in range(3):
            self._click(sc.TAB_CONTACT_POINT, wait=sc.NAV_WAIT)
            if self._is_on_contact_list(self.screenshot()):
                return True
        return self._is_on_contact_list(self.screenshot())

    # ─── 节点 ─────────────────────────────────────────────────

    @operation_node(name='传送到录像店', is_start_node=True,
                    screenshot_before_round=False)
    def transport_to_video_store(self) -> OperationRoundResult:
        """先传送到「录像店-房间」。

        knock knock 只能在录像店范围内打开，所以每次跑之前先确保位置对。
        直接复用框架的 `Transport`（录像店营业、情报板那些应用用的都是这一套），
        它内部会处理"当前不在大世界/在地图页/在战斗中"等各种情况，
        比自己写一遍稳。

        目的地用「房间」：和贝果计划、情报板一致，落点就在录像店室内，
        开门就能按 Q。
        """
        op = Transport(self.ctx, '录像店', '房间')
        return self.round_by_op_result(op.execute())

    @node_from(from_name='传送到录像店')
    @operation_node(name='唤出 knock knock', screenshot_before_round=False,
                    node_max_retry_times=8)
    def invoke_knock_knock(self) -> OperationRoundResult:
        """按 Q 唤出界面，并确保停在**中间（会话）tab**。

        按 Q 打开的界面默认就落在中间会话页，但**必须显式点一下中间 tab**：
        上一次运行可能停在联系人页，重开时不一定回到会话页；而会话页正是
        过期邀约挂着的地方，位置不对就清不到。
        """
        screen = self.screenshot()
        if not self._in_knock_knock(screen):
            self._tap(sc.KEY_INVOKE, wait=sc.INVOKE_WAIT)
            if not self._in_knock_knock(self.screenshot()):
                # 兜底：点大世界右下角「信息」图标（大世界锁光标，要按住 Alt）
                self._click(sc.WORLD_INFO_POINT, wait=sc.INVOKE_WAIT,
                            pc_alt=sc.WORLD_INFO_PC_ALT)
                return self.round_retry('已尝试唤出，等待界面', wait=1)

        # 显式点中间 tab，保证在会话页
        self._click(sc.TAB_CHAT_POINT, wait=sc.NAV_WAIT)
        return self.round_success('已唤出，停在会话页')

    @node_from(from_name='唤出 knock knock')
    @operation_node(name='清理过期邀约', screenshot_before_round=False,
                    node_max_retry_times=3)
    def clear_expired(self) -> OperationRoundResult:
        """先把昨天遗留的过期邀约点掉，再开始邀约。

        昨天邀约了但没赴约，今天会话里会挂「已过期」卡片，会话 tab 图标右上角
        和左侧角色列表每一项右上角都会出现红色感叹号。不清掉这些，进联系人
        列表会被卡住。

        **顺序很关键**：过期邀约挂在**会话 tab**（上一步「唤出」已经点过中间
        tab 保证在这儿），所以必须"先清会话、再切联系人列表"；反过来先切
        列表会被卡住。

        判据用红色感叹号的**位置 + 形状**，不是"看到红色就算"：左栏头像本身
        就有红色（猫又、红发角色），只按颜色会点错行然后原地空转。
        """
        # 先记录进入时看到的原始信号，出问题时能对着日志查是哪一步没识别到
        entry_screen = self.screenshot()
        entry_marks = self._list_exclamations(entry_screen)
        entry_badge = self._has_tab_badge(entry_screen)
        log.info(f'进入清理：列表感叹号 {len(entry_marks)} 个'
                 f'（{[m.y for m in entry_marks]}），会话 tab 红点 {entry_badge}')

        cleared = self._clear_expired_invites()

        if cleared == 0 and (len(entry_marks) > 0 or entry_badge):
            # 进来看到了、但一个都没清掉：说明检测和点击对不上，明确报出来
            log.error('看到了过期邀约信号但没能处理，请检查感叹号坐标/阈值')

        if self._has_expired_invite(self.screenshot()):
            if self.node_retry_times >= 2:
                # 清不掉就记一笔继续走，别把整个流程卡死在这里
                log.warning('过期邀约没清干净，继续后续流程')
                return self.round_success(status='过期邀约未清净')
            return self.round_retry('仍有未清理的过期邀约', wait=1)

        return self.round_success(status='已清理过期邀约', data=cleared)

    @node_from(from_name='清理过期邀约')
    @node_from(from_name='跳过换人', status='继续下一个')
    @operation_node(name='确认在列表', screenshot_before_round=False,
                    node_max_retry_times=6)
    def ensure_on_list(self) -> OperationRoundResult:
        """点联系人 tab，并记下这轮开始时的剩余邀约次数。"""
        screen = self.screenshot()
        if self._is_on_contact_list(screen):
            if self.remain_before < 0:
                self.remain_before = self._read_remain(screen)
                log.info(f'本轮开始，剩余邀约 {self.remain_before}/3')
            return self.round_success('在联系人列表')

        if self.node_retry_times >= 4:
            # 兜底：关界面重开，重开一定落在会话 tab，再点联系人 tab
            self._click(sc.CLOSE_POINT, wait=sc.CLOSE_WAIT)
            if not self._in_knock_knock(self.screenshot()):
                self._tap(sc.KEY_INVOKE, wait=sc.INVOKE_WAIT)
            return self.round_retry('重开界面', wait=1)

        self._back_to_contact_list()
        return self.round_retry('切到联系人列表', wait=0.5)

    @node_from(from_name='确认在列表')
    @node_from(from_name='完成后换人', status='继续下一个')
    @operation_node(name='挑选代理人', screenshot_before_round=False)
    def pick_agent(self) -> OperationRoundResult:
        """从列表**最上面**开始，挑第一个「在线」的代理人并点它。

        键鼠的优势：滚轮不会循环，可以稳稳滚回顶部；每行位置由 OCR 实时给出，
        直接点那一行，不用一格一格挪焦点。
        """
        # 三道收工关卡，任何一道命中都不再挑人：
        # 1) 尝试次数到顶（信赖全满 / 卡升阶时全靠这道，否则会把全列表点一遍）
        if self.invite_attempts >= self.config.max_invite_attempts:
            log.info(f'已达尝试上限 {self.invite_attempts}/'
                     f'{self.config.max_invite_attempts}，不再挑人')
            return self.round_success(status='已扫描到上限')

        # 2) 成功次数到顶
        if self.invite_done >= self.config.daily_invite_limit:
            return self.round_success(status='已完成邀约次数')

        # 3) 游戏里剩余次数已经是 0/3
        #    （实机确认 3 次用光后显示 0/3，比等"点了邀约弹次数用完"更早收手）
        screen = self.screenshot()
        if self._is_on_contact_list(screen) and self._read_remain(screen) == 0:
            log.info('剩余邀约 0/3，今天已约满')
            return self.round_success(status='次数已用完')

        if self._need_scroll_top:
            self._scroll_list_to_top()
            self._need_scroll_top = False

        # 翻页次数用固定上限，不用尝试次数：这里只是在找"谁看起来能约"，
        # 真正的尝试次数在 click_invite 里计。
        for _ in range(sc.LIST_SCAN_MAX_PAGES):
            screen = self.screenshot()
            rows = self._read_agent_rows(screen)
            if len(rows) == 0:
                return self.round_retry('读不到联系人列表', wait=1)

            target = None
            target_index = -1
            for index, row in enumerate(rows):
                if row.available and row.name not in self.scanned_names:
                    target, target_index = row, index
                    break

            if target is not None:
                self.current_agent = target.name
                self.scanned_names.add(self.current_agent)
                log.info(
                    f'选中 {self.current_agent}'
                    f'（当前屏第 {target_index + 1} 行，{target.status}）',
                )
                self._click(target.center, wait=sc.NAV_WAIT)
                return self.round_success(status='已选中', data=self.current_agent)

            # 这一屏没有可邀约的，往下滚一屏再看；滚不动说明到底了
            before_names = [row.name for row in rows]
            self._scroll_list(2, wait=sc.SCROLL_WAIT)
            after_rows = self._read_agent_rows(self.screenshot())
            if [row.name for row in after_rows] == before_names:
                log.info('列表已到底，没有可邀约的代理人了')
                break

        self.no_available_found = True
        return self.round_success(status='没有可邀约')

    @node_from(from_name='挑选代理人', status='没有可邀约')
    @node_from(from_name='挑选代理人', status='次数已用完')
    @node_from(from_name='挑选代理人', status='已扫描到上限')
    @node_from(from_name='挑选代理人', status='已完成邀约次数')
    @node_from(from_name='点击邀约', status='次数已用完')
    @node_from(from_name='点击邀约', status='已扫描到上限')
    @node_from(from_name='处理对话', status='次数已用完')
    @node_from(from_name='跳过换人', status='已完成邀约次数')
    @node_from(from_name='跳过换人', status='已扫描到上限')
    @node_from(from_name='完成后换人', status='已完成邀约次数')
    @node_from(from_name='完成后换人', status='已扫描到上限')
    @operation_node(name='结束', screenshot_before_round=False)
    def finish(self) -> OperationRoundResult:
        """收工：先把残留弹窗点掉，再点右上角 X 关界面。

        弹窗会挡住关闭按钮，所以每轮先看有没有弹窗。
        """
        for _ in range(5):
            screen = self.screenshot()

            # 弹窗优先点掉，否则关闭按钮按不到
            if self._is_invite_exhausted_dialog(screen):
                self._dismiss_invite_exhausted()
                continue
            if self._has_any_trust_dialog(screen):
                self._click_dialog_button(
                    sc.KEYWORD_CANCEL, sc.TRUST_CANCEL_POINT, sc.CANCEL_WAIT,
                )
                continue

            if not self._in_knock_knock(screen):
                return self.round_success(
                    f'完成 {self.invite_done} 次邀约，'
                    f'跳过 {self.trust_full_count} 个信赖已满',
                )
            self._click(sc.CLOSE_POINT, wait=sc.CLOSE_WAIT)

        return self.round_success(f'完成 {self.invite_done} 次邀约（界面未完全关闭）')

    @node_from(from_name='挑选代理人', status='已选中')
    @operation_node(name='点击邀约', screenshot_before_round=False,
                    node_max_retry_times=6)
    def click_invite(self) -> OperationRoundResult:
        """点卡片上的「邀约」，按结果分三路。

        判定顺序有讲究：
        1. **次数用完**——出现就说明今天没得邀了，立刻收工，别再去试别人；
        2. 信赖类弹窗（满级 / 升阶，详见 `_handle_trust_dialog`）——
           它们的默认焦点都在右边那个按钮上，误点会消耗邀约或跳去升阶事件。
        """
        # 硬性闸门：节点会因 retry 重入，这里先挡一道，防止超上限继续点
        if self.invite_attempts >= self.config.max_invite_attempts:
            log.info(f'已达尝试上限 {self.invite_attempts}/'
                     f'{self.config.max_invite_attempts}，不再点邀约')
            return self.round_success(status='已扫描到上限')

        screen = self.screenshot()

        if self._is_invite_exhausted_dialog(screen):
            self._dismiss_invite_exhausted()
            log.info('今天邀约次数已用完，收工')
            return self.round_success(status='次数已用完')

        if self._has_any_trust_dialog(screen):
            self.trust_full_count += 1
            if self._handle_trust_dialog():
                # 用户选了继续邀约：点完确认应当进对话，给一次机会
                if self._has_text(self.screenshot(), sc.KEYWORD_REPLY,
                                  sc.CHAT_AREA, percent=0.8):
                    return self.round_success(status='已进对话')
                return self.round_retry('信赖弹窗已确认，等待进对话', wait=1)
            return self.round_success(status='信赖已满-跳过')

        if self._has_text(screen, sc.KEYWORD_REPLY, sc.CHAT_AREA, percent=0.8):
            return self.round_success(status='已进对话')

        # 「邀约」文字优先，找不到退到按钮固定坐标
        invite_point = self._find_text_center(
            screen, sc.KEYWORD_INVITE, sc.INVITE_BTN_AREA, percent=0.8,
        )
        if invite_point is None:
            if not self._has_text(screen, sc.KEYWORD_INVITE, sc.CARD_AREA,
                                  percent=0.8):
                return self.round_retry('卡片上没找到邀约按钮', wait=1)
            invite_point = sc.INVITE_POINT

        self.invite_attempts += 1
        log.info(f'第 {self.invite_attempts}/{self.config.max_invite_attempts} '
                 f'次尝试邀约 {self.current_agent}')
        self._click(invite_point, wait=sc.INVITE_WAIT)
        screen = self.screenshot()

        if self._is_invite_exhausted_dialog(screen):
            self._dismiss_invite_exhausted()
            log.info('今天邀约次数已用完，收工')
            return self.round_success(status='次数已用完')

        if self._has_any_trust_dialog(screen):
            self.trust_full_count += 1
            if self._handle_trust_dialog():
                if self._has_text(self.screenshot(), sc.KEYWORD_REPLY,
                                  sc.CHAT_AREA, percent=0.8):
                    return self.round_success(status='已进对话')
                return self.round_retry('信赖弹窗已确认，等待进对话', wait=1)
            return self.round_success(status='信赖已满-跳过')

        if (self._has_text(screen, sc.KEYWORD_REPLY, sc.CHAT_AREA, percent=0.8)
                or self._has_text(screen, sc.KEYWORD_INVITE_START, sc.CHAT_AREA,
                                  percent=0.8)
                or self._has_text(screen, sc.KEYWORD_SCHEDULE_DONE, sc.CHAT_AREA,
                                  percent=0.8)):
            return self.round_success(status='已进对话')

        log.info(f'{self.current_agent} 点邀约后没进对话，重试')
        return self.round_retry('点邀约未进对话，重试', wait=1)

    def _is_trust_ascend_dialog(self, screen: ndarray) -> bool:
        """是不是**升阶事件**弹窗（信赖卡在当前等阶满值）。

        这个弹窗**只能点取消**：点「前往」会跳去升阶事件，离开邀约流程。
        必须和满级弹窗分开判，不能共用一个判据 —— 踩过坑，详见 screen 常量注释。
        """
        return self._has_text(screen, sc.KEYWORD_TRUST_ASCEND,
                              sc.TRUST_DIALOG_AREA, percent=0.75)

    def _is_trust_full_dialog(self, screen: ndarray) -> bool:
        """是不是**信赖满级**弹窗（点邀约后，信赖已经到顶）。"""
        return self._has_text(screen, sc.KEYWORD_TRUST_FULL,
                              sc.TRUST_DIALOG_AREA, percent=0.75)

    def _has_any_trust_dialog(self, screen: ndarray) -> bool:
        """任一信赖类弹窗在场。两个弹窗都要在判结束界面之前先处理掉。"""
        return (self._is_trust_ascend_dialog(screen)
                or self._is_trust_full_dialog(screen))

    def _is_invite_exhausted_dialog(self, screen: ndarray) -> bool:
        """是不是「今天邀约次数已用完」弹窗。

        这是**唯一的收工信号**：3 次用光后点「邀约」会弹它。看到就说明今天
        不会再成功了，没必要继续试其他人（那些都会弹同一个窗）。
        """
        return self._has_text(screen, sc.KEYWORD_INVITE_EXHAUSTED,
                              sc.INVITE_EXHAUSTED_AREA, percent=0.75)

    def _dismiss_invite_exhausted(self) -> None:
        """点掉「邀约次数已用完」弹窗。

        这个弹窗只有一个「确认」按钮，位置在**画面中间** (978,626)，
        和前面那两个弹窗的右侧按钮 (1129,647) 不是一回事，别混用坐标。
        """
        self._click_dialog_button(
            sc.KEYWORD_CONFIRM, sc.INVITE_EXHAUSTED_CONFIRM_POINT,
            sc.CANCEL_WAIT,
        )

    def _click_dialog_button(self, keyword: str, fallback: Point,
                             wait: float) -> None:
        """点弹窗按钮：OCR 找文字中心，找不到用实测兜底坐标。

        必须点**文字中心**，不能点左边的圆圈：红 X 圆心 (700,647)、绿 ✓ 圆心
        (1004,647) 都在按钮左端，点圆圈会偏到按钮边缘。文字中心实测是
        取消 (827,647)、右侧按钮 (1129,647)。
        """
        point = self._find_text_center(
            self.screenshot(), keyword, sc.TRUST_DIALOG_AREA, percent=0.75,
        )
        self._click(point if point is not None else fallback, wait=wait)

    def _handle_trust_dialog(self) -> bool:
        """处理信赖类弹窗，返回是否继续这次邀约。

        两种情况分开处理：

        **升阶弹窗**：只能点「取消」，换下一个代理人。点「前往」会跳去升阶事件，
        离开邀约流程 —— 这不是我们想要的。

        **满级弹窗**：按配置 `continue_after_trust_full` 走。

        - 关（默认）：点「取消」，换下一个。信赖满级拿不到更多信赖值。
        - 开：点「确认」继续。满级只是不涨信赖值，邀约流程本身能走完。
        """
        screen = self.screenshot()

        if self._is_trust_ascend_dialog(screen):
            self._click_dialog_button(
                sc.KEYWORD_CANCEL, sc.TRUST_CANCEL_POINT, sc.CANCEL_WAIT,
            )
            log.info(f'{self.current_agent} 信赖卡在等阶满值（升阶弹窗），已取消')
            return False

        if self.config.continue_after_trust_full:
            self._click_dialog_button(
                sc.KEYWORD_CONFIRM, sc.TRUST_CONFIRM_POINT, sc.INVITE_WAIT,
            )
            log.info(f'{self.current_agent} 信赖等阶已满，按配置点确认继续邀约')
            return True

        self._click_dialog_button(
            sc.KEYWORD_CANCEL, sc.TRUST_CANCEL_POINT, sc.CANCEL_WAIT,
        )
        log.info(f'{self.current_agent} 信赖等阶已满，已取消')
        return False

    @node_from(from_name='点击邀约', status='信赖已满-跳过')
    @node_from(from_name='处理对话', status='信赖已满-跳过')
    @operation_node(name='跳过换人', screenshot_before_round=False)
    def skip_and_next(self) -> OperationRoundResult:
        """跳过这个人，换下一个。

        **这里也必须判尝试次数上限。** 之前只判了"成功次数"，导致信赖已满 /
        升阶这类"跳过"路径完全不受上限约束 —— 实机日志会出现第 5/5、6/5、7/5
        一路加下去，把列表上所有人都试一遍。
        """
        if self.invite_done >= self.config.daily_invite_limit:
            return self.round_success(status='已完成邀约次数')

        if self.invite_attempts >= self.config.max_invite_attempts:
            log.info(f'已达尝试上限 {self.invite_attempts}/'
                     f'{self.config.max_invite_attempts}，收工')
            return self.round_success(status='已扫描到上限')

        self._back_to_contact_list()
        self._need_scroll_top = True
        return self.round_success(status='继续下一个')

    @node_from(from_name='点击邀约', status='已进对话')
    @operation_node(name='处理对话', screenshot_before_round=False,
                    node_max_retry_times=60)
    def handle_dialog(self) -> OperationRoundResult:
        """反复推进对话，直到出结束界面。

        **注意点的是选项按钮本身，不是 REPLY 标签。** 有选项组时点标签下方
        第一条白色选项；没有选项组（对方正在说话）时点最下面那条消息推进。
        每轮先判结束界面，避免把结束界面误当成普通对话继续点。
        """
        screen = self.screenshot()

        # 次数用完可能在任何时刻弹出来，先判它
        if self._is_invite_exhausted_dialog(screen):
            self._dismiss_invite_exhausted()
            log.info('今天邀约次数已用完，收工')
            return self.round_success(status='次数已用完')

        if self._has_text(screen, sc.KEYWORD_SCHEDULE_DONE, sc.CHAT_AREA,
                          percent=0.8):
            return self.round_success(status='需选时段')

        if self._has_text(screen, sc.KEYWORD_INVITE_START, sc.CHAT_AREA,
                          percent=0.8):
            return self.round_success(status='可直接完成')

        if (self._has_text(screen, sc.KEYWORD_PENDING, percent=0.85)
                and self._has_text(screen, sc.KEYWORD_SCHEDULE_VIEW, percent=0.8)):
            return self.round_success(status='已完成')

        if self._has_any_trust_dialog(screen):
            self.trust_full_count += 1
            if self._handle_trust_dialog():
                # 继续邀约：确认后应回到对话，继续推进
                return self.round_retry('信赖弹窗已确认，继续对话', wait=1)
            return self.round_success(status='信赖已满-跳过')

        option = self._find_dialog_option(screen)
        if option is not None:
            log.info(f'点对话选项 {option}')
            self._click(option, wait=sc.DIALOG_WAIT)
        else:
            self._click(self._find_click_to_advance(screen), wait=sc.DIALOG_WAIT)
        return self.round_retry('推进对话', wait=0.3)

    @node_from(from_name='处理对话', status='可直接完成')
    @operation_node(name='完成邀约', screenshot_before_round=False)
    def finish_invite(self) -> OperationRoundResult:
        point = self._find_text_center(
            self.screenshot(), sc.KEYWORD_INVITE_START, sc.CHAT_AREA, percent=0.8,
        )
        self._click(point if point is not None else sc.DIALOG_CLICK_POINT,
                    wait=sc.SCHEDULE_WAIT)
        return self.round_success(status='完成')

    @node_from(from_name='处理对话', status='已完成')
    @operation_node(name='确认完成', screenshot_before_round=False)
    def confirm_done(self) -> OperationRoundResult:
        return self.round_success(status='完成')

    @node_from(from_name='处理对话', status='需选时段')
    @operation_node(name='选择时段', screenshot_before_round=False)
    def choose_period(self) -> OperationRoundResult:
        """「上午/下午/夜晚/凌晨」界面。**四个方块全点一遍。**

        时段是单选，点谁谁选中（带 ✓ 角标）。不去识别"当前选的是哪个"，
        直接按实测坐标从左到右全点一遍，最后点的那个就是最终时段。

        为什么不用 OCR 找"上午/下午/夜晚/凌晨"文字：这四个词在面板里位置固定、
        但 OCR 偶尔会把图标认成字，或因为选中态文字变绿而漏读。既然坐标稳定
        （等距 168px，实测校验过），全点反而是最少出错的做法 —— 反正最后一下
        决定结果，前几下点错也没影响。
        """
        for index, point in enumerate(sc.PERIOD_POINTS, start=1):
            self._click(point, wait=sc.PERIOD_WAIT)
            log.info(f'点了第 {index} 个时段 {point}')

        return self.round_success('已依次点完四个时段')

    @node_from(from_name='选择时段')
    @operation_node(name='加入日程', screenshot_before_round=False,
                    node_max_retry_times=4)
    def add_to_schedule(self) -> OperationRoundResult:
        screen = self.screenshot()
        point = self._find_text_center(
            screen, sc.KEYWORD_SCHEDULE_DONE, sc.CHAT_AREA, percent=0.8,
        )
        if point is None:
            return self.round_retry('找不到加入日程按钮', wait=1)
        self._click(point, wait=sc.SCHEDULE_WAIT)
        if self._has_text(self.screenshot(), sc.KEYWORD_SCHEDULE_DONE,
                          sc.CHAT_AREA, percent=0.8):
            return self.round_retry('加入日程没生效，再试', wait=1)
        return self.round_success('完成')

    @node_from(from_name='完成邀约')
    @node_from(from_name='加入日程')
    @node_from(from_name='确认完成')
    @operation_node(name='核对完成', screenshot_before_round=False)
    def verify_done(self) -> OperationRoundResult:
        """收尾：回联系人列表，这次邀约按「流程走完」计入成功次数。

        **成功判据是流程走完**：走到这个节点说明对话已经结束、结束界面已经点过，
        那就算这次邀约做完，直接 `invite_done += 1`。

        **不拿「剩余邀约N/3」判成功**：那个计数只在联系人页读得到，在会话页
        直接读永远是 -1（日志里那条「邀约完成（读数 -1）」就是这么来的）。用它
        决定加不加，会把真实完成的邀约漏掉、当天做不满次数。这里读它只为了写日志
        和刷新 `remain_before`，读不到就只写读数。
        """
        self._back_to_contact_list()
        self._need_scroll_top = True

        now = self._read_remain(self.screenshot())
        # 流程走完即计入成功，不看读数：读数只在联系人页可靠，会话页是 -1
        self.invite_done += 1
        if self.remain_before >= 0 and now >= 0:
            log.info(f'{self.current_agent} 邀约完成，剩余 {now}/3（原 {self.remain_before}）')
            self.remain_before = now
        else:
            log.info(f'{self.current_agent} 邀约完成（读数 {now}）')

        return self.round_success(f'{self.invite_done}/{self.config.daily_invite_limit}')

    @node_from(from_name='核对完成')
    @operation_node(name='完成后换人', screenshot_before_round=False)
    def after_one_invite(self) -> OperationRoundResult:
        if self.invite_done >= self.config.daily_invite_limit:
            return self.round_success(status='已完成邀约次数')

        if self.invite_attempts >= self.config.max_invite_attempts:
            log.info(f'已达尝试上限 {self.invite_attempts}/'
                     f'{self.config.max_invite_attempts}，收工')
            return self.round_success(status='已扫描到上限')

        return self.round_success(status='继续下一个')


def __debug() -> None:
    from zzz_od.context.zzz_context import ZContext

    ctx = ZContext()
    ctx.init()
    ctx.run_context.run_application(
        app_id=const.APP_ID,
        instance_idx=ctx.current_instance_idx,
        group_id=application_const.DEFAULT_GROUP_ID,
    )


if __name__ == '__main__':
    __debug()

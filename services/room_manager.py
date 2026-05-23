"""
多房间管理器
管理所有监控房间的生命周期
"""
import threading
import time
from typing import Dict, Optional

import config
from services.data_service import DataService
from models.database import LiveRoom, get_china_now
from utils.logger import get_logger
from utils import apply_jitter

logger = get_logger("room_manager")


class MonitoredRoom:
    """单个监控房间实例"""

    def __init__(self, live_id: str, manager, socketio=None):
        """
        初始化监控房间
        :param live_id: 直播间ID（作为唯一标识）
        :param manager: 房间管理器引用
        :param socketio: Socket.IO实例
        """
        self.live_id = live_id
        self.manager = manager
        self.socketio = socketio

        self.fetcher = None  # WebDouyinLiveFetcher实例
        self.thread = None  # 监控线程
        self.shutdown_event = threading.Event()  # 关闭事件
        self.lifecycle_lock = threading.Lock()  # 保护启动/停止生命周期，避免重复启动线程
        self.reconnect_count = 0  # 重连次数
        self.cookie_refresh_requested = False  # Cookie 更新触发的即时重连
        self.last_connect_time = None  # 最后连接时间
        self.offline_check_count = 0  # 连续未开播检测次数（用于判断是否应该结束场次）

        # 本地统计缓存
        self.stats = {
            'current_user_count': 0,
            'total_user_count': 0,
            'total_income': 0,
            'contributor_count': 0
        }
        self.last_stats = {
            'current_user_count': 0,
            'total_user_count': 0
        }

        # 贡献榜和送礼用户缓存
        self.user_contributions = {}
        self.gift_users = set()
        self.combo_gifts = {}

        logger.info(f"创建监控房间实例: live_id={live_id}")

    def start(self):
        """启动监控（在新线程中）"""
        with self.lifecycle_lock:
            if self.thread and self.thread.is_alive():
                logger.warning(f"房间 {self.live_id} 的监控线程已在运行")
                return

            self.shutdown_event.clear()
            self.reconnect_count = 0
            self.thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name=f"monitor-room-{self.live_id}"
            )
            self.thread.start()
        logger.info(f"启动房间 {self.live_id} 的监控线程")

    def stop(self):
        """停止监控"""
        self.shutdown_event.set()

        # 停止 fetcher（如果存在且已启动）
        # 检查 fetcher 是否已初始化并尝试安全停止
        if self.fetcher:
            try:
                # 检查内部 fetcher 是否有 ws 属性（已启动）
                if hasattr(self.fetcher, '_fetcher') and hasattr(self.fetcher._fetcher, 'ws'):
                    self.fetcher.stop()
                else:
                    logger.debug(f"[{self.live_id}] Fetcher 未启动，跳过停止操作")
            except Exception as e:
                logger.debug(f"[{self.live_id}] 停止 fetcher 时出错（已忽略）: {e}")

        if self._has_been_replaced():
            logger.info(f"房间 {self.live_id} 已重新启动，跳过旧实例停止状态写入")
            return

        # 用户手动停止时，先快速释放接口响应；直播场次是否结束由后台检查。
        self._schedule_manual_stop_session_check()
        self.manager.data_service.update_live_room(
            self.live_id,
            auto_reconnect=False
        )
        self.manager.data_service.update_live_room_status(
            self.live_id,
            'stopped',
            '用户手动停止'
        )
        self.manager.data_service.log_system_event(
            self.live_id,
            'disconnect',
            '用户手动停止监控',
            anchor_name=self.anchor_name if hasattr(self, 'anchor_name') else None
        )
        logger.info(f"房间 {self.live_id} 已停止监控")

    def _has_been_replaced(self) -> bool:
        """判断当前实例是否已被一次新的启动替换。"""
        active_room = self.manager.active_rooms.get(self.live_id)
        return active_room is not None and active_room is not self

    def _schedule_manual_stop_session_check(self):
        """异步检查手动停止时是否需要结束直播场次。"""
        thread = threading.Thread(
            target=self._check_manual_stop_session,
            daemon=True,
            name=f"manual-stop-session-check-{self.live_id}"
        )
        thread.start()

    def _check_manual_stop_session(self):
        """手动停止后的场次收尾检查，避免阻塞停止接口。"""
        try:
            current_session = self.manager.data_service.get_current_live_session(self.live_id)
            if current_session and current_session.status == 'live':
                if self._has_been_replaced():
                    logger.info(f"[{self.live_id}] 已重新启动，跳过手动停止后的场次检查")
                    return

                # 创建临时 fetcher 检测主播是否还在直播
                from crawler import DouyinLiveWebFetcher
                temp_fetcher = DouyinLiveWebFetcher(self.live_id)
                is_live = temp_fetcher.get_room_status()
                try:
                    temp_fetcher.session.close()
                except Exception:
                    pass

                if self._has_been_replaced():
                    logger.info(f"[{self.live_id}] 已重新启动，跳过手动停止后的场次结束")
                    return

                if is_live is True:
                    # 主播还在直播，保留场次不结束
                    logger.info(f"[{self.live_id}] 主播还在直播，保留场次: session_id={current_session.id}")
                elif is_live is False:
                    # 主播已下播，结束场次
                    logger.info(f"[{self.live_id}] 主播已下播，结束场次: session_id={current_session.id}")
                    self.manager.data_service.end_live_session(
                        current_session.id,
                        peak_viewer_count=current_session.peak_viewer_count
                    )
                else:
                    logger.info(f"[{self.live_id}] 无法确认主播是否下播，保留场次: session_id={current_session.id}")
        except Exception as e:
            logger.error(f"检测直播状态时出错: {e}")

    def _monitor_loop(self):
        """监控循环（支持自动重连）"""
        from ws_handlers.handlers import WebDouyinLiveFetcher

        try:
            while not self.shutdown_event.is_set():
                try:
                    # 创建新的fetcher实例
                    self.fetcher = WebDouyinLiveFetcher(
                        self.live_id,
                        self.manager.data_service,
                        self.socketio,
                        self
                    )

                    # 记录连接时间
                    self.last_connect_time = get_china_now()
                    self.manager.data_service.update_live_room(
                        self.live_id,
                        last_connect_time=self.last_connect_time
                    )

                    # 获取房间状态，只有在直播时才继续连接
                    is_live = self.fetcher.get_room_status()

                    # 在耗时操作后再次检查停止信号，防止覆盖 stop() 写入的状态
                    if self.shutdown_event.is_set():
                        break

                    if not is_live:
                        logger.info(f"房间 {self.live_id} 当前未开播")
                        # None 表示状态请求失败，不作为下播证据。
                        if is_live is False:
                            self.check_and_end_session_if_offline("检测到主播未开播")

                        # 根据返回值区分：None=请求失败，False=确认未开播
                        if is_live is None:
                            error_msg = (
                                getattr(self.fetcher, "status_error_message", None)
                                or "直播间状态检测失败，等待重试..."
                            )
                        else:
                            error_msg = '主播未开播，等待开播中...'

                        # 更新状态为等待
                        self.manager.data_service.update_live_room_status(
                            self.live_id,
                            'offline',
                            error_msg
                        )
                        self.manager.data_service.log_system_event(
                            self.live_id,
                            'not_live',
                            '检测到主播未开播，进入轮询模式',
                            anchor_name=self.anchor_name if hasattr(self, 'anchor_name') else None
                        )

                        # 进入轮询模式，等待主播开播
                        logger.info(f"房间 {self.live_id} 进入轮询模式，等待主播开播")
                        if self._poll_room_status():
                            # 检测到开播，重置重连次数并继续
                            self.reconnect_count = 0
                            self.manager.data_service.update_live_room_reconnect(self.live_id, 0)
                            continue
                        else:
                            # 被手动停止
                            logger.info(f"房间 {self.live_id} 轮询被手动停止，退出监控")
                            break
                    else:
                        if self.shutdown_event.is_set():
                            break

                        # 更新状态为监控中
                        self.manager.data_service.update_live_room_status(
                            self.live_id,
                            'monitoring'
                        )
                        self.manager.data_service.log_system_event(
                            self.live_id,
                            'connect',
                            f'开始监控直播间 {self.live_id}',
                            anchor_name=self.anchor_name if hasattr(self, 'anchor_name') else None
                        )

                        if self.shutdown_event.is_set():
                            break

                        # 启动WebSocket连接（阻塞直到断开）
                        self.fetcher.start()

                except Exception as e:
                    # 如果是停止信号导致的异常，直接退出
                    if self.shutdown_event.is_set():
                        break

                    logger.error(f"房间 {self.live_id} 监控出错: {e}")

                    # 记录错误
                    self.manager.data_service.update_live_room_status(
                        self.live_id,
                        'error',
                        str(e)
                    )
                    self.manager.data_service.log_system_event(
                        self.live_id,
                        'error',
                        f'监控出错: {str(e)}',
                        {'error': str(e)},
                        anchor_name=self.anchor_name if hasattr(self, 'anchor_name') else None
                    )

                # 检查是否应该重连（仅在已连接的情况下）
                if self.shutdown_event.is_set():
                    logger.info(f"房间 {self.live_id} 收到停止信号，退出监控循环")
                    break

                try:
                    # 只有在之前成功连接过的情况下才考虑重连
                    # （避免在未开播时无限重试）
                    if self.should_reconnect() and self.last_connect_time:
                        self.reconnect_count += 1
                        self.manager.data_service.update_live_room_reconnect(
                            self.live_id,
                            self.reconnect_count
                        )
                        self.manager.data_service.log_system_event(
                            self.live_id,
                            'reconnect',
                            f'准备第 {self.reconnect_count} 次重连',
                            anchor_name=self.anchor_name if hasattr(self, 'anchor_name') else None
                        )
                        logger.info(f"房间 {self.live_id} 准备第 {self.reconnect_count} 次重连")
                        if self.cookie_refresh_requested:
                            self.cookie_refresh_requested = False
                        else:
                            time.sleep(config.MONITOR_RECONNECT_DELAY)
                    else:
                        # 检查是否应该进入轮询模式（仅当开启自动重连时）
                        room = self.manager.data_service.get_live_room(self.live_id)
                        if room and room.auto_reconnect:
                            logger.info(f"房间 {self.live_id} 达到最大重连次数，进入等待开播状态")
                            self.manager.data_service.update_live_room_status(
                                self.live_id,
                                'waiting',
                                '等待主播开播'
                            )
                            self.manager.data_service.log_system_event(
                                self.live_id,
                                'waiting',
                                '达到最大重连次数，开始轮询直播状态',
                                anchor_name=self.anchor_name if hasattr(self, 'anchor_name') else None
                            )
                            # 进入轮询模式
                            if self._poll_room_status():
                                # 检测到开播，重置重连次数并继续循环
                                self.reconnect_count = 0
                                self.manager.data_service.update_live_room_reconnect(self.live_id, 0)
                                logger.info(f"房间 {self.live_id} 检测到开播，准备重新连接")
                                continue
                            else:
                                # 轮询被中断（shutdown_event被设置）
                                break
                        else:
                            logger.info(f"房间 {self.live_id} 达到最大重连次数且未开启自动重连，退出监控循环")
                            # 结束当前直播场次（如果存在）
                            self.end_current_session(reason="达到最大重连次数且未开启自动重连")
                            self.manager.data_service.update_live_room_status(
                                self.live_id,
                                'stopped',
                                '达到最大重连次数'
                            )
                            break
                except Exception as e:
                    logger.error(f"房间 {self.live_id} 重连逻辑出错: {e}")
                    # 重连逻辑异常时直接进入轮询模式，避免线程退出
                    try:
                        self.manager.data_service.update_live_room_status(
                            self.live_id,
                            'waiting',
                            f'重连出错，进入轮询: {str(e)}'
                        )
                    except Exception:
                        pass
                    if self._poll_room_status():
                        self.reconnect_count = 0
                        continue
                    else:
                        break
        finally:
            # 线程退出时确保状态正确，防止竞争条件导致状态残留
            if self.shutdown_event.is_set() and not self._has_been_replaced():
                self.manager.data_service.update_live_room_status(
                    self.live_id,
                    'stopped',
                    '监控已停止'
                )
            logger.info(f"房间 {self.live_id} 监控线程已退出")

    def _poll_room_status(self) -> bool:
        """
        轮询直播间状态，等待主播开播（无限轮询直到开播或手动停止）
        :return: True 表示检测到开播，False 表示被手动停止
        """
        from crawler import DouyinLiveWebFetcher

        logger.info(f"房间 {self.live_id} 开始轮询直播状态（等待主播开播）")

        poll_count = 0

        while not self.shutdown_event.is_set():
            try:
                # 创建临时 fetcher 用于检测状态
                temp_fetcher = DouyinLiveWebFetcher(self.live_id)
                is_live = temp_fetcher.get_room_status()

                # get_room_status 返回 True 表示正在直播
                if is_live:
                    logger.info(f"房间 {self.live_id} 检测到正在直播，准备连接")
                    self.manager.data_service.update_live_room_status(
                        self.live_id,
                        'offline',
                        '检测到开播，准备连接...'
                    )
                    self.manager.data_service.log_system_event(
                        self.live_id,
                        'detected',
                        '检测到主播开播，准备重新连接',
                        anchor_name=self.anchor_name if hasattr(self, 'anchor_name') else None
                    )
                    return True
                elif is_live is False:
                    self.check_and_end_session_if_offline("轮询确认主播未开播")
                    poll_count += 1
                    error_msg = '主播未开播，等待开播中...'
                    self.manager.data_service.update_live_room_status(
                        self.live_id,
                        'offline',
                        error_msg
                    )
                    logger.debug(f"房间 {self.live_id} 未开播，继续等待... (第{poll_count}次检测)")
                else:
                    poll_count += 1
                    error_msg = (
                        getattr(self.fetcher, "status_error_message", None)
                        or "直播间状态检测失败，等待重试..."
                    )
                    self.manager.data_service.update_live_room_status(
                        self.live_id,
                        'offline',
                        error_msg
                    )
                    logger.debug(f"房间 {self.live_id} 状态检测失败，继续等待... (第{poll_count}次检测)")

            except Exception as e:
                logger.debug(f"房间 {self.live_id} 轮询状态时出错: {e}")

            # 等待指定间隔后再次检测（应用抖动）
            poll_interval = apply_jitter(config.MONITOR_STATUS_POLL_INTERVAL)
            for _ in range(poll_interval):
                if self.shutdown_event.is_set():
                    logger.info(f"房间 {self.live_id} 轮询被手动停止")
                    return False
                time.sleep(1)

        # 被手动停止
        logger.info(f"房间 {self.live_id} 轮询被手动停止")
        return False

    def should_reconnect(self) -> bool:
        """判断是否应该重连"""
        # 从数据库获取房间配置
        room = self.manager.data_service.get_live_room(self.live_id)
        if not room:
            return False

        if not room.auto_reconnect:
            return False

        if self.reconnect_count >= config.MONITOR_MAX_RETRIES:
            return False

        return True

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return self.stats.copy()

    def end_current_session(self, reason: str = "监控停止"):
        """
        结束当前直播场次（如果存在）
        :param reason: 结束原因
        :return: 是否成功结束场次
        """
        # 优先使用 fetcher 的方法（如果存在且有活跃场次）
        if self.fetcher and self.fetcher.current_session_id:
            return self.fetcher._end_current_session(reason=reason)

        # 如果没有 fetcher，使用 data_service 直接结束场次
        data_service = self.manager.data_service
        current_session = data_service.get_current_live_session(self.live_id)
        if current_session:
            session_id = current_session.id
            success = data_service.end_live_session(session_id)
            if success:
                logger.info(f"[{self.live_id}] 结束直播场次: session_id={session_id}, 原因={reason}")

                # 重新查询获取最新的场次数据
                from models.database import LiveSession
                db_session = data_service.get_session()
                try:
                    session = db_session.query(LiveSession).filter(LiveSession.id == session_id).first()
                    if session:
                        current_session_data = {
                            'id': session.id,
                            'start_time': session.start_time.isoformat() if session.start_time else None,
                            'end_time': session.end_time.isoformat() if session.end_time else None,
                            'status': session.status,
                            'total_income': session.total_income,
                            'total_gift_count': session.total_gift_count,
                            'total_chat_count': session.total_chat_count,
                            'peak_viewer_count': session.peak_viewer_count
                        }

                        # 推送状态更新给前端
                        if self.socketio:
                            # 获取最新房间状态
                            room = self.manager.data_service.get_live_room(self.live_id)
                            room_status = room.status if room else None
                            room_error_message = room.error_message if room else None

                            self.socketio.emit(f'room_{self.live_id}_stats', {
                                'room_status': room_status,  # 新增：监控状态
                                'room_error_message': room_error_message,
                                'current_user_count': self.stats['current_user_count'],
                                'total_user_count': self.stats['total_user_count'],
                                'total_income': self.stats['total_income'],
                                'contributor_count': self.stats['contributor_count'],
                                'contributor_info': [],
                                'current_session': current_session_data
                            }, room=f'room_{self.live_id}')
                            logger.info(f"[{self.live_id}] 推送直播结束状态更新: session_id={session.id}, status={session.status}")
                finally:
                    db_session.close()
                return True
            else:
                logger.warning(f"[{self.live_id}] 结束直播场次失败: session_id={session_id}")
        return False

    def reset_offline_counter(self):
        """重置未开播检测计数器（当成功连接时调用）"""
        if self.offline_check_count > 0:
            logger.debug(f"[{self.live_id}] 重置未开播检测计数器: {self.offline_check_count} -> 0")
            self.offline_check_count = 0

    def check_and_end_session_if_offline(self, reason: str = "检测到未开播"):
        """
        检查是否应该结束直播场次
        连续多次确认未开播后结束场次，避免短暂状态误判把同一场直播拆成多场
        :param reason: 未开播的原因
        :return: 是否结束了场次
        """
        offline_threshold = max(1, config.MONITOR_OFFLINE_END_THRESHOLD)

        self.offline_check_count += 1
        logger.debug(f"[{self.live_id}] 检测到未开播，计数器: {self.offline_check_count}/{offline_threshold}，原因={reason}")

        if self.offline_check_count >= offline_threshold:
            logger.info(f"[{self.live_id}] 连续{offline_threshold}次检测到未开播，结束直播场次")
            return self.end_current_session(reason=f"连续{offline_threshold}次未开播: {reason}")
        return False

    def update_contribution(self, user_id: str, user_name: str, gift_value: float = 0,
                           gift_count: int = 0, chat_count: int = 0, user_avatar: str = None,
                           gender: int = None, follower_count: int = None,
                           following_count: int = None, age_range: int = None,
                           fans_club_level: int = None, user_level: int = None):
        """更新用户贡献"""
        if user_id not in self.user_contributions:
            self.user_contributions[user_id] = {
                'user_name': user_name,
                'score': 0,
                'avatar': user_avatar,
                'gift_count': 0,
                'fans_club_level': fans_club_level or 0,
                'user_level': user_level or 0
            }
            logger.debug(f"[新贡献用户] {user_id}={user_name}, avatar={user_avatar}, gift_value={gift_value}")
        else:
            old_name = self.user_contributions[user_id]['user_name']
            old_avatar = self.user_contributions[user_id].get('avatar')
            # 如果已有记录，更新用户名和头像（可能发生变化）
            self.user_contributions[user_id]['user_name'] = user_name
            if user_avatar:
                self.user_contributions[user_id]['avatar'] = user_avatar
            if fans_club_level and fans_club_level > 0:
                self.user_contributions[user_id]['fans_club_level'] = fans_club_level
            if user_level and user_level > 0:
                self.user_contributions[user_id]['user_level'] = user_level
            if old_name != user_name or old_avatar != user_avatar:
                logger.debug(f"[更新用户信息] {user_id}: {old_name} -> {user_name}, avatar: {old_avatar} -> {user_avatar}")

        self.user_contributions[user_id]['score'] += gift_value
        self.user_contributions[user_id]['gift_count'] = self.user_contributions[user_id].get('gift_count', 0) + gift_count
        logger.debug(f"[更新贡献] {user_id}={user_name}, score={self.user_contributions[user_id]['score']}, gift_count={self.user_contributions[user_id]['gift_count']}")

        # 同步到数据库
        self.manager.data_service.update_user_contribution(
            self.live_id,
            self.anchor_name if hasattr(self, 'anchor_name') else None,
            user_id,
            user_name,
            gift_value=gift_value,
            gift_count=gift_count,
            chat_count=chat_count,
            user_avatar=user_avatar,
            gender=gender,
            follower_count=follower_count,
            following_count=following_count,
            age_range=age_range,
            fans_club_level=fans_club_level
        )

    def get_contribution_rank(self, limit: int = 100) -> list:
        """获取贡献排行榜（只显示送过礼物的用户）"""
        rank_list = sorted(
            [
                {
                    'user_id': k,
                    'user': v['user_name'],
                    'score': v['score'],
                    'avatar': v['avatar'],
                    'fans_club_level': v.get('fans_club_level', 0),
                    'user_level': v.get('user_level', 0)
                }
                for k, v in self.user_contributions.items()
                if v['score'] > 0  # 只包含贡献值大于0的用户（送过礼物的）
            ],
            key=lambda x: x['score'],
            reverse=True
        )[:limit]

        for i, item in enumerate(rank_list):
            item['rank'] = i + 1

        # 记录贡献榜数据用于调试
        if rank_list:
            top_5 = [{'rank': r['rank'], 'user_id': r['user_id'], 'user': r['user'], 'score': r['score']} for r in rank_list[:5]]
            logger.debug(f"[贡献榜TOP5] {top_5}")

        return rank_list


class RoomManager:
    """管理所有监控房间的生命周期"""

    def __init__(self, data_service: DataService, socketio=None):
        """
        初始化房间管理器
        :param data_service: 数据服务实例
        :param socketio: Socket.IO实例
        """
        self.data_service = data_service
        self.socketio = socketio
        self.active_rooms: Dict[str, MonitoredRoom] = {}  # live_id -> MonitoredRoom
        self.lock = threading.Lock()

        # 启动时清理状态不一致的房间
        self._cleanup_stale_statuses()

        # 启动时清理旧的未结束场次
        self._cleanup_stale_live_sessions()

        logger.info("房间管理器初始化完成")

    def _cleanup_stale_statuses(self):
        """
        清理状态不一致的房间
        当应用被强制退出后，数据库中的状态可能仍为 monitoring
        需要重置这些房间的状态为 stopped
        """
        try:
            # 获取所有状态为 monitoring 的房间
            monitoring_rooms = self.data_service.list_live_rooms(status='monitoring')

            for room in monitoring_rooms:
                # 如果房间不在活跃列表中，说明实际没有在监控
                if room.live_id not in self.active_rooms:
                    logger.warning(f"房间 {room.live_id} 状态为 monitoring 但实际未监控，重置状态")
                    self.data_service.update_live_room_status(
                        room.live_id,
                        'stopped',
                        '应用重启后状态重置'
                    )
                    self.data_service.log_system_event(
                        room.live_id,
                        'status_reset',
                        '应用重启：检测到状态不一致，已重置为 stopped',
                        anchor_name=room.anchor_name
                    )
        except Exception as e:
            logger.error(f"清理状态失败: {e}")

    def _cleanup_stale_live_sessions(self):
        """
        清理旧的未结束直播场次
        当应用被强制退出后，数据库中可能仍有 status='live' 的场次
        需要将这些旧场次标记为 'ended'
        """
        try:
            count = self.data_service.cleanup_stale_live_sessions(stale_threshold_hours=24)
            if count > 0:
                logger.info(f"清理了 {count} 个未结束的直播场次")
        except Exception as e:
            logger.error(f"清理未结束场次失败: {e}")

    def add_room(self, live_id: str, monitor_type: str = '24h', auto_reconnect: bool = True) -> Optional[str]:
        """
        添加监控房间
        :param live_id: 直播间ID
        :param monitor_type: 监控类型 (默认为 24h，参数保留以兼容旧版)
        :param auto_reconnect: 是否自动重连 (默认为 True，参数保留以兼容旧版)
        :return: 直播间ID，失败返回None
        """
        with self.lock:
            # 检查是否已存在
            existing_room = self.data_service.get_live_room(live_id)
            if existing_room:
                if existing_room.archived_at or existing_room.status == 'archived':
                    self.data_service.restore_live_room(live_id)
                    logger.info(f"直播间 {live_id} 已从历史数据恢复到主页")
                if existing_room.live_id in self.active_rooms:
                    logger.warning(f"直播间 {live_id} 已在监控中")
                    return None
            else:
                # 创建新房间记录
                self.data_service.create_live_room(
                    live_id=live_id,
                    anchor_name=live_id,  # 初始使用 live_id 作为占位符，后续会更新
                    monitor_type=monitor_type,
                    auto_reconnect=auto_reconnect,
                    status='stopped'
                )

            # 创建MonitoredRoom实例
            monitored_room = MonitoredRoom(
                live_id=live_id,
                manager=self,
                socketio=self.socketio
            )

            self.active_rooms[live_id] = monitored_room
            logger.info(f"添加监控房间: live_id={live_id}")
            return live_id

    def remove_room(self, live_id: str) -> bool:
        """
        移除监控房间
        :param live_id: 直播间ID
        :return: 是否成功
        """
        with self.lock:
            monitored_room = self.active_rooms.pop(live_id, None)
            if not monitored_room:
                logger.warning(f"房间 {live_id} 不在活跃列表中")
                return False

        try:
            monitored_room.stop()
        except Exception as e:
            logger.error(f"移除房间 {live_id} 时停止监控失败: {e}")
            return False

        logger.info(f"移除监控房间: live_id={live_id}")
        return True

    def get_room(self, live_id: str) -> Optional[MonitoredRoom]:
        """获取监控房间实例"""
        return self.active_rooms.get(live_id)

    def get_room_by_live_id(self, live_id: str) -> Optional[MonitoredRoom]:
        """根据live_id获取监控房间实例"""
        return self.active_rooms.get(live_id)

    def start_room(self, live_id: str) -> bool:
        """
        启动房间监控
        :param live_id: 直播间ID
        :return: 是否成功
        """
        already_running = False
        with self.lock:
            room = self.data_service.get_live_room(live_id)
            if room and (room.archived_at or room.status == 'archived'):
                logger.warning(f"房间 {live_id} 已归档，需要恢复后才能启动")
                return False

            monitored_room = self.active_rooms.get(live_id)
            if not monitored_room:
                # 房间不在活跃列表中，检查数据库中是否存在
                if not room:
                    logger.warning(f"房间 {live_id} 在数据库中不存在")
                    return False

                # 如果数据库状态为 monitoring，先重置为 stopped
                if room.status == 'monitoring':
                    logger.info(f"房间 {live_id} 数据库状态为 monitoring，重置为 stopped")
                    self.data_service.update_live_room_status(
                        live_id,
                        'stopped',
                        '启动前重置状态'
                    )

                # 创建 MonitoredRoom 实例并添加到活跃列表
                monitored_room = MonitoredRoom(
                    live_id=live_id,
                    manager=self,
                    socketio=self.socketio
                )
                self.active_rooms[live_id] = monitored_room
                logger.info(f"重新创建监控房间实例: live_id={live_id}")
            elif monitored_room.shutdown_event.is_set() and monitored_room.thread and monitored_room.thread.is_alive():
                logger.info(f"房间 {live_id} 上一次停止尚未完全退出，创建新监控实例")
                monitored_room = MonitoredRoom(
                    live_id=live_id,
                    manager=self,
                    socketio=self.socketio
                )
                self.active_rooms[live_id] = monitored_room
            elif monitored_room.thread and monitored_room.thread.is_alive():
                already_running = True

        if already_running:
            logger.warning(f"房间 {live_id} 的监控线程已在运行")
            self.data_service.update_live_room(live_id, auto_reconnect=True)
            return True

        # 添加防风控启动延迟。不要持有全局房间锁，避免挡住停止/启动操作。
        if config.ANTI_DETECTION_ENABLED and config.ANTI_DETECTION_THREAD_START_INTERVAL > 0:
            time.sleep(config.ANTI_DETECTION_THREAD_START_INTERVAL)

        # 用户手动启动监控时，重新启用自动重连
        self.data_service.update_live_room(live_id, auto_reconnect=True)
        self.data_service.update_live_room_status(
            live_id,
            'offline',
            '监控启动中...'
        )

        monitored_room.start()
        return True

    def stop_room(self, live_id: str) -> bool:
        """
        停止房间监控
        :param live_id: 直播间ID
        :return: 是否成功
        """
        with self.lock:
            monitored_room = self.active_rooms.pop(live_id, None)
            if not monitored_room:
                # 房间不在活跃列表中，但可能数据库状态为 monitoring
                # 检查并修复状态不一致
                room = self.data_service.get_live_room(live_id)
                if room and room.status in ('monitoring', 'offline', 'waiting', 'error'):
                    logger.warning(f"房间 {live_id} 不在活跃列表中但数据库状态为 {room.status}，重置状态")
                    self.data_service.update_live_room_status(
                        live_id,
                        'stopped',
                        '状态不一致，已重置'
                    )
                    return True
                logger.warning(f"房间 {live_id} 不存在")
                return False

        monitored_room.stop()
        return True

    def restart_failed_rooms(self) -> int:
        """
        重启失败的房间
        :return: 重启的房间数量
        """
        restarted = 0
        with self.lock:
            rooms_to_restart = []

            for live_id, monitored_room in list(self.active_rooms.items()):
                # 检查是否需要重启
                room = self.data_service.get_live_room(live_id)
                if room and not room.archived_at and room.status in ('error', 'stopped') and room.auto_reconnect:
                    # 检查线程是否已停止
                    if not monitored_room.thread or not monitored_room.thread.is_alive():
                        rooms_to_restart.append(live_id)

            for live_id in rooms_to_restart:
                monitored_room = self.active_rooms.get(live_id)
                if monitored_room:
                    monitored_room.start()
                    restarted += 1
                    logger.info(f"重启失败的房间: live_id={live_id}")

        return restarted

    def get_all_rooms_status(self) -> list:
        """获取所有房间的状态"""
        with self.lock:
            status_list = []
            for live_id, monitored_room in self.active_rooms.items():
                room = self.data_service.get_live_room(live_id)
                if room:
                    status_list.append({
                        'live_id': room.live_id,
                        'anchor_name': room.anchor_name,
                        'status': room.status,
                        'monitor_type': room.monitor_type,
                        'auto_reconnect': room.auto_reconnect,
                        'reconnect_count': room.reconnect_count,
                        'is_active': monitored_room.thread and monitored_room.thread.is_alive(),
                        'stats': monitored_room.get_stats()
                    })
            return status_list

    def update_douyin_cookie(self, douyin_cookie: str, reconnect_active: bool = True) -> int:
        """
        同步 Cookie 到运行中的房间。
        :return: 已同步的运行中房间数量
        """
        updated = 0
        with self.lock:
            for live_id, monitored_room in self.active_rooms.items():
                if monitored_room.fetcher:
                    try:
                        if reconnect_active:
                            monitored_room.cookie_refresh_requested = True
                            monitored_room.reconnect_count = 0
                            self.data_service.update_live_room_reconnect(live_id, 0)
                        monitored_room.fetcher.update_douyin_cookie(douyin_cookie, reconnect=reconnect_active)
                        updated += 1
                    except Exception as e:
                        logger.warning(f"同步 Cookie 到房间 {live_id} 失败: {e}")
        return updated

    def shutdown(self):
        """关闭所有房间"""
        with self.lock:
            rooms_to_stop = list(self.active_rooms.items())
            self.active_rooms.clear()

        for live_id, monitored_room in rooms_to_stop:
            try:
                monitored_room.stop()
            except Exception as e:
                logger.error(f"关闭房间 {live_id} 时出错: {e}")

        logger.info("所有监控房间已关闭")

    def get_display_status(self) -> list:
        """
        获取所有房间的状态摘要（供终端状态面板调用）
        :return: 房间状态列表
        """
        rows = []
        try:
            all_rooms = self.data_service.list_live_rooms()
            for room in all_rooms:
                live_id = room.live_id
                row = {
                    'anchor_name': room.anchor_name or live_id,
                    'live_id': live_id,
                    'status': room.status or 'stopped',
                    'current_user_count': 0,
                    'total_income': 0,
                    'error_message': room.error_message if hasattr(room, 'error_message') else '',
                }
                monitored = self.active_rooms.get(live_id)
                if monitored:
                    row['current_user_count'] = monitored.stats.get('current_user_count', 0)
                    row['total_income'] = monitored.stats.get('total_income', 0)
                rows.append(row)
        except Exception as e:
            logger.error(f"获取显示状态失败: {e}")
        return rows

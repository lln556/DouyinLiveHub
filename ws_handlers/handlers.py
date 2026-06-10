"""
WebSocket处理器
扩展核心抓取类，添加数据库存储和Socket.IO推送
"""
import gzip
import threading
import time
from typing import TYPE_CHECKING

import websocket

if TYPE_CHECKING:
    from services.room_manager import MonitoredRoom

from protobuf.douyin import PushFrame, Response, ChatMessage, GiftMessage, LikeMessage, RoomUserSeqMessage, ControlMessage
from utils.logger import get_logger
import config


def parse_formatted_number(value):
    """
    解析抖音返回的格式化数字（如 '46.8万', '1.2亿'）转换为整数
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)

    value_str = str(value).strip()
    if not value_str:
        return 0

    # 处理带"万"的数字
    if '万' in value_str:
        num_str = value_str.replace('万', '').strip()
        try:
            return int(float(num_str) * 10000)
        except ValueError:
            return 0

    # 处理带"亿"的数字
    if '亿' in value_str:
        num_str = value_str.replace('亿', '').strip()
        try:
            return int(float(num_str) * 100000000)
        except ValueError:
            return 0

    # 直接解析数字
    try:
        return int(value_str)
    except ValueError:
        return 0


class WebDouyinLiveFetcher:
    """
    扩展核心抓取类，添加数据库存储和Socket.IO推送
    保持与原有WebDouyinLiveFetcher的兼容性
    """

    def __init__(self, live_id: str, data_service, socketio_instance, monitored_room: 'MonitoredRoom',
                 proxy_enabled=None, proxy_url=None):
        """
        初始化
        :param live_id: 直播间ID（作为唯一标识）
        :param data_service: 数据服务实例
        :param socketio_instance: Socket.IO实例
        :param monitored_room: MonitoredRoom实例
        :param proxy_enabled: 是否启用代理（None则从配置文件读取）
        :param proxy_url: 代理URL（None则从配置文件读取）
        """
        from crawler import DouyinLiveWebFetcher

        self.live_id = live_id
        self.data_service = data_service
        self.monitored_room = monitored_room
        self.socketio = socketio_instance

        # 代理配置
        self.proxy_enabled = proxy_enabled if proxy_enabled is not None else config.PROXY_ENABLED
        self.proxy_url = proxy_url if proxy_url is not None else config.get_proxy_url()

        # 初始化房间级日志器
        self.log = get_logger("handlers", live_id)

        # 创建核心抓取器实例，传递代理配置
        self._fetcher = DouyinLiveWebFetcher(live_id, proxy_enabled=self.proxy_enabled, proxy_url=self.proxy_url)

        # 设置回调函数
        self._fetcher._wsOnMessage = self._wsOnMessage
        self._fetcher._wsOnOpen = self._wsOnOpen
        self._fetcher._wsOnError = self._wsOnError
        self._fetcher._wsOnClose = self._wsOnClose

        # 本地数据（用于实时推送）
        self.traceId_list = []
        self.like_message_keys = []
        self.gift_users = set()
        self.total_income = 0
        self.current_session_id = None  # 当前直播场次ID
        self.current_viewer_count = 0  # 当前观看人数（用于计算峰值）
        self.max_viewer_count = 0  # 峰值观看人数
        self.anchor_name = None  # 主播名称

        # Cookie 被动测活信号（内存时间戳，不入库）
        self.ws_open_time = None    # WebSocket 连接建立时间
        self.last_chat_time = None  # 最近一条弹幕时间
        self.last_gift_time = None  # 最近一条礼物时间

        self.log.info(f"初始化WebDouyinLiveFetcher: live_id={live_id}")

        # 尝试预加载当前场次数据（在所有实例变量初始化后）
        self._preload_session_data()

    def _preload_session_data(self):
        """预加载当前场次数据"""
        try:
            data_service = self.monitored_room.manager.data_service
            current_session = data_service.get_current_live_session(self.live_id)

            if current_session:
                self.current_session_id = current_session.id
                self.anchor_name = current_session.anchor_name
                self.log.info(f"预加载现有直播场次: session_id={self.current_session_id}")

                # 加载贡献榜
                if not self.monitored_room.user_contributions:
                    session_contributors = data_service.get_session_contributors(self.live_id, current_session.id, limit=1000)
                    for contributor in session_contributors:
                        self.monitored_room.user_contributions[contributor['user_id']] = {
                            'user_name': contributor['nickname'],
                            'score': contributor['contribution_value'],
                            'avatar': contributor['user_avatar'],
                            'gift_count': contributor['gift_count'],
                            'like_count': contributor.get('like_count', 0),
                            'user_level': contributor.get('user_level', 0),
                            'fans_club_level': contributor.get('fans_club_level', 0)
                        }
                    self.log.info(f"预加载了 {len(session_contributors)} 个贡献者到本地缓存")
        except Exception as e:
            self.log.error(f"预加载数据失败: {e}")

    def start(self):
        """启动WebSocket连接"""
        self._fetcher.start()

    def stop(self):
        """停止WebSocket连接"""
        self._fetcher.stop()

    def update_douyin_cookie(self, douyin_cookie: str, reconnect: bool = False):
        """更新 Cookie，并可断开当前 WebSocket 触发重连。"""
        self._fetcher.update_douyin_cookie(douyin_cookie)
        if reconnect and hasattr(self._fetcher, 'ws'):
            try:
                self._fetcher.ws.close()
            except Exception as e:
                self.log.debug(f"更新 Cookie 后断开 WebSocket 失败（已忽略）: {e}")

    def get_room_status(self):
        """获取房间状态"""
        return self._fetcher.get_room_status()

    @property
    def status_error_message(self):
        return getattr(self._fetcher, "status_error_message", None)


    def _wsOnMessage(self, ws, message):
        """处理WebSocket消息（重写原有方法）"""
        try:
            self._fetcher.record_websocket_data()
            package = PushFrame().parse(message)
            response = Response().parse(gzip.decompress(package.payload))

            # 发送ACK确认
            if response.need_ack:
                ack = PushFrame(
                    log_id=package.log_id,
                    payload_type='ack',
                    payload=response.internal_ext.encode('utf-8')
                ).SerializeToString()
                ws.send(ack, websocket.ABNF.OPCODE_BINARY)

            # 处理消息列表
            if response.messages_list:
                for msg in response.messages_list:
                    method = msg.method
                    self._fetcher.record_websocket_method(method)
                    try:
                        if method == 'WebcastChatMessage':
                            self._handle_chat_message(ChatMessage().parse(msg.payload))
                        elif method == 'WebcastGiftMessage':
                            self._handle_gift_message(GiftMessage().parse(msg.payload))
                        elif method == 'WebcastLikeMessage':
                            self._handle_like_message(LikeMessage().parse(msg.payload))
                        elif method == 'WebcastRoomUserSeqMessage':
                            self._handle_stats_message(RoomUserSeqMessage().parse(msg.payload))
                        elif method == 'WebcastControlMessage':
                            self._handle_control_message(ControlMessage().parse(msg.payload))
                    except Exception as e:
                        self.log.error(f"处理消息出错 [method={method}]: {e}")
        except Exception as e:
            self.log.error(f"解析消息出错: {e}")

    def _handle_chat_message(self, chat_msg):
        """处理聊天消息"""
        self.last_chat_time = time.time()
        user = chat_msg.user.nick_name
        content = chat_msg.content
        level = chat_msg.user.pay_grade.level if hasattr(chat_msg.user, 'pay_grade') else 0

        # 处理用户ID：优先使用 id_str，如果 ID 为匿名特征值（如 111111, 0），则使用"用户名_等级"组合
        raw_id = chat_msg.user.id_str if hasattr(chat_msg.user, 'id_str') and chat_msg.user.id_str else str(chat_msg.user.id)
        if raw_id in ['0', '111111']:
            user_id = f"anon_{user}_{level}"
        else:
            user_id = raw_id

        # 提取用户额外信息
        fans_club_level = 0
        if hasattr(chat_msg.user, 'fans_club') and chat_msg.user.fans_club and chat_msg.user.fans_club.data:
            fans_club_level = chat_msg.user.fans_club.data.level or 0
        gender = chat_msg.user.gender if hasattr(chat_msg.user, 'gender') else 0
        follower_count = 0
        following_count = 0
        if hasattr(chat_msg.user, 'follow_info') and chat_msg.user.follow_info:
            follower_count = chat_msg.user.follow_info.follower_count or 0
            following_count = chat_msg.user.follow_info.following_count or 0
        age_range = chat_msg.user.age_range if hasattr(chat_msg.user, 'age_range') else 0

        # 获取data_service（从app全局变量或monitored_room）
        data_service = self.monitored_room.manager.data_service

        # 构建包含等级图标、粉丝团图标和用户名的消息内容
        level_img_tag = f'<img src="/level_img/level_{level}.png" class="user-level-icon" alt="等级">' if level else ''
        fansclub_tag = f'<img src="/fansclub_img/fansclub_{fans_club_level}.png" class="fans-club-icon" alt="粉丝团">' if fans_club_level > 0 else ''
        message_content_html = f'{level_img_tag}{fansclub_tag} <span class="user-highlight" data-user-id="{user_id}" data-user-name="{user}">{user}</span>: {content}'

        is_gift_user = user in self.gift_users

        # 保存到数据库
        data_service.save_chat_message(
            self.live_id,
            live_session_id=self.current_session_id,
            anchor_name=self.anchor_name,
            user_id=user_id,
            user_name=user,
            user_level=level,
            content=content,
            is_gift_user=is_gift_user,
            fans_club_level=fans_club_level
        )

        # 更新场次弹幕计数
        if self.current_session_id:
            data_service.increment_session_stats(
                self.current_session_id,
                chat_count_delta=1
            )

        # 更新贡献表（弹幕计数 + 用户信息）
        self.monitored_room.update_contribution(
            user_id,
            user,
            chat_count=1,
            user_level=level,
            gender=gender,
            follower_count=follower_count,
            following_count=following_count,
            age_range=age_range,
            fans_club_level=fans_club_level
        )

        # 通过Socket.IO推送到前端
        message_data = {
            'type': 'chat',
            'user': user,
            'user_id': user_id,
            'content': message_content_html,
            'is_gift_user': is_gift_user,
            'fans_club_level': fans_club_level,
            'gender': gender,
            'follower_count': follower_count,
            'following_count': following_count,
            'age_range': age_range,
        }
        self.socketio.emit(f'room_{self.live_id}', message_data, room=f'room_{self.live_id}')
        self.log.debug(f"发送弹幕消息: {user}: {content}")

    def _handle_gift_message(self, gift_msg):
        """处理礼物消息

        礼物推送机制：
        1. 使用 trace_id 去重：防止同一消息重复处理
        2. 使用 group_id 组合连击：不依赖 send_type，所有礼物都可能是连击的
        """
        # 任何礼物消息到达都证明 Cookie 工作正常（在去重前记录）
        self.last_gift_time = time.time()
        # 获取data_service
        data_service = self.monitored_room.manager.data_service

        user = gift_msg.user.nick_name
        gift_name = gift_msg.gift.name
        gift_price = gift_msg.gift.diamond_count
        # 某些礼物的 API 返回价格不准确，按名称修正
        GIFT_PRICE_FIX = {
            '闪烁星河': 99,
            '点点星光': 9,
        }
        if gift_name in GIFT_PRICE_FIX:
            gift_price = GIFT_PRICE_FIX[gift_name]
        level = gift_msg.user.pay_grade.level if hasattr(gift_msg.user, 'pay_grade') else 0

        # 处理用户ID：优先使用 id_str，如果 ID 为匿名特征值（如 111111, 0），则使用"用户名_等级"组合
        raw_id = gift_msg.user.id_str if hasattr(gift_msg, 'id_str') and gift_msg.user.id_str else str(gift_msg.user.id)
        if raw_id in ['0', '111111']:
            user_id = f"anon_{user}_{level}"
        else:
            user_id = raw_id

        # 提取用户额外信息
        fans_club_level = 0
        if hasattr(gift_msg.user, 'fans_club') and gift_msg.user.fans_club and gift_msg.user.fans_club.data:
            fans_club_level = gift_msg.user.fans_club.data.level or 0
        gender = gift_msg.user.gender if hasattr(gift_msg.user, 'gender') else 0
        follower_count = 0
        following_count = 0
        if hasattr(gift_msg.user, 'follow_info') and gift_msg.user.follow_info:
            follower_count = gift_msg.user.follow_info.follower_count or 0
            following_count = gift_msg.user.follow_info.following_count or 0
        age_range = gift_msg.user.age_range if hasattr(gift_msg.user, 'age_range') else 0

        # 获取 gift_id 和 group_id
        gift_id_str = str(gift_msg.gift_id) if hasattr(gift_msg, 'gift_id') else None
        group_id_str = str(gift_msg.group_id) if hasattr(gift_msg, 'group_id') else None

        # 获取用户头像
        avatar = None
        if hasattr(gift_msg.user, 'avatar_thumb') and gift_msg.user.avatar_thumb:
            avatar = gift_msg.user.avatar_thumb.url_list_list[0] if gift_msg.user.avatar_thumb.url_list_list else None

        # ========== 第一步：trace_id 去重 ==========
        trace_id = getattr(gift_msg, 'trace_id', None) or None

        if trace_id and trace_id in self.traceId_list:
            self.log.debug(f"礼物消息已处理过（trace_id去重）: trace_id={trace_id}")
            return

        # 记录新的 trace_id
        if trace_id:
            self.traceId_list.append(trace_id)
            # 限制 traceId_list 大小，防止内存泄漏
            if len(self.traceId_list) > 1000:
                self.traceId_list = self.traceId_list[-500:]

        self.log.debug(f"[礼物消息] user_id={user_id}, user_name={user}, gift_name={gift_name}, price={gift_price}, send_type={gift_msg.send_type}, group_id={group_id_str}, trace_id={trace_id}")

        # ========== 第二步：使用 group_id 组合连击礼物 ==========
        # 有 group_id 的礼物都可能是连击礼物（不依赖 send_type）
        if group_id_str:
            combo_key = f"{group_id_str}_{user_id}_{gift_id_str}"

            # 检查是否有 combo_count（连击计数）
            current_count = getattr(gift_msg, 'combo_count', None)

            if current_count is not None and current_count > 0:
                # ========== 连击礼物：使用 combo_count 跟踪 ==========
                # 获取每次连击的礼物数量
                per_combo_count = gift_msg.group_count if hasattr(gift_msg, 'group_count') else 1

                # 初始化或获取 combo 状态
                if combo_key not in self.monitored_room.combo_gifts:
                    self.monitored_room.combo_gifts[combo_key] = {
                        'last_count': 0,
                        'last_gift_count': 0,
                        'db_id': None,
                    }

                last_count = self.monitored_room.combo_gifts[combo_key]['last_count']

                # 检查重复：count 不变则跳过
                if current_count == last_count:
                    self.log.debug(f"连击礼物重复消息，跳过: combo_key={combo_key}, count={current_count}")
                    return

                # 检测 combo_count 回退（如抖音发送汇总消息导致 combo_count 变小）
                count_diff = current_count - last_count
                if count_diff < 0:
                    # combo_count 回退，重置为新序列
                    self.log.debug(
                        f"连击礼物 combo_count 回退，重置为新序列: combo_key={combo_key}, "
                        f"last_count={last_count}, current_count={current_count}, "
                        f"per_combo_count={per_combo_count}"
                    )
                    self.monitored_room.combo_gifts[combo_key] = {
                        'last_count': 0,
                        'last_gift_count': 0,
                        'db_id': None,
                    }

                # 更新状态
                self.monitored_room.combo_gifts[combo_key]['last_count'] = current_count

                # 总礼物数量 = 连击次数 × 每次数量
                gift_count = current_count * per_combo_count
                total_gift_value = gift_price * gift_count

                # 判断是插入新记录还是更新已有记录
                db_id = self.monitored_room.combo_gifts[combo_key]['db_id']
                is_new_record = (db_id is None)

                if is_new_record:
                    msg = data_service.save_gift_message(
                        self.live_id,
                        live_session_id=self.current_session_id,
                        anchor_name=self.anchor_name,
                        user_id=user_id,
                        user_name=user,
                        user_level=level,
                        gift_id=gift_id_str,
                        gift_name=gift_name,
                        gift_count=gift_count,
                        gift_price=gift_price,
                        total_value=total_gift_value,
                        send_type='combo',
                        group_id=group_id_str,
                        trace_id=trace_id,
                        fans_club_level=fans_club_level
                    )
                    if msg:
                        self.monitored_room.combo_gifts[combo_key]['db_id'] = msg.id
                    self.log.debug(f"连击礼物首次保存: {user} {gift_name}x{gift_count}, db_id={msg.id if msg else None}")
                else:
                    data_service.update_gift_message(
                        db_id,
                        gift_count=gift_count,
                        total_value=total_gift_value
                    )
                    self.log.debug(f"连击礼物更新记录: {user} {gift_name}x{gift_count}, db_id={db_id}")

                # 计算本次增量（用于统计和推送）
                # 使用总礼物数量差值，而非 count_diff * per_combo_count
                # 因为 group_count 在同一连击序列中可能变化
                last_gift_count = self.monitored_room.combo_gifts[combo_key]['last_gift_count']
                partial_count = gift_count - last_gift_count
                self.monitored_room.combo_gifts[combo_key]['last_gift_count'] = gift_count
                partial_value = gift_price * partial_count

                # 防护：跳过非正增量，防止任何情况下产生负数统计
                if partial_count <= 0:
                    self.log.warning(
                        f"连击礼物增量异常，跳过统计更新: combo_key={combo_key}, "
                        f"gift_count={gift_count}, last_gift_count={last_gift_count}, "
                        f"partial_count={partial_count}"
                    )
                    # 数据库记录已更新（总量是正确的），只跳过增量统计和推送
                    if gift_msg.repeat_end == 1:
                        del self.monitored_room.combo_gifts[combo_key]
                    return

                self.total_income += partial_value
                self.gift_users.add(user)
                self.monitored_room.stats['total_income'] = self.total_income
                self.monitored_room.update_contribution(
                    user_id,
                    user,
                    gift_value=partial_value,
                    gift_count=partial_count,
                    user_avatar=avatar,
                    user_level=level,
                    gender=gender,
                    follower_count=follower_count,
                    following_count=following_count,
                    age_range=age_range,
                    fans_club_level=fans_club_level
                )

                # 更新场次统计
                if self.current_session_id:
                    data_service.increment_session_stats(
                        self.current_session_id,
                        income_delta=partial_value,
                        gift_count_delta=partial_count
                    )

                # 推送前端
                level_img_tag = f'<img src="/level_img/level_{level}.png" class="user-level-icon" alt="等级">' if level else ''
                fansclub_tag = f'<img src="/fansclub_img/fansclub_{fans_club_level}.png" class="fans-club-icon" alt="粉丝团">' if fans_club_level > 0 else ''
                if gift_msg.repeat_end == 1:
                    gift_message_content_html = f'{level_img_tag}{fansclub_tag} <span class="user-highlight" data-user-id="{user_id}" data-user-name="{user}">{user}</span> 连击完成！赠送了 {gift_count} 个 {gift_name} (价值{total_gift_value}钻石)'
                    is_combo_end = True
                else:
                    gift_message_content_html = f'{level_img_tag}{fansclub_tag} <span class="user-highlight" data-user-id="{user_id}" data-user-name="{user}">{user}</span> 连击中... {gift_name}x{gift_count} (本次+{partial_count})'
                    is_combo_end = False

                message_data = {
                    'type': 'gift',
                    'user': user,
                    'user_id': user_id,
                    'gift_name': gift_name,
                    'gift_count': partial_count,
                    'gift_price': gift_price,
                    'total_value': partial_value,
                    'content': gift_message_content_html,
                    'combo_count': current_count,
                    'is_combo_end': is_combo_end,
                    'fans_club_level': fans_club_level,
                    'gender': gender,
                    'follower_count': follower_count,
                    'following_count': following_count,
                    'age_range': age_range,
                }
                self.socketio.emit(f'room_{self.live_id}', message_data, room=f'room_{self.live_id}')

                # 连击结束时清理内存
                if gift_msg.repeat_end == 1:
                    del self.monitored_room.combo_gifts[combo_key]

                return

            # ========== 有 group_id 但没有 combo_count：普通礼物 ==========
            # 检查是否已处理过（防止 group_id 重复）
            if combo_key in self.monitored_room.combo_gifts:
                self.log.debug(f"普通礼物重复消息（group_id去重），跳过: combo_key={combo_key}")
                return

            # 标记为已处理
            self.monitored_room.combo_gifts[combo_key] = {
                'last_count': 0,
                'last_gift_count': 0,
                'db_id': None,
            }

            # 连击结束时清理内存
            if gift_msg.repeat_end == 1:
                self.monitored_room.combo_gifts.pop(combo_key, None)

            gift_count = gift_msg.group_count
            total_gift_value = gift_price * gift_count

            self.total_income += total_gift_value
            self.gift_users.add(user)
            self.monitored_room.stats['total_income'] = self.total_income
            self.monitored_room.update_contribution(
                user_id,
                user,
                gift_value=total_gift_value,
                gift_count=gift_count,
                user_avatar=avatar,
                user_level=level,
                gender=gender,
                follower_count=follower_count,
                following_count=following_count,
                age_range=age_range,
                fans_club_level=fans_club_level
            )

            data_service.save_gift_message(
                self.live_id,
                live_session_id=self.current_session_id,
                anchor_name=self.anchor_name,
                user_id=user_id,
                user_name=user,
                user_level=level,
                gift_id=gift_id_str,
                gift_name=gift_name,
                gift_count=gift_count,
                gift_price=gift_price,
                total_value=total_gift_value,
                send_type='normal',
                group_id=group_id_str,
                trace_id=trace_id,
                fans_club_level=fans_club_level
            )

            if self.current_session_id:
                data_service.increment_session_stats(
                    self.current_session_id,
                    income_delta=total_gift_value,
                    gift_count_delta=gift_count
                )

            level_img_tag = f'<img src="/level_img/level_{level}.png" class="user-level-icon" alt="等级">' if level else ''
            fansclub_tag = f'<img src="/fansclub_img/fansclub_{fans_club_level}.png" class="fans-club-icon" alt="粉丝团">' if fans_club_level > 0 else ''
            gift_message_content_html = f'{level_img_tag}{fansclub_tag} <span class="user-highlight" data-user-id="{user_id}" data-user-name="{user}">{user}</span> 赠送了 {gift_count} 个 {gift_name} (价值{gift_price}钻石)'

            message_data = {
                'type': 'gift',
                'user': user,
                'user_id': user_id,
                'gift_name': gift_name,
                'gift_count': gift_count,
                'gift_price': gift_price,
                'total_value': total_gift_value,
                'content': gift_message_content_html,
                'fans_club_level': fans_club_level,
                'gender': gender,
                'follower_count': follower_count,
                'following_count': following_count,
                'age_range': age_range,
            }
            self.socketio.emit(f'room_{self.live_id}', message_data, room=f'room_{self.live_id}')
            self.log.debug(f"发送礼物消息: {user} 送出了 {gift_name}x{gift_count},单价{gift_price},总价值{total_gift_value}")
            return

        # ========== 兜底逻辑：无 group_id 的礼物 ==========
        self.log.debug(f"[礼物消息-兜底] user={user}, gift={gift_name}, 无group_id")
        gift_count = gift_msg.group_count
        total_gift_value = gift_price * gift_count

        self.total_income += total_gift_value
        self.gift_users.add(user)
        self.monitored_room.stats['total_income'] = self.total_income
        self.monitored_room.update_contribution(
            user_id,
            user,
            gift_value=total_gift_value,
            gift_count=gift_count,
            user_avatar=avatar,
            user_level=level,
            gender=gender,
            follower_count=follower_count,
            following_count=following_count,
            age_range=age_range,
            fans_club_level=fans_club_level
        )

        data_service.save_gift_message(
            self.live_id,
            live_session_id=self.current_session_id,
            anchor_name=self.anchor_name,
            user_id=user_id,
            user_name=user,
            user_level=level,
            gift_id=gift_id_str,
            gift_name=gift_name,
            gift_count=gift_count,
            gift_price=gift_price,
            total_value=total_gift_value,
            send_type='normal',
            group_id=group_id_str,
            trace_id=trace_id,
            fans_club_level=fans_club_level
        )

        if self.current_session_id:
            data_service.increment_session_stats(
                self.current_session_id,
                income_delta=total_gift_value,
                gift_count_delta=gift_count
            )

        level_img_tag = f'<img src="/level_img/level_{level}.png" class="user-level-icon" alt="等级">' if level else ''
        fansclub_tag = f'<img src="/fansclub_img/fansclub_{fans_club_level}.png" class="fans-club-icon" alt="粉丝团">' if fans_club_level > 0 else ''
        gift_message_content_html = f'{level_img_tag}{fansclub_tag} <span class="user-highlight" data-user-id="{user_id}" data-user-name="{user}">{user}</span> 赠送了 {gift_count} 个 {gift_name} (价值{gift_price}钻石)'

        message_data = {
            'type': 'gift',
            'user': user,
            'user_id': user_id,
            'gift_name': gift_name,
            'gift_count': gift_count,
            'gift_price': gift_price,
            'total_value': total_gift_value,
            'content': gift_message_content_html,
            'fans_club_level': fans_club_level,
            'gender': gender,
            'follower_count': follower_count,
            'following_count': following_count,
            'age_range': age_range,
        }
        self.socketio.emit(f'room_{self.live_id}', message_data, room=f'room_{self.live_id}')
        self.log.debug(f"发送礼物消息: {user} 送出了 {gift_name}x{gift_count},单价{gift_price},总价值{total_gift_value}")

    def _build_like_dedupe_key(self, like_msg, user_id: str, count: int, total: int):
        """构建点赞消息去重键，优先使用平台消息 ID。"""
        common = getattr(like_msg, 'common', None)
        msg_id = getattr(common, 'msg_id', None) if common else None
        if msg_id:
            return f"msg:{msg_id}"

        detail = getattr(like_msg, 'double_like_detail', None)
        seq_id = getattr(detail, 'seq_id', None) if detail else None
        if seq_id:
            return f"seq:{user_id}:{seq_id}:{count}:{total}"

        return None

    @staticmethod
    def _apply_like_delta(stats: dict, count: int, total: int) -> int:
        """更新 stats 中的点赞基线/累计字段，返回本次「监控期间真实增量」。

        语义：
        - 首条带 total>0 的消息：建立基线，real_delta=0（不入账，避免把开播以来的存量算进监控期）。
        - 后续消息：real_delta = max(0, total - last_seen)，并把 stats[total_like_count]
          刷新为 last_seen - baseline（监控期内的真实涨幅）。
        - 平台没给 total（total<=0）且 count>0 时：兜底用 count 增量。
        """
        if total > 0:
            baseline = stats.get('like_baseline_total')
            if baseline is None:
                stats['like_baseline_total'] = total
                stats['like_last_seen_total'] = total
                stats['total_like_count'] = 0
                return 0
            last_seen = stats.get('like_last_seen_total') or baseline
            real_delta = max(0, total - last_seen)
            new_last_seen = max(last_seen, total)
            stats['like_last_seen_total'] = new_last_seen
            stats['total_like_count'] = max(0, new_last_seen - baseline)
            return real_delta
        if count > 0:
            stats['total_like_count'] = int(stats.get('total_like_count') or 0) + count
            return count
        return 0

    def _handle_like_message(self, like_msg):
        """处理点赞消息：用 platform total 差值入账「监控期间增量」，按 count 归属用户。"""
        user = like_msg.user.nick_name
        level = like_msg.user.pay_grade.level if hasattr(like_msg.user, 'pay_grade') else 0

        raw_id = like_msg.user.id_str if hasattr(like_msg.user, 'id_str') and like_msg.user.id_str else str(like_msg.user.id)
        if raw_id in ['0', '111111']:
            user_id = f"anon_{user}_{level}"
        else:
            user_id = raw_id

        count = int(like_msg.count or 0)
        total = int(like_msg.total or 0)
        dedupe_key = self._build_like_dedupe_key(like_msg, user_id, count, total)
        if dedupe_key and dedupe_key in self.like_message_keys:
            self.log.debug(f"点赞消息已处理过，跳过: key={dedupe_key}")
            return

        if dedupe_key:
            self.like_message_keys.append(dedupe_key)
            if len(self.like_message_keys) > 2000:
                self.like_message_keys = self.like_message_keys[-1000:]

        stats = self.monitored_room.stats
        real_delta = self._apply_like_delta(stats, count, total)

        self.log.info(
            f"【点赞】[{user_id}]{user} msg+{count}, 平台total={total}, "
            f"监控增量+{real_delta}, 累计{stats.get('total_like_count', 0)}"
        )

        fans_club_level = 0
        if hasattr(like_msg.user, 'fans_club') and like_msg.user.fans_club and like_msg.user.fans_club.data:
            fans_club_level = like_msg.user.fans_club.data.level or 0

        # 提取用户头像，避免「只点过赞、没送过礼物」的用户在榜单里没头像
        avatar = None
        if hasattr(like_msg.user, 'avatar_thumb') and like_msg.user.avatar_thumb:
            avatar = like_msg.user.avatar_thumb.url_list_list[0] if like_msg.user.avatar_thumb.url_list_list else None

        data_service = self.monitored_room.manager.data_service

        # 用户贡献榜按消息 count 归属（total 差值无法归属到具体用户）
        if count > 0:
            self.monitored_room.update_contribution(
                user_id,
                user,
                like_count=count,
                user_avatar=avatar,
                user_level=level,
                fans_club_level=fans_club_level
            )

        # session 用监控期间真实增量入账
        if self.current_session_id and real_delta > 0:
            data_service.increment_session_stats(
                self.current_session_id,
                like_count_delta=real_delta
            )

        if real_delta <= 0 and count <= 0:
            return

        current_session_data = None
        if self.current_session_id:
            session = data_service.get_current_live_session(self.live_id)
            if session:
                current_session_data = {
                    'id': session.id,
                    'start_time': session.start_time.isoformat() if session.start_time else None,
                    'end_time': session.end_time.isoformat() if session.end_time else None,
                    'status': session.status,
                    'total_income': session.total_income,
                    'total_gift_count': session.total_gift_count,
                    'total_chat_count': session.total_chat_count,
                    'total_like_count': session.total_like_count,
                    'peak_viewer_count': session.peak_viewer_count
                }

        self.socketio.emit(f'room_{self.live_id}_stats', {
            'current_user_count': stats['current_user_count'],
            'total_user_count': stats['total_user_count'],
            'total_like_count': stats.get('total_like_count', 0),
            'total_income': stats['total_income'],
            'contributor_count': stats['contributor_count'],
            'current_session': current_session_data,
            'like_rank_list': self.monitored_room.get_like_rank(100),
        }, room=f'room_{self.live_id}')

    def _handle_stats_message(self, stats_msg):
        """处理统计消息"""
        current = stats_msg.total
        total = stats_msg.total_pv_for_anchor

        # 更新统计信息
        if current is not None and current >= 0:
            self.monitored_room.stats['current_user_count'] = current
            self.monitored_room.last_stats['current_user_count'] = current
            self.current_viewer_count = current

            # 更新峰值观看人数
            if current > self.max_viewer_count:
                self.max_viewer_count = current
                # 同步更新到数据库的场次记录
                if self.current_session_id:
                    data_service = self.monitored_room.manager.data_service
                    data_service.update_session_stats(
                        self.current_session_id,
                        peak_viewer_count=self.max_viewer_count
                    )

        if total is not None and total != '':
            # 将格式化的数字（如'46.8万'）转换为整数
            total_numeric = parse_formatted_number(total)
            self.monitored_room.stats['total_user_count'] = total_numeric
            self.monitored_room.last_stats['total_user_count'] = total_numeric

        # 只统计当前场次送过礼物的用户数量（score > 0）
        self.monitored_room.stats['contributor_count'] = sum(
            1 for v in self.monitored_room.user_contributions.values()
            if v['score'] > 0
        )

        # 获取贡献榜
        rank_list = self.monitored_room.get_contribution_rank(100)

        # 获取当前场次数据用于实时推送
        current_session_data = None
        if self.current_session_id:
            data_service = self.monitored_room.manager.data_service
            # 从数据库重新获取最新场次数据
            session = data_service.get_current_live_session(self.live_id)
            if session:
                current_session_data = {
                    'id': session.id,
                    'start_time': session.start_time.isoformat() if session.start_time else None,
                    'end_time': session.end_time.isoformat() if session.end_time else None,
                    'status': session.status,
                    'total_income': session.total_income,
                    'total_gift_count': session.total_gift_count,
                    'total_chat_count': session.total_chat_count,
                    'total_like_count': session.total_like_count,
                    'peak_viewer_count': session.peak_viewer_count
                }

        # 获取最新房间状态
        room = data_service.get_live_room(self.live_id)
        room_status = room.status if room else None
        room_error_message = room.error_message if room else None

        # 通过Socket.IO推送到前端
        self.socketio.emit(f'room_{self.live_id}_stats', {
            'room_status': room_status,  # 新增：监控状态
            'room_error_message': room_error_message,
            'current_user_count': self.monitored_room.stats['current_user_count'],
            'total_user_count': self.monitored_room.stats['total_user_count'],
            'total_like_count': self.monitored_room.stats.get('total_like_count', 0),
            'total_income': self.monitored_room.stats['total_income'],
            'contributor_count': self.monitored_room.stats['contributor_count'],
            'contributor_info': rank_list,
            'current_session': current_session_data,
            'like_rank_list': self.monitored_room.get_like_rank(100),
        }, room=f'room_{self.live_id}')
        self.log.debug(f"发送直播间统计: 当前{current}, 累计{total}, 总收入{self.total_income}, 贡献者数{len(self.monitored_room.user_contributions)}")

    def _end_current_session(self, reason: str = "连接关闭"):
        """安全地结束当前直播场次（如果存在）"""
        if self.current_session_id:
            data_service = self.monitored_room.manager.data_service
            session_id = self.current_session_id
            success = data_service.end_live_session(
                session_id,
                peak_viewer_count=self.max_viewer_count
            )
            if success:
                self.log.info(f"结束直播场次: session_id={session_id}, 峰值观看人数={self.max_viewer_count}, 原因={reason}")

                # 获取刚结束的场次数据，推送给前端
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
                            'total_like_count': session.total_like_count,
                            'peak_viewer_count': session.peak_viewer_count
                        }

                        # 获取最新房间状态
                        room = data_service.get_live_room(self.live_id)
                        room_status = room.status if room else None
                        room_error_message = room.error_message if room else None

                        # 推送状态更新给前端
                        self.socketio.emit(f'room_{self.live_id}_stats', {
                            'room_status': room_status,  # 新增：监控状态
                            'room_error_message': room_error_message,
                            'current_user_count': self.monitored_room.stats['current_user_count'],
                            'total_user_count': self.monitored_room.stats['total_user_count'],
                            'total_like_count': self.monitored_room.stats.get('total_like_count', 0),
                            'total_income': self.monitored_room.stats['total_income'],
                            'contributor_count': self.monitored_room.stats['contributor_count'],
                            'contributor_info': [],
                            'current_session': current_session_data,
                            'like_rank_list': [],
                        }, room=f'room_{self.live_id}')
                        self.log.info(f"推送直播结束状态更新: session_id={session.id}, status={session.status}")
                finally:
                    db_session.close()
            else:
                self.log.warning(f"结束直播场次失败: session_id={session_id}")
            self.current_session_id = None
            return True
        return False

    def _wsOnOpen(self, ws):
        """WebSocket连接建立"""
        self.ws_open_time = time.time()
        self._fetcher.record_websocket_open()
        threading.Thread(target=self._fetcher._sendHeartbeat, daemon=True).start()
        self.log.success("WebSocket连接已建立")

        # 重置计数器（连接成功说明开播了）
        self.monitored_room.reset_offline_counter()
        self.monitored_room.reconnect_count = 0

        # 获取并更新主播信息
        try:
            anchor_info = self._fetcher.anchor_info
            anchor_name = anchor_info.get('anchor_name')
            if anchor_name or anchor_info.get('anchor_id'):
                data_service = self.monitored_room.manager.data_service
                data_service.update_live_room(
                    self.live_id,
                    anchor_name=anchor_name,
                    anchor_id=anchor_info.get('anchor_id')
                )

                # 更新 MonitoredRoom 的 anchor_name
                self.monitored_room.anchor_name = anchor_name
                self.anchor_name = anchor_name

                # 获取到主播名字后，更新日志上下文：显示为 "主播名(live_id)" 或 "(live_id)"
                room_display = f"{anchor_name}({self.live_id})" if anchor_name else f"({self.live_id})"
                self.log = get_logger("handlers", room_display)

                # 同时更新核心抓取器的日志上下文
                self._fetcher.update_log_context(anchor_name)

                self.log.info(f"更新主播信息: {anchor_info}")
        except Exception as e:
            self.log.warning(f"获取主播信息失败: {e}")

        # 检查或创建直播场次
        data_service = self.monitored_room.manager.data_service
        current_session = data_service.get_current_live_session(self.live_id)

        if current_session:
            # 已有进行中的场次，继续使用
            self.current_session_id = current_session.id
            self.anchor_name = current_session.anchor_name
            self.log.info(f"继续现有直播场次: session_id={self.current_session_id}")

            # 只有在本地缓存为空时才从数据库加载（避免覆盖已存在的实时数据）
            if not self.monitored_room.user_contributions:
                self.log.info("本地贡献榜为空，从数据库加载")
                session_contributors = data_service.get_session_contributors(self.live_id, current_session.id, limit=1000)
                for contributor in session_contributors:
                    # get_session_contributors 返回的是 Dict，使用字典访问
                    self.monitored_room.user_contributions[contributor['user_id']] = {
                        'user_name': contributor['nickname'],
                        'score': contributor['contribution_value'],
                        'avatar': contributor['user_avatar'],
                        'gift_count': contributor['gift_count'],
                        'like_count': contributor.get('like_count', 0),
                        'user_level': contributor.get('user_level', 0),
                        'fans_club_level': contributor.get('fans_club_level', 0)
                    }
                self.log.info(f"从数据库加载了 {len(session_contributors)} 个贡献者到本地缓存")
            else:
                self.log.info(f"本地已有 {len(self.monitored_room.user_contributions)} 个贡献者，跳过数据库加载")
        else:
            # 创建新的直播场次
            # 清空本地贡献榜缓存（新场次）
            old_count = len(self.monitored_room.user_contributions)
            self.monitored_room.user_contributions.clear()
            self.log.info(f"新直播场次：清空本地贡献榜缓存（清除了{old_count}个用户）")

            # 重置本场点赞基线，避免遗留上一场的累计值
            self.monitored_room.stats['total_like_count'] = 0
            self.monitored_room.stats['like_baseline_total'] = None
            self.monitored_room.stats['like_last_seen_total'] = None

            new_session = data_service.create_live_session(
                self.live_id,
                anchor_name=self.anchor_name,
                status='live'
            )
            if new_session:
                self.current_session_id = new_session.id
                self.current_viewer_count = 0
                self.max_viewer_count = 0
                self.log.info(f"创建新直播场次: session_id={self.current_session_id}")

        # 更新直播间状态
        data_service.update_live_room_status(self.live_id, 'monitoring')

    def _wsOnError(self, ws, error):
        """WebSocket错误"""
        error_str = str(error)
        # 502 错误是抖音服务器端问题，降低日志级别并给出更友好的提示
        if "502" in error_str or "Bad Gateway" in error_str:
            self.log.warning(f"抖音服务器暂时无响应 (502)，将在重连间隔后自动重试")
        else:
            self.log.error(f"WebSocket错误: {error}")

    def _handle_control_message(self, control_msg):
        """处理控制消息（直播状态变化）"""
        if control_msg.status == 3:
            # 直播已结束
            self.log.warning("检测到直播间已结束（收到服务器通知）")

            # 结束当前直播场次
            self._end_current_session(reason="收到服务器直播结束通知")

            # 更新直播间状态
            data_service = self.monitored_room.manager.data_service
            data_service.update_live_room_status(self.live_id, 'stopped')

            # 停止监控
            self.stop()

    def _wsOnClose(self, ws, *args):
        """WebSocket连接关闭"""
        self.log.warning("WebSocket连接已关闭")
        # 注意：不在这里增加计数器，因为可能是临时断线
        # 真正的结束逻辑在 MonitoredRoom 的 _monitor_loop 中处理（检测到未开播时）

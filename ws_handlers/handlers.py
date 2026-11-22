"""
WebSocket处理器
扩展核心抓取类，添加数据库存储和Socket.IO推送
"""
import json
import math
import os
import threading
import time
from typing import TYPE_CHECKING

import websocket

if TYPE_CHECKING:
    from services.room_manager import MonitoredRoom

from crawler.fetcher import decompress_push_frame_payload
from protobuf.douyin import (
    BindingGiftMessage,
    ChatMessage,
    ControlMessage,
    EmojiChatMessage,
    GiftMessage,
    LikeMessage,
    PushFrame,
    Response,
    RoomUserSeqMessage,
)
from utils.emoji_helper import extract_chat_content_and_emojis, extract_emoji_chat_message
from utils.logger import get_logger
from utils.gift_packet_sampler import gift_packet_sampler
from utils import gift_registry
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
        """恢复活跃场次，去重缓存随房间保留到下次真正开播。"""
        try:
            with self.monitored_room.data_lock:
                current_session = self.monitored_room.manager.data_service.get_current_live_session(self.live_id)
                if current_session:
                    self._restore_session_data(current_session)
        except Exception as e:
            self.log.error(f"预加载数据失败: {e}")

    def _restore_session_data(self, current_session):
        from models.database import GiftMessage as StoredGiftMessage

        data_service = self.monitored_room.manager.data_service
        with self.monitored_room.data_lock:
            self.current_session_id = current_session.id
            self.anchor_name = current_session.anchor_name
            self.total_income = float(current_session.total_income or 0)
            self.max_viewer_count = int(current_session.peak_viewer_count or 0)
            self.monitored_room.stats['total_income'] = self.total_income
            # 送礼身份以 user_id 判定，不能把同名用户或其它场次用户混进来。
            with data_service.get_session() as db:
                gift_users = db.query(StoredGiftMessage.user_id).filter(
                    StoredGiftMessage.live_id == self.live_id,
                    StoredGiftMessage.live_session_id == current_session.id,
                ).distinct().all()
                self.monitored_room.gift_user_ids.update(row.user_id for row in gift_users)
            if not self.monitored_room.user_contributions:
                contributors = data_service.get_session_contributors(self.live_id, current_session.id, limit=1000)
                for contributor in contributors:
                    self.monitored_room.user_contributions[contributor['user_id']] = {
                        'user_name': contributor['nickname'],
                        'score': contributor['contribution_value'],
                        'avatar': contributor['user_avatar'],
                        'gift_count': contributor['gift_count'],
                        'like_count': contributor.get('like_count', 0),
                        'user_level': contributor.get('user_level', 0),
                        'fans_club_level': contributor.get('fans_club_level', 0),
                    }
            self.monitored_room.stats['contributor_count'] = len(self.monitored_room.gift_user_ids)

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
            response = Response().parse(decompress_push_frame_payload(package))

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
                        elif method == 'WebcastEmojiChatMessage':
                            self._handle_emoji_chat_message(EmojiChatMessage().parse(msg.payload))
                        elif method == 'WebcastGiftMessage':
                            self._handle_gift_message(GiftMessage().parse(msg.payload),
                                                      raw_payload=msg.payload, source_method=method)
                        elif method == 'WebcastBindingGiftMessage':
                            self._handle_binding_gift_message(BindingGiftMessage().parse(msg.payload),
                                                              raw_payload=msg.payload, source_method=method)
                        elif method == 'WebcastLikeMessage':
                            self._handle_like_message(LikeMessage().parse(msg.payload))
                        elif method == 'WebcastRoomUserSeqMessage':
                            self._handle_stats_message(RoomUserSeqMessage().parse(msg.payload))
                        elif method == 'WebcastControlMessage':
                            self._handle_control_message(ControlMessage().parse(msg.payload))
                        else:
                            gift_packet_sampler.sample_unhandled_method(method, msg.payload)
                    except Exception as e:
                        self.log.error(f"处理消息出错 [method={method}]: {e}")
        except Exception as e:
            self.log.error(f"解析消息出错: {e}")

    @staticmethod
    def _message_user_fields(message_user):
        """共享纯文本身份字段；由前端文本节点负责显示。"""
        user = message_user.nick_name
        level = getattr(getattr(message_user, 'pay_grade', None), 'level', 0) or 0
        raw_id = getattr(message_user, 'id_str', None) or str(message_user.id)
        user_id = f"anon_{user}_{level}" if raw_id in ('0', '111111') else raw_id
        club = getattr(getattr(message_user, 'fans_club', None), 'data', None)
        follow = getattr(message_user, 'follow_info', None)
        raw_display_id = (getattr(message_user, 'display_id', None) or '').strip() or (getattr(message_user, 'special_id', None) or '').strip()
        if not raw_display_id:
            short_id = getattr(message_user, 'short_id', 0) or 0
            if short_id and str(short_id) != '0':
                raw_display_id = str(short_id)
        return {
            'user': user,
            'user_id': user_id,
            'display_id': raw_display_id or None,
            'user_level': level,
            'fans_club_level': getattr(club, 'level', 0) or 0,
            'gender': getattr(message_user, 'gender', 0) or 0,
            'follower_count': getattr(follow, 'follower_count', 0) or 0,
            'following_count': getattr(follow, 'following_count', 0) or 0,
            'age_range': getattr(message_user, 'age_range', 0) or 0,
        }

    def _persist_and_emit_chat(self, user_proto, content: str, emoji_list=None):
        """保存弹幕或表情消息并推送结构化文本，不在服务端拼接 HTML。"""
        self.last_chat_time = time.time()
        fields = self._message_user_fields(user_proto)
        data_service = self.monitored_room.manager.data_service
        with self.monitored_room.data_lock:
            is_gift_user = fields['user_id'] in self.monitored_room.gift_user_ids
            saved = data_service.save_chat_message(
                self.live_id,
                live_session_id=self.current_session_id,
                anchor_name=self.anchor_name,
                user_id=fields['user_id'],
                user_name=fields['user'],
                user_level=fields['user_level'],
                content=content,
                emoji_list=emoji_list,
                is_gift_user=is_gift_user,
                fans_club_level=fields['fans_club_level'],
            )
            if saved is None:
                return
            if self.current_session_id:
                data_service.increment_session_stats(self.current_session_id, chat_count_delta=1)
            self.monitored_room.update_contribution(
                fields['user_id'], fields['user'], chat_count=1,
                display_id=fields['display_id'],
                **{key: fields[key] for key in ('user_level', 'fans_club_level', 'gender',
                                               'follower_count', 'following_count', 'age_range')},
            )
        self.socketio.emit(f'room_{self.live_id}', {
            'type': 'chat',
            **fields,
            'text': content,
            'emoji_list': emoji_list,
            'is_gift_user': is_gift_user,
        }, room=f'room_{self.live_id}')
        self._emit_room_stats_throttled()

    def _handle_chat_message(self, chat_msg):
        """处理普通及富文本弹幕消息。"""
        content, emoji_list = extract_chat_content_and_emojis(chat_msg)
        self._persist_and_emit_chat(chat_msg.user, content, emoji_list=emoji_list)

    def _handle_emoji_chat_message(self, emoji_msg):
        """处理独立贴纸/大表情弹幕消息。"""
        content, emoji_list = extract_emoji_chat_message(emoji_msg)
        self._persist_and_emit_chat(emoji_msg.user, content, emoji_list=emoji_list)

    def _cleanup_combo_gifts(self, now):
        """清理超过十分钟无新增量的连击，保留尾包状态以拒绝乱序重放。"""
        with self.monitored_room.data_lock:
            expired = [key for key, state in self.monitored_room.combo_gifts.items()
                       if now - state.get('updated_at', 0) > 600]
            for key in expired:
                del self.monitored_room.combo_gifts[key]

    def _handle_binding_gift_message(self, binding_msg, raw_payload=b'', source_method='WebcastBindingGiftMessage'):
        """绑定消息沿用礼物记账入口，缺失的内部消息 ID 从外层补齐。"""
        inner = getattr(binding_msg, 'msg', None)
        if inner is None or not (getattr(getattr(inner, 'gift', None), 'name', '')
                                 or getattr(inner, 'gift_id', 0)):
            return
        outer_common = getattr(binding_msg, 'common', None)
        if (not getattr(getattr(inner, 'common', None), 'msg_id', 0)
                and getattr(outer_common, 'msg_id', 0)):
            inner.common = outer_common
        self._handle_gift_message(inner, raw_payload=raw_payload, source_method=source_method)

    @staticmethod
    def _resolve_gift_unit_price(gift_struct, tray_info=None, extra_json_blobs=()) -> float:
        """保留原有只返回价格的调用接口。"""
        return WebDouyinLiveFetcher._resolve_gift_unit_price_with_source(
            gift_struct, tray_info, extra_json_blobs,
        )[0]

    @staticmethod
    def _get_effective_gift_overrides() -> tuple[dict, str]:
        """返回实时合并的价格覆盖和原始环境配置，供计价与来源判断共用。"""
        # 支持测试和运行进程热更新环境配置，无需重新导入 config 模块。
        raw_overrides = os.getenv('GIFT_PRICE_OVERRIDES', '')
        overrides = (config.parse_gift_price_overrides(raw_overrides) if raw_overrides.strip()
                     else config.GIFT_PRICE_OVERRIDES)
        return overrides, raw_overrides

    @staticmethod
    def _resolve_gift_unit_price_with_source(gift_struct, tray_info=None, extra_json_blobs=()) -> tuple[float, str]:
        """按配置、进阶等级、扩展 JSON、盒内礼物、基础钻石数的顺序解析单价。"""
        gift_name = (getattr(gift_struct, 'name', '') or '').strip()
        overrides, raw_overrides = WebDouyinLiveFetcher._get_effective_gift_overrides()
        if gift_name in overrides:
            source = 'builtin_override'
            if raw_overrides.strip():
                try:
                    configured = json.loads(raw_overrides)
                except (ValueError, TypeError):
                    configured = None
                if isinstance(configured, dict):
                    for key, value in configured.items():
                        if (isinstance(key, str) and key.strip() == gift_name
                                and isinstance(value, (int, float)) and not isinstance(value, bool)
                                and value >= 0 and math.isfinite(value)):
                            source = 'env_override'
                            break
            elif (gift_name not in config.SPECIAL_GIFT_DIAMOND_OVERRIDES
                  or overrides[gift_name] != config.SPECIAL_GIFT_DIAMOND_OVERRIDES[gift_name]):
                source = 'env_override'
            return float(overrides[gift_name]), source
        base_diamond = float(getattr(gift_struct, 'diamond_count', 0) or 0)
        buff_level = int(getattr(tray_info, 'buff_level', 0) or 0)
        if buff_level > 0:
            for buff in getattr(gift_struct, 'gift_buff_infos', None) or []:
                price = getattr(buff, 'buff_diamond_count', 0) or 0
                if getattr(buff, 'buff_level', 0) == buff_level and price > 0:
                    return float(price), 'buff_level'
        price_keys = ('buff_diamond_count', 'real_diamond_count', 'total_diamond_count', 'diamond_count')
        for blob in extra_json_blobs:
            if not isinstance(blob, str) or not blob.strip():
                continue
            try:
                extra = json.loads(blob)
            except (ValueError, TypeError):
                continue
            if isinstance(extra, dict):
                for key in price_keys:
                    for candidate in (extra, extra.get('data'), extra.get('gift')):
                        if not isinstance(candidate, dict):
                            continue
                        value = candidate.get(key)
                        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                                and value > base_diamond and math.isfinite(value)):
                            return float(value), 'extra_json'
            if isinstance(extra, (dict, list)):
                diy_resolved = gift_registry.resolve_diy_composite_gift(
                    extra, outer_name=gift_name, overrides=overrides,
                )
                if diy_resolved is not None:
                    return diy_resolved[0], 'diy_gift_registry'
        return base_diamond, 'base_diamond'

    def _handle_gift_message(self, gift_msg, raw_payload=b'', source_method='WebcastGiftMessage'):
        """每个成功落库的礼物增量只记账一次，重连续用房间级缓存。"""
        self.last_gift_time = time.time()
        fields = self._message_user_fields(gift_msg.user)
        outer_name = getattr(getattr(gift_msg, 'gift', None), 'name', '') or ''
        series_items = [s for s in (getattr(gift_msg, 'series_gift_data', None) or [])
                        if getattr(getattr(s, 'gift_struct', None), 'name', '')]
        gift_name = outer_name
        extra_json_blobs = (getattr(gift_msg, 'diy_item_info', ''),
                            getattr(gift_msg, 'interact_gift_info', ''),
                            getattr(gift_msg, 'gift_play_param', ''))
        gift_price, price_source = self._resolve_gift_unit_price_with_source(
            gift_msg.gift, tray_info=getattr(gift_msg, 'tray_info', None),
            extra_json_blobs=extra_json_blobs,
        ) if not series_items else (0.0, 'series_children')
        if price_source == 'diy_gift_registry':
            overrides, _ = self._get_effective_gift_overrides()
            for blob in extra_json_blobs:
                diy_resolved = gift_registry.resolve_diy_composite_gift(
                    blob, outer_name=gift_name, overrides=overrides,
                )
                if diy_resolved is not None:
                    if diy_resolved[1]:
                        # 仅丰富落库与推送名称，采样仍使用未改动的原始礼物消息。
                        gift_name = diy_resolved[1]
                    break
        gift_id = str(getattr(gift_msg, 'gift_id', '') or '')
        group_id = str(gift_msg.group_id) if getattr(gift_msg, 'group_id', 0) else None
        if group_id == '0':
            group_id = None
        per_combo_count = ((getattr(gift_msg, 'group_count', 0) or 0)
                           or (getattr(gift_msg, 'repeat_count', 0) or 0) or 1)
        current_count = int(getattr(gift_msg, 'combo_count', 0) or 0)
        is_combo = bool(group_id and current_count > 0)
        trace_id = getattr(gift_msg, 'trace_id', None) or None
        msg_id = getattr(getattr(gift_msg, 'common', None), 'msg_id', None)
        # 缺 trace_id 的平台消息也使用数据库唯一列，兼容进程重启后的重放。
        persisted_key = str(trace_id) if trace_id else (
            f"msg:{self.live_id}:{self.current_session_id}:{msg_id}" if msg_id else None
        )
        dedupe_key = f"trace:{trace_id}" if trace_id else persisted_key
        avatar_urls = getattr(getattr(gift_msg.user, 'avatar_thumb', None), 'url_list_list', None)
        avatar = avatar_urls[0] if avatar_urls else None
        data_service = self.monitored_room.manager.data_service

        with self.monitored_room.data_lock:
            is_remembered_replay = bool(dedupe_key and dedupe_key in self.monitored_room.trace_dedupe_set)
        if not is_remembered_replay:
            try:
                gift_packet_sampler.sample_gift_message(
                    method=source_method, raw_payload=raw_payload, gift_msg=gift_msg,
                    resolved_price=gift_price, price_source=price_source,
                )
            except Exception:
                self.log.debug('礼物诊断采样失败，继续正常记账')

        if series_items:
            payloads = []
            item_count = max(1, int(per_combo_count))
            with self.monitored_room.data_lock:
                if dedupe_key and dedupe_key in self.monitored_room.trace_dedupe_set:
                    return
                completed = True
                for idx, s_item in enumerate(series_items):
                    item_trace = f'{persisted_key}:series:{idx}' if persisted_key else None
                    item_dedupe_key = f'trace:{item_trace}' if item_trace else None
                    if item_dedupe_key and item_dedupe_key in self.monitored_room.trace_dedupe_set:
                        continue
                    inner_struct = s_item.gift_struct
                    inner_name = inner_struct.name
                    display_name = (f'{outer_name}({inner_name})'
                                    if outer_name and outer_name != inner_name else inner_name)
                    item_extra_json_blobs = (getattr(s_item, 'diy_item_info', ''),
                                             getattr(gift_msg, 'interact_gift_info', ''),
                                             getattr(gift_msg, 'gift_play_param', ''))
                    item_price, item_source = self._resolve_gift_unit_price_with_source(
                        inner_struct, tray_info=None,
                        extra_json_blobs=item_extra_json_blobs,
                    )
                    if item_source == 'diy_gift_registry':
                        overrides, _ = self._get_effective_gift_overrides()
                        for blob in item_extra_json_blobs:
                            diy_resolved = gift_registry.resolve_diy_composite_gift(
                                blob, outer_name=outer_name or inner_name, overrides=overrides,
                            )
                            if diy_resolved is not None:
                                if diy_resolved[1]:
                                    display_name = diy_resolved[1]
                                break
                    item_value = item_price * item_count
                    saved = data_service.save_gift_message(
                        self.live_id, live_session_id=self.current_session_id, anchor_name=self.anchor_name,
                        user_id=fields['user_id'], user_name=fields['user'], user_level=fields['user_level'],
                        gift_id=str(getattr(inner_struct, 'id', 0) or ''), gift_name=display_name,
                        gift_count=item_count, gift_price=item_price, total_value=item_value,
                        send_type='normal', group_id=group_id, trace_id=item_trace,
                        fans_club_level=fields['fans_club_level'],
                    )
                    if saved is None:
                        completed = False
                        continue
                    if item_dedupe_key:
                        self.monitored_room.remember_trace(item_dedupe_key)
                    self.total_income = float(self.monitored_room.stats.get('total_income') or 0) + item_value
                    self.monitored_room.stats['total_income'] = self.total_income
                    self.monitored_room.gift_user_ids.add(fields['user_id'])
                    self.monitored_room.stats['contributor_count'] = len(self.monitored_room.gift_user_ids)
                    try:
                        self.monitored_room.update_contribution(
                            fields['user_id'], fields['user'], gift_value=item_value,
                            display_id=fields['display_id'],
                            gift_count=item_count, user_avatar=avatar,
                            **{key: fields[key] for key in ('user_level', 'fans_club_level', 'gender',
                                                           'follower_count', 'following_count', 'age_range')},
                        )
                    except Exception:
                        self.log.error("礼物明细已保存，贡献统计写入失败")
                    finally:
                        if self.current_session_id:
                            data_service.increment_session_stats(
                                self.current_session_id, income_delta=item_value, gift_count_delta=item_count,
                            )
                    payloads.append({
                        'type': 'gift', **fields, 'gift_name': display_name, 'gift_count': item_count,
                        'gift_price': item_price, 'total_value': item_value, 'is_combo': False,
                    })
                # 整包完成才屏蔽外层重放，部分保存失败时仍可重试未落库的子礼物。
                if completed and dedupe_key:
                    self.monitored_room.remember_trace(dedupe_key)
            for payload in payloads:
                self.socketio.emit(f'room_{self.live_id}', payload, room=f'room_{self.live_id}')
            if payloads:
                self._emit_room_stats_throttled()
            return

        with self.monitored_room.data_lock:
            self._cleanup_combo_gifts(self.last_gift_time)
            if dedupe_key and dedupe_key in self.monitored_room.trace_dedupe_set:
                return
            combo_key = (group_id, fields['user_id'], gift_id)
            previous = self.monitored_room.combo_gifts.get(combo_key, {}) if is_combo else {}
            if is_combo and current_count <= previous.get('last_count', 0):
                return
            gift_count = current_count * per_combo_count if is_combo else per_combo_count
            partial_count = gift_count - previous.get('last_gift_count', 0) if is_combo else gift_count
            if partial_count <= 0:
                return
            partial_value = gift_price * partial_count
            total_gift_value = gift_price * gift_count
            # 连击采用不可变增量明细，SUM 与每次记账一致，唯一 trace 列保护重放。
            saved = data_service.save_gift_message(
                self.live_id, live_session_id=self.current_session_id, anchor_name=self.anchor_name,
                user_id=fields['user_id'], user_name=fields['user'], user_level=fields['user_level'],
                gift_id=gift_id, gift_name=gift_name, gift_count=partial_count,
                gift_price=gift_price, total_value=partial_value,
                send_type='combo' if is_combo else 'normal', group_id=group_id,
                trace_id=persisted_key, fans_club_level=fields['fans_club_level'],
            )
            if saved is None:
                return
            if dedupe_key:
                self.monitored_room.remember_trace(dedupe_key)
            if is_combo:
                self.monitored_room.combo_gifts[combo_key] = {
                    'last_count': current_count, 'last_gift_count': gift_count,
                    'updated_at': self.last_gift_time,
                }
            self.total_income = float(self.monitored_room.stats.get('total_income') or 0) + partial_value
            self.monitored_room.stats['total_income'] = self.total_income
            self.monitored_room.gift_user_ids.add(fields['user_id'])
            self.monitored_room.stats['contributor_count'] = len(self.monitored_room.gift_user_ids)
            try:
                self.monitored_room.update_contribution(
                    fields['user_id'], fields['user'], gift_value=partial_value,
                    display_id=fields['display_id'],
                    gift_count=partial_count, user_avatar=avatar,
                    **{key: fields[key] for key in ('user_level', 'fans_club_level', 'gender',
                                                   'follower_count', 'following_count', 'age_range')},
                )
            except Exception:
                # 明细已提交；贡献失败不得阻断场次记账，也不能盲目重试未知提交状态。
                self.log.error("礼物明细已保存，贡献统计写入失败")
            finally:
                if self.current_session_id:
                    data_service.increment_session_stats(
                        self.current_session_id, income_delta=partial_value, gift_count_delta=partial_count,
                    )
        payload = {
            'type': 'gift', **fields, 'gift_name': gift_name, 'gift_count': partial_count,
            'gift_price': gift_price, 'total_value': partial_value, 'is_combo': is_combo,
        }
        if is_combo:
            payload.update(
                partial_count=partial_count, total_gift_count=gift_count,
                total_combo_value=total_gift_value, combo_count=current_count,
                is_combo_end=(getattr(gift_msg, 'repeat_end', 0) == 1),
            )
        self.socketio.emit(f'room_{self.live_id}', payload, room=f'room_{self.live_id}')
        self._emit_room_stats_throttled()

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
        """平台 total 差值入账监控期增量，count 归属具体用户。"""
        fields = self._message_user_fields(like_msg.user)
        count = int(like_msg.count or 0)
        total = int(like_msg.total or 0)
        dedupe_key = self._build_like_dedupe_key(like_msg, fields['user_id'], count, total)
        with self.monitored_room.data_lock:
            if dedupe_key and not self.monitored_room.remember_like(dedupe_key):
                return
            real_delta = self._apply_like_delta(self.monitored_room.stats, count, total)
            self.log.debug(f"点赞 msg+{count}, 平台total={total}, 监控增量+{real_delta}")
            avatar_urls = getattr(getattr(like_msg.user, 'avatar_thumb', None), 'url_list_list', None)
            if count > 0:
                self.monitored_room.update_contribution(
                    fields['user_id'], fields['user'], like_count=count,
                    display_id=fields['display_id'],
                    user_avatar=avatar_urls[0] if avatar_urls else None,
                    user_level=fields['user_level'], fans_club_level=fields['fans_club_level'],
                )
            if self.current_session_id and real_delta > 0:
                self.monitored_room.manager.data_service.increment_session_stats(
                    self.current_session_id, like_count_delta=real_delta,
                )
        if real_delta > 0 or count > 0:
            self._emit_room_stats_throttled()

    def _emit_room_stats_throttled(self):
        """房间级一秒节流；快照、数据库读取与发送按房间顺序串行完成。"""
        if self.socketio is None:
            return False
        with self.monitored_room.data_lock:
            now = time.monotonic()
            last_emit = getattr(self.monitored_room, '_last_stats_emit_time', None)
            if last_emit is not None and now - last_emit < 1:
                return False
            self.monitored_room._last_stats_emit_time = now
            self.monitored_room.stats['contributor_count'] = len(self.monitored_room.gift_user_ids)
            stats = self.monitored_room.stats.copy()
            rank_list = self.monitored_room.get_contribution_rank(100)
            like_rank_list = self.monitored_room.get_like_rank(100)
            data_service = self.monitored_room.manager.data_service
            current_session_data = None
            if self.current_session_id:
                session = data_service.get_current_live_session(self.live_id)
                if session and session.id == self.current_session_id:
                    current_session_data = {
                        'id': session.id,
                        'start_time': session.start_time.isoformat() if session.start_time else None,
                        'end_time': session.end_time.isoformat() if session.end_time else None,
                        'status': session.status,
                        'total_income': float(session.total_income or 0),
                        'total_gift_count': session.total_gift_count,
                        'total_chat_count': session.total_chat_count,
                        'total_like_count': session.total_like_count,
                        'peak_viewer_count': session.peak_viewer_count,
                    }
            room = data_service.get_live_room(self.live_id)
            self.socketio.emit(f'room_{self.live_id}_stats', {
                'room_status': room.status if room else None,
                'room_error_message': getattr(room, 'error_message', None) if room else None,
                'current_user_count': stats['current_user_count'],
                'total_user_count': stats['total_user_count'],
                'total_like_count': stats.get('total_like_count', 0),
                'total_income': stats['total_income'],
                'contributor_count': stats['contributor_count'],
                'contributor_info': rank_list,
                'current_session': current_session_data,
                'like_rank_list': like_rank_list,
            }, room=f'room_{self.live_id}')
            return True

    def _handle_stats_message(self, stats_msg):
        """更新观众数据与单调峰值，再使用统一的统计推送。"""
        data_service = self.monitored_room.manager.data_service
        current = stats_msg.total
        total = stats_msg.total_pv_for_anchor
        with self.monitored_room.data_lock:
            if current is not None and current >= 0:
                self.monitored_room.stats['current_user_count'] = current
                self.monitored_room.last_stats['current_user_count'] = current
                self.current_viewer_count = current
                if current > self.max_viewer_count:
                    self.max_viewer_count = current
                    if self.current_session_id:
                        data_service.update_session_stats(self.current_session_id, peak_viewer_count=current)
            if total is not None and total != '':
                total_numeric = parse_formatted_number(total)
                self.monitored_room.stats['total_user_count'] = total_numeric
                self.monitored_room.last_stats['total_user_count'] = total_numeric
        self._emit_room_stats_throttled()

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

                        with self.monitored_room.data_lock:
                            stats = self.monitored_room.stats.copy()

                        # 推送状态更新给前端
                        self.socketio.emit(f'room_{self.live_id}_stats', {
                            'room_status': room_status,  # 新增：监控状态
                            'room_error_message': room_error_message,
                            'current_user_count': stats['current_user_count'],
                            'total_user_count': stats['total_user_count'],
                            'total_like_count': stats.get('total_like_count', 0),
                            'total_income': stats['total_income'],
                            'contributor_count': stats['contributor_count'],
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

        # 查找、建场次与重置在同一个房间锁内完成，重连不能清空去重缓存。
        data_service = self.monitored_room.manager.data_service
        with self.monitored_room.data_lock:
            current_session = data_service.get_current_live_session(self.live_id)
            if current_session:
                self._restore_session_data(current_session)
            else:
                new_session = data_service.create_live_session(
                    self.live_id, anchor_name=self.anchor_name, status='live',
                )
                if new_session:
                    if getattr(new_session, '_was_created', True):
                        self.monitored_room.reset_dedupe()
                        self.monitored_room.user_contributions.clear()
                        self.monitored_room.stats.update(
                            current_user_count=0, total_user_count=0, total_like_count=0,
                            like_baseline_total=None, like_last_seen_total=None,
                            total_income=0, contributor_count=0,
                        )
                        self.monitored_room.last_stats.update(current_user_count=0, total_user_count=0)
                        self.current_session_id = new_session.id
                        self.total_income = 0.0
                        self.current_viewer_count = 0
                        self.max_viewer_count = 0
                    else:
                        # 数据库锁可能发现另一连接已建场；此时仍是续接。
                        self._restore_session_data(new_session)
        data_service.update_live_room_status(self.live_id, 'monitoring')
        self._emit_room_stats_throttled()

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

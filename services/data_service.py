"""
数据服务层
封装所有数据库操作
"""
from datetime import datetime, timedelta
import re
import unicodedata
from typing import List, Optional, Dict, Any
from sqlalchemy import create_engine, and_, or_, func, text, update, select, tuple_, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import config
from models.database import Base, LiveRoom, ChatMessage, GiftMessage, RoomStats, UserContribution, SystemEvent, LiveSession, get_china_now, CHINA_TZ
from utils.logger import get_logger

logger = get_logger("data_service")


_EXTRA_BLANK_CODEPOINTS = {
    0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5,
    *range(0x180B, 0x1810), *range(0x2000, 0x2010), *range(0x2028, 0x2030),
    *range(0x205F, 0x2070), 0x2800, 0x3000, 0x3164,
    *range(0xFE00, 0xFE10), 0xFEFF, 0xFFA0, *range(0xFFF9, 0xFFFC),
}


def is_blank_nickname(value: Optional[str]) -> bool:
    """Identify nicknames made entirely of whitespace or invisible characters."""
    if value is None or value == '':
        return True
    return all(ch.isspace() or ord(ch) in _EXTRA_BLANK_CODEPOINTS
               or unicodedata.category(ch) in ('Zs', 'Zl', 'Zp', 'Cf', 'Cc')
               for ch in value)


def is_blank_search_query(raw_query: Optional[str]) -> bool:
    """Nonempty blank input and explicit labels request blank nicknames."""
    return (isinstance(raw_query, str) and len(raw_query) > 0
            and (is_blank_nickname(raw_query)
                 or raw_query.strip() in ('空白昵称', '[空白昵称]')))


class DataService:
    """封装所有数据库操作"""

    def __init__(self, database_url: str = None):
        """
        初始化数据服务
        :param database_url: 数据库连接URL
        """
        self.database_url = database_url or config.DATABASE_URL
        pool_options = {} if make_url(self.database_url).get_backend_name() == 'sqlite' else {'pool_size': 15, 'max_overflow': 25}
        self.engine = create_engine(
            self.database_url,
            echo=config.SQLALCHEMY_ECHO,
            pool_pre_ping=True,
            pool_recycle=3600,
            **pool_options
        )
        if make_url(self.database_url).get_backend_name() == 'sqlite':
            event.listen(self.engine, 'connect', lambda dbapi_conn, _: dbapi_conn.create_function(
                'is_blank_nickname', 1, lambda s: 1 if is_blank_nickname(s) else 0))
        self.SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            bind=self.engine
        )

    def create_tables(self):
        """创建所有数据库表"""
        Base.metadata.create_all(bind=self.engine)

    def drop_tables(self):
        """删除所有数据库表"""
        Base.metadata.drop_all(bind=self.engine)

    def get_session(self):
        """获取数据库会话"""
        return self.SessionLocal()

    def close_session(self):
        """释放数据库连接池资源。"""
        self.engine.dispose()

    def get_related_live_ids(self, live_id: str) -> List[str]:
        """返回与该房间同属一个主播（相同 anchor_id）的所有 live_id，含自身。"""
        if not live_id:
            return []
        session = self.get_session()
        try:
            return self._related_live_ids(session, live_id)
        finally:
            session.close()

    def _related_live_ids(self, session, live_id: str) -> List[str]:
        """在已有 session 中解析同一主播的所有 live_id。无 anchor_id 时只返回自身。"""
        if not live_id:
            return []
        room = session.query(LiveRoom.live_id, LiveRoom.anchor_id).filter(
            LiveRoom.live_id == live_id
        ).first()
        if not room or not room.anchor_id:
            return [live_id]
        rows = session.query(LiveRoom.live_id).filter(
            LiveRoom.anchor_id == room.anchor_id
        ).all()
        ids = [row.live_id for row in rows]
        if live_id not in ids:
            ids.insert(0, live_id)
        else:
            ids = [live_id] + [item for item in ids if item != live_id]
        return ids

    @staticmethod
    def _latest_profile_fields(user_contrib: UserContribution = None,
                               fallback_nickname: str = '',
                               fallback_user_level: int = 0,
                               fallback_fans_club_level: int = 0) -> Dict[str, Any]:
        """统一解析榜单展示身份，优先使用最新采集到的累计资料。"""
        user_level = 0
        fans_club_level = 0
        if user_contrib:
            user_level = user_contrib.user_level or 0
            fans_club_level = user_contrib.fans_club_level or 0

        return {
            'nickname': (
                user_contrib.user_name
                if user_contrib and user_contrib.user_name
                else fallback_nickname
            ),
            'user_avatar': user_contrib.user_avatar if user_contrib else None,
            'user_level': user_level or fallback_user_level or 0,
            'fans_club_level': fans_club_level or fallback_fans_club_level or 0,
        }

    # ==================== 直播间操作 ====================

    def create_live_room(self, live_id: str, **kwargs) -> LiveRoom:
        """
        创建直播间记录
        :param live_id: 直播间ID
        :param kwargs: 其他字段
        :return: LiveRoom对象
        """
        session = self.get_session()
        try:
            room = LiveRoom(live_id=live_id, **kwargs)
            session.add(room)
            session.commit()
            session.refresh(room)
            return room
        except IntegrityError:
            session.rollback()
            return self.get_live_room(live_id)
        finally:
            session.close()

    def get_live_room(self, live_id: str) -> Optional[LiveRoom]:
        """根据live_id获取直播间"""
        session = self.get_session()
        try:
            return session.query(LiveRoom).filter(LiveRoom.live_id == live_id).first()
        finally:
            session.close()

    def list_live_rooms(self, status: str = None, include_archived: bool = False, archived_only: bool = False) -> List[LiveRoom]:
        """
        获取直播间列表
        :param status: 过滤状态
        :param include_archived: 是否包含已归档房间
        :param archived_only: 是否只返回已归档房间
        :return: LiveRoom列表
        """
        session = self.get_session()
        try:
            query = session.query(LiveRoom)
            archived_condition = or_(LiveRoom.archived_at.isnot(None), LiveRoom.status == 'archived')
            active_condition = and_(LiveRoom.archived_at.is_(None), LiveRoom.status != 'archived')

            if archived_only:
                query = query.filter(archived_condition)
            elif not include_archived:
                query = query.filter(active_condition)

            if status:
                query = query.filter(LiveRoom.status == status)
            return query.order_by(LiveRoom.created_at.desc()).all()
        finally:
            session.close()

    def get_24h_monitor_rooms(self) -> List[LiveRoom]:
        """获取所有24小时监控的房间（现在默认所有房间都是24小时监控）"""
        session = self.get_session()
        try:
            # 获取所有房间，因为现在默认都是24小时监控
            return session.query(LiveRoom).filter(
                LiveRoom.auto_reconnect == True,
                LiveRoom.archived_at.is_(None),
                LiveRoom.status != 'archived'
            ).all()
        finally:
            session.close()

    def update_live_room(self, live_id: str, **kwargs) -> bool:
        """更新直播间信息"""
        session = self.get_session()
        try:
            room = session.query(LiveRoom).filter(LiveRoom.live_id == live_id).first()
            if room:
                for key, value in kwargs.items():
                    if hasattr(room, key):
                        setattr(room, key, value)
                room.updated_at = get_china_now()
                session.commit()
                return True
            return False
        finally:
            session.close()

    def update_live_room_status(self, live_id: str, status: str, error_message: str = None) -> bool:
        """更新直播间状态"""
        session = self.get_session()
        try:
            room = session.query(LiveRoom).filter(LiveRoom.live_id == live_id).first()
            if not room:
                return False
            if (room.archived_at or room.status == 'archived') and status != 'archived':
                return True
            room.status = status
            room.error_message = error_message
            room.updated_at = get_china_now()
            session.commit()
            return True
        finally:
            session.close()

    def update_live_room_reconnect(self, live_id: str, reconnect_count: int) -> bool:
        """更新重连次数"""
        return self.update_live_room(
            live_id,
            reconnect_count=reconnect_count
        )

    def archive_live_room(self, live_id: str) -> bool:
        """归档直播间，保留所有历史数据"""
        session = self.get_session()
        try:
            room = session.query(LiveRoom).filter(LiveRoom.live_id == live_id).first()
            if room:
                room.status = 'archived'
                room.auto_reconnect = False
                room.archived_at = get_china_now()
                room.error_message = '已归档，历史数据保留'
                room.updated_at = get_china_now()
                session.commit()
                return True
            return False
        finally:
            session.close()

    def restore_live_room(self, live_id: str) -> bool:
        """从历史数据恢复直播间到主页"""
        session = self.get_session()
        try:
            room = session.query(LiveRoom).filter(LiveRoom.live_id == live_id).first()
            if room:
                room.status = 'stopped'
                room.auto_reconnect = True
                room.archived_at = None
                room.error_message = None
                room.updated_at = get_china_now()
                session.commit()
                return True
            return False
        finally:
            session.close()

    def hard_delete_live_room(self, live_id: str) -> bool:
        """永久删除直播间和所有关联数据"""
        session = self.get_session()
        try:
            room = session.query(LiveRoom).filter(LiveRoom.live_id == live_id).first()
            if room:
                session.delete(room)
                session.commit()
                return True
            return False
        finally:
            session.close()

    def delete_live_room(self, live_id: str) -> bool:
        """兼容旧调用：现在的删除等同于归档，不再删除历史数据"""
        return self.archive_live_room(live_id)

    def get_stats_summary(self) -> Dict[str, int]:
        """获取统计摘要

        三态分离（不再把所有非 monitoring 房间都算成"已停止"）：
        - monitoring_rooms: 监控线程运行中且已连上直播 (status='monitoring')
        - waiting_rooms:    监控线程运行中但直播未开播 (status='offline')
        - stopped_rooms:    用户主动停止监控 (status='stopped')；含 error 等其它非活动态
        """
        session = self.get_session()
        try:
            active_condition = and_(LiveRoom.archived_at.is_(None), LiveRoom.status != 'archived')
            archived_condition = or_(LiveRoom.archived_at.isnot(None), LiveRoom.status == 'archived')
            total_rooms = session.query(func.count(LiveRoom.live_id)).filter(active_condition).scalar() or 0
            monitoring_rooms = session.query(func.count(LiveRoom.live_id)).filter(active_condition, LiveRoom.status == 'monitoring').scalar() or 0
            waiting_rooms = session.query(func.count(LiveRoom.live_id)).filter(active_condition, LiveRoom.status.in_(['offline', 'waiting'])).scalar() or 0
            stopped_rooms = session.query(func.count(LiveRoom.live_id)).filter(active_condition, LiveRoom.status == 'stopped').scalar() or 0
            h24_rooms = session.query(func.count(LiveRoom.live_id)).filter(active_condition, LiveRoom.monitor_type == '24h').scalar() or 0
            archived_rooms = session.query(func.count(LiveRoom.live_id)).filter(archived_condition).scalar() or 0

            return {
                'total_rooms': total_rooms,
                'monitoring_rooms': monitoring_rooms,
                'waiting_rooms': waiting_rooms,
                'stopped_rooms': stopped_rooms,
                'h24_rooms': h24_rooms,
                'archived_rooms': archived_rooms,
            }
        finally:
            session.close()

    def get_archived_rooms_summary(self) -> List[Dict[str, Any]]:
        """获取已归档直播间的历史数据摘要"""
        session = self.get_session()
        try:
            rooms = session.query(LiveRoom).filter(
                or_(LiveRoom.archived_at.isnot(None), LiveRoom.status == 'archived')
            ).order_by(LiveRoom.archived_at.desc(), LiveRoom.updated_at.desc()).all()

            def dt_key(value):
                if value is None:
                    return datetime.min
                if value.tzinfo is not None:
                    return value.replace(tzinfo=None)
                return value

            def iso(value):
                return value.isoformat() if value else None

            room_ids = [room.live_id for room in rooms]
            if not room_ids:
                return []
            session_rows = session.query(LiveSession.live_id,
                    func.count(LiveSession.id),
                    func.coalesce(func.sum(LiveSession.total_income), 0),
                    func.coalesce(func.sum(LiveSession.total_gift_count), 0),
                    func.coalesce(func.sum(LiveSession.total_chat_count), 0),
                    func.coalesce(func.sum(LiveSession.total_like_count), 0),
                    func.min(LiveSession.start_time),
                    func.max(func.coalesce(LiveSession.end_time, LiveSession.start_time))
                ).filter(LiveSession.live_id.in_(room_ids)).group_by(LiveSession.live_id).all()
            session_map = {row[0]: tuple(row[1:]) for row in session_rows}

            chat_rows = session.query(ChatMessage.live_id,
                    func.count(ChatMessage.id),
                    func.min(ChatMessage.created_at),
                    func.max(ChatMessage.created_at)
                ).filter(ChatMessage.live_id.in_(room_ids)).group_by(ChatMessage.live_id).all()
            chat_map = {row[0]: tuple(row[1:]) for row in chat_rows}

            gift_rows = session.query(GiftMessage.live_id,
                    func.coalesce(func.sum(GiftMessage.total_value), 0),
                    func.coalesce(func.sum(GiftMessage.gift_count), 0),
                    func.min(GiftMessage.created_at),
                    func.max(GiftMessage.created_at)
                ).filter(GiftMessage.live_id.in_(room_ids)).group_by(GiftMessage.live_id).all()
            gift_map = {row[0]: tuple(row[1:]) for row in gift_rows}

            stats_rows = session.query(RoomStats.live_id,
                    func.min(RoomStats.stats_at),
                    func.max(RoomStats.stats_at)
                ).filter(RoomStats.live_id.in_(room_ids)).group_by(RoomStats.live_id).all()
            stats_map = {row[0]: tuple(row[1:]) for row in stats_rows}

            result = []
            for room in rooms:
                session_agg = session_map.get(room.live_id, (0, 0, 0, 0, 0, None, None))
                chat_agg = chat_map.get(room.live_id, (0, None, None))
                gift_agg = gift_map.get(room.live_id, (0, 0, None, None))
                stats_agg = stats_map.get(room.live_id, (None, None))
                first_candidates = [
                    session_agg[5],
                    chat_agg[1],
                    gift_agg[2],
                    stats_agg[0],
                ]
                last_candidates = [
                    session_agg[6],
                    chat_agg[2],
                    gift_agg[3],
                    stats_agg[1],
                ]
                first_candidates = [value for value in first_candidates if value is not None]
                last_candidates = [value for value in last_candidates if value is not None]

                gift_income = float(gift_agg[0] or 0)
                session_income = float(session_agg[1] or 0)
                gift_count = int(gift_agg[1] or 0)
                session_gift_count = int(session_agg[2] or 0)
                chat_count = int(chat_agg[0] or 0)
                session_chat_count = int(session_agg[3] or 0)
                session_like_count = int(session_agg[4] or 0)

                result.append({
                    'live_id': room.live_id,
                    'anchor_name': room.anchor_name,
                    'status': room.status,
                    'archived_at': iso(room.archived_at),
                    'created_at': iso(room.created_at),
                    'updated_at': iso(room.updated_at),
                    'first_activity_at': iso(min(first_candidates, key=dt_key)) if first_candidates else None,
                    'last_activity_at': iso(max(last_candidates, key=dt_key)) if last_candidates else None,
                    'total_sessions': int(session_agg[0] or 0),
                    'total_income': max(gift_income, session_income),
                    'total_gift_count': gift_count if gift_count > 0 else session_gift_count,
                    'total_chat_count': chat_count if chat_count > 0 else session_chat_count,
                    'total_like_count': session_like_count
                })

            return result
        finally:
            session.close()

    # ==================== 消息操作 ====================

    def save_chat_message(self, live_id: str, live_session_id: int = None, anchor_name: str = None, **kwargs) -> Optional[ChatMessage]:
        """保存弹幕消息"""
        session = self.get_session()
        try:
            msg = ChatMessage(live_id=live_id, live_session_id=live_session_id, anchor_name=anchor_name, **kwargs)
            session.add(msg)
            session.commit()
            session.refresh(msg)
            return msg
        except Exception as e:
            session.rollback()
            print(f"保存弹幕消息失败: {e}")
            return None
        finally:
            session.close()

    def save_gift_message(self, live_id: str, live_session_id: int = None, anchor_name: str = None, trace_id: str = None, **kwargs) -> Optional[GiftMessage]:
        """保存礼物消息"""
        session = self.get_session()
        try:
            msg = GiftMessage(
                live_id=live_id,
                live_session_id=live_session_id,
                anchor_name=anchor_name,
                trace_id=trace_id,
                **kwargs
            )
            session.add(msg)
            session.commit()
            session.refresh(msg)
            return msg
        except Exception as e:
            session.rollback()
            print(f"保存礼物消息失败: {e}")
            return None
        finally:
            session.close()

    def update_gift_message(self, msg_id: int, **kwargs) -> bool:
        """更新礼物消息（用于连击礼物更新数量和总价值）"""
        session = self.get_session()
        try:
            msg = session.query(GiftMessage).filter(GiftMessage.id == msg_id).first()
            if msg:
                for key, value in kwargs.items():
                    if hasattr(msg, key):
                        setattr(msg, key, value)
                session.commit()
                return True
            return False
        except Exception as e:
            session.rollback()
            print(f"更新礼物消息失败: {e}")
            return False
        finally:
            session.close()

    def get_chat_messages(self, live_id: str, limit: int = 100, offset: int = 0) -> List[ChatMessage]:
        """获取弹幕消息"""
        session = self.get_session()
        try:
            return session.query(ChatMessage).filter(
                ChatMessage.live_id == live_id
            ).order_by(ChatMessage.created_at.desc()).offset(offset).limit(limit).all()
        finally:
            session.close()

    def get_gift_messages(self, live_id: str, limit: int = 100, offset: int = 0) -> List[GiftMessage]:
        """获取礼物消息"""
        session = self.get_session()
        try:
            return session.query(GiftMessage).filter(
                GiftMessage.live_id == live_id
            ).order_by(GiftMessage.created_at.desc()).offset(offset).limit(limit).all()
        finally:
            session.close()

    def get_message_counts(self, live_id: str) -> Dict[str, int]:
        """获取消息总数"""
        session = self.get_session()
        try:
            chat_count = session.query(func.count(ChatMessage.id)).filter(
                ChatMessage.live_id == live_id
            ).scalar() or 0
            gift_count = session.query(func.count(GiftMessage.id)).filter(
                GiftMessage.live_id == live_id
            ).scalar() or 0
            return {
                'chat_count': chat_count,
                'gift_count': gift_count,
                'total_count': chat_count + gift_count
            }
        finally:
            session.close()

    def get_all_messages(self, live_id: str, limit: int = 100, offset: int = 0,
                         session_id: Optional[int] = None) -> List[Dict]:
        """Only merge each table's first offset+limit rows."""
        session = self.get_session()
        try:
            messages = []
            for model, kind in [(ChatMessage, 'chat'), (GiftMessage, 'gift')]:
                query = session.query(model).filter(model.live_id == live_id)
                if session_id is not None:
                    query = query.filter(model.live_session_id == session_id)
                rows = query.order_by(
                    model.created_at.desc(), model.id.desc()).limit(offset + limit).all()
                for msg in rows:
                    messages.append(dict(type=kind, id=msg.id, user_name=msg.user_name,
                        user_level=msg.user_level, created_at=msg.created_at,
                        display_content=msg.content if kind == 'chat' else f'{msg.user_name} 赠送了 {msg.gift_name}x{msg.gift_count}',
                        gift_name=msg.gift_name if kind == 'gift' else None,
                        gift_count=msg.gift_count if kind == 'gift' else None,
                        total_value=msg.total_value if kind == 'gift' else None,
                        user=msg.user_name, user_id=msg.user_id,
                        fans_club_level=msg.fans_club_level or 0,
                        text=msg.content if kind == 'chat' else None,
                        is_gift_user=bool(msg.is_gift_user) if kind == 'chat' else False,
                        gift_price=msg.gift_price if kind == 'gift' else None,
                        live_session_id=msg.live_session_id))
            messages.sort(key=lambda row: (row['created_at'], row['id']), reverse=True)
            return messages[offset:offset + limit]
        finally:
            session.close()

    def save_room_stats(self, live_id: str, anchor_name: str = None, **kwargs) -> Optional[RoomStats]:
        """保存统计快照"""
        session = self.get_session()
        try:
            stats = RoomStats(live_id=live_id, anchor_name=anchor_name, **kwargs)
            session.add(stats)
            session.commit()
            session.refresh(stats)
            return stats
        except Exception as e:
            session.rollback()
            print(f"保存统计快照失败: {e}")
            return None
        finally:
            session.close()

    def get_latest_stats(self, live_id: str) -> Optional[RoomStats]:
        """获取最新统计"""
        session = self.get_session()
        try:
            return session.query(RoomStats).filter(
                RoomStats.live_id == live_id
            ).order_by(RoomStats.stats_at.desc()).first()
        finally:
            session.close()

    def get_room_stats_history(self, live_id: str, hours: int = 24) -> List[RoomStats]:
        """获取统计历史"""
        session = self.get_session()
        try:
            since = get_china_now() - timedelta(hours=hours)
            return session.query(RoomStats).filter(
                and_(
                    RoomStats.live_id == live_id,
                    RoomStats.stats_at >= since
                )
            ).order_by(RoomStats.stats_at.asc()).all()
        finally:
            session.close()

    # ==================== 贡献榜操作 ====================

    def update_user_contribution(self, live_id: str, anchor_name: str, user_id: str, user_name: str,
                                 gift_value: float = 0, gift_count: int = 0,
                                 chat_count: int = 0, like_count: int = 0,
                                 user_avatar: str = None,
                                 gender: int = None, follower_count: int = None,
                                 following_count: int = None, age_range: int = None,
                                 fans_club_level: int = None,
                                 user_level: int = None,
                                 display_id: str = None) -> UserContribution:
        """Atomically add counters; replace only newly observed profile fields."""
        session = self.get_session()
        try:
            now = get_china_now()
            values = dict(live_id=live_id, anchor_name=anchor_name, user_id=user_id,
                          user_name=user_name, total_score=gift_value, gift_count=gift_count,
                          chat_count=chat_count, like_count=like_count, user_avatar=user_avatar,
                          created_at=now, updated_at=now)
            profile = dict(gender=gender, follower_count=follower_count,
                           following_count=following_count, age_range=age_range,
                           fans_club_level=fans_club_level, user_level=user_level)
            for key, val in profile.items():
                if val is not None and val > 0:
                    values[key] = val
            clean_display_id = (display_id or '').strip()[:100] or None
            if clean_display_id:
                values['display_id'] = clean_display_id
            insert = sqlite_insert if self.engine.dialect.name == 'sqlite' else mysql_insert
            statement = insert(UserContribution).values(**values)
            updates = {
                'total_score': func.coalesce(UserContribution.total_score, 0) + gift_value,
                'gift_count': func.coalesce(UserContribution.gift_count, 0) + gift_count,
                'chat_count': func.coalesce(UserContribution.chat_count, 0) + chat_count,
                'like_count': func.coalesce(UserContribution.like_count, 0) + like_count,
                'user_name': user_name, 'updated_at': now,
            }
            if clean_display_id:
                updates['display_id'] = clean_display_id
            for key in ['anchor_name', 'user_avatar'] + list(profile):
                if values.get(key):
                    updates[key] = values[key]
            if self.engine.dialect.name == 'sqlite':
                statement = statement.on_conflict_do_update(index_elements=['live_id', 'user_id'], set_=updates)
            else:
                statement = statement.on_duplicate_key_update(**updates)
            session.execute(statement)
            session.commit()
            return session.query(UserContribution).filter_by(live_id=live_id, user_id=user_id).one()
        except Exception:
            session.rollback()
            logger.error('更新用户贡献失败')
            raise
        finally:
            session.close()

    def get_top_contributors(self, live_id: str, limit: int = 100) -> List[UserContribution]:
        """获取贡献榜TOP N"""
        session = self.get_session()
        try:
            return session.query(UserContribution).filter(
                UserContribution.live_id == live_id
            ).order_by(UserContribution.total_score.desc()).limit(limit).all()
        finally:
            session.close()

    @staticmethod
    def _escape_like(value):
        return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

    def _blank_name_condition(self, column):
        if self.engine.dialect.name == 'sqlite':
            return func.is_blank_nickname(column) == 1
        return or_(column == '', column.op('REGEXP')(
            '^[[:space:]\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f'
            '\u2000-\u200f\u2028-\u202f\u205f-\u206f\u2800\u3000\u3164'
            '\ufe00-\ufe0f\ufeff\uffa0]*$'))

    def get_contributors_by_date_range(self, live_id: str = None, start_date: str = None, end_date: str = None, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """Aggregate identities and enrich just the current page in bounded queries."""
        session = self.get_session()
        try:
            related = self._related_live_ids(session, live_id) if live_id else []
            start = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=CHINA_TZ) if start_date else None
            end = datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=CHINA_TZ) if end_date else None
            def conditions(model):
                result = []
                if related:
                    result.append(model.live_id.in_(related))
                if start:
                    result.append(model.created_at >= start)
                if end:
                    result.append(model.created_at <= end)
                return result
            group = [GiftMessage.user_id] if live_id else [GiftMessage.live_id, GiftMessage.user_id]
            grouped = session.query(*group).filter(*conditions(GiftMessage)).group_by(*group).subquery()
            total = session.query(func.count()).select_from(grouped).scalar() or 0
            rows = session.query(
                func.max(GiftMessage.live_id).label('live_id'),
                func.max(GiftMessage.anchor_name).label('anchor_name'), GiftMessage.user_id,
                func.sum(GiftMessage.total_value).label('contribution_value'),
                func.sum(GiftMessage.gift_count).label('gift_count'),
                func.max(GiftMessage.user_level).label('user_level')
            ).filter(*conditions(GiftMessage)).group_by(*group).order_by(
                func.sum(GiftMessage.total_value).desc(), *group).limit(page_size).offset((page - 1) * page_size).all()
            contributors = []
            if rows:
                user_ids = list({row.user_id for row in rows})
                room_ids = related or list({row.live_id for row in rows})
                key_for = lambda rid, uid: uid if live_id else (rid, uid)
                contribs = session.query(UserContribution).filter(
                    UserContribution.user_id.in_(user_ids), UserContribution.live_id.in_(room_ids)
                ).order_by(UserContribution.updated_at.desc(), UserContribution.id.desc()).all()
                profiles, likes = {}, {}
                for item in contribs:
                    key = key_for(item.live_id, item.user_id)
                    profiles.setdefault(key, item)
                    likes[key] = likes.get(key, 0) + int(item.like_count or 0)

                def latest_messages(model):
                    partitions = [model.user_id] if live_id else [model.live_id, model.user_id]
                    ranked = session.query(
                        model.live_id, model.user_id, model.user_name, model.anchor_name,
                        model.user_level, model.fans_club_level, model.created_at,
                        func.count(model.id).over(partition_by=partitions).label('message_count'),
                        func.row_number().over(partition_by=partitions,
                            order_by=[model.created_at.desc(), model.id.desc()]).label('rn')
                    ).filter(model.user_id.in_(user_ids), model.live_id.in_(room_ids), *conditions(model)).subquery()
                    return {key_for(r.live_id, r.user_id): r for r in session.query(ranked).filter(ranked.c.rn == 1).all()}
                chats = latest_messages(ChatMessage)
                gifts = latest_messages(GiftMessage)
                for row in rows:
                    key = key_for(row.live_id, row.user_id)
                    chat, gift = chats.get(key), gifts.get(key)
                    latest = max([v for v in [chat, gift] if v], key=lambda v: v.created_at, default=None)
                    profile = self._latest_profile_fields(profiles.get(key),
                        fallback_nickname=latest.user_name if latest else row.user_id,
                        fallback_user_level=(latest.user_level or 0) if latest else row.user_level,
                        fallback_fans_club_level=(latest.fans_club_level or 0) if latest else 0)
                    contributors.append(dict(live_id=live_id or row.live_id,
                        anchor_name=(latest.anchor_name if latest and latest.anchor_name else row.anchor_name),
                        user_id=row.user_id, contribution_value=float(row.contribution_value or 0),
                        gift_count=int(row.gift_count or 0), chat_count=int(chat.message_count if chat else 0),
                        like_count=likes.get(key, 0), **profile))
            return dict(contributors=contributors, total=total, page=page, page_size=page_size,
                        total_pages=max(1, (total + page_size - 1) // page_size))
        finally:
            session.close()

    def get_summary_contributors(self, live_id: str = None, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """
        获取总贡献榜（从累计汇总表读取）。
        旧礼物明细可能被数据保留策略清理，但 user_contributions 保留了累计总贡献。
        指定 live_id 时会把同一 anchor_id 下的关联房间合并到 user_id。
        """
        session = self.get_session()
        try:
            related_ids = self._related_live_ids(session, live_id) if live_id else None
            merge_by_user = bool(live_id)
            conditions = [UserContribution.total_score > 0]
            if related_ids:
                conditions.append(UserContribution.live_id.in_(related_ids))

            if merge_by_user:
                total = session.query(func.count(func.distinct(UserContribution.user_id))).filter(
                    and_(*conditions)
                ).scalar() or 0
            else:
                total = session.query(func.count(UserContribution.id)).filter(
                    and_(*conditions)
                ).scalar() or 0

            total_pages = (total + page_size - 1) // page_size if total > 0 else 1
            offset = (page - 1) * page_size

            if merge_by_user:
                agg_rows = session.query(
                    UserContribution.user_id,
                    func.sum(UserContribution.total_score).label('total_score'),
                    func.sum(UserContribution.gift_count).label('gift_count'),
                    func.sum(UserContribution.chat_count).label('chat_count'),
                    func.sum(UserContribution.like_count).label('like_count'),
                    func.max(UserContribution.updated_at).label('updated_at'),
                ).filter(
                    and_(*conditions)
                ).group_by(
                    UserContribution.user_id
                ).order_by(
                    func.sum(UserContribution.total_score).desc(),
                    func.max(UserContribution.updated_at).desc()
                ).limit(page_size).offset(offset).all()

                user_ids = [row.user_id for row in agg_rows]
                live_ids = related_ids or []
                latest_by_user = {}
                if user_ids:
                    profiles = session.query(UserContribution).filter(
                        UserContribution.user_id.in_(user_ids),
                        UserContribution.live_id.in_(live_ids)
                    ).order_by(UserContribution.updated_at.desc()).all()
                    for profile in profiles:
                        latest_by_user.setdefault(profile.user_id, profile)

                message_extra = {}
                if user_ids:
                    chat_query = session.query(
                        ChatMessage.user_id,
                        func.count(ChatMessage.id).label('chat_count'),
                        func.max(ChatMessage.user_level).label('user_level')
                    ).filter(
                        and_(
                            ChatMessage.live_id.in_(live_ids),
                            ChatMessage.user_id.in_(user_ids)
                        )
                    ).group_by(ChatMessage.user_id)
                    for chat_row in chat_query.all():
                        message_extra[chat_row.user_id] = {
                            'chat_count': int(chat_row.chat_count or 0),
                            'user_level': chat_row.user_level or 0
                        }

                    gift_query = session.query(
                        GiftMessage.user_id,
                        func.max(GiftMessage.user_level).label('user_level')
                    ).filter(
                        and_(
                            GiftMessage.live_id.in_(live_ids),
                            GiftMessage.user_id.in_(user_ids)
                        )
                    ).group_by(GiftMessage.user_id)
                    for gift_row in gift_query.all():
                        extra = message_extra.setdefault(
                            gift_row.user_id,
                            {'chat_count': 0, 'user_level': 0}
                        )
                        extra['user_level'] = max(extra['user_level'] or 0, gift_row.user_level or 0)

                contributors = []
                for row in agg_rows:
                    profile_row = latest_by_user.get(row.user_id)
                    extra = message_extra.get(row.user_id, {})
                    profile = self._latest_profile_fields(
                        profile_row,
                        fallback_nickname=profile_row.user_name if profile_row else row.user_id,
                        fallback_user_level=extra.get('user_level', 0),
                        fallback_fans_club_level=(profile_row.fans_club_level or 0) if profile_row else 0,
                    )
                    contributors.append({
                        'live_id': live_id,
                        'anchor_name': profile_row.anchor_name if profile_row else None,
                        'user_id': row.user_id,
                        'nickname': profile['nickname'],
                        'contribution_value': int(row.total_score or 0),
                        'gift_count': int(row.gift_count or 0),
                        'chat_count': max(int(row.chat_count or 0), extra.get('chat_count', 0)),
                        'like_count': int(row.like_count or 0),
                        'user_avatar': profile['user_avatar'],
                        'user_level': profile['user_level'],
                        'fans_club_level': profile['fans_club_level']
                    })
            else:
                rows = session.query(UserContribution).filter(
                    and_(*conditions)
                ).order_by(
                    UserContribution.total_score.desc(),
                    UserContribution.updated_at.desc()
                ).limit(page_size).offset(offset).all()

                message_extra = {}
                if rows:
                    user_ids = [row.user_id for row in rows]
                    live_ids = [row.live_id for row in rows]

                    chat_query = session.query(
                        ChatMessage.live_id,
                        ChatMessage.user_id,
                        func.count(ChatMessage.id).label('chat_count'),
                        func.max(ChatMessage.user_level).label('user_level')
                    ).filter(
                        and_(
                            ChatMessage.live_id.in_(live_ids),
                            ChatMessage.user_id.in_(user_ids)
                        )
                    ).group_by(
                        ChatMessage.live_id,
                        ChatMessage.user_id
                    )
                    for chat_row in chat_query.all():
                        message_extra[(chat_row.live_id, chat_row.user_id)] = {
                            'chat_count': int(chat_row.chat_count or 0),
                            'user_level': chat_row.user_level or 0
                        }

                    gift_query = session.query(
                        GiftMessage.live_id,
                        GiftMessage.user_id,
                        func.max(GiftMessage.user_level).label('user_level')
                    ).filter(
                        and_(
                            GiftMessage.live_id.in_(live_ids),
                            GiftMessage.user_id.in_(user_ids)
                        )
                    ).group_by(
                        GiftMessage.live_id,
                        GiftMessage.user_id
                    )
                    for gift_row in gift_query.all():
                        extra = message_extra.setdefault(
                            (gift_row.live_id, gift_row.user_id),
                            {'chat_count': 0, 'user_level': 0}
                        )
                        extra['user_level'] = max(extra['user_level'] or 0, gift_row.user_level or 0)

                contributors = []
                for row in rows:
                    extra = message_extra.get((row.live_id, row.user_id), {})
                    profile = self._latest_profile_fields(
                        row,
                        fallback_nickname=row.user_name,
                        fallback_user_level=extra.get('user_level', 0),
                        fallback_fans_club_level=row.fans_club_level or 0,
                    )
                    contributors.append({
                        'live_id': row.live_id,
                        'anchor_name': row.anchor_name,
                        'user_id': row.user_id,
                        'nickname': profile['nickname'],
                        'contribution_value': int(row.total_score or 0),
                        'gift_count': int(row.gift_count or 0),
                        'chat_count': max(int(row.chat_count or 0), extra.get('chat_count', 0)),
                        'like_count': int(row.like_count or 0),
                        'user_avatar': profile['user_avatar'],
                        'user_level': profile['user_level'],
                        'fans_club_level': profile['fans_club_level']
                    })

            return {
                'contributors': contributors,
                'total': total,
                'page': page,
                'page_size': page_size,
                'total_pages': total_pages,
                'source': 'summary'
            }
        finally:
            session.close()

    def get_top_likers(self, live_id: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        """获取累积点赞榜（基于 UserContribution.like_count，per-(live_id, user_id) 累积）。
        like_count > 0 才进入榜单；不传 live_id 则跨房间，同一 user_id 在不同房间显示多行。
        指定 live_id 时会把同一 anchor_id 下的关联房间按 user_id 合并。
        """
        session = self.get_session()
        try:
            conditions = [UserContribution.like_count > 0]
            related_ids = self._related_live_ids(session, live_id) if live_id else None
            if related_ids:
                conditions.append(UserContribution.live_id.in_(related_ids))

            if live_id and related_ids and len(related_ids) > 1:
                agg_rows = session.query(
                    UserContribution.user_id,
                    func.sum(UserContribution.like_count).label('like_count'),
                    func.sum(UserContribution.gift_count).label('gift_count'),
                    func.max(UserContribution.updated_at).label('updated_at'),
                ).filter(and_(*conditions)).group_by(
                    UserContribution.user_id
                ).order_by(
                    func.sum(UserContribution.like_count).desc(),
                    func.max(UserContribution.updated_at).desc()
                ).limit(limit).all()

                user_ids = [row.user_id for row in agg_rows]
                latest_by_user = {}
                if user_ids:
                    profiles = session.query(UserContribution).filter(
                        UserContribution.user_id.in_(user_ids),
                        UserContribution.live_id.in_(related_ids)
                    ).order_by(UserContribution.updated_at.desc()).all()
                    for profile in profiles:
                        latest_by_user.setdefault(profile.user_id, profile)

                result = []
                for row in agg_rows:
                    profile = latest_by_user.get(row.user_id)
                    result.append({
                        'live_id': live_id,
                        'anchor_name': profile.anchor_name if profile else None,
                        'user_id': row.user_id,
                        'user_name': profile.user_name if profile else row.user_id,
                        'user_avatar': profile.user_avatar if profile else None,
                        'user_level': (profile.user_level or 0) if profile else 0,
                        'like_count': int(row.like_count or 0),
                        'gift_count': int(row.gift_count or 0),
                        'fans_club_level': (profile.fans_club_level or 0) if profile else 0,
                    })
                return result

            rows = session.query(UserContribution).filter(
                and_(*conditions)
            ).order_by(
                UserContribution.like_count.desc(),
                UserContribution.updated_at.desc()
            ).limit(limit).all()

            return [{
                'live_id': r.live_id,
                'anchor_name': r.anchor_name,
                'user_id': r.user_id,
                'user_name': r.user_name,
                'user_avatar': r.user_avatar,
                'user_level': r.user_level or 0,
                'like_count': int(r.like_count or 0),
                'gift_count': int(r.gift_count or 0),
                'fans_club_level': r.fans_club_level or 0,
            } for r in rows]
        finally:
            session.close()

    def get_room_date_range(self, live_id: str = None) -> Dict[str, Optional[str]]:
        """
        获取房间的直播数据日期范围（最早和最晚的直播日期）
        :param live_id: 房间ID，None 表示所有房间
        :return: {'min_date': 'YYYY-MM-DD', 'max_date': 'YYYY-MM-DD'}
        """
        def format_date(value):
            if not value:
                return None
            if isinstance(value, str):
                return value[:10]
            return value.strftime('%Y-%m-%d')

        session = self.get_session()
        try:
            conditions = []
            if live_id:
                related_ids = self._related_live_ids(session, live_id)
                conditions.append(LiveSession.live_id.in_(related_ids))
            else:
                # 查询所有有数据的房间
                conditions.append(LiveSession.live_id.isnot(None))

            # 查询最早的直播日期
            min_date_query = session.query(
                func.date(LiveSession.start_time)
            ).filter(
                and_(*conditions, LiveSession.start_time.isnot(None))
            ).order_by(LiveSession.start_time.asc()).limit(1).scalar()

            # 查询最晚的直播日期
            max_date_query = session.query(
                func.date(LiveSession.start_time)
            ).filter(
                and_(*conditions, LiveSession.start_time.isnot(None))
            ).order_by(LiveSession.start_time.desc()).limit(1).scalar()

            return {
                'min_date': format_date(min_date_query),
                'max_date': format_date(max_date_query)
            }
        finally:
            session.close()

    def get_all_rooms_date_range(self) -> Dict[str, Optional[str]]:
        """获取所有房间中最早和最晚的直播日期"""
        return self.get_room_date_range(live_id=None)

    def get_user_contribution(self, live_id: str, user_id: str) -> Optional[UserContribution]:
        """获取用户贡献"""
        session = self.get_session()
        try:
            return session.query(UserContribution).filter(
                and_(
                    UserContribution.live_id == live_id,
                    UserContribution.user_id == user_id
                )
            ).first()
        finally:
            session.close()

    def get_session_contributors(self, live_id: str, session_id: int, limit: int = 100) -> List[Dict]:
        """获取指定直播场次的贡献榜（按礼物消息聚合）"""
        session = self.get_session()
        try:
            # 从礼物消息中聚合统计每个用户的贡献
            # 由于 GiftMessage 表没有 user_avatar 字段，如果不关联查询，头像将为空
            # 这里简化处理：先聚合礼物数据，再单独批量查询用户头像（比复杂的 join 更可控）

            from sqlalchemy import func

            # 1. 聚合礼物数据
            gift_stats = session.query(
                GiftMessage.user_id,
                func.max(GiftMessage.user_name).label('user_name'),
                func.max(GiftMessage.user_level).label('user_level'),
                func.sum(GiftMessage.total_value).label('total_score'),
                func.sum(GiftMessage.gift_count).label('gift_count')
            ).filter(
                and_(
                    GiftMessage.live_id == live_id,
                    GiftMessage.live_session_id == session_id
                )
            ).group_by(
                GiftMessage.user_id
            ).order_by(
                func.sum(GiftMessage.total_value).desc()
            ).limit(limit).all()

            if not gift_stats:
                return []

            # 2. 获取涉及到的用户ID
            user_ids = [row.user_id for row in gift_stats]

            # 3. 批量查询用户头像和粉丝团等级
            user_extra = {}
            if user_ids:
                user_rows = session.query(
                    UserContribution.user_id,
                    UserContribution.user_name,
                    UserContribution.user_avatar,
                    UserContribution.user_level,
                    UserContribution.fans_club_level,
                    UserContribution.like_count
                ).filter(
                    and_(
                        UserContribution.live_id == live_id,
                        UserContribution.user_id.in_(user_ids)
                    )
                ).all()
                user_extra = {
                    r.user_id: {
                        'user_name': r.user_name,
                        'avatar': r.user_avatar,
                        'user_level': r.user_level or 0,
                        'fans_club_level': r.fans_club_level or 0,
                        'like_count': r.like_count or 0
                    }
                    for r in user_rows
                }

            # 4. 组装结果
            contributors = []
            for row in gift_stats:
                extra = user_extra.get(row.user_id, {})
                contributors.append({
                    'user_id': row.user_id,
                    'nickname': extra.get('user_name') or row.user_name or '',
                    'contribution_value': float(row.total_score),
                    'gift_count': row.gift_count,
                    'like_count': int(extra.get('like_count') or 0),
                    'user_level': extra.get('user_level') or row.user_level or 0,
                    'user_avatar': extra.get('avatar'),
                    'fans_club_level': extra.get('fans_club_level', 0)
                })
            return contributors
        finally:
            session.close()

    def get_session_messages(self, session_id: int, message_type: str = 'chat', limit: int = 100, offset: int = 0) -> List[Dict]:
        """获取指定直播场次的弹幕或礼物消息"""
        session = self.get_session()
        try:
            if message_type == 'chat':
                messages = session.query(ChatMessage).filter(
                    ChatMessage.live_session_id == session_id
                ).order_by(ChatMessage.created_at.desc()).offset(offset).limit(limit).all()

                return [{
                    'id': msg.id,
                    'type': 'chat',
                    'live_id': msg.live_id,
                    'live_session_id': msg.live_session_id,
                    'anchor_name': msg.anchor_name,
                    'user_id': msg.user_id,
                    'nickname': msg.user_name,
                    'user_name': msg.user_name,
                    'user': msg.user_name,
                    'user_level': msg.user_level,
                    'content': msg.content,
                    'text': msg.content,
                    'is_gift_user': bool(msg.is_gift_user),
                    'fans_club_level': msg.fans_club_level or 0,
                    'created_at': msg.created_at.isoformat() if msg.created_at else None
                } for msg in messages]

            elif message_type == 'gift':
                messages = session.query(GiftMessage).filter(
                    GiftMessage.live_session_id == session_id
                ).order_by(GiftMessage.created_at.desc()).offset(offset).limit(limit).all()

                return [{
                    'id': msg.id,
                    'type': 'gift',
                    'live_id': msg.live_id,
                    'live_session_id': msg.live_session_id,
                    'anchor_name': msg.anchor_name,
                    'user_id': msg.user_id,
                    'nickname': msg.user_name,
                    'user_name': msg.user_name,
                    'user': msg.user_name,
                    'user_level': msg.user_level,
                    'gift_name': msg.gift_name,
                    'gift_count': msg.gift_count,
                    'gift_price': msg.gift_price,
                    'total_value': msg.total_value,
                    'send_type': msg.send_type,
                    'text': None,
                    'is_gift_user': False,
                    'combo_count': msg.gift_count if msg.send_type == 'combo' else 1,
                    'diamond_count': msg.total_value,
                    'fans_club_level': msg.fans_club_level or 0,
                    'created_at': msg.created_at.isoformat() if msg.created_at else None
                } for msg in messages]

            return []
        finally:
            session.close()

    def get_session_message_counts(self, session_id: int) -> Dict[str, int]:
        """获取场次消息总数"""
        session = self.get_session()
        try:
            chat_count = session.query(func.count(ChatMessage.id)).filter(
                ChatMessage.live_session_id == session_id
            ).scalar() or 0
            gift_count = session.query(func.count(GiftMessage.id)).filter(
                GiftMessage.live_session_id == session_id
            ).scalar() or 0
            return {
                'chat_count': chat_count,
                'gift_count': gift_count
            }
        finally:
            session.close()

    # ==================== 事件日志 ====================

    def log_system_event(self, live_id: str, event_type: str, message: str = None, data: Dict = None, anchor_name: str = None) -> SystemEvent:
        """记录系统事件"""
        session = self.get_session()
        try:
            event = SystemEvent(
                live_id=live_id,
                anchor_name=anchor_name,
                event_type=event_type,
                event_message=message,
                event_data=data
            )
            session.add(event)
            session.commit()
            session.refresh(event)
            return event
        except Exception as e:
            session.rollback()
            print(f"记录系统事件失败: {e}")
            return None
        finally:
            session.close()

    def get_system_events(self, live_id: str = None, event_type: str = None, limit: int = 100) -> List[SystemEvent]:
        """获取系统事件"""
        session = self.get_session()
        try:
            query = session.query(SystemEvent)
            if live_id:
                query = query.filter(SystemEvent.live_id == live_id)
            if event_type:
                query = query.filter(SystemEvent.event_type == event_type)
            return query.order_by(SystemEvent.created_at.desc()).limit(limit).all()
        finally:
            session.close()

    # ==================== 数据清理 ====================

    def cleanup_old_data(self, retention_days: int = None) -> Dict[str, int]:
        """Commit bounded batches and retain messages belonging to active sessions."""
        retention_days = config.DATA_RETENTION_DAYS if retention_days is None else retention_days
        if retention_days == 0:
            return {'message': '数据保留设置为永久保留，不清理'}
        cutoff_date = get_china_now() - timedelta(days=retention_days)
        session = self.get_session()
        try:
            result = {'cutoff_date': cutoff_date.isoformat()}
            for model, date_col, key in [
                (ChatMessage, ChatMessage.created_at, 'chat_messages_deleted'),
                (GiftMessage, GiftMessage.created_at, 'gift_messages_deleted'),
                (RoomStats, RoomStats.stats_at, 'stats_deleted'),
                (SystemEvent, SystemEvent.created_at, 'events_deleted')]:
                conditions = [date_col < cutoff_date]
                if model in (ChatMessage, GiftMessage):
                    active = select(LiveSession.id).where(LiveSession.status == 'live')
                    conditions.append(or_(model.live_session_id.is_(None), model.live_session_id.not_in(active)))
                deleted = 0
                while True:
                    ids = [row[0] for row in session.query(model.id).filter(*conditions).order_by(model.id).limit(10000).all()]
                    if not ids:
                        break
                    # Recheck eligibility so a resumed live session is protected.
                    deleted += session.query(model).filter(model.id.in_(ids), *conditions).delete(synchronize_session=False)
                    session.commit()
                result[key] = deleted
            return result
        except Exception:
            session.rollback()
            logger.error('清理旧数据失败')
            return {'error': '清理旧数据失败'}
        finally:
            session.close()

    def create_live_session(self, live_id: str, anchor_name: str = None, **kwargs) -> Optional[LiveSession]:
        """创建新的直播场次"""
        session = self.get_session()
        try:
            requested_status = kwargs.get('status', 'live')
            if requested_status == 'live':
                # 锁住房间行，让并发 WebSocket 打开时的“查找当前场次 -> 创建场次”
                # 在数据库层面串行化，避免同一直播同时插入多条 live 场次。
                session.query(LiveRoom).filter(LiveRoom.live_id == live_id).with_for_update().first()
                existing_session = session.query(LiveSession).filter(
                    and_(
                        LiveSession.live_id == live_id,
                        LiveSession.status == 'live'
                    )
                ).order_by(LiveSession.start_time.desc()).first()
                if existing_session:
                    existing_session._was_created = False
                    return existing_session

            session_obj = LiveSession(live_id=live_id, anchor_name=anchor_name, **kwargs)
            session.add(session_obj)
            session.commit()
            session.refresh(session_obj)
            session_obj._was_created = True
            return session_obj
        except Exception as e:
            session.rollback()
            logger.error(f"创建直播场次失败: {e}")
            return None
        finally:
            session.close()

    def get_current_live_session(self, live_id: str) -> Optional[LiveSession]:
        """获取当前进行中的直播场次"""
        session = self.get_session()
        try:
            return session.query(LiveSession).filter(
                and_(
                    LiveSession.live_id == live_id,
                    LiveSession.status == 'live'
                )
            ).order_by(LiveSession.start_time.desc()).first()
        finally:
            session.close()

    def end_live_session(self, session_id: int, peak_viewer_count: int = None) -> bool:
        """Close once; retained aggregates survive expired message details."""
        session = self.get_session()
        try:
            obj = session.query(LiveSession).filter_by(id=session_id).with_for_update().first()
            if obj is None:
                return False
            if obj.status != 'ended':
                self._reconcile_session(session, obj)
                obj.status = 'ended'
                obj.end_time = get_china_now()
            elif obj.end_time is None:
                obj.end_time = get_china_now()
            if peak_viewer_count is not None:
                obj.peak_viewer_count = max(obj.peak_viewer_count or 0, peak_viewer_count)
            obj.updated_at = get_china_now()
            session.commit()
            return True
        except Exception:
            session.rollback()
            logger.error('结束直播场次失败')
            return False
        finally:
            session.close()

    def _reconcile_session(self, session, obj):
        income, gifts = session.query(func.coalesce(func.sum(GiftMessage.total_value), 0),
                                     func.coalesce(func.sum(GiftMessage.gift_count), 0)).filter(
                                         GiftMessage.live_session_id == obj.id).one()
        chats = session.query(func.count(ChatMessage.id)).filter(ChatMessage.live_session_id == obj.id).scalar() or 0
        cutoff = (get_china_now() - timedelta(days=config.DATA_RETENTION_DAYS)).replace(tzinfo=None)
        expired = config.DATA_RETENTION_DAYS > 0 and obj.start_time.replace(tzinfo=None) < cutoff
        missing_gifts = not income and not gifts and (obj.total_income or obj.total_gift_count)
        if not (expired and missing_gifts):
            obj.total_income = float(income)
            obj.total_gift_count = int(gifts)
        if not (expired and chats == 0 and obj.total_chat_count):
            obj.total_chat_count = int(chats)

    def update_session_stats(self, session_id: int, **kwargs) -> bool:
        """Update snapshots while keeping the peak monotone in SQL."""
        session = self.get_session()
        try:
            values = {key: value for key, value in kwargs.items() if key in LiveSession.__table__.columns}
            if values.get('peak_viewer_count') is not None:
                maximum = func.max if self.engine.dialect.name == 'sqlite' else func.greatest
                values['peak_viewer_count'] = maximum(func.coalesce(LiveSession.peak_viewer_count, 0), values['peak_viewer_count'])
            else:
                values.pop('peak_viewer_count', None)
            values['updated_at'] = get_china_now()
            result = session.execute(update(LiveSession).where(LiveSession.id == session_id).values(**values))
            session.commit()
            return result.rowcount > 0
        except Exception:
            session.rollback()
            logger.error('更新直播场次统计失败')
            return False
        finally:
            session.close()

    def increment_session_stats(self, session_id: int, income_delta: float = 0,
                               gift_count_delta: int = 0, chat_count_delta: int = 0,
                               like_count_delta: int = 0) -> bool:
        """One SQL UPDATE prevents lost increments between collectors."""
        session = self.get_session()
        try:
            result = session.execute(update(LiveSession).where(LiveSession.id == session_id).values(
                total_income=func.coalesce(LiveSession.total_income, 0) + income_delta,
                total_gift_count=func.coalesce(LiveSession.total_gift_count, 0) + gift_count_delta,
                total_chat_count=func.coalesce(LiveSession.total_chat_count, 0) + chat_count_delta,
                total_like_count=func.coalesce(LiveSession.total_like_count, 0) + like_count_delta,
                updated_at=get_china_now()))
            session.commit()
            return result.rowcount > 0
        except Exception:
            session.rollback()
            logger.error('增量更新直播场次统计失败')
            return False
        finally:
            session.close()

    def get_live_sessions(self, live_id: str = None, status: str = None, limit: int = 100) -> List[LiveSession]:
        """获取直播场次列表"""
        session = self.get_session()
        try:
            query = session.query(LiveSession)
            if live_id:
                query = query.filter(LiveSession.live_id == live_id)
            if status:
                query = query.filter(LiveSession.status == status)
            return query.order_by(LiveSession.start_time.desc()).limit(limit).all()
        finally:
            session.close()

    def get_live_session_stats(self, session_id: int) -> Optional[Dict]:
        """获取直播场次统计详情"""
        session = self.get_session()
        try:
            session_obj = session.query(LiveSession).filter(LiveSession.id == session_id).first()
            if not session_obj:
                return None

            return {
                'id': session_obj.id,
                'live_id': session_obj.live_id,
                'anchor_name': session_obj.anchor_name,
                'start_time': session_obj.start_time.isoformat() if session_obj.start_time else None,
                'end_time': session_obj.end_time.isoformat() if session_obj.end_time else None,
                'status': session_obj.status,
                'total_income': session_obj.total_income,
                'total_gift_count': session_obj.total_gift_count,
                'total_chat_count': session_obj.total_chat_count,
                'total_like_count': session_obj.total_like_count,
                'peak_viewer_count': session_obj.peak_viewer_count
            }
        finally:
            session.close()

    def get_room_sessions_stats(self, live_id: str, start_date: str = None, end_date: str = None, limit: int = 100) -> List[Dict]:
        """获取房间的直播场次统计列表"""
        session = self.get_session()
        try:
            related_ids = self._related_live_ids(session, live_id)
            query = session.query(LiveSession).filter(LiveSession.live_id.in_(related_ids))

            if start_date:
                # 添加时间部分，确保包含整天
                start_dt = datetime.fromisoformat(start_date + 'T00:00:00')
                # 添加时区信息
                start_dt = start_dt.replace(tzinfo=CHINA_TZ)
                query = query.filter(LiveSession.start_time >= start_dt)

            if end_date:
                # 添加时间部分，确保包含整天
                end_dt = datetime.fromisoformat(end_date + 'T23:59:59.999999')
                # 添加时区信息
                end_dt = end_dt.replace(tzinfo=CHINA_TZ)
                query = query.filter(LiveSession.start_time <= end_dt)

            sessions = query.order_by(LiveSession.start_time.desc()).limit(limit).all()

            result = []
            for s in sessions:
                result.append({
                    'id': s.id,
                    'live_id': s.live_id,
                    'anchor_name': s.anchor_name,
                    'start_time': s.start_time.isoformat() if s.start_time else None,
                    'end_time': s.end_time.isoformat() if s.end_time else None,
                    'status': s.status,
                    'total_income': s.total_income,
                    'total_gift_count': s.total_gift_count,
                    'total_chat_count': s.total_chat_count,
                    'total_like_count': s.total_like_count,
                    'peak_viewer_count': s.peak_viewer_count
                })
            return result
        finally:
            session.close()

    def get_sessions_aggregated_stats(self, live_id: str = None, start_date: str = None, end_date: str = None) -> Dict:
        """获取按时间段聚合的直播统计数据"""
        session = self.get_session()
        try:
            query = session.query(LiveSession)
            related_ids = []

            if live_id:
                related_ids = self._related_live_ids(session, live_id)
                query = query.filter(LiveSession.live_id.in_(related_ids))

            if start_date:
                # 添加时间部分，确保包含整天
                start_dt = datetime.fromisoformat(start_date + 'T00:00:00')
                # 添加时区信息
                start_dt = start_dt.replace(tzinfo=CHINA_TZ)
                query = query.filter(LiveSession.start_time >= start_dt)

            if end_date:
                # 添加时间部分，确保包含整天
                end_dt = datetime.fromisoformat(end_date + 'T23:59:59.999999')
                # 添加时区信息
                end_dt = end_dt.replace(tzinfo=CHINA_TZ)
                query = query.filter(LiveSession.start_time <= end_dt)

            sessions = query.all()

            total_income = sum(s.total_income or 0 for s in sessions)
            total_gift_count = sum(s.total_gift_count or 0 for s in sessions)
            total_chat_count = sum(s.total_chat_count or 0 for s in sessions)
            total_like_count = sum(s.total_like_count or 0 for s in sessions)
            total_sessions = len(sessions)
            # Directive: 每个"场均"指标的分母用各自的"有效场次"，不要用 total_sessions。
            # 零值场次会拉低均值，且不同指标的稀疏程度不同（如未开播场次没有时长，
            # 未带货场次没有收入），分母必须按指标特化。新增 avg_gift_count / avg_chat_count
            # 时也请遵循同样规则。
            income_sessions = [s for s in sessions if (s.total_income or 0) > 0]
            like_sessions = [s for s in sessions if (s.total_like_count or 0) > 0]
            duration_sessions = [s for s in sessions if s.start_time]
            avg_income_per_active_session = (
                total_income / len(income_sessions)
                if income_sessions
                else 0
            )
            avg_likes_per_active_session = (
                total_like_count / len(like_sessions)
                if like_sessions
                else 0
            )
            live_sessions = sum(1 for s in sessions if s.status == 'live')
            ended_sessions = sum(1 for s in sessions if s.status == 'ended')
            peak_viewer_max = max((s.peak_viewer_count or 0) for s in sessions) if sessions else 0

            # 计算总时长。LiveSession.start_time 当前 schema 强制非空，
            # 这里仍保留 start_time 过滤作为防御性 guard：避免未来 schema 放开 nullable
            # 或外部工具直插脏数据时，分子分母不同源导致均值被稀释。
            total_duration_seconds = 0
            for s in duration_sessions:
                start = s.start_time
                if start.tzinfo is None:
                    start = start.replace(tzinfo=CHINA_TZ)

                end = s.end_time if s.end_time else get_china_now()
                if end.tzinfo is None:
                    end = end.replace(tzinfo=CHINA_TZ)

                total_duration_seconds += (end - start).total_seconds()

            avg_duration = (
                total_duration_seconds / len(duration_sessions)
                if duration_sessions
                else 0
            )
            trend_by_date = {}
            for s in sessions:
                if not s.start_time:
                    continue
                start = s.start_time
                if start.tzinfo is None:
                    start = start.replace(tzinfo=CHINA_TZ)
                date_key = start.astimezone(CHINA_TZ).date().isoformat()
                row = trend_by_date.setdefault(date_key, {
                    'date': date_key,
                    'sessions': 0,
                    'income': 0,
                    'gift_count': 0,
                    'chat_count': 0,
                    'like_count': 0,
                    'peak_viewer': 0,
                })
                row['sessions'] += 1
                row['income'] += s.total_income or 0
                row['gift_count'] += s.total_gift_count or 0
                row['chat_count'] += s.total_chat_count or 0
                row['like_count'] += s.total_like_count or 0
                row['peak_viewer'] = max(row['peak_viewer'], s.peak_viewer_count or 0)

            return {
                'total_sessions': total_sessions,
                'live_sessions': live_sessions,
                'ended_sessions': ended_sessions,
                'total_income': total_income,
                'total_gift_count': total_gift_count,
                'total_chat_count': total_chat_count,
                'total_like_count': total_like_count,
                'avg_income_per_active_session': avg_income_per_active_session,
                'avg_likes_per_active_session': avg_likes_per_active_session,
                'peak_viewer_max': peak_viewer_max,
                'total_duration_seconds': total_duration_seconds,
                'avg_duration_seconds': avg_duration,
                'trend': [trend_by_date[key] for key in sorted(trend_by_date.keys())],
                'related_live_ids': related_ids,
            }
        finally:
            session.close()

    def cleanup_stale_live_sessions(self, stale_threshold_hours: int = 24) -> int:
        """Use observed last activity, including snapshots and persisted updates."""
        session = self.get_session()
        try:
            now = get_china_now().replace(tzinfo=None)
            threshold = now - timedelta(hours=stale_threshold_hours)
            candidates = session.query(LiveSession).filter(LiveSession.status == 'live', LiveSession.start_time < threshold).with_for_update().all()
            count = 0
            for obj in candidates:
                last_chat = session.query(func.max(ChatMessage.created_at)).filter(ChatMessage.live_session_id == obj.id).scalar()
                last_gift = session.query(func.max(GiftMessage.created_at)).filter(GiftMessage.live_session_id == obj.id).scalar()
                next_start = session.query(func.min(LiveSession.start_time)).filter(
                    LiveSession.live_id == obj.live_id, LiveSession.start_time > obj.start_time).scalar()
                snapshot_filters = [RoomStats.live_id == obj.live_id, RoomStats.stats_at >= obj.start_time]
                if next_start:
                    snapshot_filters.append(RoomStats.stats_at < next_start)
                last_stats = session.query(func.max(RoomStats.stats_at)).filter(*snapshot_filters).scalar()
                activities = [v.replace(tzinfo=None) for v in [obj.start_time, obj.updated_at, last_chat, last_gift, last_stats] if v]
                last_activity = min(max(activities), now)
                if last_activity >= threshold:
                    continue
                cutoff = now - timedelta(days=config.DATA_RETENTION_DAYS)
                if config.DATA_RETENTION_DAYS == 0 or obj.start_time.replace(tzinfo=None) >= cutoff:
                    self._reconcile_session(session, obj)
                obj.status = 'ended'
                obj.end_time = last_activity
                obj.updated_at = get_china_now()
                count += 1
            session.commit()
            return count
        except Exception:
            session.rollback()
            logger.error('清理未结束场次失败')
            return 0
        finally:
            session.close()

    def get_user_messages(self, live_id: str = None, user_id: str = None, user_name: str = None, session_id: int = None,
                          start_date: str = None, end_date: str = None,
                          message_type: str = 'all', limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """
        获取用户在指定条件下的消息记录
        :param live_id: 房间ID（可选，None 表示所有房间）
        :param user_id: 用户ID（可选，榜单点击时用于精确查询）
        :param user_name: 用户名/昵称（可选，手动搜索时用于模糊查询）
        :param session_id: 场次ID（可选，指定场次）
        :param start_date: 开始日期（可选，格式：YYYY-MM-DD）
        :param end_date: 结束日期（可选，格式：YYYY-MM-DD）
        :param message_type: 消息类型 (all/chat/gift)
        :param limit: 每页数量
        :param offset: 偏移量
        :return: {'user': {...}, 'stats': {...}, 'messages': [...], 'pagination': {...}}
        """
        session = self.get_session()
        try:
            if not user_id and not user_name:
                raise ValueError('请提供 user_id 或 user_name')

            # 构建基础查询条件
            chat_conditions = []
            gift_conditions = []
            related_ids = self._related_live_ids(session, live_id) if live_id else []

            if related_ids:
                chat_conditions.append(ChatMessage.live_id.in_(related_ids))
                gift_conditions.append(GiftMessage.live_id.in_(related_ids))

            if user_id:
                chat_conditions.append(ChatMessage.user_id == user_id)
                gift_conditions.append(GiftMessage.user_id == user_id)
            else:
                keyword = f"%{self._escape_like(user_name.strip())}%"
                chat_conditions.append(ChatMessage.user_name.ilike(keyword, escape='\\'))
                gift_conditions.append(GiftMessage.user_name.ilike(keyword, escape='\\'))

            # 如果指定了场次ID，按场次筛选
            if session_id:
                chat_conditions.append(ChatMessage.live_session_id == session_id)
                gift_conditions.append(GiftMessage.live_session_id == session_id)
            # 如果指定了日期范围，按日期筛选
            else:
                if start_date:
                    start_datetime = datetime.strptime(start_date, '%Y-%m-%d').replace(
                        hour=0, minute=0, second=0, microsecond=0,
                        tzinfo=CHINA_TZ
                    )
                    chat_conditions.append(ChatMessage.created_at >= start_datetime)
                    gift_conditions.append(GiftMessage.created_at >= start_datetime)

                if end_date:
                    end_datetime = datetime.strptime(end_date, '%Y-%m-%d').replace(
                        hour=23, minute=59, second=59, microsecond=999999,
                        tzinfo=CHINA_TZ
                    )
                    chat_conditions.append(ChatMessage.created_at <= end_datetime)
                    gift_conditions.append(GiftMessage.created_at <= end_datetime)

            # 统计用户数据
            chat_count = session.query(func.count(ChatMessage.id)).filter(
                and_(*chat_conditions)
            ).scalar() or 0

            gift_count = session.query(func.count(GiftMessage.id)).filter(
                and_(*gift_conditions)
            ).scalar() or 0

            # 计算累计贡献值（礼物总价值）
            total_value = session.query(func.sum(GiftMessage.total_value)).filter(
                and_(*gift_conditions)
            ).scalar() or 0

            # 获取用户最高等级
            max_chat_level = session.query(func.max(ChatMessage.user_level)).filter(
                and_(*chat_conditions)
            ).scalar() or 0
            max_gift_level = session.query(func.max(GiftMessage.user_level)).filter(
                and_(*gift_conditions)
            ).scalar() or 0
            max_level = max(max_chat_level, max_gift_level)

            # 获取用户昵称（优先从最近的消息获取）
            latest_chat = session.query(ChatMessage).filter(
                and_(*chat_conditions)
            ).order_by(ChatMessage.created_at.desc()).first()
            latest_gift = session.query(GiftMessage).filter(
                and_(*gift_conditions)
            ).order_by(GiftMessage.created_at.desc()).first()

            display_name = user_name or user_id
            resolved_user_id = user_id
            if latest_chat:
                display_name = latest_chat.user_name
                resolved_user_id = latest_chat.user_id
            elif latest_gift:
                display_name = latest_gift.user_name
                resolved_user_id = latest_gift.user_id

            # 从 user_contributions 表获取用户头像
            contrib_conditions = []
            if related_ids:
                contrib_conditions.append(UserContribution.live_id.in_(related_ids))
            if resolved_user_id:
                contrib_conditions.append(UserContribution.user_id == resolved_user_id)
            elif user_name:
                contrib_conditions.append(UserContribution.user_name.ilike(f"%{self._escape_like(user_name.strip())}%", escape="\\"))

            user_contrib = None
            like_count = 0
            if contrib_conditions:
                contrib_rows = session.query(UserContribution).filter(
                    and_(*contrib_conditions)
                ).order_by(UserContribution.updated_at.desc()).all()
                user_contrib = contrib_rows[0] if contrib_rows else None
                like_count = 0 if session_id is not None else sum(int(row.like_count or 0) for row in contrib_rows)
            user_avatar = user_contrib.user_avatar if user_contrib else None
            if user_contrib and not latest_chat and not latest_gift:
                display_name = user_contrib.user_name
                resolved_user_id = user_contrib.user_id
                if session_id is None and not start_date and not end_date:
                    total_value = user_contrib.total_score or 0
                    gift_count = user_contrib.gift_count or 0
                    chat_count = user_contrib.chat_count or 0

            # 构建用户信息（含额外信息）
            user_info = {
                'user_id': resolved_user_id or user_id or '',
                'display_id': user_contrib.display_id if user_contrib else None,
                'nickname': display_name,
                'avatar': user_avatar,
                'level': max_level,
                'gender': user_contrib.gender if user_contrib else None,
                'follower_count': user_contrib.follower_count if user_contrib else None,
                'following_count': user_contrib.following_count if user_contrib else None,
                'age_range': user_contrib.age_range if user_contrib else None,
                'fans_club_level': user_contrib.fans_club_level if user_contrib else 0,
            }

            # 构建统计信息
            stats_info = {
                'total_messages': chat_count + gift_count,
                'chat_count': chat_count,
                'gift_count': gift_count,
                'like_count': int(like_count or 0),
                'total_value': int(total_value)
            }

            # 获取消息列表
            messages = []
            total = 0

            if message_type in ['all', 'chat']:
                chat_query = session.query(ChatMessage).filter(
                    and_(*chat_conditions)
                ).order_by(ChatMessage.created_at.desc())

                if message_type == 'chat':
                    # 只返回弹幕，需要计算总数
                    total = chat_count
                    chat_query = chat_query.offset(offset).limit(limit)

                    for msg in (chat_query.limit(offset + limit) if message_type == 'all' else chat_query).all():
                        messages.append({
                            'id': msg.id,
                            'type': 'chat',
                            'live_id': msg.live_id,
                            'anchor_name': msg.anchor_name,
                            'user_id': msg.user_id,
                            'nickname': msg.user_name,
                            'user_level': msg.user_level,
                            'content': msg.content,
                            'fans_club_level': msg.fans_club_level or 0,
                            'created_at': msg.created_at.isoformat() if msg.created_at else None
                        })
                else:
                    # 返回所有类型，稍后合并排序
                    for msg in (chat_query.limit(offset + limit) if message_type == 'all' else chat_query).all():
                        messages.append({
                            'id': msg.id,
                            'type': 'chat',
                            'live_id': msg.live_id,
                            'anchor_name': msg.anchor_name,
                            'user_id': msg.user_id,
                            'nickname': msg.user_name,
                            'user_level': msg.user_level,
                            'content': msg.content,
                            'fans_club_level': msg.fans_club_level or 0,
                            'created_at': msg.created_at.isoformat() if msg.created_at else None
                        })

            if message_type in ['all', 'gift']:
                gift_query = session.query(GiftMessage).filter(
                    and_(*gift_conditions)
                ).order_by(GiftMessage.created_at.desc())

                if message_type == 'gift':
                    # 只返回礼物
                    total = gift_count
                    gift_query = gift_query.offset(offset).limit(limit)

                    for msg in (gift_query.limit(offset + limit) if message_type == 'all' else gift_query).all():
                        messages.append({
                            'id': msg.id,
                            'type': 'gift',
                            'live_id': msg.live_id,
                            'anchor_name': msg.anchor_name,
                            'user_id': msg.user_id,
                            'nickname': msg.user_name,
                            'user_level': msg.user_level,
                            'gift_name': msg.gift_name,
                            'gift_count': msg.gift_count,
                            'diamond_count': msg.total_value,
                            'fans_club_level': msg.fans_club_level or 0,
                            'created_at': msg.created_at.isoformat() if msg.created_at else None
                        })
                else:
                    # 返回所有类型
                    for msg in (gift_query.limit(offset + limit) if message_type == 'all' else gift_query).all():
                        messages.append({
                            'id': msg.id,
                            'type': 'gift',
                            'live_id': msg.live_id,
                            'anchor_name': msg.anchor_name,
                            'user_id': msg.user_id,
                            'nickname': msg.user_name,
                            'user_level': msg.user_level,
                            'gift_name': msg.gift_name,
                            'gift_count': msg.gift_count,
                            'diamond_count': msg.total_value,
                            'fans_club_level': msg.fans_club_level or 0,
                            'created_at': msg.created_at.isoformat() if msg.created_at else None
                        })

            # 如果是 all 类型，需要合并排序后分页
            if message_type == 'all':
                total = chat_count + gift_count
                # 按时间排序
                messages.sort(key=lambda x: x['created_at'], reverse=True)
                # 分页
                messages = messages[offset:offset + limit]

            # 计算分页信息
            page_size = limit
            page = (offset // page_size) + 1 if page_size > 0 else 1
            total_pages = (total + page_size - 1) // page_size if total > 0 else 1

            pagination = {
                'total': total,
                'page': page,
                'page_size': page_size,
                'total_pages': total_pages
            }

            return {
                'user': user_info,
                'stats': stats_info,
                'messages': messages,
                'pagination': pagination
            }
        finally:
            session.close()

    def search_users_by_name(self, live_id: str = None, user_name: str = None,
                             session_id: int = None,
                             start_date: str = None, end_date: str = None,
                             limit: int = 30) -> List[Dict[str, Any]]:
        """按用户名搜索匹配用户列表，不返回消息详情。"""
        if user_name is None or user_name == '':
            return []

        session = self.get_session()
        try:
            related_ids = self._related_live_ids(session, live_id) if live_id else []
            if is_blank_search_query(user_name):
                chat_match = self._blank_name_condition(ChatMessage.user_name)
                gift_match = self._blank_name_condition(GiftMessage.user_name)
                contrib_match = self._blank_name_condition(UserContribution.user_name)
            else:
                clean_term = re.sub(r'^(?:id|抖音号)\s*[:：]\s*', '', user_name.strip(),
                                    flags=re.IGNORECASE).strip() or user_name.strip()
                keyword = f"%{self._escape_like(clean_term)}%"
                uid_query = session.query(UserContribution.user_id).filter(or_(
                    UserContribution.display_id.ilike(keyword, escape='\\'),
                    UserContribution.user_id.ilike(keyword, escape='\\')))
                if related_ids:
                    uid_query = uid_query.filter(UserContribution.live_id.in_(related_ids))
                matched_uids = [row.user_id for row in uid_query.all()]
                chat_match = or_(
                    ChatMessage.user_name.ilike(keyword, escape='\\'),
                    ChatMessage.user_id.ilike(keyword, escape='\\'),
                    *([ChatMessage.user_id.in_(matched_uids)] if matched_uids else []))
                gift_match = or_(
                    GiftMessage.user_name.ilike(keyword, escape='\\'),
                    GiftMessage.user_id.ilike(keyword, escape='\\'),
                    *([GiftMessage.user_id.in_(matched_uids)] if matched_uids else []))
                contrib_match = or_(
                    UserContribution.user_name.ilike(keyword, escape='\\'),
                    UserContribution.user_id.ilike(keyword, escape='\\'),
                    UserContribution.display_id.ilike(keyword, escape='\\'))
            chat_conditions = [chat_match]
            gift_conditions = [gift_match]

            if related_ids:
                chat_conditions.append(ChatMessage.live_id.in_(related_ids))
                gift_conditions.append(GiftMessage.live_id.in_(related_ids))

            if session_id:
                chat_conditions.append(ChatMessage.live_session_id == session_id)
                gift_conditions.append(GiftMessage.live_session_id == session_id)
            else:
                if start_date:
                    start_datetime = datetime.strptime(start_date, '%Y-%m-%d').replace(
                        hour=0, minute=0, second=0, microsecond=0,
                        tzinfo=CHINA_TZ
                    )
                    chat_conditions.append(ChatMessage.created_at >= start_datetime)
                    gift_conditions.append(GiftMessage.created_at >= start_datetime)

                if end_date:
                    end_datetime = datetime.strptime(end_date, '%Y-%m-%d').replace(
                        hour=23, minute=59, second=59, microsecond=999999,
                        tzinfo=CHINA_TZ
                    )
                    chat_conditions.append(ChatMessage.created_at <= end_datetime)
                    gift_conditions.append(GiftMessage.created_at <= end_datetime)

            users = {}

            chat_rows = session.query(
                ChatMessage.live_id,
                func.max(ChatMessage.anchor_name).label('anchor_name'),
                ChatMessage.user_id,
                func.max(ChatMessage.user_name).label('user_name'),
                func.max(ChatMessage.user_level).label('user_level'),
                func.max(ChatMessage.fans_club_level).label('fans_club_level'),
                func.count(ChatMessage.id).label('chat_count'),
                func.max(ChatMessage.created_at).label('last_seen_at')
            ).filter(
                and_(*chat_conditions)
            ).group_by(
                ChatMessage.live_id,
                ChatMessage.user_id
            ).all()

            gift_rows = session.query(
                GiftMessage.live_id,
                func.max(GiftMessage.anchor_name).label('anchor_name'),
                GiftMessage.user_id,
                func.max(GiftMessage.user_name).label('user_name'),
                func.max(GiftMessage.user_level).label('user_level'),
                func.max(GiftMessage.fans_club_level).label('fans_club_level'),
                func.sum(GiftMessage.gift_count).label('gift_count'),
                func.count(GiftMessage.id).label('gift_message_count'),
                func.sum(GiftMessage.total_value).label('total_value'),
                func.max(GiftMessage.created_at).label('last_seen_at')
            ).filter(
                and_(*gift_conditions)
            ).group_by(
                GiftMessage.live_id,
                GiftMessage.user_id
            ).all()

            def ensure_user(row):
                key = (row.live_id, row.user_id)
                if key not in users:
                    users[key] = {
                        'live_id': row.live_id,
                        'session_id': session_id,
                        'anchor_name': row.anchor_name,
                        'user_id': row.user_id,
                        'nickname': row.user_name,
                        'display_id': None,
                        'user_level': row.user_level or 0,
                        'fans_club_level': row.fans_club_level or 0,
                        'chat_count': 0,
                        'gift_count': 0,
                        'gift_message_count': 0,
                        'like_count': 0,
                        'total_value': 0,
                        'last_seen_at': row.last_seen_at,
                        'user_avatar': None,
                        'gender': None,
                        'follower_count': None,
                        'following_count': None,
                        'age_range': None,
                    }
                return users[key]

            for row in chat_rows:
                item = ensure_user(row)
                item['chat_count'] = int(row.chat_count or 0)
                item['user_level'] = max(item['user_level'] or 0, row.user_level or 0)
                item['fans_club_level'] = max(item['fans_club_level'] or 0, row.fans_club_level or 0)
                if row.last_seen_at and (not item['last_seen_at'] or row.last_seen_at > item['last_seen_at']):
                    item['last_seen_at'] = row.last_seen_at
                    item['nickname'] = row.user_name
                    item['anchor_name'] = row.anchor_name

            for row in gift_rows:
                item = ensure_user(row)
                item['gift_count'] = int(row.gift_count or 0)
                item['gift_message_count'] = int(row.gift_message_count or 0)
                item['total_value'] = int(row.total_value or 0)
                item['user_level'] = max(item['user_level'] or 0, row.user_level or 0)
                item['fans_club_level'] = max(item['fans_club_level'] or 0, row.fans_club_level or 0)
                if row.last_seen_at and (not item['last_seen_at'] or row.last_seen_at > item['last_seen_at']):
                    item['last_seen_at'] = row.last_seen_at
                    item['nickname'] = row.user_name
                    item['anchor_name'] = row.anchor_name

            if session_id is None:
                contrib_conditions = [contrib_match]
                if related_ids:
                    contrib_conditions.append(UserContribution.live_id.in_(related_ids))
                if start_date:
                    contrib_conditions.append(UserContribution.updated_at >= start_datetime)
                if end_date:
                    contrib_conditions.append(UserContribution.updated_at <= end_datetime)
                contrib_rows = session.query(UserContribution).filter(
                    and_(*contrib_conditions)
                ).order_by(UserContribution.total_score.desc(), UserContribution.updated_at.desc()).limit(limit).all()
                for row in contrib_rows:
                    if (row.live_id, row.user_id) in users:
                        continue
                    users[(row.live_id, row.user_id)] = {
                        'live_id': row.live_id,
                        'session_id': None,
                        'anchor_name': row.anchor_name,
                        'user_id': row.user_id,
                        'nickname': row.user_name,
                        'display_id': row.display_id,
                        'user_level': 0,
                        'fans_club_level': row.fans_club_level or 0,
                        'chat_count': 0 if start_date or end_date else row.chat_count or 0,
                        'gift_count': 0 if start_date or end_date else row.gift_count or 0,
                        'gift_message_count': 0 if start_date or end_date else row.gift_count or 0,
                        'like_count': row.like_count or 0,
                        'total_value': 0 if start_date or end_date else int(row.total_score or 0),
                        'last_seen_at': row.updated_at,
                        'user_avatar': row.user_avatar,
                        'gender': row.gender,
                        'follower_count': row.follower_count,
                        'following_count': row.following_count,
                        'age_range': row.age_range,
                    }

            display_updated_at = {}
            if users:
                user_ids = [item['user_id'] for item in users.values()]
                contrib_query = session.query(UserContribution).filter(UserContribution.user_id.in_(user_ids))
                if related_ids:
                    contrib_query = contrib_query.filter(UserContribution.live_id.in_(related_ids))
                for row in contrib_query.all():
                    key = (row.live_id, row.user_id)
                    if key in users:
                        display_updated_at[key] = row.updated_at
                        users[key].update({
                            'display_id': row.display_id,
                            'user_avatar': row.user_avatar,
                            'gender': row.gender,
                            'follower_count': row.follower_count,
                            'following_count': row.following_count,
                            'age_range': row.age_range,
                            'like_count': row.like_count or users[key].get('like_count', 0),
                            'fans_club_level': users[key]['fans_club_level'] or row.fans_club_level or 0,
                        })

            if live_id and related_ids:
                merged = {}
                merged_display_at = {}
                for item in users.values():
                    uid = item['user_id']
                    if uid not in merged:
                        cloned = dict(item)
                        cloned['live_id'] = live_id
                        merged[uid] = cloned
                        if item.get('display_id'):
                            merged_display_at[uid] = display_updated_at[(item['live_id'], uid)]
                        continue
                    existing = merged[uid]
                    if item.get('display_id'):
                        observed_at = display_updated_at[(item['live_id'], uid)]
                        if uid not in merged_display_at or observed_at >= merged_display_at[uid]:
                            existing['display_id'] = item['display_id']
                            merged_display_at[uid] = observed_at
                    existing['chat_count'] = (existing['chat_count'] or 0) + (item['chat_count'] or 0)
                    existing['gift_count'] = (existing['gift_count'] or 0) + (item['gift_count'] or 0)
                    existing['gift_message_count'] = existing.get('gift_message_count', 0) + item.get('gift_message_count', 0)
                    existing['like_count'] = (existing['like_count'] or 0) + (item['like_count'] or 0)
                    existing['total_value'] = (existing['total_value'] or 0) + (item['total_value'] or 0)
                    existing['user_level'] = max(existing['user_level'] or 0, item['user_level'] or 0)
                    existing['fans_club_level'] = max(existing['fans_club_level'] or 0, item['fans_club_level'] or 0)
                    if item['last_seen_at'] and (
                        not existing['last_seen_at'] or item['last_seen_at'] > existing['last_seen_at']
                    ):
                        existing['last_seen_at'] = item['last_seen_at']
                        existing['nickname'] = item['nickname']
                        existing['anchor_name'] = item['anchor_name']
                        if item.get('user_avatar'):
                            existing['user_avatar'] = item['user_avatar']
                users = {(live_id, uid): value for uid, value in merged.items()}

            results = sorted(
                users.values(),
                key=lambda item: (
                    item['total_value'] or 0,
                    (item['chat_count'] or 0) + (item['gift_count'] or 0) + (item['like_count'] or 0),
                    item['last_seen_at'] or datetime.min.replace(tzinfo=CHINA_TZ)
                ),
                reverse=True
            )[:limit]

            for item in results:
                item['total_messages'] = (item['chat_count'] or 0) + item.pop('gift_message_count', item['gift_count'] or 0)
                item['last_seen_at'] = item['last_seen_at'].isoformat() if item['last_seen_at'] else None

            return results
        finally:
            session.close()

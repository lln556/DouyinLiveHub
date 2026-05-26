"""
数据服务层
封装所有数据库操作
"""
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from sqlalchemy import create_engine, and_, or_, func, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

import config
from models.database import Base, LiveRoom, ChatMessage, GiftMessage, RoomStats, UserContribution, SystemEvent, LiveSession, get_china_now, CHINA_TZ
from utils.logger import get_logger

logger = get_logger("data_service")


class DataService:
    """封装所有数据库操作"""

    def __init__(self, database_url: str = None):
        """
        初始化数据服务
        :param database_url: 数据库连接URL
        """
        self.database_url = database_url or config.DATABASE_URL
        self.engine = create_engine(
            self.database_url,
            echo=config.SQLALCHEMY_ECHO,
            pool_pre_ping=True,
            pool_recycle=3600
        )
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
            return self.get_live_room_by_live_id(live_id)
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
        """获取统计摘要"""
        session = self.get_session()
        try:
            active_condition = and_(LiveRoom.archived_at.is_(None), LiveRoom.status != 'archived')
            archived_condition = or_(LiveRoom.archived_at.isnot(None), LiveRoom.status == 'archived')
            total_rooms = session.query(func.count(LiveRoom.live_id)).filter(active_condition).scalar()
            monitoring_rooms = session.query(func.count(LiveRoom.live_id)).filter(active_condition, LiveRoom.status == 'monitoring').scalar()
            h24_rooms = session.query(func.count(LiveRoom.live_id)).filter(active_condition, LiveRoom.monitor_type == '24h').scalar()
            archived_rooms = session.query(func.count(LiveRoom.live_id)).filter(archived_condition).scalar()

            return {
                'total_rooms': total_rooms or 0,
                'monitoring_rooms': monitoring_rooms or 0,
                'h24_rooms': h24_rooms or 0,
                'stopped_rooms': (total_rooms or 0) - (monitoring_rooms or 0),
                'archived_rooms': archived_rooms or 0
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

            result = []
            for room in rooms:
                session_agg = session.query(
                    func.count(LiveSession.id),
                    func.coalesce(func.sum(LiveSession.total_income), 0),
                    func.coalesce(func.sum(LiveSession.total_gift_count), 0),
                    func.coalesce(func.sum(LiveSession.total_chat_count), 0),
                    func.coalesce(func.sum(LiveSession.total_like_count), 0),
                    func.min(LiveSession.start_time),
                    func.max(func.coalesce(LiveSession.end_time, LiveSession.start_time))
                ).filter(LiveSession.live_id == room.live_id).one()

                chat_agg = session.query(
                    func.count(ChatMessage.id),
                    func.min(ChatMessage.created_at),
                    func.max(ChatMessage.created_at)
                ).filter(ChatMessage.live_id == room.live_id).one()

                gift_agg = session.query(
                    func.coalesce(func.sum(GiftMessage.total_value), 0),
                    func.coalesce(func.sum(GiftMessage.gift_count), 0),
                    func.min(GiftMessage.created_at),
                    func.max(GiftMessage.created_at)
                ).filter(GiftMessage.live_id == room.live_id).one()

                stats_agg = session.query(
                    func.min(RoomStats.stats_at),
                    func.max(RoomStats.stats_at)
                ).filter(RoomStats.live_id == room.live_id).one()

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
                    'total_income': gift_income if gift_income > 0 else session_income,
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

    def get_all_messages(self, live_id: str, limit: int = 100, offset: int = 0) -> List[Dict]:
        """获取所有消息（弹幕和礼物混合）"""
        session = self.get_session()
        try:
            # 使用原生SQL查询合并两种消息，支持分页
            sql = text("""
                SELECT 'chat' as type, id, user_name, user_level, content as display_content,
                       NULL as gift_name, NULL as gift_count, NULL as total_value, created_at
                FROM chat_messages
                WHERE live_id = :live_id
                UNION ALL
                SELECT 'gift' as type, id, user_name, user_level,
                       CONCAT(user_name, ' 赠送了 ', gift_name, 'x', gift_count) as display_content,
                       gift_name, gift_count, total_value, created_at
                FROM gift_messages
                WHERE live_id = :live_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """)
            result = session.execute(sql, {'live_id': live_id, 'limit': limit, 'offset': offset})
            # SQLAlchemy 2.0 兼容方式转换 Row 为 Dict
            return [row._asdict() if hasattr(row, '_asdict') else dict(row._mapping) for row in result]
        finally:
            session.close()

    # ==================== 统计操作 ====================

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
                                 fans_club_level: int = None) -> UserContribution:
        """更新用户贡献"""
        session = self.get_session()
        try:
            contribution = session.query(UserContribution).filter(
                and_(
                    UserContribution.live_id == live_id,
                    UserContribution.user_id == user_id
                )
            ).first()

            if contribution:
                contribution.total_score += gift_value
                contribution.gift_count += gift_count
                contribution.chat_count += chat_count
                contribution.like_count += like_count
                if user_avatar:
                    contribution.user_avatar = user_avatar
                contribution.user_name = user_name  # 更新用户名
                if anchor_name:
                    contribution.anchor_name = anchor_name  # 更新主播名
                # 更新用户额外信息（只在有值时更新）
                if gender is not None and gender > 0:
                    contribution.gender = gender
                if follower_count is not None and follower_count > 0:
                    contribution.follower_count = follower_count
                if following_count is not None and following_count > 0:
                    contribution.following_count = following_count
                if age_range is not None and age_range > 0:
                    contribution.age_range = age_range
                if fans_club_level is not None and fans_club_level > 0:
                    contribution.fans_club_level = fans_club_level
                contribution.updated_at = get_china_now()
            else:
                contribution = UserContribution(
                    live_id=live_id,
                    anchor_name=anchor_name,
                    user_id=user_id,
                    user_name=user_name,
                    total_score=gift_value,
                    gift_count=gift_count,
                    chat_count=chat_count,
                    like_count=like_count,
                    user_avatar=user_avatar,
                    gender=gender if gender and gender > 0 else None,
                    follower_count=follower_count if follower_count and follower_count > 0 else None,
                    following_count=following_count if following_count and following_count > 0 else None,
                    age_range=age_range if age_range and age_range > 0 else None,
                    fans_club_level=fans_club_level if fans_club_level and fans_club_level > 0 else 0
                )
                session.add(contribution)

            session.commit()
            session.refresh(contribution)
            return contribution
        except Exception as e:
            session.rollback()
            print(f"更新用户贡献失败: {e}")
            return None
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

    def get_contributors_by_date_range(self, live_id: str = None, start_date: str = None, end_date: str = None, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """
        按日期范围获取贡献榜（从礼物消息中聚合）
        :param live_id: 房间ID，None 表示所有房间
        :param start_date: 开始日期 (YYYY-MM-DD)
        :param end_date: 结束日期 (YYYY-MM-DD)
        :param page: 页码（从1开始）
        :param page_size: 每页数量
        :return: {'contributors': [...], 'total': 总数, 'page': 当前页, 'page_size': 每页数量, 'total_pages': 总页数}
        """
        session = self.get_session()
        try:
            # 构建查询条件
            conditions = []

            if live_id:
                conditions.append(GiftMessage.live_id == live_id)

            if start_date:
                start_datetime = datetime.strptime(start_date, '%Y-%m-%d').replace(
                    hour=0, minute=0, second=0, microsecond=0,
                    tzinfo=CHINA_TZ
                )
                conditions.append(GiftMessage.created_at >= start_datetime)

            if end_date:
                end_datetime = datetime.strptime(end_date, '%Y-%m-%d').replace(
                    hour=23, minute=59, second=59, microsecond=999999,
                    tzinfo=CHINA_TZ
                )
                conditions.append(GiftMessage.created_at <= end_datetime)

            # 先获取总数（不分组）
            count_query = session.query(
                func.count(func.distinct(
                    func.concat(GiftMessage.live_id, '_', GiftMessage.user_id)
                ))
            )
            if conditions:
                count_query = count_query.filter(and_(*conditions))
            total = count_query.scalar() or 0

            # 计算分页
            total_pages = (total + page_size - 1) // page_size if total > 0 else 1
            offset = (page - 1) * page_size

            # 聚合查询：按用户ID统计礼物贡献，昵称只作为展示信息。
            # 用户改名后历史礼物流水保留旧昵称，不能把昵称放进分组键，否则同一人会被拆成多条榜单记录。
            # 使用子查询获取聚合数据
            subquery = session.query(
                GiftMessage.live_id,
                func.max(GiftMessage.anchor_name).label('anchor_name'),
                GiftMessage.user_id,
                func.sum(GiftMessage.total_value).label('contribution_value'),
                func.count(GiftMessage.id).label('gift_count'),
                func.min(GiftMessage.user_level).label('user_level')
            )

            if conditions:
                subquery = subquery.filter(and_(*conditions))

            subquery = subquery.group_by(
                GiftMessage.live_id,
                GiftMessage.user_id,
            ).order_by(func.sum(GiftMessage.total_value).desc())

            # 添加分页到子查询
            subquery = subquery.limit(page_size).offset(offset)

            # 执行查询
            results = subquery.all()

            # 转换为字典列表，并从 user_contributions 表获取头像
            contributors = []
            for row in results:
                # 从 user_contributions 表获取用户头像
                user_contrib = session.query(UserContribution).filter(
                    and_(
                        UserContribution.live_id == row.live_id,
                        UserContribution.user_id == row.user_id
                    )
                ).first()

                message_identity_conditions = [
                    ChatMessage.live_id == row.live_id,
                    ChatMessage.user_id == row.user_id
                ]
                gift_identity_conditions = [
                    GiftMessage.live_id == row.live_id,
                    GiftMessage.user_id == row.user_id
                ]
                if start_date:
                    message_identity_conditions.append(ChatMessage.created_at >= start_datetime)
                    gift_identity_conditions.append(GiftMessage.created_at >= start_datetime)
                if end_date:
                    message_identity_conditions.append(ChatMessage.created_at <= end_datetime)
                    gift_identity_conditions.append(GiftMessage.created_at <= end_datetime)

                latest_chat = session.query(ChatMessage).filter(
                    and_(*message_identity_conditions)
                ).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).first()
                latest_gift = session.query(GiftMessage).filter(
                    and_(*gift_identity_conditions)
                ).order_by(GiftMessage.created_at.desc(), GiftMessage.id.desc()).first()

                latest_message = None
                if latest_chat and latest_gift:
                    latest_message = latest_chat if latest_chat.created_at >= latest_gift.created_at else latest_gift
                else:
                    latest_message = latest_chat or latest_gift

                nickname = (
                    latest_message.user_name if latest_message else
                    user_contrib.user_name if user_contrib else
                    row.user_id
                )

                anchor_name = (
                    latest_message.anchor_name if latest_message and latest_message.anchor_name else
                    row.anchor_name
                )

                # 计算弹幕数
                chat_conditions = [ChatMessage.user_id == row.user_id]
                if live_id:
                    chat_conditions.append(ChatMessage.live_id == live_id)
                else:
                    chat_conditions.append(ChatMessage.live_id == row.live_id)
                if start_date:
                    chat_conditions.append(ChatMessage.created_at >= start_datetime)
                if end_date:
                    chat_conditions.append(ChatMessage.created_at <= end_datetime)

                chat_count = session.query(func.count(ChatMessage.id)).filter(
                    and_(*chat_conditions)
                ).scalar() or 0

                contributors.append({
                    'live_id': row.live_id,
                    'anchor_name': anchor_name,
                    'user_id': row.user_id,
                    'nickname': nickname,
                    'contribution_value': int(row.contribution_value),
                    'gift_count': int(row.gift_count),
                    'chat_count': chat_count,
                    'like_count': int(user_contrib.like_count or 0) if user_contrib else 0,
                    'user_avatar': user_contrib.user_avatar if user_contrib else None,
                    'user_level': row.user_level,
                    'fans_club_level': user_contrib.fans_club_level if user_contrib else 0
                })

            return {
                'contributors': contributors,
                'total': total,
                'page': page,
                'page_size': page_size,
                'total_pages': total_pages
            }
        finally:
            session.close()

    def get_summary_contributors(self, live_id: str = None, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """
        获取总贡献榜（从累计汇总表读取）。
        旧礼物明细可能被数据保留策略清理，但 user_contributions 保留了累计总贡献。
        """
        session = self.get_session()
        try:
            conditions = [UserContribution.total_score > 0]
            if live_id:
                conditions.append(UserContribution.live_id == live_id)

            total = session.query(func.count(UserContribution.id)).filter(
                and_(*conditions)
            ).scalar() or 0

            total_pages = (total + page_size - 1) // page_size if total > 0 else 1
            offset = (page - 1) * page_size

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
                contributors.append({
                    'live_id': row.live_id,
                    'anchor_name': row.anchor_name,
                    'user_id': row.user_id,
                    'nickname': row.user_name,
                    'contribution_value': int(row.total_score or 0),
                    'gift_count': int(row.gift_count or 0),
                    'chat_count': max(int(row.chat_count or 0), extra.get('chat_count', 0)),
                    'like_count': int(row.like_count or 0),
                    'user_avatar': row.user_avatar,
                    'user_level': extra.get('user_level', 0),
                    'fans_club_level': row.fans_club_level or 0
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
        """
        session = self.get_session()
        try:
            conditions = [UserContribution.like_count > 0]
            if live_id:
                conditions.append(UserContribution.live_id == live_id)

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
        session = self.get_session()
        try:
            conditions = []
            if live_id:
                conditions.append(LiveSession.live_id == live_id)
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
                'min_date': min_date_query.strftime('%Y-%m-%d') if min_date_query else None,
                'max_date': max_date_query.strftime('%Y-%m-%d') if max_date_query else None
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
                func.count(GiftMessage.id).label('gift_count')
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
                    UserContribution.user_avatar,
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
                        'avatar': r.user_avatar,
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
                    'nickname': row.user_name or '',
                    'contribution_value': float(row.total_score),
                    'gift_count': row.gift_count,
                    'like_count': int(extra.get('like_count') or 0),
                    'user_level': row.user_level,
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
                    'user_id': msg.user_id,
                    'nickname': msg.user_name,
                    'user_level': msg.user_level,
                    'content': msg.content,
                    'fans_club_level': msg.fans_club_level or 0,
                    'created_at': msg.created_at.isoformat() if msg.created_at else None
                } for msg in messages]

            elif message_type == 'gift':
                messages = session.query(GiftMessage).filter(
                    GiftMessage.live_session_id == session_id
                ).order_by(GiftMessage.created_at.desc()).offset(offset).limit(limit).all()

                return [{
                    'id': msg.id,
                    'user_id': msg.user_id,
                    'nickname': msg.user_name,
                    'user_level': msg.user_level,
                    'gift_name': msg.gift_name,
                    'gift_count': msg.gift_count,
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
        """清理旧数据"""
        retention_days = retention_days or config.DATA_RETENTION_DAYS
        if retention_days == 0:
            return {'message': '数据保留设置为永久保留，不清理'}

        cutoff_date = get_china_now() - timedelta(days=retention_days)
        session = self.get_session()
        try:
            chat_deleted = session.query(ChatMessage).filter(
                ChatMessage.created_at < cutoff_date
            ).delete()
            gift_deleted = session.query(GiftMessage).filter(
                GiftMessage.created_at < cutoff_date
            ).delete()
            stats_deleted = session.query(RoomStats).filter(
                RoomStats.stats_at < cutoff_date
            ).delete()
            event_deleted = session.query(SystemEvent).filter(
                SystemEvent.created_at < cutoff_date
            ).delete()

            session.commit()
            return {
                'chat_messages_deleted': chat_deleted,
                'gift_messages_deleted': gift_deleted,
                'stats_deleted': stats_deleted,
                'events_deleted': event_deleted,
                'cutoff_date': cutoff_date.isoformat()
            }
        except Exception as e:
            session.rollback()
            print(f"清理旧数据失败: {e}")
            return {'error': str(e)}
        finally:
            session.close()

    # ==================== 直播场次操作 ====================

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
                    return existing_session

            session_obj = LiveSession(live_id=live_id, anchor_name=anchor_name, **kwargs)
            session.add(session_obj)
            session.commit()
            session.refresh(session_obj)
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
        """结束直播场次"""
        session = self.get_session()
        try:
            session_obj = session.query(LiveSession).filter(LiveSession.id == session_id).first()
            if session_obj:
                already_ended = session_obj.status == 'ended'
                if not already_ended:
                    session_obj.status = 'ended'
                    session_obj.end_time = get_china_now()
                elif session_obj.end_time is None:
                    session_obj.end_time = get_china_now()

                if peak_viewer_count is not None:
                    session_obj.peak_viewer_count = max(session_obj.peak_viewer_count or 0, peak_viewer_count)

                # 从 gift_messages 表重新聚合统计，校正增量累加可能产生的误差
                gift_agg = session.query(
                    func.coalesce(func.sum(GiftMessage.total_value), 0),
                    func.coalesce(func.sum(GiftMessage.gift_count), 0)
                ).filter(
                    GiftMessage.live_session_id == session_id
                ).one()
                reconciled_income = int(gift_agg[0])
                reconciled_gift_count = int(gift_agg[1])

                if reconciled_income != session_obj.total_income or reconciled_gift_count != session_obj.total_gift_count:
                    logger.info(
                        f"场次统计校正: session_id={session_id}, "
                        f"income {session_obj.total_income} -> {reconciled_income}, "
                        f"gift_count {session_obj.total_gift_count} -> {reconciled_gift_count}"
                    )
                    session_obj.total_income = reconciled_income
                    session_obj.total_gift_count = reconciled_gift_count

                session_obj.updated_at = get_china_now()
                session.commit()
                return True
            return False
        except Exception as e:
            session.rollback()
            logger.error(f"结束直播场次失败: {e}")
            return False
        finally:
            session.close()

    def update_session_stats(self, session_id: int, **kwargs) -> bool:
        """更新直播场次统计"""
        session = self.get_session()
        try:
            session_obj = session.query(LiveSession).filter(LiveSession.id == session_id).first()
            if session_obj:
                for key, value in kwargs.items():
                    if hasattr(session_obj, key):
                        setattr(session_obj, key, value)
                session_obj.updated_at = get_china_now()
                session.commit()
                return True
            return False
        except Exception as e:
            session.rollback()
            logger.error(f"更新直播场次统计失败: {e}")
            return False
        finally:
            session.close()

    def increment_session_stats(self, session_id: int, income_delta: float = 0,
                               gift_count_delta: int = 0, chat_count_delta: int = 0,
                               like_count_delta: int = 0) -> bool:
        """增量更新直播场次统计"""
        session = self.get_session()
        try:
            session_obj = session.query(LiveSession).filter(LiveSession.id == session_id).first()
            if session_obj:
                session_obj.total_income += income_delta
                session_obj.total_gift_count += gift_count_delta
                session_obj.total_chat_count += chat_count_delta
                session_obj.total_like_count += like_count_delta
                session_obj.updated_at = get_china_now()
                session.commit()
                return True
            return False
        except Exception as e:
            session.rollback()
            logger.error(f"增量更新直播场次统计失败: {e}")
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
            query = session.query(LiveSession).filter(LiveSession.live_id == live_id)

            if start_date:
                # 添加时间部分，确保包含整天
                start_dt = datetime.fromisoformat(start_date + 'T00:00:00')
                # 添加时区信息
                start_dt = start_dt.replace(tzinfo=CHINA_TZ)
                query = query.filter(LiveSession.start_time >= start_dt)

            if end_date:
                # 添加时间部分，确保包含整天
                end_dt = datetime.fromisoformat(end_date + 'T23:59:59')
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

            if live_id:
                query = query.filter(LiveSession.live_id == live_id)

            if start_date:
                # 添加时间部分，确保包含整天
                start_dt = datetime.fromisoformat(start_date + 'T00:00:00')
                # 添加时区信息
                start_dt = start_dt.replace(tzinfo=CHINA_TZ)
                query = query.filter(LiveSession.start_time >= start_dt)

            if end_date:
                # 添加时间部分，确保包含整天
                end_dt = datetime.fromisoformat(end_date + 'T23:59:59')
                # 添加时区信息
                end_dt = end_dt.replace(tzinfo=CHINA_TZ)
                query = query.filter(LiveSession.start_time <= end_dt)

            sessions = query.all()

            total_income = sum(s.total_income or 0 for s in sessions)
            total_gift_count = sum(s.total_gift_count or 0 for s in sessions)
            total_chat_count = sum(s.total_chat_count or 0 for s in sessions)
            total_like_count = sum(s.total_like_count or 0 for s in sessions)
            total_sessions = len(sessions)
            live_sessions = sum(1 for s in sessions if s.status == 'live')
            ended_sessions = sum(1 for s in sessions if s.status == 'ended')
            peak_viewer_max = max((s.peak_viewer_count or 0) for s in sessions) if sessions else 0

            # 计算总时长
            total_duration_seconds = 0
            for s in sessions:
                if s.start_time:
                    # 确保 start_time 和 end_time 都是带时区的
                    start = s.start_time
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=CHINA_TZ)

                    end = s.end_time if s.end_time else get_china_now()
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=CHINA_TZ)

                    total_duration_seconds += (end - start).total_seconds()

            avg_duration = total_duration_seconds / total_sessions if total_sessions > 0 else 0

            return {
                'total_sessions': total_sessions,
                'live_sessions': live_sessions,
                'ended_sessions': ended_sessions,
                'total_income': total_income,
                'total_gift_count': total_gift_count,
                'total_chat_count': total_chat_count,
                'total_like_count': total_like_count,
                'peak_viewer_max': peak_viewer_max,
                'total_duration_seconds': total_duration_seconds,
                'avg_duration_seconds': avg_duration
            }
        finally:
            session.close()

    def cleanup_stale_live_sessions(self, stale_threshold_hours: int = 24) -> int:
        """
        清理长时间处于 'live' 状态但实际已结束的场次
        :param stale_threshold_hours: 超过多少小时的 'live' 场次被认为已结束，默认24小时
        :return: 清理的场次数量
        """
        from models.database import get_china_now
        from datetime import timedelta

        session = self.get_session()
        try:
            # 计算阈值时间
            threshold_time = get_china_now() - timedelta(hours=stale_threshold_hours)

            # 查找所有超过阈值时间且状态仍为 'live' 的场次
            stale_sessions = session.query(LiveSession).filter(
                and_(
                    LiveSession.status == 'live',
                    LiveSession.start_time < threshold_time
                )
            ).all()

            count = 0
            for stale_session in stale_sessions:
                stale_session.status = 'ended'
                stale_session.end_time = stale_session.start_time + timedelta(hours=2)  # 假设直播2小时后结束
                stale_session.updated_at = get_china_now()
                count += 1
                logger.info(f"清理未结束的直播场次: id={stale_session.id}, live_id={stale_session.live_id}, start_time={stale_session.start_time}")

            session.commit()
            return count
        except Exception as e:
            session.rollback()
            logger.error(f"清理未结束场次失败: {e}")
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

            if live_id:
                chat_conditions.append(ChatMessage.live_id == live_id)
                gift_conditions.append(GiftMessage.live_id == live_id)

            if user_id:
                chat_conditions.append(ChatMessage.user_id == user_id)
                gift_conditions.append(GiftMessage.user_id == user_id)
            else:
                keyword = f"%{user_name.strip()}%"
                chat_conditions.append(ChatMessage.user_name.ilike(keyword))
                gift_conditions.append(GiftMessage.user_name.ilike(keyword))

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
            if live_id:
                contrib_conditions.append(UserContribution.live_id == live_id)
            if resolved_user_id:
                contrib_conditions.append(UserContribution.user_id == resolved_user_id)
            elif user_name:
                contrib_conditions.append(UserContribution.user_name.ilike(f"%{user_name.strip()}%"))

            user_contrib = None
            if contrib_conditions:
                user_contrib = session.query(UserContribution).filter(
                    and_(*contrib_conditions)
                ).order_by(UserContribution.updated_at.desc()).first()
            user_avatar = user_contrib.user_avatar if user_contrib else None
            like_count = user_contrib.like_count if user_contrib else 0
            if user_contrib and not latest_chat and not latest_gift:
                display_name = user_contrib.user_name
                resolved_user_id = user_contrib.user_id
                total_value = user_contrib.total_score or 0
                gift_count = user_contrib.gift_count or 0
                chat_count = user_contrib.chat_count or 0

            # 构建用户信息（含额外信息）
            user_info = {
                'user_id': resolved_user_id or user_id or '',
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

                    for msg in chat_query.all():
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
                    for msg in chat_query.all():
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

                    for msg in gift_query.all():
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
                    for msg in gift_query.all():
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
        if not user_name or not user_name.strip():
            return []

        session = self.get_session()
        try:
            keyword = f"%{user_name.strip()}%"
            chat_conditions = [ChatMessage.user_name.ilike(keyword)]
            gift_conditions = [GiftMessage.user_name.ilike(keyword)]

            if live_id:
                chat_conditions.append(ChatMessage.live_id == live_id)
                gift_conditions.append(GiftMessage.live_id == live_id)

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
                func.count(GiftMessage.id).label('gift_count'),
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
                        'user_level': row.user_level or 0,
                        'fans_club_level': row.fans_club_level or 0,
                        'chat_count': 0,
                        'gift_count': 0,
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
                item['total_value'] = int(row.total_value or 0)
                item['user_level'] = max(item['user_level'] or 0, row.user_level or 0)
                item['fans_club_level'] = max(item['fans_club_level'] or 0, row.fans_club_level or 0)
                if row.last_seen_at and (not item['last_seen_at'] or row.last_seen_at > item['last_seen_at']):
                    item['last_seen_at'] = row.last_seen_at
                    item['nickname'] = row.user_name
                    item['anchor_name'] = row.anchor_name

            if not users and not session_id and not start_date and not end_date:
                contrib_conditions = [UserContribution.user_name.ilike(keyword)]
                if live_id:
                    contrib_conditions.append(UserContribution.live_id == live_id)
                contrib_rows = session.query(UserContribution).filter(
                    and_(*contrib_conditions)
                ).order_by(UserContribution.total_score.desc()).limit(limit).all()
                for row in contrib_rows:
                    users[(row.live_id, row.user_id)] = {
                        'live_id': row.live_id,
                        'session_id': None,
                        'anchor_name': row.anchor_name,
                        'user_id': row.user_id,
                        'nickname': row.user_name,
                        'user_level': 0,
                        'fans_club_level': row.fans_club_level or 0,
                        'chat_count': row.chat_count or 0,
                        'gift_count': row.gift_count or 0,
                        'like_count': row.like_count or 0,
                        'total_value': int(row.total_score or 0),
                        'last_seen_at': row.updated_at,
                        'user_avatar': row.user_avatar,
                        'gender': row.gender,
                        'follower_count': row.follower_count,
                        'following_count': row.following_count,
                        'age_range': row.age_range,
                    }

            if users:
                user_ids = [item['user_id'] for item in users.values()]
                contrib_query = session.query(UserContribution).filter(UserContribution.user_id.in_(user_ids))
                if live_id:
                    contrib_query = contrib_query.filter(UserContribution.live_id == live_id)
                for row in contrib_query.all():
                    key = (row.live_id, row.user_id)
                    if key in users:
                        users[key].update({
                            'user_avatar': row.user_avatar,
                            'gender': row.gender,
                            'follower_count': row.follower_count,
                            'following_count': row.following_count,
                            'age_range': row.age_range,
                            'like_count': row.like_count or users[key].get('like_count', 0),
                            'fans_club_level': users[key]['fans_club_level'] or row.fans_club_level or 0,
                        })

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
                item['total_messages'] = (item['chat_count'] or 0) + (item['gift_count'] or 0)
                item['last_seen_at'] = item['last_seen_at'].isoformat() if item['last_seen_at'] else None

            return results
        finally:
            session.close()

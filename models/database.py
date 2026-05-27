"""
数据库模型
使用SQLAlchemy ORM定义所有数据表结构
"""
from datetime import datetime, timezone, timedelta
from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, Float, Text,
    ForeignKey, Index, UniqueConstraint, JSON as SQLAlchemyJSON
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

# 定义东八区时间
CHINA_TZ = timezone(timedelta(hours=8))

def get_china_now():
    """获取当前东八区时间"""
    return datetime.now(CHINA_TZ)

Base = declarative_base()


class LiveRoom(Base):
    """直播间表"""
    __tablename__ = 'live_rooms'

    live_id = Column(String(50), primary_key=True, comment='直播间ID')
    anchor_name = Column(String(100), nullable=False, comment='主播名称')
    anchor_id = Column(String(50), nullable=True, comment='主播ID')
    status = Column(String(20), nullable=False, default='stopped', comment='监控状态: monitoring/stopped/offline/error/archived')
    monitor_type = Column(String(10), nullable=False, default='manual', comment='监控类型: 24h/manual')
    auto_reconnect = Column(Boolean, nullable=False, default=False, comment='是否自动重连')
    reconnect_count = Column(Integer, nullable=False, default=0, comment='重连次数')
    last_connect_time = Column(DateTime, nullable=True, comment='最后连接时间')
    last_disconnect_time = Column(DateTime, nullable=True, comment='最后断开时间')
    error_message = Column(Text, nullable=True, comment='错误信息')
    archived_at = Column(DateTime, nullable=True, index=True, comment='归档时间，为空表示显示在主页')
    created_at = Column(DateTime, nullable=False, default=get_china_now, comment='创建时间')
    updated_at = Column(DateTime, nullable=False, default=get_china_now, onupdate=get_china_now, comment='更新时间')

    # 关系
    chat_messages = relationship('ChatMessage', back_populates='live_room', cascade='all, delete-orphan')
    gift_messages = relationship('GiftMessage', back_populates='live_room', cascade='all, delete-orphan')
    room_stats = relationship('RoomStats', back_populates='live_room', cascade='all, delete-orphan')
    user_contributions = relationship('UserContribution', back_populates='live_room', cascade='all, delete-orphan')
    system_events = relationship('SystemEvent', back_populates='live_room', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<LiveRoom(live_id={self.live_id}, anchor_name={self.anchor_name}, status={self.status})>'


class ChatMessage(Base):
    """弹幕记录表"""
    __tablename__ = 'chat_messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    live_id = Column(String(50), ForeignKey('live_rooms.live_id', ondelete='CASCADE'), nullable=False, index=True, comment='直播间ID')
    anchor_name = Column(String(100), nullable=True, comment='主播名称')
    live_session_id = Column(Integer, ForeignKey('live_sessions.id', ondelete='SET NULL'), nullable=True, index=True, comment='直播场次ID')
    user_id = Column(String(50), nullable=False, index=True, comment='用户ID')
    user_name = Column(String(100), nullable=False, comment='用户名称')
    user_level = Column(Integer, nullable=True, comment='用户等级')
    content = Column(Text, nullable=False, comment='弹幕内容')
    is_gift_user = Column(Boolean, nullable=False, default=False, comment='是否是送礼用户')
    fans_club_level = Column(Integer, nullable=True, default=0, comment='粉丝团等级')
    created_at = Column(DateTime, nullable=False, default=get_china_now, index=True, comment='创建时间')

    # 关系
    live_room = relationship('LiveRoom', back_populates='chat_messages')

    # 索引
    __table_args__ = (
        Index('idx_chat_room_time', 'live_id', 'created_at'),
    )

    def __repr__(self):
        return f'<ChatMessage(user={self.user_name}, content={self.content[:20]})>'


class GiftMessage(Base):
    """礼物记录表"""
    __tablename__ = 'gift_messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    live_id = Column(String(50), ForeignKey('live_rooms.live_id', ondelete='CASCADE'), nullable=False, index=True, comment='直播间ID')
    anchor_name = Column(String(100), nullable=True, comment='主播名称')
    live_session_id = Column(Integer, ForeignKey('live_sessions.id', ondelete='SET NULL'), nullable=True, index=True, comment='直播场次ID')
    user_id = Column(String(50), nullable=False, index=True, comment='用户ID')
    user_name = Column(String(100), nullable=False, comment='用户名称')
    user_level = Column(Integer, nullable=True, comment='用户等级')
    gift_id = Column(String(50), nullable=True, comment='礼物ID')
    gift_name = Column(String(100), nullable=False, comment='礼物名称')
    gift_count = Column(Integer, nullable=False, comment='礼物数量')
    gift_price = Column(Float, nullable=False, comment='礼物单价(钻石)')
    total_value = Column(Float, nullable=False, comment='总价值(钻石)')
    send_type = Column(String(10), nullable=False, default='normal', comment='发送类型: normal/combo')
    group_id = Column(String(50), nullable=True, comment='连击组ID')
    trace_id = Column(String(100), nullable=True, unique=True, comment='消息追踪ID，用于去重')
    fans_club_level = Column(Integer, nullable=True, default=0, comment='粉丝团等级')
    created_at = Column(DateTime, nullable=False, default=get_china_now, index=True, comment='创建时间')

    # 关系
    live_room = relationship('LiveRoom', back_populates='gift_messages')

    # 索引
    __table_args__ = (
        Index('idx_gift_room_time', 'live_id', 'created_at'),
        Index('idx_gift_user', 'user_id', 'created_at'),
    )

    def __repr__(self):
        return f'<GiftMessage(user={self.user_name}, gift={self.gift_name}x{self.gift_count})>'


class RoomStats(Base):
    """统计快照表"""
    __tablename__ = 'room_stats'

    id = Column(Integer, primary_key=True, autoincrement=True)
    live_id = Column(String(50), ForeignKey('live_rooms.live_id', ondelete='CASCADE'), nullable=False, index=True, comment='直播间ID')
    anchor_name = Column(String(100), nullable=True, comment='主播名称')
    current_user_count = Column(Integer, nullable=True, comment='当前观看人数')
    total_user_count = Column(Integer, nullable=True, comment='累计观看人数')
    total_like_count = Column(Integer, nullable=False, default=0, comment='累计点赞数')
    total_income = Column(Float, nullable=False, default=0, comment='总收入(钻石)')
    contributor_count = Column(Integer, nullable=False, default=0, comment='贡献者数量')
    stats_at = Column(DateTime, nullable=False, default=get_china_now, index=True, comment='统计时间')

    # 关系
    live_room = relationship('LiveRoom', back_populates='room_stats')

    # 索引
    __table_args__ = (
        Index('idx_stats_room_time', 'live_id', 'stats_at'),
    )

    def __repr__(self):
        return f'<RoomStats(live_id={self.live_id}, income={self.total_income})>'


class UserContribution(Base):
    """用户贡献榜"""
    __tablename__ = 'user_contributions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    live_id = Column(String(50), ForeignKey('live_rooms.live_id', ondelete='CASCADE'), nullable=False, index=True, comment='直播间ID')
    anchor_name = Column(String(100), nullable=True, comment='主播名称')
    user_id = Column(String(50), nullable=False, index=True, comment='用户ID')
    user_name = Column(String(100), nullable=False, comment='用户名称')
    total_score = Column(Float, nullable=False, default=0, comment='总贡献值(钻石)')
    gift_count = Column(Integer, nullable=False, default=0, comment='送礼次数')
    chat_count = Column(Integer, nullable=False, default=0, comment='弹幕次数')
    like_count = Column(Integer, nullable=False, default=0, comment='点赞次数')
    user_avatar = Column(String(500), nullable=True, comment='用户头像URL')
    user_level = Column(Integer, nullable=True, default=0, comment='用户等级')
    gender = Column(Integer, nullable=True, comment='性别: 0未知 1男 2女')
    follower_count = Column(Integer, nullable=True, comment='粉丝数')
    following_count = Column(Integer, nullable=True, comment='关注数')
    age_range = Column(Integer, nullable=True, comment='年龄段')
    fans_club_level = Column(Integer, nullable=True, default=0, comment='粉丝团等级')
    created_at = Column(DateTime, nullable=False, default=get_china_now, comment='首次贡献时间')
    updated_at = Column(DateTime, nullable=False, default=get_china_now, onupdate=get_china_now, comment='更新时间')

    # 关系
    live_room = relationship('LiveRoom', back_populates='user_contributions')

    # 唯一约束
    __table_args__ = (
        UniqueConstraint('live_id', 'user_id', name='uq_room_user'),
        Index('idx_contribution_score', 'live_id', 'total_score'),
    )

    def __repr__(self):
        return f'<UserContribution(user={self.user_name}, score={self.total_score})>'


class SystemEvent(Base):
    """系统事件日志"""
    __tablename__ = 'system_events'

    id = Column(Integer, primary_key=True, autoincrement=True)
    live_id = Column(String(50), ForeignKey('live_rooms.live_id', ondelete='CASCADE'), nullable=True, index=True, comment='直播间ID')
    anchor_name = Column(String(100), nullable=True, comment='主播名称')
    event_type = Column(String(50), nullable=False, index=True, comment='事件类型: connect/disconnect/error/reconnect')
    event_message = Column(Text, nullable=True, comment='事件消息')
    event_data = Column(SQLAlchemyJSON, nullable=True, comment='事件数据(JSON)')
    created_at = Column(DateTime, nullable=False, default=get_china_now, index=True, comment='创建时间')

    # 关系
    live_room = relationship('LiveRoom', back_populates='system_events')

    # 索引
    __table_args__ = (
        Index('idx_event_room_time', 'live_id', 'created_at'),
        Index('idx_event_type_time', 'event_type', 'created_at'),
    )

    def __repr__(self):
        return f'<SystemEvent(type={self.event_type}, message={self.event_message})>'


class LiveSession(Base):
    """直播场次表 - 记录每场直播的开始和结束"""
    __tablename__ = 'live_sessions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    live_id = Column(String(50), ForeignKey('live_rooms.live_id', ondelete='CASCADE'), nullable=False, index=True, comment='直播间ID')
    anchor_name = Column(String(100), nullable=True, comment='主播名称')
    start_time = Column(DateTime, nullable=False, default=get_china_now, comment='开播时间')
    end_time = Column(DateTime, nullable=True, comment='结束时间')
    status = Column(String(20), nullable=False, default='live', comment='状态: live/ended')
    total_income = Column(Float, nullable=False, default=0, comment='总收入(钻石)')
    total_gift_count = Column(Integer, nullable=False, default=0, comment='礼物总数')
    total_chat_count = Column(Integer, nullable=False, default=0, comment='弹幕总数')
    total_like_count = Column(Integer, nullable=False, default=0, comment='点赞总数')
    peak_viewer_count = Column(Integer, nullable=True, comment='峰值观看人数')
    created_at = Column(DateTime, nullable=False, default=get_china_now, comment='创建时间')
    updated_at = Column(DateTime, nullable=False, default=get_china_now, onupdate=get_china_now, comment='更新时间')

    # 关系
    live_room = relationship('LiveRoom')
    # gift_messages 和 chat_messages 的关系在各自模型中通过 backref 定义

    # 索引
    __table_args__ = (
        Index('idx_session_room_time', 'live_id', 'start_time'),
        Index('idx_session_status', 'status', 'start_time'),
    )

    def __repr__(self):
        return f'<LiveSession(live_id={self.live_id}, status={self.status}, income={self.total_income})>'

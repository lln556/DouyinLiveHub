"""
房间管理API路由
"""
import re
from datetime import datetime

from flask import Blueprint, request, jsonify

from services.data_service import DataService
from models.database import LiveSession
from utils.logger import get_logger

logger = get_logger("api_rooms")


class _InvalidIntParam(ValueError):
    """仅用于客户端整数参数，避免将业务 ValueError 当作参数错误返回。"""


def _parse_int_param(raw, name, default, min_val=None, max_val=None):
    """严格解析整数和范围，错误信息不包含请求原值。"""
    value = default if raw is None else raw
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _InvalidIntParam(f'{name} 必须为整数')
    if isinstance(value, str) and not re.fullmatch(r'[+-]?[0-9]+', value):
        raise _InvalidIntParam(f'{name} 必须为整数')
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise _InvalidIntParam(f'{name} 必须为整数') from None
    if min_val is not None and value < min_val:
        raise _InvalidIntParam(f'{name} 超出允许范围')
    if max_val is not None and value > max_val:
        raise _InvalidIntParam(f'{name} 超出允许范围')
    return value


def init_rooms_api(data_service: DataService, room_manager, socketio):
    """初始化房间API路由。

    Blueprint 在此处实例化（而非模块级单例），这样每次调用都返回一个全新的
    Blueprint，避免在测试中重复注册时触发 "setup method can no longer be
    called" 错误。
    """
    rooms_bp = Blueprint('rooms', __name__, url_prefix='/api/rooms')

    @rooms_bp.before_request
    def validate_live_id():
        live_ids = [(request.view_args or {}).get('live_id'), request.args.get('live_id')]
        for live_id in live_ids:
            if live_id is not None and not re.fullmatch(r'^[A-Za-z0-9_.\-]{1,50}$', live_id):
                return jsonify({'error': '直播间ID格式错误'}), 400

    @rooms_bp.route('', methods=['GET'])
    def list_rooms():
        """获取房间列表"""
        try:
            status = request.args.get('status')
            include_archived = request.args.get('include_archived') in ('1', 'true', 'True', 'yes')
            archived_only = (
                request.args.get('archived') in ('1', 'true', 'True', 'yes')
                or status == 'archived'
            )
            rooms = data_service.list_live_rooms(
                status=status,
                include_archived=include_archived,
                archived_only=archived_only
            )

            # One query for all ongoing sessions; snapshot rooms before iterating.
            with data_service.get_session() as db:
                live_sessions = db.query(
                    LiveSession.id, LiveSession.live_id, LiveSession.total_income,
                    LiveSession.peak_viewer_count, LiveSession.start_time
                ).filter(LiveSession.status == 'live').all()
            live_session_map = {row.live_id: row for row in live_sessions}
            active_status = {}
            with room_manager.lock:
                active_rooms = list(room_manager.active_rooms.items())
            for live_id, monitored_room in active_rooms:
                active_status[live_id] = {
                    'is_active': monitored_room.thread and monitored_room.thread.is_alive(),
                    'stats': monitored_room.get_stats()
                }

            result = []
            for room in rooms:
                # 获取监控线程状态
                is_monitor_alive = False
                if room.live_id in active_status:
                    is_monitor_alive = active_status[room.live_id]['is_active']
                active_stats = active_status.get(room.live_id, {}).get('stats')
                live_session = live_session_map.get(room.live_id)

                # 判断直播状态：基于数据库中是否有进行中的直播场次
                live_status = 'live' if live_session else 'offline'

                room_dict = {
                    'id': room.live_id,
                    'live_id': room.live_id,
                    'anchor_name': room.anchor_name,
                    'anchor_id': room.anchor_id,
                    'status': room.status,  # 监控状态: monitoring/stopped/offline/error
                    'error_message': room.error_message,
                    'live_status': live_status,  # 直播状态: live/offline
                    'is_monitor_alive': is_monitor_alive,  # 监控线程是否活跃
                    'monitor_type': room.monitor_type,
                    'auto_reconnect': bool(room.auto_reconnect),
                    'current_user_count': int((active_stats or {}).get('current_user_count') or 0) if is_monitor_alive else 0,
                    'session_income': round(max(float(live_session.total_income or 0), float((active_stats or {}).get('total_income') or 0)), 2) if live_session else 0.0,
                    'current_session_id': live_session.id if live_session else None,
                    'is_archived': bool(room.archived_at or room.status == 'archived'),
                    'archived_at': room.archived_at.isoformat() if room.archived_at else None
                }

                result.append(room_dict)

            return jsonify({'rooms': result})
        except Exception:
            logger.exception('获取房间列表失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('', methods=['POST'])
    def add_room():
        """添加监控房间"""
        try:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({'error': '房间参数必须为对象'}), 400
            live_id = data.get('live_id')

            if not live_id:
                return jsonify({'error': '请提供直播间ID'}), 400

            if not isinstance(live_id, str) or not re.fullmatch(r'^[A-Za-z0-9_.\-]{1,50}$', live_id):
                return jsonify({'error': '直播间ID格式错误，应为 1-50 位字母、数字、下划线、点或短横线'}), 400

            # 默认监控类型为 24h，自动重连为 True
            monitor_type = '24h'
            auto_reconnect = True

            # 添加到管理器
            result_live_id = room_manager.add_room(live_id, monitor_type, auto_reconnect)

            if result_live_id is None:
                return jsonify({'error': '该直播间已在监控中'}), 400

            room = data_service.get_live_room(result_live_id)

            return jsonify({
                'message': '房间添加成功',
                'room': {
                    'live_id': room.live_id,
                    'anchor_name': room.anchor_name,
                    'status': room.status,
                    'monitor_type': room.monitor_type,
                    'auto_reconnect': room.auto_reconnect
                }
            })
        except Exception:
            logger.exception('添加房间失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/archived', methods=['GET'])
    def list_archived_rooms():
        """获取已归档房间的历史数据摘要"""
        try:
            rooms = data_service.get_archived_rooms_summary()
            return jsonify({'rooms': rooms})
        except Exception:
            logger.exception('获取历史房间失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>', methods=['GET'])
    def get_room(live_id):
        """获取房间详情"""
        try:
            room = data_service.get_live_room(live_id)
            if not room:
                return jsonify({'error': '房间不存在'}), 404

            # 获取监控中的房间实例
            monitored_room = room_manager.get_room(live_id)
            stats = None
            is_active = False
            if monitored_room:
                stats = monitored_room.get_stats()
                is_active = monitored_room.thread and monitored_room.thread.is_alive()

            return jsonify({
                'room': {
                    'live_id': room.live_id,
                    'anchor_name': room.anchor_name,
                    'anchor_id': room.anchor_id,
                    'status': room.status,
                    'monitor_type': room.monitor_type,
                    'auto_reconnect': room.auto_reconnect,
                    'reconnect_count': room.reconnect_count,
                    'last_connect_time': room.last_connect_time.isoformat() if room.last_connect_time else None,
                    'last_disconnect_time': room.last_disconnect_time.isoformat() if room.last_disconnect_time else None,
                    'error_message': room.error_message,
                    'is_archived': bool(room.archived_at or room.status == 'archived'),
                    'archived_at': room.archived_at.isoformat() if room.archived_at else None,
                    'created_at': room.created_at.isoformat() if room.created_at else None,
                    'updated_at': room.updated_at.isoformat() if room.updated_at else None,
                    'is_active': is_active,
                    'stats': stats
                }
            })
        except Exception:
            logger.exception('获取房间详情失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/start', methods=['POST'])
    def start_room(live_id):
        """启动房间监控"""
        try:
            room = data_service.get_live_room(live_id)
            if not room:
                return jsonify({'error': '房间不存在'}), 404
            if room.archived_at or room.status == 'archived':
                return jsonify({'error': '该直播间已归档，请先恢复到主页后再启动监控'}), 400

            # 确保房间在活跃列表中
            if live_id not in room_manager.active_rooms:
                # 重新添加到管理器（使用默认值）
                result_live_id = room_manager.add_room(live_id, '24h', True)
                if result_live_id != live_id:
                    return jsonify({'error': '重新添加房间失败'}), 500

            # 启动监控
            if room_manager.start_room(live_id):
                return jsonify({'message': '房间监控已启动'})
            else:
                return jsonify({'error': '启动监控失败'}), 500
        except Exception:
            logger.exception('启动房间监控失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/stop', methods=['POST'])
    def stop_room(live_id):
        """停止房间监控"""
        try:
            if room_manager.stop_room(live_id):
                return jsonify({'message': '房间监控已停止'})
            else:
                return jsonify({'error': '房间不存在或未在监控中'}), 404
        except Exception:
            logger.exception('停止房间监控失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>', methods=['DELETE'])
    def delete_room(live_id):
        """归档房间，保留历史数据"""
        try:
            # 先停止监控
            room_manager.remove_room(live_id)

            # 从主页归档，不删除历史数据
            if data_service.archive_live_room(live_id):
                data_service.log_system_event(live_id, 'archive', '直播间已归档，历史数据保留')
                return jsonify({'message': '房间已归档，历史数据已保留'})
            else:
                return jsonify({'error': '房间不存在'}), 404
        except Exception:
            logger.exception('归档房间失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/restore', methods=['POST'])
    def restore_room(live_id):
        """从历史数据恢复房间到主页"""
        try:
            if data_service.restore_live_room(live_id):
                data_service.log_system_event(live_id, 'restore', '直播间已从历史数据恢复到主页')
                return jsonify({'message': '房间已恢复到主页'})
            return jsonify({'error': '房间不存在'}), 404
        except Exception:
            logger.exception('恢复房间失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/hard-delete', methods=['DELETE'])
    def hard_delete_room(live_id):
        """永久删除房间及所有历史数据"""
        try:
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict):
                return jsonify({'error': '删除确认参数必须为对象'}), 400
            if data.get('confirm_live_id') != live_id:
                return jsonify({'error': '请提供匹配的 confirm_live_id 以确认永久删除'}), 400

            room_manager.remove_room(live_id)
            if data_service.hard_delete_live_room(live_id):
                return jsonify({'message': '房间及所有历史数据已永久删除'})
            return jsonify({'error': '房间不存在'}), 404
        except Exception:
            logger.exception('永久删除房间失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/messages', methods=['GET'])
    def get_room_messages(live_id):
        """获取房间消息（支持分页）"""
        try:
            page_size = _parse_int_param(request.args.get('limit'), 'limit', 50, 1, 1000)
            page = _parse_int_param(request.args.get('page'), 'page', 1, 1, 2147483647)
            offset = _parse_int_param(request.args.get('offset'), 'offset', (page - 1) * page_size, 0, 2147483647)
            msg_type = request.args.get('type', 'all')  # chat/gift/all
            raw_session_id = request.args.get('session_id')
            session_id = _parse_int_param(raw_session_id, 'session_id', None, 1, 2147483647) if raw_session_id else None

            # 获取消息总数
            if session_id is not None:
                counts = dict(data_service.get_session_message_counts(session_id))
                counts['total_count'] = counts['chat_count'] + counts['gift_count']
            else:
                counts = data_service.get_message_counts(live_id)

            if session_id is not None and msg_type in ('chat', 'gift'):
                total = counts[f'{msg_type}_count']
                messages_data = data_service.get_session_messages(session_id, msg_type, page_size, offset)
            elif msg_type == 'chat':
                total = counts['chat_count']
                messages = data_service.get_chat_messages(live_id, page_size, offset)
                messages_data = [
                    {
                        'id': msg.id,
                        'type': 'chat',
                        'live_id': msg.live_id,
                        'anchor_name': msg.anchor_name,
                        'user_id': msg.user_id,
                        'user_name': msg.user_name,
                        'user_level': msg.user_level,
                        'fans_club_level': msg.fans_club_level or 0,
                        'content': msg.content,
                        'is_gift_user': msg.is_gift_user,
                        'created_at': msg.created_at.isoformat() if msg.created_at else None
                    }
                    for msg in messages
                ]
            elif msg_type == 'gift':
                total = counts['gift_count']
                messages = data_service.get_gift_messages(live_id, page_size, offset)
                messages_data = [
                    {
                        'id': msg.id,
                        'type': 'gift',
                        'live_id': msg.live_id,
                        'anchor_name': msg.anchor_name,
                        'user_id': msg.user_id,
                        'user_name': msg.user_name,
                        'user_level': msg.user_level,
                        'fans_club_level': msg.fans_club_level or 0,
                        'gift_name': msg.gift_name,
                        'gift_count': msg.gift_count,
                        'gift_price': msg.gift_price,
                        'total_value': msg.total_value,
                        'send_type': msg.send_type,
                        'created_at': msg.created_at.isoformat() if msg.created_at else None
                    }
                    for msg in messages
                ]
            else:  # all
                total = counts['total_count']
                if session_id is not None:
                    messages_data = data_service.get_all_messages(live_id, page_size, offset, session_id=session_id)
                else:
                    messages_data = data_service.get_all_messages(live_id, page_size, offset)

            for message in messages_data:
                if isinstance(message.get('created_at'), datetime):
                    message['created_at'] = message['created_at'].isoformat()

            total_pages = (total + page_size - 1) // page_size if total > 0 else 1

            return jsonify({
                'messages': messages_data,
                'pagination': {
                    'total': total,
                    'page': page,
                    'page_size': page_size,
                    'total_pages': total_pages
                },
                'counts': counts
            })
        except _InvalidIntParam as e:
            return jsonify({'error': str(e)}), 400
        except Exception:
            logger.exception('获取房间消息失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/contributors', methods=['GET'])
    def get_room_contributors(live_id):
        """获取房间贡献榜"""
        try:
            limit = _parse_int_param(request.args.get('limit'), 'limit', 100, 1, 1000)
            contributors = data_service.get_top_contributors(live_id, limit)

            return jsonify({
                'contributors': [
                    {
                        'live_id': c.live_id,
                        'anchor_name': c.anchor_name,
                        'user_id': c.user_id,
                        'user_name': c.user_name,
                        'total_score': c.total_score,
                        'gift_count': c.gift_count,
                        'chat_count': c.chat_count,
                        'like_count': c.like_count,
                        'user_avatar': c.user_avatar,
                        'user_level': c.user_level or 0,
                        'fans_club_level': c.fans_club_level or 0
                    }
                    for c in contributors
                ]
            })
        except _InvalidIntParam as e:
            return jsonify({'error': str(e)}), 400
        except Exception:
            logger.exception('获取房间贡献榜失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/session-contributors', methods=['GET'])
    def get_session_contributors(live_id):
        """获取当前直播场次的贡献榜"""
        try:
            limit = _parse_int_param(request.args.get('limit'), 'limit', 100, 1, 1000)
            # 获取当前直播场次
            current_session = data_service.get_current_live_session(live_id)
            if not current_session:
                return jsonify({'contributors': [], 'message': '暂无进行中的直播场次'})

            contributors = data_service.get_session_contributors(live_id, current_session.id, limit)

            return jsonify({
                'session_id': current_session.id,
                'live_id': live_id,
                'contributors': contributors
            })
        except _InvalidIntParam as e:
            return jsonify({'error': str(e)}), 400
        except Exception:
            logger.exception('获取场次贡献榜失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/contributors-by-date', methods=['GET'])
    def get_contributors_by_date():
        """按日期范围获取贡献榜（支持分页）"""
        try:
            live_id = request.args.get('live_id')  # 可选，不传表示所有房间
            start_date = request.args.get('start_date')
            end_date = request.args.get('end_date')
            page = _parse_int_param(request.args.get('page'), 'page', 1, 1, 2147483647)
            page_size = _parse_int_param(request.args.get('page_size'), 'page_size', 20, 1, 100)
            source = (request.args.get('source') or '').strip().lower()

            if source == 'summary':
                result = data_service.get_summary_contributors(
                    live_id=live_id,
                    page=page,
                    page_size=page_size
                )
                return jsonify(result)

            # 验证日期参数
            if not start_date or not end_date:
                return jsonify({'error': '请提供 start_date 和 end_date 参数'}), 400

            result = data_service.get_contributors_by_date_range(
                live_id=live_id,
                start_date=start_date,
                end_date=end_date,
                page=page,
                page_size=page_size
            )

            return jsonify(result)
        except _InvalidIntParam as e:
            return jsonify({'error': str(e)}), 400
        except Exception:
            logger.exception('按日期获取贡献榜失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/date-range', methods=['GET'])
    def get_date_range():
        """获取直播数据的日期范围"""
        try:
            live_id = request.args.get('live_id')
            date_range = data_service.get_room_date_range(live_id)
            return jsonify(date_range)
        except Exception:
            logger.exception('获取日期范围失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/stats', methods=['GET'])
    def get_room_stats(live_id):
        """获取房间统计"""
        try:
            hours = _parse_int_param(request.args.get('hours'), 'hours', 24, 1, 2160)
            stats_history = data_service.get_room_stats_history(live_id, hours)
            latest_stats = data_service.get_latest_stats(live_id)

            # 获取实时统计（如果正在监控）
            monitored_room = room_manager.get_room(live_id)
            realtime_stats = None
            if monitored_room and monitored_room.thread and monitored_room.thread.is_alive():
                realtime_stats = monitored_room.get_stats()

            return jsonify({
                'latest': {
                    'live_id': latest_stats.live_id if latest_stats else None,
                    'anchor_name': latest_stats.anchor_name if latest_stats else None,
                    'current_user_count': latest_stats.current_user_count if latest_stats else 0,
                    'total_user_count': latest_stats.total_user_count if latest_stats else 0,
                    'total_like_count': latest_stats.total_like_count if latest_stats else 0,
                    'total_income': latest_stats.total_income if latest_stats else 0,
                    'contributor_count': latest_stats.contributor_count if latest_stats else 0,
                    'stats_at': latest_stats.stats_at.isoformat() if latest_stats else None
                } if latest_stats else None,
                'realtime': realtime_stats,
                'history': [
                    {
                        'live_id': s.live_id,
                        'anchor_name': s.anchor_name,
                        'current_user_count': s.current_user_count,
                        'total_user_count': s.total_user_count,
                        'total_like_count': s.total_like_count,
                        'total_income': s.total_income,
                        'contributor_count': s.contributor_count,
                        'stats_at': s.stats_at.isoformat() if s.stats_at else None
                    }
                    for s in stats_history
                ]
            })
        except _InvalidIntParam as e:
            return jsonify({'error': str(e)}), 400
        except Exception:
            logger.exception('获取房间统计失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/stats/summary', methods=['GET'])
    def get_stats_summary():
        """获取全局统计摘要"""
        try:
            summary = data_service.get_stats_summary()
            return jsonify(summary)
        except Exception:
            logger.exception('获取统计摘要失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/top-likers', methods=['GET'])
    def get_top_likers_route():
        """累积点赞榜（基于 UserContribution.like_count）。
        Query 参数：
        - live_id: 可选，不传则跨房间
        - limit: 可选，默认 100，上限 1000
        """
        try:
            live_id = (request.args.get('live_id') or '').strip() or None
            limit = _parse_int_param(request.args.get('limit'), 'limit', 100, 1, 1000)

            likers = data_service.get_top_likers(live_id=live_id, limit=limit)

            return jsonify({
                'likers': likers,
                'total': len(likers),
                'source': 'summary'
            })
        except _InvalidIntParam as e:
            return jsonify({'error': str(e)}), 400
        except Exception:
            logger.exception('获取累积点赞榜失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/current-session', methods=['GET'])
    def get_current_session(live_id):
        """获取当前直播场次数据"""
        try:
            # 先检查房间是否存在
            room = data_service.get_live_room(live_id)
            if not room:
                return jsonify({'error': '房间不存在'}), 404

            # 获取当前进行中的直播场次
            current_session = data_service.get_current_live_session(live_id)

            # 如果没有进行中的场次，尝试获取最近结束的场次
            if not current_session:
                recent_sessions = data_service.get_live_sessions(live_id, status='ended', limit=1)
                if recent_sessions:
                    current_session = recent_sessions[0]

            if not current_session:
                return jsonify({'session': None, 'message': '暂无直播场次数据'})

            return jsonify({
                'session': {
                    'id': current_session.id,
                    'live_id': current_session.live_id,
                    'anchor_name': current_session.anchor_name,
                    'start_time': current_session.start_time.isoformat() if current_session.start_time else None,
                    'end_time': current_session.end_time.isoformat() if current_session.end_time else None,
                    'status': current_session.status,
                    'total_income': current_session.total_income,
                    'total_gift_count': current_session.total_gift_count,
                    'total_chat_count': current_session.total_chat_count,
                    'total_like_count': current_session.total_like_count,
                    'peak_viewer_count': current_session.peak_viewer_count
                }
            })
        except Exception:
            logger.exception('获取当前直播场次失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/sessions', methods=['GET'])
    def get_room_sessions(live_id):
        """获取房间的直播场次列表"""
        try:
            limit = _parse_int_param(request.args.get('limit'), 'limit', 50, 1, 200)
            # 先检查房间是否存在
            room = data_service.get_live_room(live_id)
            if not room:
                return jsonify({'error': '房间不存在'}), 404

            start_date = request.args.get('start_date')
            end_date = request.args.get('end_date')
            logger.debug(f"获取场次列表: live_id={live_id}, start_date={start_date}, end_date={end_date}")

            sessions = data_service.get_room_sessions_stats(live_id, start_date, end_date, limit)
            logger.debug(f"场次列表结果: 共 {len(sessions)} 条")

            return jsonify({'sessions': sessions})
        except _InvalidIntParam as e:
            return jsonify({'error': str(e)}), 400
        except Exception:
            logger.exception('获取直播场次列表失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/sessions/stats', methods=['GET'])
    def get_room_sessions_stats(live_id):
        """获取房间的聚合统计数据"""
        try:
            # 先检查房间是否存在
            room = data_service.get_live_room(live_id)
            if not room:
                return jsonify({'error': '房间不存在'}), 404

            start_date = request.args.get('start_date')
            end_date = request.args.get('end_date')
            logger.debug(f"获取房间统计: live_id={live_id}, start_date={start_date}, end_date={end_date}")

            stats = data_service.get_sessions_aggregated_stats(live_id, start_date, end_date)
            logger.debug(f"房间统计结果: {stats}")

            return jsonify({'stats': stats})
        except Exception:
            logger.exception('获取聚合统计数据失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/<live_id>/config', methods=['PUT', 'PATCH'])
    def update_room_config(live_id):
        """更新房间配置（已废弃，保留以兼容旧版客户端）"""
        try:
            # 先检查房间是否存在
            room = data_service.get_live_room(live_id)
            if not room:
                return jsonify({'error': '房间不存在'}), 404

            # 监控模式已统一为持续监控，无需配置更新
            # 直接返回当前房间信息
            return jsonify({
                'message': '监控模式已统一为持续监控',
                'room': {
                    'live_id': room.live_id,
                    'anchor_name': room.anchor_name,
                    'monitor_type': '24h',
                    'auto_reconnect': True,
                    'status': room.status
                }
            })
        except Exception:
            logger.exception('获取房间配置失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/sessions/stats', methods=['GET'])
    def get_all_sessions_stats():
        """获取全局聚合统计数据"""
        try:
            start_date = request.args.get('start_date')
            end_date = request.args.get('end_date')
            logger.debug(f"获取全局统计: start_date={start_date}, end_date={end_date}")

            stats = data_service.get_sessions_aggregated_stats(None, start_date, end_date)
            logger.debug(f"全局统计结果: {stats}")

            return jsonify({'stats': stats})
        except Exception:
            logger.exception('获取全局聚合统计数据失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/sessions/<session_id>', methods=['GET'])
    def get_session_detail(session_id):
        """获取直播场次详情（弹幕、礼物、贡献榜）"""
        try:
            session_id = _parse_int_param(session_id, 'session_id', None, 1, 2147483647)
            page_size = _parse_int_param(request.args.get('limit'), 'limit', 50, 1, 200)
            page = _parse_int_param(request.args.get('page'), 'page', 1, 1, 2147483647)
            offset = _parse_int_param(request.args.get('offset'), 'offset', (page - 1) * page_size, 0, 2147483647)
            msg_type = request.args.get('type', 'chat')  # chat/gift/contributors
            logger.debug(f"获取场次详情: session_id={session_id}, page={page}, page_size={page_size}, type={msg_type}")

            # 获取场次基本信息
            session_obj = data_service.get_live_session_stats(session_id)
            if not session_obj:
                return jsonify({'error': '场次不存在'}), 404

            # 获取消息总数
            counts = data_service.get_session_message_counts(session_id)

            result = {
                'session': session_obj,
                'counts': counts
            }

            # 根据请求类型返回对应数据
            if msg_type == 'chat':
                messages = data_service.get_session_messages(session_id, 'chat', page_size, offset)
                total = counts['chat_count']
                result['chats'] = messages
                result['pagination'] = {
                    'total': total,
                    'page': page,
                    'page_size': page_size,
                    'total_pages': (total + page_size - 1) // page_size if total > 0 else 1
                }
            elif msg_type == 'gift':
                messages = data_service.get_session_messages(session_id, 'gift', page_size, offset)
                total = counts['gift_count']
                result['gifts'] = messages
                result['pagination'] = {
                    'total': total,
                    'page': page,
                    'page_size': page_size,
                    'total_pages': (total + page_size - 1) // page_size if total > 0 else 1
                }
            elif msg_type == 'contributors':
                contributors = data_service.get_session_contributors(session_obj['live_id'], session_id, 100)
                result['contributors'] = contributors
            else:
                # 兼容旧的调用方式，返回所有数据（但只返回第一页）
                result['chats'] = data_service.get_session_messages(session_id, 'chat', page_size, 0)
                result['gifts'] = data_service.get_session_messages(session_id, 'gift', page_size, 0)
                result['contributors'] = data_service.get_session_contributors(session_obj['live_id'], session_id, 100)

            return jsonify(result)
        except _InvalidIntParam as e:
            return jsonify({'error': str(e)}), 400
        except Exception:
            logger.exception('获取场次详情失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    def _get_user_messages_response(live_id=None):
        """获取用户在指定房间、场次或日期范围内的消息记录"""
        try:
            # 获取参数
            user_id = (request.args.get('user_id') or '').strip() or None
            user_name = (
                request.args.get('user_name')
                or request.args.get('username')
                or request.args.get('keyword')
                or ''
            ).strip() or None
            query_live_id = (request.args.get('live_id') or '').strip() or None
            effective_live_id = live_id or query_live_id

            if not user_id and not user_name:
                return jsonify({'error': '请提供 user_name 或 user_id 参数'}), 400

            session_id = _parse_int_param(request.args.get('session_id'), 'session_id', None, 1, 2147483647)

            start_date = request.args.get('start_date')
            end_date = request.args.get('end_date')
            message_type = request.args.get('type', 'all')  # all/chat/gift
            page = _parse_int_param(request.args.get('page'), 'page', 1, 1, 2147483647)
            page_size = _parse_int_param(request.args.get('limit'), 'limit', 50, 1, 200)
            offset = _parse_int_param(request.args.get('offset'), 'offset', (page - 1) * page_size, 0, 2147483647)

            # 调用数据服务获取用户消息
            result = data_service.get_user_messages(
                live_id=effective_live_id,
                user_id=user_id,
                user_name=user_name,
                session_id=session_id,
                start_date=start_date,
                end_date=end_date,
                message_type=message_type,
                limit=page_size,
                offset=offset
            )

            logger.debug(f"获取用户消息: live_id={effective_live_id}, user_id={user_id}, user_name={user_name}, session_id={session_id}, "
                         f"type={message_type}, page={page}, total={result['pagination']['total']}")

            return jsonify(result)
        except _InvalidIntParam as e:
            return jsonify({'error': str(e)}), 400
        except Exception:
            logger.exception('获取用户消息失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    def _search_users_response(live_id=None):
        """按用户名搜索匹配用户列表。"""
        try:
            user_name = (
                request.args.get('user_name')
                or request.args.get('username')
                or request.args.get('keyword')
                or ''
            ).strip()
            query_live_id = (request.args.get('live_id') or '').strip() or None
            effective_live_id = live_id or query_live_id
            session_id = _parse_int_param(request.args.get('session_id'), 'session_id', None, 1, 2147483647)
            start_date = request.args.get('start_date')
            end_date = request.args.get('end_date')
            limit = _parse_int_param(request.args.get('limit'), 'limit', 30, 1, 100)

            if not user_name:
                return jsonify({'error': '请提供 user_name 参数'}), 400

            users = data_service.search_users_by_name(
                live_id=effective_live_id,
                user_name=user_name,
                session_id=session_id,
                start_date=start_date,
                end_date=end_date,
                limit=limit
            )

            return jsonify({
                'users': users,
                'total': len(users)
            })
        except _InvalidIntParam as e:
            return jsonify({'error': str(e)}), 400
        except Exception:
            logger.exception('搜索用户失败')
            return jsonify({'error': '操作失败，请稍后重试'}), 500

    @rooms_bp.route('/user-search', methods=['GET'])
    def search_all_users():
        """跨房间按用户名搜索用户列表。"""
        return _search_users_response()

    @rooms_bp.route('/<live_id>/user-search', methods=['GET'])
    def search_room_users(live_id):
        """在指定房间内按用户名搜索用户列表。"""
        return _search_users_response(live_id)

    @rooms_bp.route('/user-messages', methods=['GET'])
    def get_all_user_messages():
        """跨房间按用户名或用户ID获取消息记录"""
        return _get_user_messages_response()

    @rooms_bp.route('/<live_id>/user-messages', methods=['GET'])
    def get_user_messages(live_id):
        """获取用户在指定房间、场次或日期范围内的消息记录"""
        return _get_user_messages_response(live_id)

    return rooms_bp

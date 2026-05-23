"""
调度服务
后台定时任务调度
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import config
from services.data_service import DataService
from services.room_manager import RoomManager
from utils.logger import get_logger

logger = get_logger("scheduler")


class SchedulerService:
    """后台定时任务调度"""

    def __init__(self, room_manager: RoomManager, data_service: DataService):
        """
        初始化调度服务
        :param room_manager: 房间管理器
        :param data_service: 数据服务
        """
        self.room_manager = room_manager
        self.data_service = data_service
        self.scheduler = BackgroundScheduler()
        logger.info("调度服务初始化完成")

    def start(self):
        """启动调度器"""
        # 添加定时任务

        # 1. 每30秒检查并重连失败的房间
        self.scheduler.add_job(
            self._restart_failed_rooms,
            'interval',
            seconds=config.SCHEDULER_RESTART_FAILED_INTERVAL,
            id='restart_failed_rooms',
            name='重启失败的房间'
        )

        # 2. 每1分钟保存统计快照到数据库
        self.scheduler.add_job(
            self._save_stats_snapshot,
            'interval',
            seconds=config.SCHEDULER_STATS_SNAPSHOT_INTERVAL,
            id='save_stats_snapshot',
            name='保存统计快照'
        )

        # 3. 每小时清理旧数据
        if config.DATA_RETENTION_DAYS > 0:
            self.scheduler.add_job(
                self._cleanup_old_data,
                'interval',
                seconds=config.SCHEDULER_CLEANUPOldData_INTERVAL,
                id='cleanup_old_data',
                name='清理旧数据'
            )

        # 4. 启动时自动启动所有24h监控房间
        self.scheduler.add_job(
            self._auto_start_24h_rooms,
            'date',
            id='auto_start_24h_rooms',
            name='自动启动24小时监控房间'
        )

        self.scheduler.start()
        logger.info("调度器已启动")

    def stop(self):
        """停止调度器"""
        self.scheduler.shutdown(wait=False)
        logger.info("调度器已停止")

    def _restart_failed_rooms(self):
        """检查并重连失败的房间"""
        try:
            restarted = self.room_manager.restart_failed_rooms()
            if restarted > 0:
                logger.info(f"定时任务: 重启了 {restarted} 个失败的房间")
        except Exception as e:
            logger.error(f"重启失败房间时出错: {e}")

    def _save_stats_snapshot(self):
        """保存统计快照到数据库"""
        try:
            saved_count = 0
            for room_id, monitored_room in self.room_manager.active_rooms.items():
                # 只保存正在监控的房间
                room = self.data_service.get_live_room(room_id)
                if room and room.status == 'monitoring':
                    stats = monitored_room.get_stats()
                    self.data_service.save_room_stats(
                        room_id,
                        current_user_count=stats.get('current_user_count'),
                        total_user_count=stats.get('total_user_count'),
                        total_income=stats.get('total_income'),
                        contributor_count=stats.get('contributor_count')
                    )
                    saved_count += 1

            if saved_count > 0:
                logger.debug(f"定时任务: 保存了 {saved_count} 个房间的统计快照")
        except Exception as e:
            logger.error(f"保存统计快照时出错: {e}")

    def _cleanup_old_data(self):
        """清理旧数据"""
        try:
            result = self.data_service.cleanup_old_data()
            if 'error' not in result:
                total_deleted = (
                    result.get('chat_messages_deleted', 0) +
                    result.get('gift_messages_deleted', 0) +
                    result.get('stats_deleted', 0) +
                    result.get('events_deleted', 0)
                )
                if total_deleted > 0:
                    logger.info(f"定时任务: 清理了 {total_deleted} 条旧数据")
        except Exception as e:
            logger.error(f"清理旧数据时出错: {e}")

    def _auto_start_24h_rooms(self):
        """自动启动所有24小时监控房间"""
        try:
            rooms_24h = self.data_service.get_24h_monitor_rooms()
            started_count = 0

            for room in rooms_24h:
                # 检查是否已在活跃列表中
                if room.live_id not in self.room_manager.active_rooms:
                    # 添加到管理器
                    room_id = self.room_manager.add_room(
                        room.live_id,
                        monitor_type='24h',
                        auto_reconnect=True
                    )
                    if room_id:
                        # 启动监控
                        if self.room_manager.start_room(room_id):
                            started_count += 1
                            logger.info(f"自动启动24小时监控: {room.live_id}")
                else:
                    # 已在列表中，确保正在运行
                    monitored_room = self.room_manager.active_rooms[room.live_id]
                    if not monitored_room.thread or not monitored_room.thread.is_alive():
                        if self.room_manager.start_room(room.live_id):
                            started_count += 1
                            logger.info(f"重新启动24小时监控: {room.live_id}")

            if started_count > 0:
                logger.info(f"定时任务: 自动启动了 {started_count} 个24小时监控房间")
        except Exception as e:
            logger.error(f"自动启动24小时监控房间时出错: {e}")

    def add_job(self, func, trigger, **kwargs):
        """
        添加自定义定时任务
        :param func: 执行函数
        :param trigger: 触发器 ('interval', 'cron', 'date')
        :param kwargs: 其他参数
        """
        job_id = kwargs.pop('id', f'custom_job_{func.__name__}')
        job_name = kwargs.pop('name', f'自定义任务: {func.__name__}')

        if trigger == 'interval':
            seconds = kwargs.pop('seconds', 60)
            self.scheduler.add_job(
                func,
                IntervalTrigger(seconds=seconds),
                id=job_id,
                name=job_name,
                **kwargs
            )
            logger.info(f"添加定时任务: {job_name}, 间隔: {seconds}秒")
        else:
            logger.warning(f"暂不支持 {trigger} 类型的触发器")

    def remove_job(self, job_id: str):
        """移除定时任务"""
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"移除定时任务: {job_id}")
        except Exception as e:
            logger.error(f"移除定时任务失败: {e}")

    def get_jobs(self):
        """获取所有定时任务"""
        return self.scheduler.get_jobs()

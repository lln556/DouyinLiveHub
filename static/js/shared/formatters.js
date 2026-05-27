/**
 * 格式化工具 Vue Mixin
 * 提供通用的数据格式化方法
 */
const FormattersMixin = {
    methods: {
        /**
         * 格式化数字（支持万、亿单位）
         * @param {number} value - 要格式化的数字
         * @returns {string} 格式化后的字符串
         */
        formatNumber(value) {
            if (!value) return '0';
            value = Number(value);
            if (value >= 100000000) {
                return (value / 100000000).toFixed(1) + '亿';
            } else if (value >= 10000) {
                return (value / 10000).toFixed(1) + '万';
            } else {
                return value.toLocaleString();
            }
        },

        /**
         * 格式化日期时间
         * @param {string} dateStr - 日期字符串
         * @returns {string} 格式化后的日期时间
         */
        formatDateTime(dateStr) {
            if (!dateStr) return '-';
            const d = new Date(dateStr);
            return d.toLocaleString('zh-CN', {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit'
            });
        },

        /**
         * 格式化收入（钻石）
         * @param {number} value - 收入值
         * @returns {string} 格式化后的收入字符串
         */
        formatIncome(value) {
            return value ? value.toLocaleString() + ' 钻石' : '0 钻石';
        },

        /**
         * 格式化时长（秒转小时分钟）
         * @param {number} seconds - 秒数
         * @returns {string} 格式化后的时长
         */
        formatDuration(seconds) {
            if (!seconds || seconds === 0) return '0分钟';

            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);

            if (hours > 0) {
                return `${hours}小时${minutes}分钟`;
            }
            return `${minutes}分钟`;
        },

        /**
         * 格式化年龄段
         * @param {number} val - 年龄段代码
         * @returns {string} 年龄段文本
         */
        formatAgeRange(val) {
            const map = {
                0: '-',
                1: '<18',
                2: '18-23',
                3: '24-30',
                4: '31-40',
                5: '41-50',
                6: '>50'
            };
            return map[val] || '-';
        },

        /**
         * 格式化用户消息时间（简短格式）
         * @param {string} dateStr - 日期字符串
         * @returns {string} 格式化后的时间（MM-DD HH:mm:ss）
         */
        formatUserMessageTime(dateStr) {
            if (!dateStr) return '';
            const d = new Date(dateStr);
            const MM = String(d.getMonth() + 1).padStart(2, '0');
            const DD = String(d.getDate()).padStart(2, '0');
            const time = d.toLocaleTimeString('zh-CN', {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            });
            return `${MM}-${DD} ${time}`;
        },

        /**
         * 格式化日期为输入框格式（YYYY-MM-DD）
         * @param {Date|string} date - 日期对象或字符串
         * @returns {string} 格式化后的日期字符串
         */
        formatDateForInput(date) {
            const d = new Date(date);
            const year = d.getFullYear();
            const month = String(d.getMonth() + 1).padStart(2, '0');
            const day = String(d.getDate()).padStart(2, '0');
            return `${year}-${month}-${day}`;
        }
    }
};

/**
 * 用户消息模态框 Vue Mixin
 * 提供用户消息查看功能，可在多个页面复用
 */
const UserMessagesModalMixin = {
    data() {
        return {
            // 用户消息模态框
            showUserMessagesModal: false,
            userMessagesLoading: false,
            userMessagesTab: 'all',
            userMessagesData: {
                user: {},
                stats: {
                    total_messages: 0,
                    chat_count: 0,
                    gift_count: 0,
                    like_count: 0,
                    total_value: 0
                },
                messages: [],
                pagination: {
                    page: 1,
                    page_size: 50,
                    total: 0,
                    total_pages: 1
                }
            },
            userMessagesQuery: {
                user_id: null,
                user_name: null,
                live_id: null,
                session_id: null,
                start_date: null,
                end_date: null
            }
        };
    },
    methods: {
        /**
         * 打开用户消息模态框
         * @param {string} userId - 用户ID
         * @param {string} userName - 用户名
         * @param {object} options - 可选参数 { live_id, session_id, start_date, end_date, user_name, defer_show }
         */
        async openUserMessagesModal(userId, userName, options = {}) {
            this.showUserMessagesModal = !options.defer_show;
            this.userMessagesLoading = !options.defer_show;
            this.userMessagesTab = 'all';

            // 设置查询参数
            this.userMessagesQuery = {
                user_id: userId,
                user_name: options.user_name || null,
                live_id: options.live_id || this.selectedRoomId || null,
                session_id: options.session_id || null,
                start_date: options.start_date || null,
                end_date: options.end_date || null
            };

            // 重置数据
            this.userMessagesData = {
                user: {
                    user_id: userId,
                    nickname: userName
                },
                stats: {
                    total_messages: 0,
                    chat_count: 0,
                    gift_count: 0,
                    like_count: 0,
                    total_value: 0
                },
                messages: [],
                pagination: {
                    page: 1,
                    page_size: 50,
                    total: 0,
                    total_pages: 1
                }
            };

            const found = await this.loadUserMessages({ showLoading: !options.defer_show });
            if (options.defer_show && found) {
                this.showUserMessagesModal = true;
            }
            return found;
        },

        /**
         * 关闭用户消息模态框
         */
        closeUserMessagesModal() {
            this.showUserMessagesModal = false;
            this.userMessagesData = {
                user: {},
                stats: {
                    total_messages: 0,
                    chat_count: 0,
                    gift_count: 0,
                    like_count: 0,
                    total_value: 0
                },
                messages: [],
                pagination: {
                    page: 1,
                    page_size: 50,
                    total: 0,
                    total_pages: 1
                }
            };
            this.userMessagesQuery = {
                user_id: null,
                user_name: null,
                live_id: null,
                session_id: null,
                start_date: null,
                end_date: null
            };
        },

        /**
         * 加载用户消息
         * @param {object} options - 可选参数 { showLoading }
         * @returns {boolean} 是否找到消息
         */
        async loadUserMessages(options = {}) {
            if (!this.userMessagesQuery.user_id && !this.userMessagesQuery.user_name) return false;

            const showLoading = options.showLoading !== false;
            if (showLoading) {
                this.userMessagesLoading = true;
            }
            try {
                const params = new URLSearchParams({
                    type: this.userMessagesTab,
                    page: this.userMessagesData.pagination.page,
                    limit: this.userMessagesData.pagination.page_size
                });
                if (this.userMessagesQuery.user_id) {
                    params.append('user_id', this.userMessagesQuery.user_id);
                } else {
                    params.append('user_name', this.userMessagesQuery.user_name);
                }

                if (this.userMessagesQuery.session_id) {
                    params.append('session_id', this.userMessagesQuery.session_id);
                } else if (this.userMessagesQuery.start_date && this.userMessagesQuery.end_date) {
                    params.append('start_date', this.userMessagesQuery.start_date);
                    params.append('end_date', this.userMessagesQuery.end_date);
                }

                const url = this.userMessagesQuery.live_id
                    ? `/api/rooms/${this.userMessagesQuery.live_id}/user-messages?${params}`
                    : `/api/rooms/user-messages?${params}`;
                const response = await fetch(url);
                const data = await response.json();

                if (data.user) {
                    this.userMessagesData.user = data.user;
                }
                if (data.stats) {
                    this.userMessagesData.stats = data.stats;
                }
                if (data.messages) {
                    this.userMessagesData.messages = data.messages;
                }
                if (data.pagination) {
                    this.userMessagesData.pagination = data.pagination;
                }
                return (data.stats && data.stats.total_messages > 0) || (data.messages && data.messages.length > 0);
            } catch (error) {
                console.error('加载用户消息失败:', error);
                if (this.userSearchError !== undefined) {
                    this.userSearchError = '搜索失败，请稍后重试';
                }
                return false;
            } finally {
                if (showLoading) {
                    this.userMessagesLoading = false;
                }
            }
        },

        /**
         * 切换用户消息 tab
         * @param {string} tab - tab 名称 ('all', 'chat', 'gift')
         */
        async switchUserMessagesTab(tab) {
            if (this.userMessagesTab === tab) return;
            this.userMessagesTab = tab;
            this.userMessagesData.pagination.page = 1;
            await this.loadUserMessages();
        },

        /**
         * 跳转到指定页
         * @param {number} page - 页码
         */
        async userMessagesGoToPage(page) {
            const pagination = this.userMessagesData.pagination;
            if (page < 1 || page > pagination.total_pages) return;
            pagination.page = page;
            await this.loadUserMessages();
        }
    }
};

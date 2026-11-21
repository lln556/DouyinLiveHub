/**
 * 抖音直播记录站 - 通用前端交互工具（非阻塞 Toast、确认弹窗、永久删除二次确认、抖音链接提取）
 */
(function (global) {
    'use strict';

    const STATE = {
        toastContainer: null
    };

    function ensureToastContainer() {
        if (STATE.toastContainer && document.body.contains(STATE.toastContainer)) {
            return STATE.toastContainer;
        }
        const el = document.createElement('div');
        el.className = 'hub-toast-container';
        el.setAttribute('role', 'status');
        el.setAttribute('aria-live', 'polite');
        document.body.appendChild(el);
        STATE.toastContainer = el;
        return el;
    }

    function toast(message, type = 'info', duration = 3400) {
        const text = String(message || '').trim();
        if (!text) return;
        const container = ensureToastContainer();
        while (container.children.length >= 4) {
            container.removeChild(container.firstChild);
        }

        const item = document.createElement('div');
        item.className = `hub-toast is-${type}`;

        const badge = document.createElement('span');
        badge.textContent = type === 'success' ? '✓' : type === 'error' ? '!' : type === 'warning' ? '⚠' : 'i';
        badge.style.fontWeight = '800';
        item.appendChild(badge);

        const body = document.createElement('span');
        body.textContent = text;
        body.style.flex = '1';
        body.style.wordBreak = 'break-word';
        item.appendChild(body);

        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'hub-toast-close';
        closeBtn.textContent = '×';
        closeBtn.setAttribute('aria-label', '关闭提示');
        item.appendChild(closeBtn);

        let removed = false;
        const remove = () => {
            if (removed) return;
            removed = true;
            if (item.parentNode) {
                item.parentNode.removeChild(item);
            }
        };
        closeBtn.addEventListener('click', remove);
        container.appendChild(item);

        if (duration > 0) {
            window.setTimeout(remove, duration);
        }
    }

    function confirm(options = {}) {
        const title = options.title || '确认操作';
        const message = options.message || '';
        const confirmText = options.confirmText || '确定';
        const cancelText = options.cancelText || '取消';
        const danger = Boolean(options.danger);

        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'hub-dialog-overlay';
            overlay.setAttribute('role', 'dialog');
            overlay.setAttribute('aria-modal', 'true');

            const box = document.createElement('div');
            box.className = 'hub-dialog-box';

            const head = document.createElement('div');
            head.className = 'hub-dialog-head';
            const h3 = document.createElement('h3');
            h3.className = 'hub-dialog-title';
            h3.textContent = title;
            head.appendChild(h3);

            const body = document.createElement('div');
            body.className = 'hub-dialog-body';
            body.textContent = message;

            const foot = document.createElement('div');
            foot.className = 'hub-dialog-foot';

            const cancelBtn = document.createElement('button');
            cancelBtn.type = 'button';
            cancelBtn.className = 'hub-btn hub-btn-secondary';
            cancelBtn.textContent = cancelText;

            const okBtn = document.createElement('button');
            okBtn.type = 'button';
            okBtn.className = `hub-btn ${danger ? 'hub-btn-danger' : 'hub-btn-primary'}`;
            okBtn.textContent = confirmText;

            foot.appendChild(cancelBtn);
            foot.appendChild(okBtn);
            box.appendChild(head);
            box.appendChild(body);
            box.appendChild(foot);
            overlay.appendChild(box);
            document.body.appendChild(overlay);

            let settled = false;
            const finish = (result) => {
                if (settled) return;
                settled = true;
                document.removeEventListener('keydown', onKeyDown);
                if (overlay.parentNode) {
                    overlay.parentNode.removeChild(overlay);
                }
                resolve(result);
            };
            const onKeyDown = (e) => {
                if (e.key === 'Escape') {
                    e.stopPropagation();
                    finish(false);
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    finish(true);
                }
            };

            cancelBtn.addEventListener('click', () => finish(false));
            okBtn.addEventListener('click', () => finish(true));
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) finish(false);
            });
            document.addEventListener('keydown', onKeyDown);
            okBtn.focus();
        });
    }

    function confirmHardDelete(options = {}) {
        const liveId = String(options.liveId || '');
        const anchorName = String(options.anchorName || '');

        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'hub-dialog-overlay';
            overlay.setAttribute('role', 'dialog');
            overlay.setAttribute('aria-modal', 'true');

            const box = document.createElement('div');
            box.className = 'hub-dialog-box';

            const head = document.createElement('div');
            head.className = 'hub-dialog-head';
            const h3 = document.createElement('h3');
            h3.className = 'hub-dialog-title';
            h3.style.color = 'var(--hub-danger)';
            h3.textContent = '确认永久删除直播间';
            head.appendChild(h3);

            const body = document.createElement('div');
            body.className = 'hub-dialog-body';

            const warn = document.createElement('p');
            warn.style.marginTop = '0';
            warn.textContent = `永久删除将清除「${anchorName || liveId}」的全部历史场次、弹幕、礼物和贡献统计且无法恢复。`;
            body.appendChild(warn);

            const targetBox = document.createElement('div');
            targetBox.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 12px;margin:10px 0;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;';
            const idCode = document.createElement('code');
            idCode.style.cssText = 'font-weight:700;color:#991b1b;word-break:break-all;';
            idCode.textContent = liveId;

            const fillBtn = document.createElement('button');
            fillBtn.type = 'button';
            fillBtn.className = 'hub-btn hub-btn-secondary';
            fillBtn.style.cssText = 'padding:4px 10px;font-size:12px;flex-shrink:0;';
            fillBtn.textContent = '点击填入 ID';

            targetBox.appendChild(idCode);
            targetBox.appendChild(fillBtn);
            body.appendChild(targetBox);

            const input = document.createElement('input');
            input.type = 'text';
            input.placeholder = `请输入 ${liveId} 以确认`;
            input.className = 'w-full px-3 py-2 border rounded-lg text-sm focus:outline-none focus:border-red-500';
            body.appendChild(input);

            const foot = document.createElement('div');
            foot.className = 'hub-dialog-foot';

            const cancelBtn = document.createElement('button');
            cancelBtn.type = 'button';
            cancelBtn.className = 'hub-btn hub-btn-secondary';
            cancelBtn.textContent = '取消';

            const okBtn = document.createElement('button');
            okBtn.type = 'button';
            okBtn.className = 'hub-btn hub-btn-danger';
            okBtn.textContent = '确认永久删除';
            okBtn.disabled = true;

            foot.appendChild(cancelBtn);
            foot.appendChild(okBtn);
            box.appendChild(head);
            box.appendChild(body);
            box.appendChild(foot);
            overlay.appendChild(box);
            document.body.appendChild(overlay);

            const syncState = () => {
                okBtn.disabled = input.value.trim() !== liveId;
            };
            input.addEventListener('input', syncState);
            fillBtn.addEventListener('click', () => {
                input.value = liveId;
                syncState();
                okBtn.focus();
            });

            let settled = false;
            const finish = (result) => {
                if (settled) return;
                settled = true;
                document.removeEventListener('keydown', onKeyDown);
                if (overlay.parentNode) {
                    overlay.parentNode.removeChild(overlay);
                }
                resolve(result);
            };
            const onKeyDown = (e) => {
                if (e.key === 'Escape') {
                    e.stopPropagation();
                    finish(false);
                } else if (e.key === 'Enter' && input.value.trim() === liveId) {
                    e.preventDefault();
                    finish(true);
                }
            };

            cancelBtn.addEventListener('click', () => finish(false));
            okBtn.addEventListener('click', () => {
                if (input.value.trim() === liveId) finish(true);
            });
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) finish(false);
            });
            document.addEventListener('keydown', onKeyDown);
            input.focus();
        });
    }

    function extractLiveId(rawInput) {
        const raw = String(rawInput || '').trim();
        if (!raw) {
            return { liveId: '', fromUrl: false };
        }
        // 支持粘贴 live.douyin.com 链接或手机分享整段文案
        const liveUrlMatch = raw.match(/https?:\/\/(?:live\.douyin\.com)\/([A-Za-z0-9_.\-]{1,50})/i);
        if (liveUrlMatch && liveUrlMatch[1]) {
            return { liveId: liveUrlMatch[1], fromUrl: true };
        }
        const bareHostMatch = raw.match(/(?:^|[\s/])live\.douyin\.com\/([A-Za-z0-9_.\-]{1,50})/i);
        if (bareHostMatch && bareHostMatch[1]) {
            return { liveId: bareHostMatch[1], fromUrl: true };
        }
        const webRidMatch = raw.match(/[?&](?:web_rid|live_id)=([A-Za-z0-9_.\-]{1,50})/i);
        if (webRidMatch && webRidMatch[1]) {
            return { liveId: webRidMatch[1], fromUrl: true };
        }
        return { liveId: raw, fromUrl: false };
    }

    global.HubUI = {
        toast,
        confirm,
        confirmHardDelete,
        extractLiveId
    };
})(window);

/**
 * 抖音直播记录站 - 通用前端交互与 Web 平台能力封装
 */
(function (global) {
    'use strict';

    const STATE = {
        toastContainer: null,
        liveRegion: null
    };

    const INTL_INT = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 });
    const INTL_DATETIME = new Intl.DateTimeFormat('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
    });
    const INTL_DATE = new Intl.DateTimeFormat('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
    });
    const INTL_TIME = new Intl.DateTimeFormat('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });

    function parseValidDate(value) {
        if (!value) return null;
        const date = value instanceof Date ? value : new Date(value);
        return Number.isNaN(date.getTime()) ? null : date;
    }

    function formatInteger(value) {
        const num = Number(value ?? 0);
        return Number.isFinite(num) ? INTL_INT.format(Math.round(num)) : '0';
    }

    function formatCompactNumber(value) {
        const num = Number(value ?? 0);
        if (!Number.isFinite(num) || num === 0) return '0';
        if (Math.abs(num) >= 100000000) {
            return `${(num / 100000000).toFixed(1)}亿`;
        }
        if (Math.abs(num) >= 10000) {
            return `${(num / 10000).toFixed(1)}万`;
        }
        return INTL_INT.format(num);
    }

    function formatDateTime(value) {
        const date = parseValidDate(value);
        return date ? INTL_DATETIME.format(date) : '-';
    }

    function formatDate(value) {
        const date = parseValidDate(value);
        return date ? INTL_DATE.format(date) : '';
    }

    function formatShortMessageTime(value) {
        const date = parseValidDate(value);
        if (!date) return '';
        const mm = String(date.getMonth() + 1).padStart(2, '0');
        const dd = String(date.getDate()).padStart(2, '0');
        return `${mm}-${dd} ${INTL_TIME.format(date)}`;
    }

    /**
     * 判断当前 keydown 事件是否处于 IME 组合输入态（含 Safari keyCode 229 及 compositionend 邻近窗）。
     */
    function isImeComposing(event, lastCompositionEndAt = null) {
        if (!event) return false;
        if (event.isComposing || event.keyCode === 229) return true;
        if (lastCompositionEndAt !== null && typeof event.timeStamp === 'number') {
            return Math.abs(event.timeStamp - lastCompositionEndAt) < 50;
        }
        return false;
    }

    function bindImeCompositionTracker(inputEl) {
        let lastCompositionEndAt = null;
        const onCompEnd = (event) => {
            lastCompositionEndAt = event.timeStamp;
        };
        if (inputEl) {
            inputEl.addEventListener('compositionend', onCompEnd);
        }
        return {
            isComposing(event) {
                return isImeComposing(event, lastCompositionEndAt);
            },
            destroy() {
                if (inputEl) {
                    inputEl.removeEventListener('compositionend', onCompEnd);
                }
            }
        };
    }

    /**
     * 基于 AbortSignal.timeout / AbortSignal.any 的 JSON 请求辅助函数，支持后台请求降级优先级。
     */
    async function requestJson(url, options = {}) {
        const {
            timeoutMs = 15000,
            signal: userSignal = null,
            priority = undefined,
            ...restOptions
        } = options;

        let combinedSignal = userSignal;
        let fallbackTimer = null;
        let fallbackController = null;

        if (timeoutMs > 0) {
            if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
                const timeoutSignal = AbortSignal.timeout(timeoutMs);
                if (userSignal && typeof AbortSignal.any === 'function') {
                    combinedSignal = AbortSignal.any([userSignal, timeoutSignal]);
                } else if (!userSignal) {
                    combinedSignal = timeoutSignal;
                }
            } else if (!userSignal && typeof AbortController !== 'undefined') {
                fallbackController = new AbortController();
                combinedSignal = fallbackController.signal;
                fallbackTimer = global.setTimeout(() => fallbackController.abort(), timeoutMs);
            }
        }

        const fetchOptions = { ...restOptions };
        if (combinedSignal) {
            fetchOptions.signal = combinedSignal;
        }
        if (priority) {
            fetchOptions.priority = priority;
        }

        try {
            const response = await fetch(url, fetchOptions);
            let data = {};
            try {
                data = await response.json();
            } catch (parseError) {
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`, { cause: parseError });
                }
            }
            return {
                response,
                ok: response.ok,
                status: response.status,
                data
            };
        } finally {
            if (fallbackTimer !== null) {
                global.clearTimeout(fallbackTimer);
            }
        }
    }

    let cachedSupportsDisplayTransition;
    function canTransitionDisplay() {
        if (cachedSupportsDisplayTransition !== undefined) return cachedSupportsDisplayTransition;
        if (!global.CSS?.supports?.('transition-behavior', 'allow-discrete') || !document.body) {
            return false;
        }
        const probe = document.createElement('div');
        probe.style.cssText = 'transition: display 1s allow-discrete !important; display: block;';
        document.body.appendChild(probe);
        global.getComputedStyle(probe).display;
        probe.style.display = 'none';
        cachedSupportsDisplayTransition = global.getComputedStyle(probe).display === 'block';
        probe.remove();
        return cachedSupportsDisplayTransition;
    }

    function supportsTopLayerExit() {
        return Boolean(global.CSS?.supports?.('overlay', 'auto') && canTransitionDisplay());
    }

    async function waitDialogAnimations(element, timeoutMs = 220) {
        if (!element || typeof element.getAnimations !== 'function') return;
        const animations = element.getAnimations({ subtree: true });
        if (!animations.length) return;
        await Promise.race([
            Promise.allSettled(animations.map((a) => a.finished)),
            new Promise((resolve) => global.setTimeout(resolve, timeoutMs))
        ]);
    }

    async function closeTopLayer(dialogEl) {
        if (!dialogEl || !dialogEl.open) return;
        if (!supportsTopLayerExit()) {
            dialogEl.setAttribute('data-closing', '');
            await waitDialogAnimations(dialogEl, 220);
            if (!dialogEl.hasAttribute('data-closing')) return;
            dialogEl.removeAttribute('data-closing');
        }
        if (dialogEl.open) {
            dialogEl.close();
        }
        dialogEl.returnValue = '';
    }

    const BOUND_LIGHT_DISMISS = new WeakSet();
    function ensureDialogLightDismiss(dialogEl, getClosedBy, onDismiss) {
        if (!dialogEl || ('closedBy' in (global.HTMLDialogElement?.prototype || {}))) return;
        if (BOUND_LIGHT_DISMISS.has(dialogEl)) return;
        BOUND_LIGHT_DISMISS.add(dialogEl);

        const outsideBox = (event) => {
            const rect = dialogEl.getBoundingClientRect();
            return (
                event.clientX < rect.left ||
                event.clientX > rect.right ||
                event.clientY < rect.top ||
                event.clientY > rect.bottom
            );
        };

        let pressedOutside = false;
        dialogEl.addEventListener('pointerdown', (event) => {
            if (!dialogEl.open) return;
            const mode = typeof getClosedBy === 'function' ? getClosedBy() : dialogEl.getAttribute('closedby');
            pressedOutside = mode === 'any' && outsideBox(event);
        });
        dialogEl.addEventListener('pointerup', (event) => {
            if (!pressedOutside || !dialogEl.open) {
                pressedOutside = false;
                return;
            }
            pressedOutside = false;
            const mode = typeof getClosedBy === 'function' ? getClosedBy() : dialogEl.getAttribute('closedby');
            if (mode === 'any' && outsideBox(event)) {
                if (typeof onDismiss === 'function') {
                    onDismiss();
                } else {
                    dialogEl.close();
                }
            }
        });
    }

    function syncModalDialog(dialogEl, isOpen, options = {}) {
        if (!dialogEl) return;
        if (isOpen) {
            ensureDialogLightDismiss(dialogEl, options.getClosedBy, options.onDismiss);
            dialogEl.removeAttribute('data-closing');
            if (!dialogEl.open && typeof dialogEl.showModal === 'function') {
                dialogEl.showModal();
            }
        } else if (dialogEl.open) {
            dialogEl.removeAttribute('data-closing');
            dialogEl.close();
            dialogEl.returnValue = '';
        }
    }

    function ensureToastContainer() {
        if (!STATE.liveRegion || !document.body.contains(STATE.liveRegion)) {
            const region = document.createElement('div');
            region.id = 'hub-sr-status';
            region.className = 'hub-sr-only';
            region.setAttribute('aria-live', 'polite');
            region.setAttribute('aria-atomic', 'true');
            document.body.appendChild(region);
            STATE.liveRegion = region;
        }
        if (STATE.toastContainer && document.body.contains(STATE.toastContainer)) {
            return STATE.toastContainer;
        }
        const el = document.createElement('div');
        el.className = 'hub-toast-container';
        el.setAttribute('role', 'region');
        el.setAttribute('aria-label', '系统通知');
        if (typeof el.showPopover === 'function') {
            el.setAttribute('popover', 'manual');
        }
        document.body.appendChild(el);
        STATE.toastContainer = el;
        return el;
    }

    function promoteToastPopover(container) {
        if (!container || typeof container.showPopover !== 'function') return;
        try {
            if (container.matches(':popover-open')) {
                container.hidePopover();
            }
            container.showPopover();
        } catch {
            // 非顶层可用环境或断开连接时回退为常规固定定位
        }
    }

    function announceToast(text) {
        if (!text) return;
        if (typeof document.ariaNotify === 'function') {
            try {
                document.ariaNotify(text);
                return;
            } catch {
                // 回退到 DOM live region
            }
        }
        if (STATE.liveRegion) {
            STATE.liveRegion.textContent = '';
            global.requestAnimationFrame(() => {
                if (STATE.liveRegion) {
                    STATE.liveRegion.textContent = text;
                }
            });
        }
    }

    function toast(message, type = 'info', duration = 3400) {
        const text = String(message || '').trim();
        if (!text) return;
        const container = ensureToastContainer();
        while (container.children.length >= 4) {
            container.firstElementChild?.remove();
        }

        const item = document.createElement('div');
        item.className = `hub-toast is-${type}`;

        const badge = document.createElement('span');
        badge.className = 'hub-toast-badge';
        badge.setAttribute('aria-hidden', 'true');
        badge.textContent = type === 'success' ? '✓' : type === 'error' ? '!' : type === 'warning' ? '⚠' : 'i';
        item.appendChild(badge);

        const body = document.createElement('span');
        body.className = 'hub-toast-body';
        body.textContent = text;
        item.appendChild(body);

        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'hub-toast-close';
        closeBtn.textContent = '×';
        closeBtn.setAttribute('aria-label', '关闭提示');
        item.appendChild(closeBtn);

        let removed = false;
        let timerId = null;
        const clearTimer = () => {
            if (timerId !== null) {
                global.clearTimeout(timerId);
                timerId = null;
            }
        };
        const remove = () => {
            if (removed) return;
            removed = true;
            clearTimer();
            item.remove();
            if (container.children.length === 0 && typeof container.hidePopover === 'function') {
                try {
                    if (container.matches(':popover-open')) {
                        container.hidePopover();
                    }
                } catch {
                    // 忽略已断开节点的异常
                }
            }
        };
        const scheduleDismiss = () => {
            clearTimer();
            if (duration > 0 && !removed) {
                timerId = global.setTimeout(remove, duration);
            }
        };

        closeBtn.addEventListener('click', remove);
        item.addEventListener('mouseenter', clearTimer);
        item.addEventListener('mouseleave', scheduleDismiss);
        item.addEventListener('focusin', clearTimer);
        item.addEventListener('focusout', scheduleDismiss);

        container.appendChild(item);
        promoteToastPopover(container);
        announceToast(text);
        scheduleDismiss();
    }

    function confirm(options = {}) {
        const title = options.title || '确认操作';
        const message = options.message || '';
        const confirmText = options.confirmText || '确定';
        const cancelText = options.cancelText || '取消';
        const danger = Boolean(options.danger);

        return new Promise((resolve) => {
            const dialog = document.createElement('dialog');
            dialog.className = 'hub-dialog hub-dialog-overlay';
            dialog.setAttribute('closedby', 'any');
            const titleId = `hub-confirm-title-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
            dialog.setAttribute('aria-labelledby', titleId);

            const head = document.createElement('div');
            head.className = 'hub-dialog-head';
            const h3 = document.createElement('h3');
            h3.id = titleId;
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
            if (danger) {
                cancelBtn.setAttribute('autofocus', '');
            }

            const okBtn = document.createElement('button');
            okBtn.type = 'button';
            okBtn.className = `hub-btn ${danger ? 'hub-btn-danger' : 'hub-btn-primary'}`;
            okBtn.textContent = confirmText;
            if (!danger) {
                okBtn.setAttribute('autofocus', '');
            }

            foot.appendChild(cancelBtn);
            foot.appendChild(okBtn);
            dialog.appendChild(head);
            dialog.appendChild(body);
            dialog.appendChild(foot);
            document.body.appendChild(dialog);

            let settled = false;
            const finish = (result) => {
                if (settled) return;
                settled = true;
                resolve(result);
                closeTopLayer(dialog).finally(() => {
                    waitDialogAnimations(dialog, 200).finally(() => {
                        dialog.remove();
                    });
                });
            };

            ensureDialogLightDismiss(dialog, () => 'any', () => finish(false));
            dialog.addEventListener('cancel', (e) => {
                e.preventDefault();
                finish(false);
            });
            dialog.addEventListener('close', () => finish(false));
            cancelBtn.addEventListener('click', () => finish(false));
            okBtn.addEventListener('click', () => finish(true));

            dialog.showModal();
            if (danger) {
                cancelBtn.focus();
            } else {
                okBtn.focus();
            }
        });
    }

    function confirmHardDelete(options = {}) {
        const liveId = String(options.liveId || '');
        const anchorName = String(options.anchorName || '');

        return new Promise((resolve) => {
            const dialog = document.createElement('dialog');
            dialog.className = 'hub-dialog hub-dialog-overlay';
            dialog.setAttribute('closedby', 'any');
            const titleId = `hub-hd-title-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
            dialog.setAttribute('aria-labelledby', titleId);

            const head = document.createElement('div');
            head.className = 'hub-dialog-head';
            const h3 = document.createElement('h3');
            h3.id = titleId;
            h3.className = 'hub-dialog-title is-danger';
            h3.textContent = '确认永久删除直播间';
            head.appendChild(h3);

            const body = document.createElement('div');
            body.className = 'hub-dialog-body';

            const warn = document.createElement('p');
            warn.className = 'hub-dialog-warn';
            warn.textContent = `永久删除将清除「${anchorName || liveId}」的全部历史场次、弹幕、礼物和贡献统计且无法恢复。`;
            body.appendChild(warn);

            const targetBox = document.createElement('div');
            targetBox.className = 'hub-hard-delete-target';
            const idCode = document.createElement('code');
            idCode.className = 'hub-hard-delete-code';
            idCode.textContent = liveId;

            const fillBtn = document.createElement('button');
            fillBtn.type = 'button';
            fillBtn.className = 'hub-btn hub-btn-secondary hub-hard-delete-fill-btn';
            fillBtn.textContent = '点击填入 ID';

            targetBox.appendChild(idCode);
            targetBox.appendChild(fillBtn);
            body.appendChild(targetBox);

            const input = document.createElement('input');
            input.type = 'text';
            input.placeholder = `请输入 ${liveId} 以确认`;
            input.setAttribute('aria-label', `输入 ${liveId} 以确认永久删除`);
            input.setAttribute('autofocus', '');
            input.className = 'hub-dialog-input';
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
            dialog.appendChild(head);
            dialog.appendChild(body);
            dialog.appendChild(foot);
            document.body.appendChild(dialog);

            const imeTracker = bindImeCompositionTracker(input);

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
                imeTracker.destroy();
                resolve(result);
                closeTopLayer(dialog).finally(() => {
                    waitDialogAnimations(dialog, 200).finally(() => {
                        dialog.remove();
                    });
                });
            };

            input.addEventListener('keydown', (event) => {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    if (imeTracker.isComposing(event)) {
                        return;
                    }
                    if (input.value.trim() === liveId) {
                        finish(true);
                    }
                }
            });

            ensureDialogLightDismiss(dialog, () => 'any', () => finish(false));
            dialog.addEventListener('cancel', (event) => {
                event.preventDefault();
                finish(false);
            });
            dialog.addEventListener('close', () => finish(false));
            cancelBtn.addEventListener('click', () => finish(false));
            okBtn.addEventListener('click', () => {
                if (input.value.trim() === liveId) finish(true);
            });

            dialog.showModal();
            input.focus();
        });
    }

    function extractLiveId(rawInput) {
        const raw = String(rawInput || '').trim();
        if (!raw) {
            return { liveId: '', fromUrl: false };
        }
        const liveUrlMatch = raw.match(/https?:\/\/(?:live\.douyin\.com)\/([A-Za-z0-9_.\-]{1,50})/i);
        if (liveUrlMatch?.[1]) {
            return { liveId: liveUrlMatch[1], fromUrl: true };
        }
        const bareHostMatch = raw.match(/(?:^|[\s/])live\.douyin\.com\/([A-Za-z0-9_.\-]{1,50})/i);
        if (bareHostMatch?.[1]) {
            return { liveId: bareHostMatch[1], fromUrl: true };
        }
        const webRidMatch = raw.match(/[?&](?:web_rid|live_id)=([A-Za-z0-9_.\-]{1,50})/i);
        if (webRidMatch?.[1]) {
            return { liveId: webRidMatch[1], fromUrl: true };
        }
        return { liveId: raw, fromUrl: false };
    }

    const BLANK_CHAR_RE = /^[\s\u00AD\u034F\u061C\u115F\u1160\u17B4\u17B5\u180B-\u180F\u2000-\u200F\u2028-\u202F\u205F-\u206F\u2800\u3000\u3164\uFE00-\uFE0F\uFEFF\uFFA0\uFFF9-\uFFFB]*$/;

    function isBlankNickname(name) {
        if (name === null || name === undefined) return true;
        const str = String(name);
        if (str.length === 0) return true;
        return BLANK_CHAR_RE.test(str);
    }

    function isBlankSearchInput(raw) {
        if (typeof raw !== 'string' || raw.length === 0) return false;
        const trimmed = raw.trim();
        return isBlankNickname(raw) || trimmed === '空白昵称' || trimmed === '[空白昵称]';
    }

    function formatDisplayName(name, fallback = '[空白昵称]') {
        if (name === null || name === undefined) return fallback;
        const str = String(name);
        if (str.length === 0) return fallback;
        return isBlankNickname(str) ? '[空白昵称]' : str;
    }

    function formatAvatarInitial(name, fallbackId = 'U') {
        if (name !== null && name !== undefined && String(name).length > 0 && isBlankNickname(name)) {
            return '空';
        }
        const source = formatDisplayName(name, String(fallbackId || 'U'));
        return source.substring(0, 1) || 'U';
    }

    const ALLOWED_EMOJI_HOSTS = [
        'douyinpic.com',
        'byteimg.com',
        'bytescm.com',
        'ibytedtos.com',
        'bdurl.net',
        'pstatp.com',
        'snssdk.com',
        'zijieapi.com'
    ];
    const SAFE_LOCAL_EMOJI_FILE_RE = /^[A-Za-z0-9_-]+\.(?:webp|png|gif|jpg|svg)$/;
    const EMOJI_TOKEN_RE = /(\[[^\[\]\r\n]{1,16}\])/g;

    function isSafeEmojiUrl(url) {
        if (typeof url !== 'string' || !url || url.length > 1000) return false;
        if (/[\s<>"']/.test(url)) return false;
        try {
            const parsed = new URL(url);
            if (parsed.protocol !== 'https:' || parsed.username || parsed.password) {
                return false;
            }
            const host = (parsed.hostname || '').toLowerCase().replace(/\.+$/, '');
            if (!host) return false;
            return ALLOWED_EMOJI_HOSTS.some((domain) => host === domain || host.endsWith('.' + domain));
        } catch {
            return false;
        }
    }

    function parseChatSegments(rawText, emojiList) {
        const text = rawText === null || rawText === undefined ? '' : String(rawText);
        const localMap = (global.DOUYIN_EMOJI_MAP && typeof global.DOUYIN_EMOJI_MAP === 'object')
            ? global.DOUYIN_EMOJI_MAP
            : {};

        const customMap = Object.create(null);
        const customEntries = [];
        if (Array.isArray(emojiList)) {
            for (const item of emojiList) {
                if (!item || typeof item !== 'object') continue;
                const alt = typeof item.alt === 'string' ? item.alt : '';
                if (!alt || !isSafeEmojiUrl(item.url)) continue;
                if (!Object.hasOwn(customMap, alt)) {
                    const entry = {
                        alt,
                        url: item.url,
                        sticker: Boolean(item.sticker),
                        used: false
                    };
                    customMap[alt] = entry;
                    customEntries.push(entry);
                }
            }
        }

        const segments = [];
        const pushText = (chunk) => {
            if (!chunk) return;
            const prev = segments.at(-1);
            if (prev?.type === 'text') {
                prev.text += chunk;
            } else {
                segments.push({ type: 'text', text: chunk });
            }
        };

        if (text) {
            for (const part of text.split(EMOJI_TOKEN_RE)) {
                if (!part) continue;
                if (Object.hasOwn(customMap, part)) {
                    const custom = customMap[part];
                    custom.used = true;
                    segments.push({
                        type: 'emoji',
                        alt: part,
                        src: custom.url,
                        sticker: custom.sticker,
                        external: true
                    });
                } else if (
                    Object.hasOwn(localMap, part) &&
                    SAFE_LOCAL_EMOJI_FILE_RE.test(localMap[part])
                ) {
                    segments.push({
                        type: 'emoji',
                        alt: part,
                        src: '/emoji_img/' + localMap[part],
                        sticker: false,
                        external: false
                    });
                } else {
                    pushText(part);
                }
            }
        }

        for (const entry of customEntries) {
            if (!entry.used) {
                segments.push({
                    type: 'emoji',
                    alt: entry.alt,
                    src: entry.url,
                    sticker: entry.sticker,
                    external: true
                });
            }
        }

        return segments;
    }

    function handleEmojiError(event) {
        const img = event?.target;
        if (!img?.parentNode) return;
        const alt = img.getAttribute('alt') || '';
        img.parentNode.replaceChild(document.createTextNode(alt), img);
    }

    global.HubUI = {
        formatInteger,
        formatCompactNumber,
        formatDateTime,
        formatDate,
        formatShortMessageTime,
        isImeComposing,
        bindImeCompositionTracker,
        requestJson,
        supportsTopLayerExit,
        closeTopLayer,
        ensureDialogLightDismiss,
        syncModalDialog,
        toast,
        confirm,
        confirmHardDelete,
        extractLiveId,
        isBlankNickname,
        isBlankSearchInput,
        formatDisplayName,
        formatAvatarInitial,
        isSafeEmojiUrl,
        parseChatSegments,
        handleEmojiError
    };
})(window);

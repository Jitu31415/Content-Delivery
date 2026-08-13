document.addEventListener('DOMContentLoaded', () => {
    const videoModal = document.getElementById('videoModal');
    if (!videoModal) return; // playlist page doesn't have this modal

    const modalIframe = document.getElementById('modalIframe');
    const bsModal = new bootstrap.Modal(videoModal);

    function openVideoModal(embedUrl, videoId, title) {
        if (!embedUrl) return;
        modalIframe.src = embedUrl;
        bsModal.show();

        // Same history mechanism used across the whole app — a video
        // played from search, tabs, or "jump back in" all log here.
        if (videoId) {
            fetch('/youtube/click', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ video_id: videoId, title: title || '' }),
            }).catch(() => {});
        }
    }

    videoModal.addEventListener('hidden.bs.modal', () => { modalIframe.src = ''; });

    document.addEventListener('click', async (e) => {
        const trigger = e.target.closest('.video-trigger');
        if (trigger) {
            openVideoModal(trigger.dataset.embed, trigger.dataset.id, trigger.dataset.title);
        }

        const saveBtn = e.target.closest('.save-btn-dark');
        if (saveBtn) {
            e.preventDefault();
            e.stopPropagation();
            const { module, id } = saveBtn.dataset;
            try {
                const resp = await fetch(`/save/${module}/${id}`, { method: 'POST' });
                if (!resp.ok) throw new Error(resp.status);
                const data = await resp.json();
                saveBtn.textContent = data.is_saved ? '\u2605' : '\u2606';
            } catch (err) {
                console.error('save failed', err);
            }
        }

        const deleteBtn = e.target.closest('.history-delete-btn');
        if (deleteBtn) {
            e.preventDefault();
            e.stopPropagation();
            const entryId = deleteBtn.dataset.entryId;
            try {
                const resp = await fetch(`/youtube/history/${entryId}/delete`, { method: 'POST' });
                if (!resp.ok) throw new Error(resp.status);
                const card = document.querySelector(`.custom-card[data-entry-id="${entryId}"]`);
                if (card) card.remove();
            } catch (err) {
                console.error('history delete failed', err);
            }
        }

        const clearBtn = e.target.closest('.clear-history-btn');
        if (clearBtn) {
            e.preventDefault();
            if (!confirm('Clear all history on this device? This cannot be undone.')) return;
            try {
                const resp = await fetch('/youtube/history/clear', { method: 'POST' });
                if (!resp.ok) throw new Error(resp.status);
                document.querySelectorAll('#historyGrid .custom-card').forEach(c => c.remove());
                clearBtn.remove();
                const grid = document.getElementById('historyGrid');
                if (grid) grid.innerHTML = '<p class="empty-note">no history on this device yet</p>';
            } catch (err) {
                console.error('clear history failed', err);
            }
        }
    });
});

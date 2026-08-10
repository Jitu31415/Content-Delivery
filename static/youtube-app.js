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
    });
});

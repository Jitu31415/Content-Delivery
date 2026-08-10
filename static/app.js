// Initialize the heartbeat once upon page load
setInterval(() => {
  fetch("/api/heartbeat", { 
    method: "POST",
    headers: { "Content-Type": "application/json" }
  }).catch(err => console.error("Heartbeat failed", err));
}, 60000);

// Handle isolated click events
document.addEventListener('click', async (e) => {
  const link = e.target.closest('.video-item .video-link');
  if (link) {
    const item = link.closest('.video-item');
    fetch('/youtube/click', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ video_id: item.dataset.videoId, title: item.dataset.title }),
    }).catch(() => {});
  }

  const btn = e.target.closest('.save-btn');
  if (btn) {
    e.preventDefault();
    const { module, id } = btn.dataset;
    try {
      const resp = await fetch(`/save/${module}/${id}`, { method: 'POST' });
      if (!resp.ok) throw new Error(resp.status);
      const data = await resp.json();
      btn.textContent = data.is_saved ? '\u2605' : '\u2606';
    } catch (err) {
      console.error('save failed', err);
    }
  }
});